"""First SA18 original HTTP Owner/CSRF/body/privacy checks, not old suite reruns."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from mgar_v3.api import create_app
from mgar_v3.web_access import SESSION_COOKIE, LoginInput, WebAccounts

ORIGIN = "http://127.0.0.1:18081"
PREFIX = "/api/v3/source-reviews"
ID = str(uuid4())
READS = ["/capabilities", "", "/events", "/" + ID, "/" + ID + "/patch"]
WRITES = [
    "",
    "/" + ID + "/inspect",
    "/" + ID + "/approve",
    "/" + ID + "/cancel",
    "/tick",
    "/" + ID + "/reconcile",
    "/" + ID + "/native-links",
    "/maintain",
]
ASSETS = ["/source-reviews", "/assets/source-reviews.js", "/assets/source-reviews.css"]


@pytest.fixture
def browser(engine):
    accounts = WebAccounts(engine)
    owner = accounts.create_owner(
        LoginInput(username="sa18-owner", password=SecretStr("fixture-sa18-password"))
    )
    cookie = accounts.issue_session(owner)
    user = accounts.session(cookie)
    app = create_app(engine.settings, engine, run_background=False)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        yield SimpleNamespace(client=client, engine=engine, cookie=cookie, csrf=user.csrf_token)


def auth(e):
    e.client.cookies.set(SESSION_COOKIE, e.cookie)
    e.client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": e.csrf})


@pytest.mark.parametrize(
    "method,path",
    [("GET", PREFIX + p) for p in READS]
    + [("POST", PREFIX + p) for p in WRITES]
    + [("GET", p) for p in ASSETS],
)
def test_every_formal_route_requires_current_owner_cookie(browser, method, path):
    r = browser.client.request(method, path, json={})
    assert r.status_code == 401 and r.json()["code"] == "AUTH_REQUIRED"
    assert r.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", WRITES)
def test_every_owner_mutation_requires_csrf_and_origin(browser, path):
    browser.client.cookies.set(SESSION_COOKIE, browser.cookie)
    r = browser.client.post(PREFIX + path, json={})
    assert r.status_code == 403 and r.json()["code"] == "ORIGIN_DENIED"
    r = browser.client.post(PREFIX + path, json={}, headers={"Origin": ORIGIN})
    assert r.status_code == 403 and r.json()["code"] == "CSRF_REQUIRED"


def test_operator_token_does_not_replace_owner_cookie_for_approval_or_inspection(browser):
    r = browser.client.post(
        PREFIX + "/" + ID + "/approve",
        json={},
        headers={"X-MGAR-Token": browser.engine.settings.local_token.get_secret_value()},
    )
    assert r.status_code == 401
    with browser.engine.transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM app.approvals").fetchone()["n"] == 0


def test_inert_capabilities_and_private_page_assets_use_strict_security_headers(browser):
    auth(browser)
    r = browser.client.get(PREFIX + "/capabilities")
    assert r.status_code == 200 and r.json()["profiles"] == []
    assert not r.json()["paid"] and r.json()["live_publication"] == "NOT_RUN"
    for p in ASSETS:
        r = browser.client.get(p)
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "innerHTML" not in browser.client.get("/assets/source-reviews.js").text
    assert browser.client.get(PREFIX).json() == {"reviews": [], "partial": False}


@pytest.mark.parametrize(
    "extra",
    [
        "diff",
        "files",
        "body",
        "actor",
        "approved",
        "credential",
        "provider",
        "tests_passed",
        "remote_url",
    ],
)
def test_http_prepare_rejects_claimed_evidence_without_creating_business_rows(browser, extra):
    auth(browser)
    raw = dict(
        request_id=str(uuid4()),
        coding_id=str(uuid4()),
        verification_id=str(uuid4()),
        expected_epoch=1,
        expected_input_revision=1,
        profile_sha256="a" * 64,
    )
    raw[extra] = "PRIVATE_TEST_VALUE"
    r = browser.client.post(PREFIX, json=raw)
    assert r.status_code == 422 and "PRIVATE_TEST_VALUE" not in r.text
    with browser.engine.transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 0


@pytest.mark.parametrize("limit", ["0", "33", "-1"])
def test_list_and_event_limits_fail_closed(browser, limit):
    auth(browser)
    for path in ("", "/events"):
        r = browser.client.get(PREFIX + path + "?limit=" + limit)
        assert r.status_code == 422
