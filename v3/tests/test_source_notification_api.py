"""Actual original app, eleven Owner controls and inert defaults; no platform IO."""

from uuid import uuid4

import pytest
from test_super_agent_sa13_api import authenticate
from test_super_agent_sa13_api import browser as browser

from mgar_v3.super_agent.source_notification_api import PREFIX

ROUTES = [
    ("GET", "/capabilities"),
    ("GET", "/sources/{source_id}"),
    ("PUT", "/sources/{source_id}"),
    ("POST", "/plans"),
    ("GET", "/plans"),
    ("GET", "/plans/{action_id}"),
    ("POST", "/plans/{action_id}/cancel"),
    ("POST", "/tick"),
    ("POST", "/plans/{action_id}/reconcile"),
    ("POST", "/maintain"),
    ("GET", "/events"),
]
WRITES = [(m, p) for m, p in ROUTES if m != "GET"]


def path(route):
    return PREFIX + route.replace("{source_id}", str(uuid4())).replace("{action_id}", str(uuid4()))


def test_exact_eleven_formal_routes_and_no_default_sender(browser):
    actual = {
        (method, r.path)
        for r in browser.app.routes
        if hasattr(r, "methods")
        for method in r.methods
        if r.path.startswith(PREFIX + "/")
    }
    assert actual == {(method, PREFIX + p) for method, p in ROUTES}
    assert browser.engine.source_notifications.engine is browser.engine
    assert not browser.engine.source_notifications._profiles


@pytest.mark.parametrize("method,route", ROUTES)
def test_operator_or_worker_bearer_is_not_current_owner(method, route, browser):
    browser.client.headers.update(
        {
            "X-MGAR-Token": browser.engine.settings.local_token.get_secret_value(),
            "Authorization": "Bearer controlled-unprivileged-worker",
        }
    )
    response = browser.client.request(
        method, path(route), **({"json": {}} if method != "GET" else {})
    )
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,route", WRITES)
@pytest.mark.parametrize("fault", ["csrf", "origin", "foreign"])
def test_each_owner_write_rechecks_browser_origin_and_csrf(method, route, fault, browser):
    authenticate(browser)
    if fault == "csrf":
        del browser.client.headers["X-CSRF-Token"]
    elif fault == "origin":
        del browser.client.headers["Origin"]
    else:
        browser.client.headers["Origin"] = "https://foreign.invalid"
    result = browser.client.request(method, path(route), json={})
    assert result.status_code == 403
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("route", ["/capabilities", "/plans", "/events"])
def test_authenticated_owner_views_are_current_inert_and_bodyless(route, browser):
    authenticate(browser)
    response = browser.client.get(PREFIX + route)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "body" not in response.json() and "secret" not in response.text
    if route == "/capabilities":
        assert response.json()["default"] == "disabled"
        assert not response.json()["unknown_retry"]
        assert response.json()["real_platform"] == "NOT_RUN"


@pytest.mark.parametrize("field", ["body", "url", "destination", "provider", "approved", "runtime"])
def test_request_rejects_injected_notification_authority_without_echo(field, browser):
    authenticate(browser)
    secret = "SA17-PRIVATE-HTTP-INPUT-DO-NOT-ECHO"
    response = browser.client.post(PREFIX + "/plans", json={field: secret})
    assert response.status_code == 422 and secret not in response.text
    with browser.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.outbox").fetchone()["n"] == 0


def test_bounded_json_and_invalid_limit_do_not_enqueue(browser):
    authenticate(browser)
    response = browser.client.post(
        PREFIX + "/plans", content=b"x" * 16385, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert browser.client.get(PREFIX + "/plans?limit=65").status_code == 422
    assert browser.client.post(PREFIX + "/tick", json={"limit": 0}).status_code == 422
    assert browser.client.post(PREFIX + "/tick", json={"limit": 1}).json()["dispatched"] == []
