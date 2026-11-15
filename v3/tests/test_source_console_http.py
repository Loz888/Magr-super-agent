"""First real HTTP security and current-identity/health projections, with real PG."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sa06_support import setup as feishu_setup
from sa19_support import create_source, owner, policy
from test_source_console_preparation import secret_payload, source_payload

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError
from mgar_v3.sources.identity_domain import RevisionCommand
from mgar_v3.super_agent.policy_domain import PolicyPreview
from mgar_v3.web_access import SESSION_COOKIE

PREFIX = "/api/v3/source-console"
ROUTE_ID = "00000000-0000-4000-8000-000000000019"
OWNER_ROUTES = [
    ("GET", ""),
    ("GET", f"/sources/{ROUTE_ID}"),
    ("PUT", f"/sources/{ROUTE_ID}"),
    ("PUT", f"/sources/{ROUTE_ID}/credentials"),
    ("POST", f"/sources/{ROUTE_ID}/revoke"),
    ("PUT", f"/sources/{ROUTE_ID}/ingress"),
    ("POST", "/disable-all"),
    ("PUT", f"/policies/{ROUTE_ID}"),
    ("POST", f"/policies/{ROUTE_ID}/preview"),
    ("GET", f"/tasks/{ROUTE_ID}"),
    ("POST", f"/tasks/{ROUTE_ID}/stop"),
    ("GET", "/decisions"),
]


def client_for(engine, env, **runtimes):
    client = TestClient(
        create_app(engine.settings, engine, run_background=False, **runtimes),
        base_url="http://127.0.0.1:18081",
    )
    client.cookies.set(SESSION_COOKIE, env.cookie)
    client.headers.update(
        {
            "Origin": "http://127.0.0.1:18081",
            "X-CSRF-Token": env.accounts.session(env.cookie).csrf_token,
        }
    )
    return client


@pytest.mark.parametrize("method,path", OWNER_ROUTES)
def test_all_twelve_owner_routes_deny_local_bearer_without_cookie(engine, method, path):
    with TestClient(
        create_app(engine.settings, engine, run_background=False), base_url="http://127.0.0.1:18081"
    ) as client:
        client.headers["X-MGAR-Token"] = engine.settings.local_token.get_secret_value()
        r = client.request(method, PREFIX + path, **({"json": {}} if method != "GET" else {}))
        assert r.status_code == 401, r.text
    assert not engine.source_console.vault.key_path.exists()


@pytest.mark.parametrize(
    "path,code",
    [
        ("/source-console", 303),
        ("/static/source-console.js", 401),
        ("/static/source-console.css", 401),
    ],
)
def test_private_page_and_both_assets_require_owner(engine, path, code):
    with TestClient(
        create_app(engine.settings, engine, run_background=False), base_url="http://127.0.0.1:18081"
    ) as client:
        assert client.get(path, follow_redirects=False).status_code == code


@pytest.mark.parametrize(
    "failure", ["missing-origin", "foreign-origin", "missing-csrf", "wrong-csrf"]
)
def test_current_cookie_mutation_still_requires_origin_and_csrf(engine, failure):
    env = owner(engine)
    with client_for(engine, env) as client:
        if failure == "missing-origin":
            client.headers.pop("Origin")
        elif failure == "foreign-origin":
            client.headers["Origin"] = "https://foreign.invalid"
        elif failure == "missing-csrf":
            client.headers.pop("X-CSRF-Token")
        else:
            client.headers["X-CSRF-Token"] = "wrong"
        r = client.put(PREFIX + "/sources/" + str(uuid4()), json=source_payload("feishu"))
        assert r.status_code == 403
    assert not env.service.vault.key_path.exists()


@pytest.mark.parametrize(
    "body,content_type,code",
    [
        ('{"credentials":"secret-marker", "credentials":"duplicate"}', "application/json", 422),
        ('{"secret-marker":', "application/json", 422),
        ('{"secret-marker":"' + "x" * 65536 + '"}', "application/json", 413),
        ("secret-marker", "text/plain", 415),
    ],
)
def test_bounded_parser_errors_do_not_reflect_input_secrets(engine, body, content_type, code):
    env = owner(engine)
    with client_for(engine, env) as client:
        r = client.put(
            PREFIX + "/sources/" + str(uuid4()),
            content=body,
            headers={"Content-Type": content_type},
        )
        assert r.status_code == code and "secret-marker" not in r.text
    assert not env.service.vault.key_path.exists()


def test_http_source_credentials_page_cache_controls_and_safe_validation(engine):
    env = owner(engine)
    identifier = uuid4()
    payload = source_payload("feishu")
    payload.update(
        credentials=secret_payload("feishu"),
        credential_valid_until=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    with client_for(engine, env) as client:
        r = client.put(f"{PREFIX}/sources/{identifier}", json=payload)
        assert r.status_code == 200, r.text
        for path in [
            PREFIX,
            f"{PREFIX}/sources/{identifier}",
            "/source-console",
            "/static/source-console.js",
            "/static/source-console.css",
        ]:
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert all(s not in response.text for s in secret_payload("feishu").values())
        bad = {**payload, "expected_source_revision": 2, "unexpected": "secret-marker"}
        response = client.put(f"{PREFIX}/sources/{identifier}", json=bad)
        assert response.status_code == 422 and "secret-marker" not in response.text
        rotated = client.put(
            f"{PREFIX}/sources/{identifier}/credentials",
            json={
                "expected_source_revision": 2,
                "expected_credential_revision": 1,
                "credentials": secret_payload("feishu"),
                "credential_valid_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "review_ref": "fixture-rotate-http",
            },
        )
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["credential"]["revision"] == 2
        snapshot = client.get(PREFIX).json()
        assert not snapshot["capabilities"]["automatic_repair"]
        assert not snapshot["tasks"]["items"] and not snapshot["actions"]["items"]


def test_identity_snapshot_uses_auth_revision_and_does_not_write_authenticated_read_audits(engine):
    e = feishu_setup(engine, mode="shadow")
    with engine.transaction() as conn:
        before = conn.execute("SELECT count(*) AS n FROM app.source_identity_audit").fetchone()["n"]
        secrets = conn.execute(
            "SELECT credential_hash,payload_sha256 FROM app.source_identity_evidence"
        ).fetchall()
    first = engine.source_console.snapshot(e.cookie)
    info = first["identities"]
    assert len(info["bindings"]) == 1 and len(info["evidence"]) == 1
    assert not info["evidence"][0][
        "usable"
    ]  # Binding itself advanced auth_revision; obtain a new trusted proof.
    assert info["evidence"][0]["identity_revision"] == e.binding["identity_revision"]
    engine.source_console.snapshot(e.cookie)
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_identity_audit").fetchone()["n"]
            == before
        )
    text = json.dumps(first, default=str)
    assert all(value not in text for item in secrets for value in item.values())


@pytest.mark.parametrize(
    "connection,permission",
    [("online", "insufficient"), ("online", "granted"), ("offline", "unknown")],
)
def test_original_runtime_health_keeps_connection_separate_from_message_permissions(
    engine, connection, permission
):
    e = feishu_setup(engine, mode="shadow")
    e.sources.observe_health(
        e.source_id,
        1,
        e.runtime._mode(),
        connection=connection,
        permissions=permission,
        code="FIXTURE_DIAGNOSIS",
    )
    status = engine.source_console.source(e.cookie, e.source_id, (e.runtime,))
    assert status["health"]["connection"] == connection
    assert status["health"]["permissions"] == permission
    assert not e.calls


def test_current_binding_revocation_committed_inside_status_blocks_old_projection(
    engine, monkeypatch
):
    e = feishu_setup(engine, mode="shadow")
    status = e.runtime.status

    def revoke():
        e.identities.revoke_binding(
            e.cookie,
            e.binding["binding_id"],
            RevisionCommand(
                expected_revision=e.binding["revision"], review_ref="fixture-in-status"
            ),
        )
        return status()

    monkeypatch.setattr(e.runtime, "status", revoke)
    with pytest.raises(DomainError, match="配置已更新"):
        engine.source_console.snapshot(e.cookie, (e.runtime,))
    assert not engine.source_console.snapshot(e.cookie)["identities"]["bindings"][0]["active"]


def test_metadata_task_projection_excludes_original_message_and_stops_only_exact_epoch(
    engine, trigger
):
    env = owner(engine)
    run = engine.ingest(
        trigger.model_copy(update={"body": "/mgar investigate secret-marker task payload"})
    )
    with client_for(engine, env) as client:
        value = client.get(f"{PREFIX}/tasks/{run.run_id}")
        assert value.status_code == 200, value.text
        assert "secret-marker" not in value.text
        assert value.json()["source_authorities"] == []
        stale = client.post(
            f"{PREFIX}/tasks/{run.run_id}/stop",
            json={
                "expected_epoch": value.json()["run"]["execution_epoch"] + 1,
                "review_ref": "fixture-stale",
            },
        )
        assert stale.status_code == 409
        valid = client.post(
            f"{PREFIX}/tasks/{run.run_id}/stop",
            json={
                "expected_epoch": value.json()["run"]["execution_epoch"],
                "review_ref": "fixture-explicit-stop",
            },
        )
        assert valid.status_code == 200, valid.text
        assert client.get(f"{PREFIX}/tasks/{run.run_id}").json()["run"]["status"] == "cancelled"


def test_preview_over_http_is_read_only_and_marks_semantic_not_run(engine):
    env = owner(engine)
    source = create_source(env)
    d, actor, thread = policy(source)
    request = PolicyPreview(
        expected_revision=0,
        definition=d,
        actor=actor,
        source_thread=thread,
        facts={
            "event_kind": "issue_comment.created",
            "request_kind": "explicit_request",
            "repository": engine.settings.repository,
        },
    )
    with client_for(engine, env) as client:
        r = client.post(
            f"{PREFIX}/policies/{uuid4()}/preview", json=request.model_dump(mode="json")
        )
        assert r.status_code == 200, r.text
        assert r.json()["semantic_evaluation"] == "NOT_RUN"
        assert not r.json()["creates_task"]
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.source_policies").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.router_calls").fetchone()["n"] == 0
