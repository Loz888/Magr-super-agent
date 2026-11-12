"""Registered create_app integration; execute only after confirmed0039/main wiring."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sa08_support import ingest, original_state, setup

from mgar_v3.api import create_app
from mgar_v3.super_agent import source_eval
from mgar_v3.super_agent.source_eval_domain import SourceEvalConfigure, SourceEvalSettings
from mgar_v3.web_access import SESSION_COOKIE, WebAccounts

ORIGIN = "http://127.0.0.1:18080"
PREFIX = "/api/v3/source-eval"
VIEWS = ("", "/preview", "/decisions", "/metrics")


@pytest.fixture
def browser(engine):
    engine.settings = engine.settings.model_copy(update={
        "allow_paid": False, "allow_external_write": False,
    })
    source = setup(engine)
    instance_id = source.installation.scope.source_instance_id
    source_eval.SourceEval(engine).configure(source.cookie, instance_id, SourceEvalConfigure(
        expected_revision=0, settings=SourceEvalSettings(),
        review_ref="SA09-disabled-source-evaluation-setup",
    ))
    session = WebAccounts(engine).session(source.cookie)
    assert session is not None
    app = create_app(engine.settings, engine, run_background=False)
    with TestClient(
        app, base_url=ORIGIN, client=("127.0.0.1", 12345),
    ) as client:
        yield SimpleNamespace(
            source=source, engine=engine, client=client, app=app, cookie=source.cookie,
            csrf=session.csrf_token, owner=session.user_id,
            path=PREFIX + "/" + str(instance_id),
        )


def authenticate(env):
    env.client.cookies.set(SESSION_COOKIE, env.cookie)
    env.client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": env.csrf})


def request(*, revision=1, **settings):
    return {
        "expected_revision": revision,
        "settings": {"enabled": False, **settings},
        "review_ref": "SA09-current-owner-HTTP-review",
    }


def persisted(env):
    with env.engine.transaction() as conn:
        return {
            table: conn.execute("SELECT * FROM app." + table + " ORDER BY 1,2").fetchall()
            for table in (
                "sa_source_eval_controls", "sa_source_eval_versions", "sa_source_eval_snapshots",
                "sa_source_eval_inputs", "sa_source_eval_results", "sa_source_eval_decisions",
            )
        }


def fast_batch(env):
    secret = "SA09-HTTP-AUTHORIZED-SOURCE-" + uuid4().hex
    receipt = ingest(env.source, body=secret)
    preview = env.engine.fast_path.preview(
        env.cookie, env.source.installation.scope.source_instance_id,
    )
    batch = next(b for b in preview["batches"] if receipt.input_ids[0] in b["active_input_ids"])
    return str(batch["batch_id"]), secret


def test_application_registers_all_six_source_eval_management_routes(browser):
    routes = {
        (method, route.path)
        for route in browser.app.routes if hasattr(route, "methods")
        for method in route.methods
        if route.path.startswith(PREFIX)
    }
    base = PREFIX + "/{source_id}"
    assert routes == {
        ("PUT", base), ("GET", base), ("POST", base + "/evaluate"),
        ("GET", base + "/preview"), ("GET", base + "/decisions"),
        ("GET", base + "/metrics"),
    }


def test_cookie_origin_and_csrf_are_required_before_mutation(browser):
    env = browser
    before = persisted(env)
    business = original_state(env.engine)
    for suffix in VIEWS:
        assert env.client.get(env.path + suffix).status_code == 401
    env.client.headers["X-MGAR-Token"] = env.engine.settings.local_token.get_secret_value()
    assert env.client.put(env.path, json=request()).status_code == 401
    assert env.client.post(
        env.path + "/evaluate", json={"fast_batch_ids": [str(uuid4())]},
    ).status_code == 401
    env.client.cookies.set(SESSION_COOKIE, env.cookie)
    assert env.client.put(env.path, json=request()).status_code == 403
    env.client.headers["Origin"] = ORIGIN
    assert env.client.put(env.path, json=request()).status_code == 403
    env.client.headers["X-CSRF-Token"] = env.csrf
    assert env.client.put(
        env.path, json=request(), headers={"Origin": "https://outside.invalid"},
    ).status_code == 403
    assert env.client.post(
        env.path + "/evaluate", json={"fast_batch_ids": [str(uuid4())]},
        headers={"Origin": "https://outside.invalid"},
    ).status_code == 403
    assert persisted(env) == before and original_state(env.engine) == business
    response = env.client.put(env.path, json=request())
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["revision"] == 2


def test_registered_views_are_bodyless_without_business_or_model_effects(browser):
    env = browser
    authenticate(env)
    _, secret = fast_batch(env)
    before = original_state(env.engine)
    for suffix in VIEWS:
        response = env.client.get(env.path + suffix)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        for value in (
            secret, env.cookie, env.csrf, env.engine.settings.local_token.get_secret_value(),
        ):
            assert value not in response.text
        assert "ciphertext" not in response.text
    assert original_state(env.engine) == before
    assert not env.engine.settings.allow_paid and not env.engine.settings.allow_external_write


def test_http_save_is_signed_cas_and_does_not_rewrite_prior_version(browser):
    env = browser
    authenticate(env)
    before = persisted(env)
    business = original_state(env.engine)
    response = env.client.put(env.path, json=request(max_pending=7))
    assert response.status_code == 200, response.text
    after = persisted(env)
    assert after["sa_source_eval_versions"][0] == before["sa_source_eval_versions"][0]
    assert len(after["sa_source_eval_versions"]) == 2
    signed = after["sa_source_eval_versions"][1]
    assert signed["signed_by"] == env.owner and signed["signed_at"] is not None
    assert signed["review_ref"] == request()["review_ref"]
    rejected = env.client.put(env.path, json=request(max_pending=9))
    assert rejected.status_code == 409
    assert persisted(env) == after and original_state(env.engine) == business


def test_concurrent_http_configuration_has_one_cas_winner(browser):
    env = browser
    authenticate(env)
    before = original_state(env.engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda pending: env.client.put(env.path, json=request(max_pending=pending)), (7, 11),
        ))
    assert sorted(r.status_code for r in responses) == [200, 409]
    current = env.client.get(env.path)
    assert current.json()["revision"] == 2
    assert current.json()["settings"]["max_pending"] in {7, 11}
    assert len(persisted(env)["sa_source_eval_versions"]) == 2
    assert original_state(env.engine) == before


@pytest.mark.parametrize("method", ["get", "put", "evaluate", "preview", "decisions", "metrics"])
def test_session_revocation_after_dependency_is_rechecked_in_service(browser, monkeypatch, method):
    env = browser
    authenticate(env)
    batch_id, _ = fast_batch(env)
    before = persisted(env)
    business = original_state(env.engine)
    original = WebAccounts.session

    def revoke_after_read(accounts, cookie):
        session = original(accounts, cookie)
        if session is not None:
            accounts.revoke(cookie)
        return session

    monkeypatch.setattr(WebAccounts, "session", revoke_after_read)
    if method == "put":
        response = env.client.put(env.path, json=request())
    elif method == "evaluate":
        response = env.client.post(env.path + "/evaluate", json={"fast_batch_ids": [batch_id]})
    else:
        suffix = "" if method == "get" else "/" + method
        response = env.client.get(env.path + suffix)
    assert response.status_code == 401
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("assertion", [
    {"body": "untrusted source body"}, {"actor": "administrator"},
    {"policy_revision": 9}, {"budget": {"allow_paid": True}}, {"priority": 100},
    {"proposals": [{"action": "create"}]}, {"target_id": str(uuid4())},
    {"model_input": True}, {"eligible_for_execution": True},
])
def test_evaluate_json_cannot_supply_body_model_output_or_authority(browser, assertion):
    env = browser
    authenticate(env)
    batch_id, _ = fast_batch(env)
    before = persisted(env)
    business = original_state(env.engine)
    response = env.client.post(
        env.path + "/evaluate", json={"fast_batch_ids": [batch_id], **assertion},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INVALID_INPUT"
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("case", ["duplicate", "empty", "too-many", "invalid-uuid"])
def test_evaluate_transport_rejects_invalid_batch_references_with_normal422(browser, case):
    env = browser
    authenticate(env)
    batch_id, _ = fast_batch(env)
    values = {
        "duplicate": [batch_id, batch_id], "empty": [],
        "too-many": [str(uuid4()) for _ in range(17)], "invalid-uuid": ["not-a-UUID"],
    }[case]
    before = persisted(env)
    business = original_state(env.engine)
    response = env.client.post(env.path + "/evaluate", json={"fast_batch_ids": values})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INVALID_INPUT"
    assert persisted(env) == before and original_state(env.engine) == business


def test_disabled_evaluation_refuses_valid_http_uuid_arrays_without_business_effects(browser):
    env = browser
    authenticate(env)
    batch_id, _ = fast_batch(env)
    before = persisted(env)
    business = original_state(env.engine)
    # A valid JSON array must pass the transport boundary and reach disabled-service policy.
    response = env.client.post(env.path + "/evaluate", json={"fast_batch_ids": [batch_id]})
    assert response.status_code == 409, response.text
    assert response.json()["code"] != "INVALID_INPUT"
    assert persisted(env) == before and original_state(env.engine) == business


def test_views_are_bounded_and_unknown_source_is_scoped(browser):
    env = browser
    authenticate(env)
    before = persisted(env)
    for suffix in ("/preview", "/decisions"):
        for limit in (0, 101, "1.5", "true"):
            assert env.client.get(env.path + suffix, params={"limit": limit}).status_code == 422
    for suffix in VIEWS:
        assert env.client.get(PREFIX + "/" + str(uuid4()) + suffix).status_code == 404
    assert persisted(env) == before


def test_known_source_cannot_be_read_under_another_current_project(browser):
    env = browser
    authenticate(env)
    settings = env.engine.settings
    try:
        env.engine.settings = settings.model_copy(update={"project_id": "foreign-project"})
        for suffix in VIEWS:
            response = env.client.get(env.path + suffix)
            assert response.status_code == 404, response.text
            assert env.cookie not in response.text
    finally:
        env.engine.settings = settings


@pytest.mark.parametrize("field,value", [
    ("mode", "auto_repair"), ("allow_paid", True), ("body", "untrusted source body"),
])
def test_evaluation_configuration_cannot_supply_source_mode_payment_or_body(browser, field, value):
    env = browser
    authenticate(env)
    before = persisted(env)
    response = env.client.put(env.path, json=request(**{field: value}))
    assert response.status_code == 422
    assert persisted(env) == before
    policy = env.engine.source_policies.read(env.cookie, env.source.policy_id)
    assert policy["definition"]["mode"] == "shadow"
