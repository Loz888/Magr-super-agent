"""Budget administration uses the real authenticated API and the sole Engine PG state."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mgar_v3.api import create_app
from mgar_v3.web_access import SESSION_COOKIE, LoginInput, WebAccounts

ORIGIN = "http://127.0.0.1:18080"
PREFIX = "/api/v3/project-budgets"
PASSWORD = "sa07-owner-password"
ORGANIZATION = "SA07-Reviewed-Organization"


@pytest.fixture
def budget_browser(settings, engine):
    # These administration tests start without payment or publication authority,
    # independently of the operator's local environment flags.
    settings = settings.model_copy(update={"allow_paid": False, "allow_external_write": False})
    engine.settings = settings
    accounts = WebAccounts(engine)
    owner = accounts.create_owner(LoginInput(username="budget-owner", password=PASSWORD))
    cookie = accounts.issue_session(owner)
    session = accounts.session(cookie)
    assert session is not None
    with TestClient(
        create_app(settings, engine, run_background=False),
        base_url=ORIGIN,
        client=("127.0.0.1", 12345),
    ) as client:
        yield SimpleNamespace(
            engine=engine,
            settings=settings,
            client=client,
            owner=owner,
            cookie=cookie,
            csrf=session.csrf_token,
            organization=ORGANIZATION,
            organization_path=PREFIX + "/organization/" + ORGANIZATION,
            project_path=PREFIX + "/project/" + settings.project_id,
        )


def budget_request(*, organization_id=None, revision=0):
    now = datetime.now(UTC)
    return {
        "expected_revision": revision,
        "definition": {
            "enabled": False,
            "organization_id": organization_id,
            "timezone": "Asia/Shanghai",
            "valid_from": (now - timedelta(minutes=1)).isoformat(),
            "valid_until": (now + timedelta(hours=1)).isoformat(),
        },
        "review_ref": "sa07-reviewed-administration",
    }


def authenticate(env):
    env.client.cookies.set(SESSION_COOKIE, env.cookie)
    env.client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": env.csrf})


def save_project(env):
    authenticate(env)
    response = env.client.put(env.organization_path, json=budget_request())
    assert response.status_code == 200, response.text
    response = env.client.put(
        env.project_path, json=budget_request(organization_id=env.organization)
    )
    assert response.status_code == 200, response.text
    return response


def budget_counts(env):
    with env.engine.transaction() as conn:
        return {
            table: conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"]
            for table in (
                "sa_budget_policies",
                "sa_budget_versions",
                "sa_budget_calls",
                "sa_budget_quota",
                "usage_calls",
                "router_calls",
            )
        }


def test_budget_management_requires_cookie_origin_csrf_and_signed_save(budget_browser):
    env = budget_browser
    body = budget_request()
    assert env.client.get(PREFIX).status_code == 401
    assert env.client.get(env.organization_path).status_code == 401
    env.client.headers["X-MGAR-Token"] = env.settings.local_token.get_secret_value()
    assert env.client.put(env.organization_path, json=body).status_code == 401
    env.client.cookies.set(SESSION_COOKIE, env.cookie)
    assert env.client.put(env.organization_path, json=body).status_code == 403
    env.client.headers["Origin"] = ORIGIN
    assert env.client.put(env.organization_path, json=body).status_code == 403
    env.client.headers["X-CSRF-Token"] = env.csrf
    assert env.client.put(
        env.organization_path, json=body, headers={"Origin": "https://outside.invalid"}
    ).status_code == 403
    assert budget_counts(env)["sa_budget_policies"] == 0
    response = env.client.put(env.organization_path, json=body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    with env.engine.transaction() as conn:
        row = conn.execute("SELECT * FROM app.sa_budget_policies").fetchone()
        assert row["scope_type"] == "organization" and row["scope_id"] == env.organization
        assert row["signed_by"] == env.owner and row["signed_at"] is not None
        assert row["revision"] == 1 and row["definition"]["enabled"] is False


def test_budget_admin_views_do_not_allocate_calls_or_payment_authority(budget_browser):
    env = budget_browser
    response = save_project(env)
    current = None
    for path in (PREFIX, env.project_path, env.organization_path):
        result = env.client.get(path)
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store"
        view = result.json()
        assert view["configured"] is True and view["current"]["revision"] == 1
        assert view["current"]["definition"]["enabled"] is False
        assert view["usage"]["total_micros"] == view["usage"]["total_tokens"] == 0
        assert view["usage"]["unknown_calls"] == view["usage"]["unknown_token_calls"] == 0
        if path == PREFIX:
            current = view["current"]
        elif path == env.project_path:
            assert view["current"] == current
        for secret in (env.cookie, env.csrf, PASSWORD, env.settings.local_token.get_secret_value()):
            assert secret not in result.text
    assert response.headers["cache-control"] == "no-store"
    assert budget_counts(env) == {
        "sa_budget_policies": 2,
        "sa_budget_versions": 2,
        "sa_budget_calls": 0,
        "sa_budget_quota": 0,
        "usage_calls": 0,
        "router_calls": 0,
    }
    assert not env.engine.settings.allow_paid and not env.engine.settings.allow_external_write
    with env.engine.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM app.sa_budget_policies WHERE scope_type='project' AND scope_id=%s",
            (env.settings.project_id,),
        ).fetchone()
        assert row["organization_id"] == env.organization and row["revision"] == 1


def test_budget_api_cas_keeps_previous_signed_configuration(budget_browser):
    env = budget_browser
    save_project(env)
    with env.engine.transaction() as conn:
        original = conn.execute(
            "SELECT definition_sha256,definition FROM app.sa_budget_versions "
            "WHERE scope_type='project' AND scope_id=%s AND revision=1",
            (env.settings.project_id,),
        ).fetchone()
    request = budget_request(organization_id=env.organization)
    assert env.client.put(env.project_path, json=request).status_code == 409
    request["expected_revision"] = 1
    request["definition"]["timezone"] = "UTC"
    response = env.client.put(env.project_path, json=request)
    assert response.status_code == 200, response.text
    with env.engine.transaction() as conn:
        retained = conn.execute(
            "SELECT definition_sha256,definition FROM app.sa_budget_versions "
            "WHERE scope_type='project' AND scope_id=%s AND revision=1",
            (env.settings.project_id,),
        ).fetchone()
        assert retained == original
        current = conn.execute(
            "SELECT revision,definition FROM app.sa_budget_policies "
            "WHERE scope_type='project' AND scope_id=%s",
            (env.settings.project_id,),
        ).fetchone()
        assert current["revision"] == 2 and current["definition"]["timezone"] == "UTC"
    assert budget_counts(env)["sa_budget_versions"] == 3


@pytest.mark.parametrize("method", ["get", "put"])
def test_budget_api_rejects_other_project_scope(budget_browser, method):
    env = budget_browser
    save_project(env)
    before = budget_counts(env)
    path = PREFIX + "/project/Another-Project"
    response = (
        env.client.get(path)
        if method == "get"
        else env.client.put(path, json=budget_request(organization_id=env.organization))
    )
    assert response.status_code == 403
    assert budget_counts(env) == before


@pytest.mark.parametrize("method", ["get", "put"])
def test_budget_api_rechecks_session_revoked_after_http_dependency(
    budget_browser, method, monkeypatch
):
    env = budget_browser
    authenticate(env)
    original = WebAccounts.session

    def revoke_after_read(accounts, cookie):
        session = original(accounts, cookie)
        if session is not None:
            accounts.revoke(cookie)
        return session

    monkeypatch.setattr(WebAccounts, "session", revoke_after_read)
    response = (
        env.client.get(PREFIX)
        if method == "get"
        else env.client.put(env.organization_path, json=budget_request())
    )
    assert response.status_code == 401
    counts = budget_counts(env)
    assert counts["sa_budget_policies"] == counts["sa_budget_versions"] == 0
    assert counts["sa_budget_calls"] == counts["usage_calls"] == counts["router_calls"] == 0


@pytest.mark.parametrize("operation", ["reserve", "settle", "release", "quota", "activate", "pay"])
def test_budget_api_has_no_payment_or_consumer_endpoints(budget_browser, operation):
    env = budget_browser
    save_project(env)
    before = budget_counts(env)
    response = env.client.post(
        env.project_path + "/" + operation,
        json={"amount": 1, "approved": True, "body": "PRIVATE_UNTRUSTED_REQUEST"},
    )
    assert response.status_code == 404
    assert "PRIVATE_UNTRUSTED_REQUEST" not in response.text
    assert budget_counts(env) == before


@pytest.mark.parametrize("bad_scope", ["run", "source", "worker"])
def test_budget_api_rejects_unrecognized_scope_type(budget_browser, bad_scope):
    env = budget_browser
    authenticate(env)
    response = env.client.put(PREFIX + "/" + bad_scope + "/untrusted", json=budget_request())
    assert response.status_code == 422
    assert budget_counts(env)["sa_budget_policies"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False, "caps": {"day_micros": True}},
        {"enabled": False, "caps": {"day_micros": 1.5}},
        {"enabled": False, "allow_paid": True},
    ],
)
def test_budget_api_invalid_configuration_is_fixed_error_without_writes(budget_browser, change):
    env = budget_browser
    authenticate(env)
    request = budget_request()
    request["definition"].update(change)
    request["review_ref"] = "PRIVATE_INVALID_BUDGET_BODY"
    response = env.client.put(env.organization_path, json=request)
    assert response.status_code == 422 and response.json()["code"] == "INVALID_INPUT"
    assert "PRIVATE_INVALID_BUDGET_BODY" not in response.text
    assert budget_counts(env)["sa_budget_policies"] == 0
