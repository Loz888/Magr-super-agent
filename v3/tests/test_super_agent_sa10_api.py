"""SA10 owner HTTP boundaries on real PG; no model, worker or external channel."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sa08_support import ingest, original_state, setup
from sa10_support import fresh_target, install_test_schema, link_id
from sa10_support import setup as native_setup

from mgar_v3 import api
from mgar_v3.super_agent import source_correlation
from mgar_v3.super_agent.source_correlation_domain import CorrelationResolveRequest
from mgar_v3.web_access import SESSION_COOKIE, WebAccounts

ORIGIN = "http://127.0.0.1:18080"
PREFIX = "/api/v3/source-correlations"
PRIVATE = "SA10-HTTP-PRIVATE-CURRENT-SOURCE"
TABLES = (
    "sa_correlation_resources", "sa_task_correlations", "sa_task_correlation_versions",
    "sa_task_correlation_claims", "sa_thread_link_inputs",
)


def formal_app(engine):
    """Use production registration without test-only route composition."""
    return api.create_app(engine.settings, engine, run_background=False)


@pytest.fixture
def browser(engine):
    # Formal0041 is installed by the normal disposable database fixture.
    install_test_schema(engine)
    engine.settings = engine.settings.model_copy(update={
        "allow_paid": False, "allow_external_write": False,
    })
    source = setup(engine)
    receipt = ingest(source, body=PRIVATE)
    session = WebAccounts(engine).session(source.cookie)
    assert session is not None
    app = formal_app(engine)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        yield SimpleNamespace(
            engine=engine, source=source, client=client, app=app, cookie=source.cookie,
            csrf=session.csrf_token, owner=session.user_id, input_id=receipt.input_ids[0],
            correlation_id=uuid4(),
        )


@pytest.fixture
def native_browser(engine):
    engine.settings = engine.settings.model_copy(update={
        "allow_paid": False, "allow_external_write": False,
    })
    source = native_setup(engine)
    session = WebAccounts(engine).session(source.cookie)
    assert session is not None
    app = formal_app(engine)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        yield SimpleNamespace(
            engine=engine, source=source, client=client, app=app, cookie=source.cookie,
            csrf=session.csrf_token, owner=session.user_id, input_id=source.native_input_id,
        )


def authenticate(env):
    env.client.cookies.set(SESSION_COOKIE, env.cookie)
    env.client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": env.csrf})


def persisted(env):
    with env.engine.transaction() as conn:
        return {
            name: conn.execute("SELECT * FROM app." + name + " ORDER BY 1,2").fetchall()
            for name in TABLES
        }


def target(env):
    return {
        "ticket_id": str(uuid4()), "run_id": str(uuid4()), "thread_id": str(uuid4()),
        "thread_revision": 0, "execution_epoch": 0, "mandate_id": str(uuid4()),
        "mandate_sha256": "a" * 64, "project_id": env.engine.settings.project_id,
        "repository": env.engine.settings.repository, "allowed": True,
    }


def payload(env, action):
    if action == "resolve":
        return {"input_ids": [str(env.input_id)]}
    value = {
        "snapshot_sha256": "a" * 64, "expected_revision": 1,
        "review_ref": "SA10-HTTP-synthetic-owner-review",
    }
    if action != "remove":
        value["target"] = target(env)
    if action == "link":
        value.update(input_ids=[str(env.input_id)], resource_key="b" * 64, expected_revision=0)
    return value


def path(env, action):
    return {
        "resolve": PREFIX + "/resolve",
        "link": PREFIX + "/links",
        "correct": PREFIX + f"/links/{env.correlation_id}/correct",
        "remove": PREFIX + f"/links/{env.correlation_id}/remove",
        "get": PREFIX + f"/links/{env.correlation_id}",
        "links": PREFIX + f"/inputs/{env.input_id}/links",
    }[action]


def call(env, action):
    if action in {"get", "links"}:
        return env.client.get(path(env, action))
    return env.client.post(path(env, action), json=payload(env, action))


def test_application_registers_exact_six_correlation_management_routes(browser):
    routes = {
        (method, route.path)
        for route in browser.app.routes if hasattr(route, "methods")
        for method in route.methods if route.path.startswith(PREFIX)
    }
    assert routes == {
        ("POST", PREFIX + "/resolve"), ("POST", PREFIX + "/links"),
        ("POST", PREFIX + "/links/{correlation_id}/correct"),
        ("POST", PREFIX + "/links/{correlation_id}/remove"),
        ("GET", PREFIX + "/links/{correlation_id}"),
        ("GET", PREFIX + "/inputs/{input_id}/links"),
    }


def test_all_routes_require_browser_cookie_even_with_existing_operator_token(browser):
    env = browser
    before, business = persisted(env), original_state(env.engine)
    env.client.headers["X-MGAR-Token"] = env.engine.settings.local_token.get_secret_value()
    for action in ("resolve", "link", "correct", "remove", "get", "links"):
        response = call(env, action)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("action", ["resolve", "link", "correct", "remove"])
def test_mutations_require_current_cookie_origin_and_csrf(browser, action):
    env = browser
    before, business = persisted(env), original_state(env.engine)
    env.client.cookies.set(SESSION_COOKIE, env.cookie)
    response = call(env, action)
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    env.client.headers["Origin"] = ORIGIN
    assert call(env, action).status_code == 403
    env.client.headers["X-CSRF-Token"] = env.csrf
    response = env.client.post(
        path(env, action), json=payload(env, action), headers={"Origin": "https://elsewhere.invalid"},
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("action", ["resolve", "link", "correct", "remove", "get", "links"])
def test_dependency_session_revocation_is_rechecked_inside_service(browser, monkeypatch, action):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    original = WebAccounts.session

    def revoke_after_read(accounts, cookie):
        session = original(accounts, cookie)
        if session is not None:
            accounts.revoke(cookie)
        return session

    monkeypatch.setattr(WebAccounts, "session", revoke_after_read)
    response = call(env, action)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert persisted(env) == before and original_state(env.engine) == business


def test_valid_json_uuid_array_reaches_real_read_only_unresolved_service(browser, monkeypatch):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    observed = []
    original = source_correlation.SourceCorrelations.resolve

    def observe(service, cookie, command):
        assert type(command) is CorrelationResolveRequest
        assert type(command.input_ids) is tuple
        assert all(type(value) is UUID for value in command.input_ids)
        observed.append(command)
        return original(service, cookie, command)

    monkeypatch.setattr(source_correlation.SourceCorrelations, "resolve", observe)
    response = call(env, "resolve")
    assert response.status_code == 200, response.text
    assert len(observed) == 1 and observed[0].input_ids == (env.input_id,)
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["snapshot"]["inputs"][0]["input_id"] == str(env.input_id)
    assert result["resolutions"][0]["outcome"] == "unmatched"
    assert result["resolutions"][0]["candidates"] == []
    assert result["eligible_for_execution"] is False
    assert persisted(env) == before and original_state(env.engine) == business
    for secret in (PRIVATE, env.cookie, env.csrf, "ciphertext"):
        assert secret not in response.text


@pytest.mark.parametrize("action", ["resolve", "link", "correct", "remove"])
def test_http_cannot_supply_factory_source_body_resource_or_execution_authority(browser, action):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    for field, value in (
        ("factory", "create_ticket"), ("body", PRIVATE), ("sender", {"actor_kind": "human"}),
        ("resource", {"repository_id": 1}), ("allow_paid", True),
        ("eligible_for_execution", True), ("model_input", True), ("execution_epoch", 99),
    ):
        response = env.client.post(path(env, action), json={**payload(env, action), field: value})
        assert response.status_code == 422, (field, response.text)
        assert response.json()["code"] == "INVALID_INPUT"
        assert response.headers["cache-control"] == "no-store"
        assert PRIVATE not in response.text
    assert persisted(env) == before and original_state(env.engine) == business


def test_input_membership_json_bounds_reject_duplicates_and_forged_ids(browser):
    env = browser
    authenticate(env)
    before = persisted(env)
    for values in (
        [], [str(env.input_id)] * 2, [str(uuid4()) for _ in range(17)],
        ["not-a-uuid"], [True], "not-an-array",
    ):
        response = env.client.post(PREFIX + "/resolve", json={"input_ids": values})
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "INVALID_INPUT"
        assert response.headers["cache-control"] == "no-store"
    assert persisted(env) == before


@pytest.mark.parametrize("action", ["link", "correct"])
def test_target_transport_preserves_strict_boolean_counter_uuid_and_extra_fields(browser, action):
    env = browser
    authenticate(env)
    before = persisted(env)
    for field, value in (
        ("allowed", "true"), ("allowed", 1), ("execution_epoch", True),
        ("thread_revision", 1.5), ("ticket_id", "not-a-uuid"), ("body", PRIVATE),
    ):
        data = payload(env, action)
        data["target"][field] = value
        response = env.client.post(path(env, action), json=data)
        assert response.status_code == 422, (field, response.text)
        assert response.json()["code"] == "INVALID_INPUT"
        assert response.headers["cache-control"] == "no-store"
        assert PRIVATE not in response.text
    assert persisted(env) == before


def test_views_are_bounded_no_store_and_missing_correlations_are_scoped(browser):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    for limit in (0, 101, "1.5", "true"):
        response = env.client.get(path(env, "links"), params={"limit": limit})
        assert response.status_code == 422, response.text
    response = call(env, "links")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert PRIVATE not in response.text
    missing = call(env, "get")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"
    assert persisted(env) == before and original_state(env.engine) == business


def test_other_project_cannot_resolve_or_read_source_links(browser):
    env = browser
    authenticate(env)
    settings = env.engine.settings
    before = persisted(env)
    try:
        env.engine.settings = settings.model_copy(update={"project_id": "SA10-foreign-project"})
        for action in ("resolve", "links", "get"):
            response = call(env, action)
            assert response.status_code == 404, response.text
            assert PRIVATE not in response.text
    finally:
        env.engine.settings = settings
    assert persisted(env) == before


def test_correlation_cache_middleware_does_not_change_neighbor_prefix_errors(browser):
    response = browser.client.get(PREFIX + "-other/not-a-correlation")
    assert response.status_code == 404
    assert "cache-control" not in response.headers


def test_native_http_link_correct_remove_preserves_history_and_business_targets(native_browser):
    env = native_browser
    authenticate(env)
    original_business = original_state(env.engine)
    initial = env.client.post(PREFIX + "/resolve", json={"input_ids": [str(env.input_id)]})
    assert initial.status_code == 200, initial.text
    primary = fresh_target(env.source)
    command = {
        "input_ids": [str(env.input_id)], "resource_key": env.source.resource_key,
        "snapshot_sha256": initial.json()["snapshot_sha256"], "expected_revision": 0,
        "target": primary.model_dump(mode="json"), "review_ref": "SA10-HTTP-native-link",
    }
    created = env.client.post(PREFIX + "/links", json=command)
    assert created.status_code == 200, created.text
    identifier = str(link_id(created.json()))
    base = PREFIX + "/links/" + identifier
    env.correlation_id = UUID(identifier)
    first_version = persisted(env)["sa_task_correlation_versions"][0]
    assert first_version["revision"] == 1
    assert first_version["ticket_id"] == primary.ticket_id
    views = [env.client.get(base), env.client.get(PREFIX + f"/inputs/{env.input_id}/links")]
    assert all(value.status_code == 200 for value in views)
    assert str(primary.ticket_id) in views[0].text

    current = call(env, "resolve")
    assert current.status_code == 200, current.text
    assert current.json()["resolutions"][0]["outcome"] == "matched"
    assert current.json()["resolutions"][0]["candidates"][0]["ticket_id"] == str(primary.ticket_id)
    secondary = fresh_target(env.source, second=True)
    correction = {
        "snapshot_sha256": current.json()["snapshot_sha256"], "expected_revision": 1,
        "target": secondary.model_dump(mode="json"), "review_ref": "SA10-HTTP-correction",
    }
    corrected = env.client.post(base + "/correct", json=correction)
    assert corrected.status_code == 200, corrected.text
    after_correct = persisted(env)
    assert after_correct["sa_task_correlation_versions"][0] == first_version
    assert after_correct["sa_task_correlation_versions"][1]["ticket_id"] == secondary.ticket_id
    assert after_correct["sa_task_correlations"][0]["revision"] == 2
    conflict = env.client.post(base + "/correct", json=correction)
    assert conflict.status_code == 409, conflict.text
    assert conflict.headers["cache-control"] == "no-store"
    assert persisted(env) == after_correct

    current = call(env, "resolve")
    assert current.status_code == 200, current.text
    removal = {
        "snapshot_sha256": current.json()["snapshot_sha256"], "expected_revision": 2,
        "review_ref": "SA10-HTTP-removal",
    }
    removed = env.client.post(base + "/remove", json=removal)
    assert removed.status_code == 200, removed.text
    after_remove = persisted(env)
    assert after_remove["sa_task_correlations"][0]["revision"] == 3
    assert after_remove["sa_task_correlation_versions"][:2] == (
        after_correct["sa_task_correlation_versions"]
    )
    assert after_remove["sa_task_correlation_versions"][2]["payload"]["state"] == "removed"
    final = call(env, "resolve")
    assert final.status_code == 200, final.text
    assert final.json()["resolutions"][0]["outcome"] == "blocked"
    assert final.json()["resolutions"][0]["candidates"] == []
    for response in (initial, created, *views, current, corrected, removed, final):
        assert response.headers["cache-control"] == "no-store"
        for secret in ("SA10_SYNTHETIC_PRIVATE_NATIVE_7", env.cookie, env.csrf, "ciphertext"):
            assert secret not in response.text
    assert original_state(env.engine) == original_business
    assert not env.engine.settings.allow_paid and not env.engine.settings.allow_external_write
