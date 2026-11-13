"""SA13 metadata HTTP uses original create_app, Core and formal disposable PG."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sa08_support import original_state
from sa12_support import session as register_session
from sa13_support import setup as coding_setup

from mgar_v3 import api
from mgar_v3.super_agent.source_coding_domain import CodingCancelRequest
from mgar_v3.super_agent.source_scheduler_domain import SchedulerSessionRevokeRequest
from mgar_v3.web_access import SESSION_COOKIE, LoginInput, WebAccounts

ORIGIN = "http://127.0.0.1:18080"
PREFIX = "/api/v3/source-coding"
SECRET = "SA13-PRIVATE-HTTP-INPUT-NEVER-ECHO"
WORKER_TOKEN = "SA13-synthetic-independent-bearer-not-a-real-session"
TABLES = (
    "sa_coding_bindings", "sa_coding_attempts", "sa_coding_operations",
    "sa_coding_payloads", "sa_coding_verifications", "sa_coding_events",
)
OWNER_READS = ("capabilities", "binding")
OWNER_WRITES = ("admit", "cancel")
WORKER_ACTIONS = ("run", "state")


def tables(engine):
    with engine.transaction() as conn:
        return {
            table: conn.execute(
                "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                "'[]'::jsonb) AS value FROM app." + table + " t"
            ).fetchone()["value"]
            for table in TABLES
        }


@pytest.fixture
def browser(engine):
    with engine.transaction() as conn:
        database = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        assert database.startswith("mgar_v3_test_")
        for table in TABLES:
            assert conn.execute("SELECT to_regclass(%s) AS value", ("app." + table,)).fetchone()[
                "value"
            ] is not None
    accounts = WebAccounts(engine)
    owner = accounts.create_owner(LoginInput(
        username="sa13-api-owner", password=SecretStr("fixture-sa13-owner-password"),
    ))
    cookie = accounts.issue_session(owner)
    user = accounts.session(cookie)
    assert user is not None
    app = api.create_app(engine.settings, engine, run_background=False)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        yield SimpleNamespace(
            engine=engine, app=app, client=client, accounts=accounts,
            cookie=cookie, csrf=user.csrf_token, coding_id=uuid4(), run_id=uuid4(),
            assignment_id=uuid4(), session_id=uuid4(), request_id=uuid4(),
        )


def authenticate(env):
    env.client.cookies.set(SESSION_COOKIE, env.cookie)
    env.client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": env.csrf})


def payload(env, action):
    if action == "admit":
        return {
            "request_id": str(env.request_id), "run_id": str(env.run_id),
            "expected_epoch": 0, "expected_policy_revision": 1, "expected_revision": 0,
            "target_sha256": "a" * 64, "profile_sha256": "b" * 64,
            "review_ref": "SA13-HTTP-existing-new-original-Coding-Mandate",
            "source_input": {
                "input_id": str(uuid4()), "source_instance_id": str(uuid4()),
                "project_id": env.engine.settings.project_id,
                "version_fingerprint": "c" * 64, "state_sha256": "d" * 64,
            },
        }
    result = {
        "request_id": str(env.request_id), "coding_id": str(env.coding_id),
        "run_id": str(env.run_id), "expected_revision": 1,
    }
    if action == "cancel":
        result.update(expected_epoch=0, review_ref="SA13-HTTP-current-owner-cancellation")
    else:
        result.update(
            assignment_id=str(env.assignment_id), session_id=str(env.session_id), epoch=1,
        )
    return result


def path(env, action):
    if action == "capabilities":
        return PREFIX + "/capabilities"
    if action == "admit":
        return PREFIX + "/admissions"
    if action == "binding":
        return PREFIX + f"/bindings/{env.coding_id}"
    if action == "cancel":
        return PREFIX + f"/bindings/{env.coding_id}/cancel"
    return PREFIX + f"/worker/{env.coding_id}/" + action


def call(env, action, *, body=None):
    if action in OWNER_READS:
        return env.client.get(path(env, action))
    if action == "state":
        return env.client.get(path(env, action), params={"run_id": str(env.run_id)})
    return env.client.post(path(env, action), json=payload(env, action) if body is None else body)


def private(env, response):
    assert response.headers["cache-control"] == "no-store"
    for secret in (
        SECRET, WORKER_TOKEN, env.cookie, env.csrf, "ciphertext", "token_sha256",
        "SA13 synthetic private repair request",
    ):
        assert secret not in response.text


def test_original_app_registers_exact_six_coding_routes(browser):
    actual = [
        (method, route.path)
        for route in browser.app.routes if hasattr(route, "methods")
        for method in route.methods if getattr(route, "path", "").startswith(PREFIX + "/")
    ]
    assert len(actual) == 6
    assert set(actual) == {
        ("GET", PREFIX + "/capabilities"), ("POST", PREFIX + "/admissions"),
        ("GET", PREFIX + "/bindings/{coding_id}"),
        ("POST", PREFIX + "/bindings/{coding_id}/cancel"),
        ("POST", PREFIX + "/worker/{coding_id}/run"),
        ("GET", PREFIX + "/worker/{coding_id}/state"),
    }


def test_owner_routes_require_current_cookie_not_worker_or_operator(browser):
    env = browser
    env.client.headers.update({
        "Authorization": "Bearer " + WORKER_TOKEN,
        "X-MGAR-Token": env.engine.settings.local_token.get_secret_value(),
    })
    before, original = tables(env.engine), original_state(env.engine)
    for action in (*OWNER_READS, *OWNER_WRITES):
        result = call(env, action)
        assert result.status_code == 401, (action, result.text)
        private(env, result)
    assert tables(env.engine) == before and original_state(env.engine) == original


@pytest.mark.parametrize("fault", ["origin_missing", "csrf_missing", "cross_origin"])
def test_owner_writes_require_origin_and_csrf(browser, fault):
    env = browser
    authenticate(env)
    if fault == "origin_missing":
        del env.client.headers["Origin"]
    elif fault == "csrf_missing":
        del env.client.headers["X-CSRF-Token"]
    else:
        env.client.headers["Origin"] = "https://other.invalid"
    before, original = tables(env.engine), original_state(env.engine)
    for action in OWNER_WRITES:
        result = call(env, action)
        assert result.status_code == 403, (action, result.text)
        private(env, result)
    assert tables(env.engine) == before and original_state(env.engine) == original


def test_worker_routes_cannot_use_owner_cookie_or_operator_token(browser):
    env = browser
    authenticate(env)
    env.client.headers["X-MGAR-Token"] = env.engine.settings.local_token.get_secret_value()
    before, original = tables(env.engine), original_state(env.engine)
    for action in WORKER_ACTIONS:
        result = call(env, action)
        assert result.status_code == 401, (action, result.text)
        private(env, result)
    assert tables(env.engine) == before and original_state(env.engine) == original


@pytest.mark.parametrize("authorization", [
    "Basic " + WORKER_TOKEN, "Bearer ", "Bearer " + WORKER_TOKEN + " extra",
    "Bearer\t" + WORKER_TOKEN, "Bearer " + "x" * 513,
])
def test_worker_bearer_syntax_is_rejected_without_echo(browser, authorization):
    env = browser
    env.client.headers["Authorization"] = authorization
    before = tables(env.engine)
    for action in WORKER_ACTIONS:
        result = call(env, action)
        assert result.status_code == 401, result.text
        assert authorization not in result.text
        private(env, result)
    assert tables(env.engine) == before


def test_multiple_authorization_headers_cannot_select_a_worker(browser):
    env = browser
    before = tables(env.engine)
    result = env.client.post(path(env, "run"), json=payload(env, "run"), headers=[
        ("Authorization", "Bearer " + WORKER_TOKEN), ("Authorization", "Bearer other"),
    ])
    assert result.status_code == 401
    private(env, result)
    assert tables(env.engine) == before


@pytest.mark.parametrize("action", ["admit", "cancel", "run"])
@pytest.mark.parametrize("field", [
    "body", "command", "host_path", "factory", "token", "allow_paid", "succeeded",
])
def test_transport_body_cannot_add_authority_or_side_effects(browser, action, field):
    env = browser
    authenticate(env)
    env.client.headers["Authorization"] = "Bearer " + WORKER_TOKEN
    before, original = tables(env.engine), original_state(env.engine)
    body = payload(env, action)
    body[field] = SECRET
    result = call(env, action, body=body)
    assert result.status_code == 422
    assert result.json()["code"] == "SOURCE_CODING_REQUEST_INVALID"
    private(env, result)
    assert tables(env.engine) == before and original_state(env.engine) == original


@pytest.mark.parametrize("action", ["cancel", "run"])
def test_path_body_coding_identity_must_match_before_service(browser, action):
    env = browser
    authenticate(env)
    env.client.headers["Authorization"] = "Bearer " + WORKER_TOKEN
    before = tables(env.engine)
    body = payload(env, action)
    body["coding_id"] = str(uuid4())
    result = call(env, action, body=body)
    assert result.status_code == 422
    assert result.json()["code"] == "SOURCE_CODING_REQUEST_INVALID"
    private(env, result)
    assert tables(env.engine) == before


@pytest.mark.parametrize("action,field", [
    ("admit", "expected_epoch"), ("admit", "expected_policy_revision"),
    ("admit", "expected_revision"), ("cancel", "expected_epoch"),
    ("cancel", "expected_revision"), ("run", "epoch"), ("run", "expected_revision"),
])
@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_json_preserves_strict_integer_fences(browser, action, field, value):
    env = browser
    authenticate(env)
    env.client.headers["Authorization"] = "Bearer " + WORKER_TOKEN
    before = tables(env.engine)
    body = payload(env, action)
    body[field] = value
    result = call(env, action, body=body)
    assert result.status_code == 422
    assert result.json()["code"] == "SOURCE_CODING_REQUEST_INVALID"
    private(env, result)
    assert tables(env.engine) == before


def test_invalid_json_and_prefix_errors_never_echo_private_input_or_cache(browser):
    env = browser
    authenticate(env)
    before = tables(env.engine)
    for content_type in ("application/json", "text/plain"):
        result = env.client.post(
            path(env, "admit"), content='{"secret":"' + SECRET,
            headers={"Content-Type": content_type},
        )
        assert result.status_code == 422
        assert result.json()["code"] == "SOURCE_CODING_REQUEST_INVALID"
        private(env, result)
    for endpoint, expected in [
        (PREFIX, 404), (PREFIX + "/unknown", 404), (path(env, "admit"), 405),
        (PREFIX + "/bindings/not-a-uuid", 422),
        (PREFIX + "/worker/not-a-uuid/state?run_id=not-a-uuid", 401),
    ]:
        result = env.client.get(endpoint)
        assert result.status_code == expected, result.text
        private(env, result)
    assert tables(env.engine) == before


def test_default_capabilities_are_metadata_and_do_not_enable_runtime(browser):
    env = browser
    authenticate(env)
    assert env.engine.source_coding.runtime is None
    before, original = tables(env.engine), original_state(env.engine)
    result = call(env, "capabilities")
    assert result.status_code == 200, result.text
    assert result.json()["runtime_configured"] is False
    private(env, result)
    assert env.engine.source_coding.runtime is None
    assert not env.engine.settings.allow_paid and not env.engine.settings.allow_external_write
    assert tables(env.engine) == before and original_state(env.engine) == original


@pytest.mark.parametrize("action", ["capabilities", "admit", "binding", "cancel"])
def test_owner_session_is_rechecked_inside_original_transaction(browser, monkeypatch, action):
    env = browser
    authenticate(env)
    before = tables(env.engine)
    original = WebAccounts.session

    def revoke_after_read(accounts, cookie):
        user = original(accounts, cookie)
        if user is not None and cookie == env.cookie:
            accounts.revoke(cookie)
        return user

    monkeypatch.setattr(WebAccounts, "session", revoke_after_read)
    result = call(env, action)
    assert result.status_code == 401, result.text
    private(env, result)
    assert tables(env.engine) == before


@pytest.fixture
def coding_browser(engine, tmp_path):
    """Real Source/Engine/PG, explicit filesystem fixture; no Broker/model claim."""
    source = coding_setup(engine, tmp_path)
    user = WebAccounts(engine).session(source.cookie)
    assert user is not None
    app = api.create_app(engine.settings, engine, run_background=False)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        yield SimpleNamespace(
            engine=engine, source=source, app=app, client=client,
            cookie=source.cookie, csrf=user.csrf_token,
            coding_id=source.mandate.coding_id, run_id=source.target.run_id,
        )


def worker_call(env, action, *, body=None, run_id=None, bearer=None):
    env.client.cookies.clear()
    for header in ("Origin", "X-CSRF-Token", "X-MGAR-Token"):
        env.client.headers.pop(header, None)
    token = bearer or env.source.worker["bearer"]
    env.client.headers["Authorization"] = "Bearer " + token
    if action == "state":
        result = env.client.get(path(env, action), params={"run_id": str(run_id or env.run_id)})
    else:
        result = env.client.post(
            path(env, action),
            json=env.source.run_request.model_dump(mode="json") if body is None else body,
        )
    assert SESSION_COOKIE not in result.request.headers.get("cookie", "")
    private(env, result)
    assert token not in result.text
    assert str(env.source.driver.root) not in result.text
    return result


def test_http_binding_and_worker_state_are_actual_original_metadata(coding_browser):
    env = coding_browser
    authenticate(env)
    before = tables(env.engine)
    result = call(env, "binding")
    assert result.status_code == 200, result.text
    assert result.json()["coding_id"] == str(env.coding_id)
    private(env, result)
    result = worker_call(env, "state")
    assert result.status_code == 200, result.text
    assert result.json()["coding_id"] == str(env.coding_id)
    assert result.json()["run_id"] == str(env.run_id)
    assert tables(env.engine) == before


def test_http_run_cannot_activate_default_absent_runtime(coding_browser):
    env = coding_browser
    assert env.engine.source_coding.runtime is None
    before, original = tables(env.engine), original_state(env.engine)
    result = worker_call(env, "run")
    assert result.status_code == 403, result.text
    assert result.json()["code"] == "SOURCE_CODING_RUNTIME_REQUIRED"
    assert tables(env.engine) == before and original_state(env.engine) == original


def test_http_worker_state_rejects_wrong_original_run_and_bearer(coding_browser):
    env = coding_browser
    before = tables(env.engine)
    wrong_run = worker_call(env, "state", run_id=uuid4())
    assert wrong_run.status_code in {403, 404, 409}, wrong_run.text
    wrong_bearer = worker_call(env, "state", bearer=WORKER_TOKEN)
    assert wrong_bearer.status_code == 403, wrong_bearer.text
    assert tables(env.engine) == before


def test_http_current_bearer_for_another_real_session_cannot_borrow_assignment(coding_browser):
    env = coding_browser
    other = register_session(env.source.scheduler)
    before = tables(env.engine)
    result = worker_call(env, "state", bearer=other["bearer"])
    assert result.status_code == 403, result.text
    assert worker_call(env, "state").status_code == 200
    assert tables(env.engine) == before


@pytest.mark.parametrize("field", [
    "run_id", "assignment_id", "session_id", "epoch", "expected_revision",
])
def test_http_worker_run_checks_exact_current_binding_before_runtime_availability(
    coding_browser, field,
):
    env = coding_browser
    before, original = tables(env.engine), original_state(env.engine)
    body = env.source.run_request.model_dump(mode="json")
    body[field] = str(uuid4()) if field.endswith("_id") else body[field] + 1
    result = worker_call(env, "run", body=body)
    assert result.status_code in {403, 404, 409}, result.text
    assert result.json()["code"] != "SOURCE_CODING_RUNTIME_REQUIRED"
    assert tables(env.engine) == before and original_state(env.engine) == original


def test_http_worker_state_query_cannot_smuggle_or_duplicate_authority(coding_browser):
    env = coding_browser
    worker_call(env, "state")
    before = tables(env.engine)
    for query in (
        [("run_id", str(env.run_id)), ("session_id", SECRET)],
        [("run_id", str(env.run_id)), ("run_id", str(uuid4()))],
    ):
        result = env.client.get(path(env, "state"), params=query)
        assert result.status_code == 422
        assert result.json()["code"] == "SOURCE_CODING_REQUEST_INVALID"
        private(env, result)
    assert tables(env.engine) == before


def test_http_worker_session_revocation_blocks_metadata_immediately(coding_browser):
    env = coding_browser
    assert worker_call(env, "state").status_code == 200
    worker = env.source.worker
    env.engine.source_scheduler.revoke_session(env.cookie, SchedulerSessionRevokeRequest(
        session_id=UUID(str(worker["session_id"])), expected_revision=worker["revision"],
        review_ref="SA13-HTTP-explicit-revoke-existing-Worker-session",
    ))
    before = tables(env.engine)
    result = worker_call(env, "state")
    assert result.status_code == 403, result.text
    assert tables(env.engine) == before


def test_http_owner_cancel_does_not_allow_worker_to_resume_or_complete_task(coding_browser):
    env = coding_browser
    authenticate(env)
    body = CodingCancelRequest(
        request_id=uuid4(), coding_id=env.coding_id, run_id=env.run_id,
        expected_epoch=env.source.run_request.epoch,
        expected_revision=env.source.binding["revision"],
        review_ref="SA13-HTTP-cancel-current-exact-Coding-binding",
    )
    result = call(env, "cancel", body=body.model_dump(mode="json"))
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "revoked"
    private(env, result)
    before = tables(env.engine)
    late = worker_call(env, "state")
    assert late.status_code in {403, 409}, late.text
    assert env.engine.get_run(env.run_id)["status"] != "succeeded"
    assert tables(env.engine) == before


def test_http_admission_uses_existing_new_mandate_and_is_idempotent(engine, tmp_path):
    source = coding_setup(engine, tmp_path, admitted=False, ready=False)
    user = WebAccounts(engine).session(source.cookie)
    assert user is not None
    app = api.create_app(engine.settings, engine, run_background=False)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        env = SimpleNamespace(
            engine=engine, source=source, app=app, client=client,
            cookie=source.cookie, csrf=user.csrf_token, coding_id=source.mandate.coding_id,
            run_id=source.target.run_id,
        )
        authenticate(env)
        original = original_state(engine)
        body = source.request.model_dump(mode="json")
        created = call(env, "admit", body=body)
        assert created.status_code == 200, created.text
        assert created.json()["coding_id"] == str(env.coding_id)
        private(env, created)
        after = tables(engine)
        repeated = call(env, "admit", body=body)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json() == created.json()
        private(env, repeated)
        assert tables(engine) == after
        assert len(after["sa_coding_bindings"]) == 1
        assert not after["sa_coding_attempts"] and not after["sa_coding_operations"]
        current = original_state(engine)
        for table in ("threads", "tickets", "runs", "actions", "outbox", "usage_calls"):
            assert current[table] == original[table]
