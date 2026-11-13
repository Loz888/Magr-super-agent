"""SA12 HTTP contracts on the formal original app and disposable real PostgreSQL."""

import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sa08_support import original_state
from sa12_support import enqueue as scheduler_enqueue
from sa12_support import setup as scheduler_setup

from mgar_v3 import api
from mgar_v3.domain import TriggerInput
from mgar_v3.web_access import SESSION_COOKIE, LoginInput, WebAccounts

ORIGIN = "http://127.0.0.1:18080"
PREFIX = "/api/v3/source-scheduler"
SECRET = "SA12-PRIVATE-HTTP-INPUT-MUST-NOT-BE-ECHOED"
WORKER_TOKEN = "SA12-synthetic-independent-bearer-not-a-real-session"
TABLES = (
    "sa_scheduler_configs", "sa_scheduler_config_versions", "sa_scheduler_queue",
    "sa_scheduler_sessions", "sa_scheduler_assignments", "sa_scheduler_operations",
    "sa_scheduler_events",
)
OWNER_WRITES = ("configure", "enqueue", "cancel", "register", "revoke", "drain", "reconcile")
OWNER_READS = ("state", "queue", "assignments", "assignment")
WORKER_ACTIONS = ("claim", "heartbeat", "ready", "report")


@pytest.fixture
def browser(engine):
    # No docs SQL fallback or private API registration: conftest must install
    # the formal migration and the original create_app must compose all routes.
    with engine.transaction() as conn:
        name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        assert name.startswith("mgar_v3_test_")
        for table in TABLES:
            assert conn.execute("SELECT to_regclass(%s) AS value", ("app." + table,)).fetchone()[
                "value"
            ] is not None
    accounts = WebAccounts(engine)
    owner = accounts.create_owner(LoginInput(
        username="sa12-api-owner", password=SecretStr("fixture-sa12-owner-password"),
    ))
    cookie = accounts.issue_session(owner)
    session = accounts.session(cookie)
    assert session is not None
    app = api.create_app(engine.settings, engine, run_background=False)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        yield SimpleNamespace(
            engine=engine, client=client, app=app, cookie=cookie, csrf=session.csrf_token,
            queue_id=uuid4(), session_id=uuid4(), assignment_id=uuid4(), operation_id=uuid4(),
            run_id=uuid4(), request_id=uuid4(), accounts=accounts,
        )


def authenticate(env):
    env.client.cookies.set(SESSION_COOKIE, env.cookie)
    env.client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": env.csrf})


def persisted(env):
    with env.engine.transaction() as conn:
        return {
            table: conn.execute(
                "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                "'[]'::jsonb) AS value FROM app." + table + " t"
            ).fetchone()["value"]
            for table in TABLES
        }


def configuration():
    at = datetime.now(UTC)
    return {
        "expected_revision": 0,
        "configuration": {
            "enabled": False, "limits": {},
            "valid_from": (at - timedelta(minutes=1)).isoformat(),
            "valid_until": (at + timedelta(hours=1)).isoformat(),
        },
        "review_ref": "SA12-HTTP-explicit-metadata-only-configuration",
    }


def payload(env, action):
    if action == "configure":
        return configuration()
    if action == "enqueue":
        return {
            "request_id": str(env.request_id), "run_id": str(env.run_id), "expected_epoch": 0,
            "target_sha256": "a" * 64, "resources": {"cpu_millis": 100, "memory_mib": 64},
            "review_ref": "SA12-HTTP-existing-reviewed-Run",
        }
    if action == "register":
        return {
            "request_id": str(env.request_id), "profile_sha256": "b" * 64,
            "ttl_seconds": 300, "review_ref": "SA12-HTTP-existing-server-profile",
        }
    if action in {"revoke", "cancel", "drain", "reconcile"}:
        key = "session_id" if action == "revoke" else (
            "queue_id" if action == "cancel" else "assignment_id"
        )
        value = {
            key: str(getattr(env, key)), "expected_revision": 1,
            "review_ref": "SA12-HTTP-exact-owner-resource-request",
        }
        if action == "drain":
            value["reason_code"] = "OWNER_DRAIN"
        if action == "reconcile":
            value.update(operation_id=str(env.operation_id), request_sha256="c" * 64)
        return value
    if action == "claim":
        return {
            "request_id": str(env.request_id), "session_id": str(env.session_id),
            "expected_config_revision": 1,
        }
    value = {
        "assignment_id": str(env.assignment_id), "session_id": str(env.session_id),
        "epoch": 1, "expected_revision": 1,
    }
    if action in {"ready", "report"}:
        value.update(operation_id=str(env.operation_id), request_sha256="c" * 64)
    if action == "report":
        value["status"] = "finished"
    return value


def path(env, action):
    if action == "configure":
        return PREFIX + "/config"
    if action in {"state", "queue", "assignments"}:
        return PREFIX + "/" + action
    if action == "enqueue":
        return PREFIX + "/queue"
    if action == "cancel":
        return PREFIX + f"/queue/{env.queue_id}/cancel"
    if action == "register":
        return PREFIX + "/sessions"
    if action == "revoke":
        return PREFIX + f"/sessions/{env.session_id}/revoke"
    if action == "assignment":
        return PREFIX + f"/assignments/{env.assignment_id}"
    if action in {"drain", "reconcile"}:
        return PREFIX + f"/assignments/{env.assignment_id}/" + action
    if action == "claim":
        return PREFIX + "/worker/claim"
    return PREFIX + f"/worker/{env.assignment_id}/" + action


def call(env, action, *, body=None):
    if action in OWNER_READS:
        return env.client.get(path(env, action))
    method = env.client.put if action == "configure" else env.client.post
    return method(path(env, action), json=payload(env, action) if body is None else body)


def assert_private(env, response):
    assert response.headers["cache-control"] == "no-store"
    for secret in (SECRET, WORKER_TOKEN, env.cookie, env.csrf, "ciphertext"):
        assert secret not in response.text


def test_formal_application_registers_exact_fifteen_scheduler_routes(browser):
    actual = [
        (method, route.path)
        for route in browser.app.routes if hasattr(route, "methods")
        for method in route.methods if getattr(route, "path", "").startswith(PREFIX + "/")
    ]
    assert len(actual) == 15
    assert set(actual) == {
        ("PUT", PREFIX + "/config"), ("GET", PREFIX + "/state"),
        ("POST", PREFIX + "/queue"), ("GET", PREFIX + "/queue"),
        ("POST", PREFIX + "/queue/{queue_id}/cancel"),
        ("POST", PREFIX + "/sessions"),
        ("POST", PREFIX + "/sessions/{session_id}/revoke"),
        ("GET", PREFIX + "/assignments"),
        ("GET", PREFIX + "/assignments/{assignment_id}"),
        ("POST", PREFIX + "/assignments/{assignment_id}/drain"),
        ("POST", PREFIX + "/assignments/{assignment_id}/reconcile"),
        ("POST", PREFIX + "/worker/claim"),
        ("POST", PREFIX + "/worker/{assignment_id}/heartbeat"),
        ("POST", PREFIX + "/worker/{assignment_id}/ready"),
        ("POST", PREFIX + "/worker/{assignment_id}/report"),
    }


def test_owner_routes_require_cookie_not_operator_or_worker_token(browser):
    env = browser
    before, business = persisted(env), original_state(env.engine)
    env.client.headers.update({
        "X-MGAR-Token": env.engine.settings.local_token.get_secret_value(),
        "Authorization": "Bearer " + WORKER_TOKEN,
    })
    for action in (*OWNER_WRITES, *OWNER_READS):
        response = call(env, action)
        assert response.status_code == 401, (action, response.text)
        assert_private(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("fault", ["origin_missing", "csrf_missing", "cross_origin"])
def test_owner_mutations_require_origin_and_csrf(browser, fault):
    env = browser
    authenticate(env)
    if fault == "origin_missing":
        del env.client.headers["Origin"]
    elif fault == "csrf_missing":
        del env.client.headers["X-CSRF-Token"]
    else:
        env.client.headers["Origin"] = "https://other.invalid"
    before, business = persisted(env), original_state(env.engine)
    for action in OWNER_WRITES:
        response = call(env, action)
        assert response.status_code == 403, (action, response.text)
        assert_private(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


def test_worker_routes_never_use_owner_cookie_or_operator_token(browser):
    env = browser
    authenticate(env)
    env.client.headers["X-MGAR-Token"] = env.engine.settings.local_token.get_secret_value()
    before, business = persisted(env), original_state(env.engine)
    for action in WORKER_ACTIONS:
        response = call(env, action)
        assert response.status_code == 401, (action, response.text)
        assert_private(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("authorization", [
    "Basic " + WORKER_TOKEN, "Bearer ", "Bearer " + WORKER_TOKEN + " extra",
    "Bearer\t" + WORKER_TOKEN, "Bearer " + "x" * 513,
])
def test_worker_bearer_syntax_is_independent_and_never_echoed(browser, authorization):
    env = browser
    env.client.headers["Authorization"] = authorization
    before = persisted(env)
    response = call(env, "claim")
    assert response.status_code == 401
    assert authorization not in response.text
    assert_private(env, response)
    assert persisted(env) == before


@pytest.mark.parametrize("action", OWNER_WRITES)
def test_owner_json_rejects_extra_body_authority_without_side_effects(browser, action):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    body = payload(env, action)
    body.update(body=SECRET, actor="owner", factory="paid", quiescent=True)
    response = call(env, action, body=body)
    assert response.status_code == 422
    assert response.json()["code"] == "SOURCE_SCHEDULER_REQUEST_INVALID"
    assert_private(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("action", WORKER_ACTIONS)
def test_worker_json_cannot_supply_commands_paths_or_quiescence(browser, action):
    env = browser
    env.client.headers["Authorization"] = "Bearer " + WORKER_TOKEN
    before, business = persisted(env), original_state(env.engine)
    body = payload(env, action)
    body.update(argv=["sh", "-c", SECRET], host_path="/home/" + SECRET, quiescent=True)
    response = call(env, action, body=body)
    assert response.status_code == 422
    assert response.json()["code"] == "SOURCE_SCHEDULER_REQUEST_INVALID"
    assert_private(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("action,key", [
    ("cancel", "queue_id"), ("revoke", "session_id"), ("drain", "assignment_id"),
    ("reconcile", "assignment_id"), ("heartbeat", "assignment_id"),
    ("ready", "assignment_id"), ("report", "assignment_id"),
])
def test_path_and_body_reference_mismatch_is_rejected_before_service(browser, action, key):
    env = browser
    authenticate(env)
    env.client.headers["Authorization"] = "Bearer " + WORKER_TOKEN
    before, business = persisted(env), original_state(env.engine)
    body = payload(env, action)
    body[key] = str(uuid4())
    response = call(env, action, body=body)
    assert response.status_code == 422
    assert response.json()["code"] == "SOURCE_SCHEDULER_REQUEST_INVALID"
    assert_private(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("field,value", [
    ("expected_revision", True), ("expected_revision", "0"),
    ("enabled", "false"), ("slots", True), ("slots", "1"),
    ("valid_from", "2026-11-13T00:00:00"),
])
def test_configuration_json_preserves_strict_scalars_and_aware_datetimes(browser, field, value):
    env = browser
    authenticate(env)
    before = persisted(env)
    body = configuration()
    if field == "expected_revision":
        body[field] = value
    elif field in {"enabled", "valid_from"}:
        body["configuration"][field] = value
    else:
        body["configuration"]["limits"][field] = value
    response = call(env, "configure", body=body)
    assert response.status_code == 422
    assert response.json()["code"] == "SOURCE_SCHEDULER_REQUEST_INVALID"
    assert_private(env, response)
    assert persisted(env) == before


def test_invalid_json_is_sanitized_and_prefix_errors_are_not_cached(browser):
    env = browser
    authenticate(env)
    before = persisted(env)
    response = env.client.put(
        path(env, "configure"), content='{"secret":"' + SECRET,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "SOURCE_SCHEDULER_REQUEST_INVALID"
    assert_private(env, response)
    for route, status in [
        (PREFIX, 404), (PREFIX + "/unknown", 404),
        (PREFIX + "/worker/claim", 405), (PREFIX + "/assignments/not-a-uuid", 422),
        (PREFIX + "/queue?limit=0", 422), (PREFIX + "/assignments?limit=101", 422),
    ]:
        result = env.client.get(route)
        assert result.status_code == status, result.text
        assert_private(env, result)
    assert persisted(env) == before


def test_owner_configuration_json_roundtrip_and_cas_without_driver_or_business_write(browser):
    env = browser
    authenticate(env)
    assert env.engine.source_scheduler.driver is None
    before = original_state(env.engine)
    body = configuration()
    created = call(env, "configure", body=body)
    assert created.status_code == 200, created.text
    assert created.json()["revision"] == 1
    assert created.json()["configuration"]["enabled"] is False
    assert_private(env, created)
    current = call(env, "state")
    assert current.status_code == 200, current.text
    assert current.json()["target"] is None
    assert current.json()["capacity"]["driver_configured"] is False
    assert_private(env, current)
    stale = call(env, "configure", body=body)
    assert stale.status_code == 409
    assert_private(env, stale)
    assert original_state(env.engine) == before
    state = persisted(env)
    assert len(state["sa_scheduler_config_versions"]) == 1
    assert not state["sa_scheduler_assignments"] and not state["sa_scheduler_operations"]


def test_state_optional_run_snapshot_is_original_bodyless_target_with_same_fifteen_routes(browser):
    env = browser
    authenticate(env)
    result = call(env, "configure")
    assert result.status_code == 200, result.text
    receipt = env.engine.ingest(TriggerInput(
        source="web", external_event_id="SA12-HTTP-target-" + uuid4().hex,
        project_id=env.engine.settings.project_id, repository=env.engine.settings.repository,
        issue_number=12, actor=env.engine.settings.local_actor,
        body="/mgar investigate " + SECRET, execution_mode="fixture",
    ))
    assert receipt.run_id is not None
    before = persisted(env)
    response = env.client.get(PREFIX + f"/state?run_id={receipt.run_id}")
    assert response.status_code == 200, response.text
    target = response.json()["target"]
    assert set(target) == {"run_id", "epoch", "target_sha256"}
    assert target["run_id"] == str(receipt.run_id) and target["epoch"] == 0
    assert len(target["target_sha256"]) == 64
    assert_private(env, response)
    assert persisted(env) == before


def test_session_registration_cannot_activate_an_uninjected_driver(browser):
    env = browser
    authenticate(env)
    configured = call(env, "configure")
    assert configured.status_code == 200, configured.text
    before, business = persisted(env), original_state(env.engine)
    response = call(env, "register")
    assert response.status_code == 403
    assert response.json()["code"] == "SOURCE_SCHEDULER_DRIVER_REQUIRED"
    assert_private(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


def test_current_owner_session_is_rechecked_after_browser_dependency(browser, monkeypatch):
    env = browser
    authenticate(env)
    before = persisted(env)
    original = WebAccounts.session

    def revoked_after_read(accounts, cookie):
        user = original(accounts, cookie)
        if user is not None and cookie == env.cookie:
            accounts.revoke(cookie)
        return user

    monkeypatch.setattr(WebAccounts, "session", revoked_after_read)
    response = call(env, "configure", body=deepcopy(configuration()))
    assert response.status_code == 401
    assert_private(env, response)
    assert persisted(env) == before


@pytest.fixture
def worker_browser(engine, tmp_path):
    """Actual Engine/Core/PG plus filesystem-only controlled resource receipts.

    This exercises HTTP resource ownership, never a real Harness process or
    model-success claim. The separate Runtime tests own real Broker evidence.
    """
    source = scheduler_setup(engine, tmp_path)
    session = WebAccounts(engine).session(source.cookie)
    assert session is not None
    app = api.create_app(engine.settings, engine, run_background=False)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        yield SimpleNamespace(
            engine=engine, source=source, client=client, app=app,
            cookie=source.cookie, csrf=session.csrf_token, worker_secrets=[],
        )


def worker_private(env, response):
    assert_private(env, response)
    for secret in (*env.worker_secrets, str(env.source.driver.root)):
        assert secret not in response.text
    assert "token_sha256" not in response.text


def worker_post(env, action, worker, body, *, assignment_id=None):
    # A real Worker request receives no Owner cookie or CSRF credentials.
    env.client.cookies.clear()
    for header in ("Origin", "X-CSRF-Token", "X-MGAR-Token"):
        env.client.headers.pop(header, None)
    env.client.headers["Authorization"] = "Bearer " + worker["bearer"]
    target = PREFIX + "/worker/claim" if action == "claim" else (
        PREFIX + f"/worker/{assignment_id}/" + action
    )
    response = env.client.post(target, json=body)
    assert SESSION_COOKIE not in response.request.headers.get("cookie", "")
    worker_private(env, response)
    return response


def owner_assignment(env, assignment_id):
    authenticate(env)
    env.client.headers.pop("Authorization", None)
    response = env.client.get(PREFIX + f"/assignments/{assignment_id}")
    assert response.status_code == 200, response.text
    worker_private(env, response)
    return response.json()


def register_worker(env, *, ttl=300):
    authenticate(env)
    env.client.headers.pop("Authorization", None)
    request = {
        "request_id": str(uuid4()), "profile_sha256": env.source.driver.profile_sha256,
        "ttl_seconds": ttl, "review_ref": "SA12-HTTP-controlled-filesystem-session",
    }
    response = env.client.post(PREFIX + "/sessions", json=request)
    assert response.status_code == 200, response.text
    assert_private(env, response)
    worker = response.json()
    assert isinstance(worker["bearer"], str) and len(worker["bearer"]) >= 32
    env.worker_secrets.append(worker["bearer"])
    repeated = env.client.post(PREFIX + "/sessions", json=request)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["session_id"] == worker["session_id"]
    assert repeated.json()["bearer"] is None
    worker_private(env, repeated)
    return worker


def http_claim(env, worker):
    request = {
        "request_id": str(uuid4()), "session_id": worker["session_id"],
        "expected_config_revision": env.source.config["revision"],
    }
    response = worker_post(env, "claim", worker, request)
    assert response.status_code == 200, response.text
    claimed = response.json()
    assert claimed["state"] == "preparing"
    assert claimed["prepare_operation"]["state"] == "known_terminal"
    repeated = worker_post(env, "claim", worker, request)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["assignment_id"] == claimed["assignment_id"]
    assert repeated.json()["prepare_operation"] == claimed["prepare_operation"]
    return claimed


def fenced_request(row, worker, operation=None):
    request = {
        "assignment_id": row["assignment_id"], "session_id": worker["session_id"],
        "epoch": row["execution_epoch"], "expected_revision": row["revision"],
    }
    if operation is not None:
        request.update(
            operation_id=operation["operation_id"], request_sha256=operation["request_sha256"],
        )
    return request


def http_ready(env, worker, claimed):
    row = owner_assignment(env, claimed["assignment_id"])
    request = fenced_request(row, worker, claimed["prepare_operation"])
    response = worker_post(env, "ready", worker, request, assignment_id=row["assignment_id"])
    assert response.status_code == 200, response.text
    active = response.json()
    assert active["state"] == "active" and active["revision"] == row["revision"] + 1
    return active


def test_http_worker_claim_ready_heartbeat_uses_actual_core_and_private_resource_directory(
    worker_browser,
):
    env = worker_browser
    queued = scheduler_enqueue(env.source)
    worker = register_worker(env)
    claimed = http_claim(env, worker)
    assert claimed["run_id"] == str(queued["run_id"])
    workspace = env.source.driver.root / claimed["workspace_id"]
    assert workspace.is_dir() and (workspace / "fixture.txt").is_file()
    active = http_ready(env, worker, claimed)
    response = worker_post(
        env, "heartbeat", worker, fenced_request(active, worker),
        assignment_id=active["assignment_id"],
    )
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["state"] == "active" and current["revision"] == active["revision"] + 1
    assert current["execution_epoch"] == active["execution_epoch"]
    stale = worker_post(
        env, "heartbeat", worker, fenced_request(active, worker),
        assignment_id=active["assignment_id"],
    )
    assert stale.status_code in {403, 409}
    assert owner_assignment(env, active["assignment_id"])["revision"] == current["revision"]
    business = original_state(env.engine)
    assert len(business["runs"]) == len(business["tickets"]) == 1
    assert business["usage_calls"] == business["router_calls"] == business["actions"] == []
    assert not env.engine.settings.allow_paid and not env.engine.settings.allow_external_write


@pytest.mark.parametrize("suggestion", ["finished", "waiting_user", "failed"])
def test_http_worker_report_is_resource_suggestion_without_task_completion_or_slot_release(
    worker_browser, suggestion,
):
    env = worker_browser
    scheduler_enqueue(env.source)
    worker = register_worker(env)
    claimed = http_claim(env, worker)
    active = http_ready(env, worker, claimed)
    request = fenced_request(active, worker, claimed["prepare_operation"])
    request["status"] = suggestion
    response = worker_post(
        env, "report", worker, request, assignment_id=active["assignment_id"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "draining"
    assert response.json()["business_outcome_changed"] is False
    original = env.engine.get_run(UUID(active["run_id"]))
    assert original["status"] == "paused" and original["ticket_status"] == "open"
    assert original["execution_epoch"] == active["execution_epoch"] + 1
    row = owner_assignment(env, active["assignment_id"])
    assert row["state"] == "draining" and row["released_at"] is None
    authenticate(env)
    state = env.client.get(PREFIX + "/state")
    assert state.status_code == 200, state.text
    worker_private(env, state)
    assert state.json()["capacity"]["usage"]["slots"] == 1
    late = worker_post(env, "report", worker, request, assignment_id=active["assignment_id"])
    assert late.status_code in {403, 409}
    assert env.engine.get_run(UUID(active["run_id"]))["status"] == "paused"
    business = original_state(env.engine)
    for table in ("actions", "approvals", "outbox", "usage_calls", "router_calls", "verdicts"):
        assert business[table] == []


def test_http_worker_bearer_is_bound_to_its_registered_session_and_revocation_is_immediate(
    worker_browser,
):
    env = worker_browser
    scheduler_enqueue(env.source)
    worker = register_worker(env)
    other = register_worker(env)
    claimed = http_claim(env, worker)
    active = http_ready(env, worker, claimed)
    before = persisted(env)
    wrong = worker_post(
        env, "heartbeat", other, fenced_request(active, worker),
        assignment_id=active["assignment_id"],
    )
    assert wrong.status_code == 403
    assert persisted(env) == before
    authenticate(env)
    env.client.headers.pop("Authorization", None)
    revoked = env.client.post(PREFIX + f"/sessions/{worker['session_id']}/revoke", json={
        "session_id": worker["session_id"], "expected_revision": worker["revision"],
        "review_ref": "SA12-HTTP-revoke-exact-current-worker-session",
    })
    assert revoked.status_code == 200, revoked.text
    worker_private(env, revoked)
    row = owner_assignment(env, active["assignment_id"])
    assert row["state"] == "draining"
    before = persisted(env)
    rejected = worker_post(
        env, "heartbeat", worker, fenced_request(row, worker),
        assignment_id=row["assignment_id"],
    )
    assert rejected.status_code == 403
    assert persisted(env) == before
    original = env.engine.get_run(UUID(active["run_id"]))
    assert original["execution_epoch"] > active["execution_epoch"]


def test_http_owner_drain_fences_the_old_worker_epoch_without_implicit_release(worker_browser):
    env = worker_browser
    scheduler_enqueue(env.source)
    worker = register_worker(env)
    claimed = http_claim(env, worker)
    active = http_ready(env, worker, claimed)
    authenticate(env)
    env.client.headers.pop("Authorization", None)
    drained = env.client.post(PREFIX + f"/assignments/{active['assignment_id']}/drain", json={
        "assignment_id": active["assignment_id"], "expected_revision": active["revision"],
        "reason_code": "OWNER_DRAIN", "review_ref": "SA12-HTTP-fence-before-worker-return",
    })
    assert drained.status_code == 200, drained.text
    worker_private(env, drained)
    original = env.engine.get_run(UUID(active["run_id"]))
    assert original["execution_epoch"] > active["execution_epoch"]
    current = owner_assignment(env, active["assignment_id"])
    assert current["state"] == "draining" and current["released_at"] is None
    before = persisted(env)
    late = worker_post(
        env, "heartbeat", worker, fenced_request(current, worker),
        assignment_id=current["assignment_id"],
    )
    assert late.status_code in {403, 409}
    assert persisted(env) == before
    assert env.engine.get_run(UUID(active["run_id"]))["status"] == "paused"


def test_http_expired_real_session_bearer_cannot_claim_or_create_resources(worker_browser):
    env = worker_browser
    worker = register_worker(env, ttl=1)
    # Observe the real PostgreSQL clock; do not rewrite expiry/state to fake it.
    deadline = time.monotonic() + 5
    while True:
        with env.engine.transaction() as conn:
            expired = conn.execute(
                "SELECT expires_at <= clock_timestamp() AS expired "
                "FROM app.sa_scheduler_sessions WHERE session_id=%s",
                (UUID(worker["session_id"]),),
            ).fetchone()["expired"]
        if expired:
            break
        assert time.monotonic() < deadline
        time.sleep(0.02)
    before, business = persisted(env), original_state(env.engine)
    rejected = worker_post(env, "claim", worker, {
        "request_id": str(uuid4()), "session_id": worker["session_id"],
        "expected_config_revision": env.source.config["revision"],
    })
    assert rejected.status_code == 403
    assert rejected.json()["code"] == "SOURCE_SCHEDULER_SESSION_FENCED"
    assert persisted(env) == before and original_state(env.engine) == business
