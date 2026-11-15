"""New console-specific decision provenance and current authentication boundaries."""

import json
from uuid import UUID

import pytest
from sa06_support import payload, signed
from sa06_support import setup as feishu_setup
from sa09_support import apply, prepared
from sa09_support import setup as eval_setup
from sa19_support import create_source, owner
from test_source_console_http import PREFIX, client_for
from test_super_agent_sa08 import controller


@pytest.mark.parametrize("action", ["ignore", "observe", "create", "ask", "attach"])
def test_shadow_decision_retains_actual_intended_action_without_inventing_model_execution(
    engine, trigger, action
):
    e = eval_setup(engine)
    target = controller(e, trigger) if action == "attach" else None
    batch = prepared(e)
    _, decisions = apply(e, batch, action=action, target=target.ticket_id if target else None)
    before = engine.source_console.snapshot(e.cookie)
    row = before["decisions"]["items"][0]
    assert row["input_id"] == UUID(batch["source_snapshot"]["groups"][0]["inputs"][0]["input_id"])
    assert row["semantic_action"] == decisions[0].intended_action == "would_" + action
    assert row["semantic_reason"] == decisions[0].reason_code
    assert row["semantic_evaluation"] == "NOT_RUN" and row["call_id"] is None
    assert row["source_instance_id"] is not None
    assert not before["actions"]["items"] and not before["decisions"]["observing_invokes_model"]
    text = json.dumps(before, default=str)
    assert "Please investigate this issue" not in text
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.router_calls").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == (
            1 if target else 0
        )


def test_static_page_alias_cannot_expose_page_and_owner_asset_routes_precede_fallback(engine):
    e = owner(engine)
    with client_for(engine, e) as c:
        assert c.get("/static/source-console.html").status_code == 404
        assert c.get("/static/source-console%2ehtml").status_code == 404
        assert c.get("/static/source-console.js").headers["cache-control"] == "no-store"
        e.accounts.revoke(e.cookie)
        for path in [PREFIX, "/static/source-console.js", "/static/source-console.css"]:
            assert c.get(path).status_code == 401


def test_fresh_verified_identity_event_after_binding_has_current_usable_proof_only(engine):
    e = feishu_setup(engine, mode="shadow")
    fresh = e.identities.authenticate(e.source_id, signed(e, payload(event_id="console-new-proof")))
    first = engine.source_console.snapshot(e.cookie)
    proofs = {row["evidence_id"]: row for row in first["identities"]["evidence"]}
    assert len(proofs) == 2 and sum(row["usable"] for row in proofs.values()) == 1
    assert proofs[fresh.evidence_id]["usable"]
    assert proofs[fresh.evidence_id]["actor_ref"]["actor_id"] == e.binding["actor_ref"]["actor_id"]
    assert not {"credential_hash", "payload_sha256", "remote_event_id"} & set(
        proofs[fresh.evidence_id]
    )


def test_source_revoke_over_authenticated_http_invalidates_cipher_and_current_settings(engine):
    e = owner(engine)
    source = create_source(e, "feishu")
    with client_for(engine, e) as c:
        r = c.post(
            f"{PREFIX}/sources/{source.source_id}/revoke",
            json={
                "expected_source_revision": source.value["source_revision"],
                "review_ref": "fixture-source-revoke-http",
            },
        )
        assert r.status_code == 200, r.text
        value = c.get(f"{PREFIX}/sources/{source.source_id}").json()
        assert not value["credential"]["configured"] and value["credential"]["revoked"]
        assert not value["settings"]["enabled"]


@pytest.mark.parametrize(
    "path",
    [
        "/static/%2e/source-console.html",
        "/static/nested/%2e%2e/source-console.js",
        "/static//source-console.css",
        "/static/source-console.js%2f..%2fsource-console.css",
    ],
)
def test_static_normalized_aliases_cannot_fall_through_to_public_mount(engine, path):
    from fastapi.testclient import TestClient
    from mgar_v3.api import create_app

    with TestClient(
        create_app(engine.settings, engine, run_background=False), base_url="http://127.0.0.1:18081"
    ) as c:
        response = c.get(path)
        assert response.status_code == 404
        assert "function" not in response.text and "<!doctype" not in response.text
        assert response.headers["cache-control"] == "no-store"


def test_static_head_does_not_fall_through_owner_get_route_to_public_mount(engine):
    from fastapi.testclient import TestClient
    from mgar_v3.api import create_app

    with TestClient(
        create_app(engine.settings, engine, run_background=False), base_url="http://127.0.0.1:18081"
    ) as c:
        response = c.head("/static/source-console.js")
        assert response.status_code == 405 and not response.content
        assert response.headers["cache-control"] == "no-store"
