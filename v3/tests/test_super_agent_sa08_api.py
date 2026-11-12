"""Owner-only source observation HTTP behavior through the registered application."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sa08_support import ingest, input_row, original_state, setup

from mgar_v3.api import create_app
from mgar_v3.web_access import SESSION_COOKIE, WebAccounts

ORIGIN = "http://127.0.0.1:18080"
PREFIX = "/api/v3/source-fast-path"


@pytest.fixture
def browser(engine):
    engine.settings = engine.settings.model_copy(update={
        "allow_paid": False, "allow_external_write": False,
    })
    env = setup(engine, enabled=False)
    session = WebAccounts(engine).session(env.cookie)
    assert session is not None
    with TestClient(
        create_app(engine.settings, engine, run_background=False),
        base_url=ORIGIN, client=("127.0.0.1", 12345),
    ) as client:
        yield SimpleNamespace(
            source=env, engine=engine, client=client, cookie=env.cookie,
            csrf=session.csrf_token, owner=session.user_id,
            path=PREFIX + "/" + str(env.installation.scope.source_instance_id),
        )


def authenticate(env):
    env.client.cookies.set(SESSION_COOKIE, env.cookie)
    env.client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": env.csrf})


def request(*, revision=1, **changes):
    return {
        "expected_revision": revision,
        "settings": {"enabled": True, **changes},
        "review_ref": "SA08-current-owner-HTTP-review",
    }


def persisted(env):
    with env.engine.transaction() as conn:
        return {
            table: conn.execute("SELECT * FROM app." + table + " ORDER BY 1,2").fetchall()
            for table in (
                "sa_fast_path_controls", "sa_fast_path_versions", "sa_fast_path_decisions",
            )
        }


def test_routes_require_cookie_and_writes_require_origin_and_csrf(browser):
    env = browser
    before = persisted(env)
    for suffix in ("", "/preview", "/decisions", "/metrics"):
        assert env.client.get(env.path + suffix).status_code == 401
    env.client.headers["X-MGAR-Token"] = env.engine.settings.local_token.get_secret_value()
    assert env.client.put(env.path, json=request()).status_code == 401
    env.client.cookies.set(SESSION_COOKIE, env.cookie)
    assert env.client.put(env.path, json=request()).status_code == 403
    env.client.headers["Origin"] = ORIGIN
    assert env.client.put(env.path, json=request()).status_code == 403
    env.client.headers["X-CSRF-Token"] = env.csrf
    assert env.client.put(
        env.path, json=request(), headers={"Origin": "https://outside.invalid"},
    ).status_code == 403
    assert persisted(env) == before
    response = env.client.put(env.path, json=request())
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["revision"] == 2 and response.json()["settings"]["enabled"]
    with env.engine.transaction() as conn:
        signed = conn.execute(
            "SELECT * FROM app.sa_fast_path_versions WHERE revision=2",
        ).fetchone()
        assert signed["signed_by"] == env.owner
        assert signed["review_ref"] == request()["review_ref"] and signed["signed_at"] is not None


def test_registered_views_and_observe_are_bodyless_hypotheses_with_no_business_effect(browser):
    env = browser
    authenticate(env)
    secret = "SA08-HTTP-AUTHORIZED-SOURCE-" + uuid4().hex
    receipt = ingest(env.source, body=secret)
    before = original_state(env.engine)
    assert env.client.put(env.path, json=request()).status_code == 200
    response = env.client.post(
        env.path + "/observe", json={"input_ids": [str(value) for value in receipt.input_ids]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["semantic_evaluation"] == "NOT_RUN"
    assert response.json()["decisions"][0]["intended_action"] == "would_evaluate"
    for suffix in ("", "/preview", "/decisions", "/metrics"):
        result = env.client.get(env.path + suffix)
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store"
        assert secret not in result.text and "ciphertext" not in result.text
        credentials = (env.cookie, env.csrf, env.engine.settings.local_token.get_secret_value())
        for credential in credentials:
            assert credential not in result.text
    assert original_state(env.engine) == before
    assert not env.engine.settings.allow_paid and not env.engine.settings.allow_external_write


def test_http_cas_conflict_preserves_the_immutable_previous_signed_configuration(browser):
    env = browser
    authenticate(env)
    before = persisted(env)
    saved = env.client.put(env.path, json=request(cooldown_seconds=9))
    assert saved.status_code == 200, saved.text
    after = persisted(env)
    rejected = env.client.put(env.path, json=request(cooldown_seconds=99))
    assert rejected.status_code == 409
    assert persisted(env) == after
    assert after["sa_fast_path_versions"][0] == before["sa_fast_path_versions"][0]
    assert len(after["sa_fast_path_versions"]) == 2


def test_simultaneous_http_saves_have_one_cas_winner(browser):
    env = browser
    authenticate(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda seconds: env.client.put(env.path, json=request(cooldown_seconds=seconds)),
            (7, 11),
        ))
    assert sorted(result.status_code for result in results) == [200, 409]
    current = env.client.get(env.path)
    assert current.json()["revision"] == 2
    assert current.json()["settings"]["cooldown_seconds"] in {7, 11}
    assert len(persisted(env)["sa_fast_path_versions"]) == 2


@pytest.mark.parametrize("method", ["get", "put", "observe"])
def test_session_revocation_after_http_dependency_blocks_protected_operation(
    browser, monkeypatch, method,
):
    env = browser
    authenticate(env)
    receipt = ingest(env.source)
    before = persisted(env)
    original = WebAccounts.session

    def revoke_after_read(accounts, cookie):
        session = original(accounts, cookie)
        if session is not None:
            accounts.revoke(cookie)
        return session

    monkeypatch.setattr(WebAccounts, "session", revoke_after_read)
    if method == "get":
        response = env.client.get(env.path)
    elif method == "put":
        response = env.client.put(env.path, json=request())
    else:
        response = env.client.post(
            env.path + "/observe", json={"input_ids": [str(receipt.input_ids[0])]},
        )
    assert response.status_code == 401
    assert persisted(env) == before
    assert input_row(env.source, receipt.input_ids[0])["consumed_at"] is None


@pytest.mark.parametrize("assertion", [
    {"body": "untrusted plaintext"}, {"historical": False}, {"buffered": False},
    {"priority": 100}, {"control_action": "cancel"}, {"model_input": True},
])
def test_observe_json_cannot_supply_source_body_origin_or_authority(browser, assertion):
    env = browser
    authenticate(env)
    receipt = ingest(env.source)
    before = persisted(env)
    response = env.client.post(
        env.path + "/observe",
        json={"input_ids": [str(receipt.input_ids[0])], **assertion},
    )
    assert response.status_code == 422
    assert persisted(env) == before
    assert input_row(env.source, receipt.input_ids[0])["consumed_at"] is None


def test_views_are_bounded_and_configuration_cannot_switch_policy_mode(browser):
    env = browser
    authenticate(env)
    for route in ("/preview", "/decisions"):
        for limit in (0, 101):
            assert env.client.get(env.path + route, params={"limit": limit}).status_code == 422
    assert env.client.put(env.path, json=request(mode="auto_repair")).status_code == 422
    current = env.engine.source_policies.read(env.cookie, env.source.policy_id)
    assert current["definition"]["mode"] == "shadow"
    assert len(persisted(env)["sa_fast_path_versions"]) == 1


def test_unknown_source_and_canonical_input_do_not_reveal_another_scope(browser):
    env = browser
    authenticate(env)
    assert env.client.get(PREFIX + "/" + str(uuid4())).status_code == 404
    assert env.client.post(
        env.path + "/observe", json={"input_ids": [str(uuid4())]},
    ).status_code == 404
    assert not persisted(env)["sa_fast_path_decisions"]
