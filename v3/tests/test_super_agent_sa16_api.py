"""Actual original API, current cookie/Origin/CSRF and thirteen bounded routes."""

from uuid import uuid4

import pytest
from sa08_support import original_state
from test_super_agent_sa13_api import authenticate
from test_super_agent_sa13_api import browser as browser

from mgar_v3.super_agent.source_autonomy_api import PREFIX

ROUTES = [
    ("GET", "/capabilities"),
    ("GET", "/rules"),
    ("PUT", "/rules/{rule_id}"),
    ("GET", "/rules/{rule_id}"),
    ("POST", "/rules/{rule_id}/disable"),
    ("GET", "/claims"),
    ("GET", "/attempts"),
    ("GET", "/attempts/{attempt_id}"),
    ("POST", "/attempts/{attempt_id}/stop"),
    ("POST", "/attempts/{attempt_id}/reopen"),
    ("POST", "/maintain"),
    ("POST", "/tick"),
    ("POST", "/cycle"),
]


def test_actual_original_app_exact_thirteen_routes(browser):
    actual = {
        (method, route.path)
        for route in browser.app.routes
        if hasattr(route, "methods")
        for method in route.methods
        if route.path.startswith(PREFIX + "/")
    }
    assert actual == {(method, PREFIX + route) for method, route in ROUTES}
    assert browser.engine.source_autonomy.engine is browser.engine
    assert browser.engine.source_autonomy._profiles == {}


@pytest.mark.parametrize("method,route", ROUTES)
def test_operator_or_bearer_never_substitutes_for_current_owner(browser, method, route):
    browser.client.headers.update(
        {
            "X-MGAR-Token": browser.engine.settings.local_token.get_secret_value(),
            "Authorization": "Bearer synthetic-unprivileged-worker",
        }
    )
    route = route.replace("{rule_id}", str(uuid4())).replace("{attempt_id}", str(uuid4()))
    response = browser.client.request(
        method, PREFIX + route, **({"json": {}} if method != "GET" else {})
    )
    assert response.status_code == 401, response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("route", ["/capabilities", "/rules", "/claims", "/attempts"])
def test_owner_metadata_is_bodyless_current_and_inert(browser, route):
    authenticate(browser)
    before = original_state(browser.engine)
    response = browser.client.get(PREFIX + route)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert original_state(browser.engine) == before
    assert "source-autonomy" in response.text or "partial" in response.json()


@pytest.mark.parametrize("route", ["/maintain", "/tick", "/cycle"])
def test_empty_owner_work_cycles_make_no_model_task_or_reply(browser, route):
    authenticate(browser)
    before = original_state(browser.engine)
    response = browser.client.post(PREFIX + route, json={"limit": 16})
    assert response.status_code == 200, response.text
    assert response.json()["model_calls"] == 0
    assert not response.json()["notification_sent"]
    assert original_state(browser.engine) == before


@pytest.mark.parametrize("fault", ["origin", "csrf", "foreign_origin"])
def test_owner_mutation_requires_original_origin_and_csrf(browser, fault):
    authenticate(browser)
    if fault == "origin":
        del browser.client.headers["Origin"]
    elif fault == "csrf":
        del browser.client.headers["X-CSRF-Token"]
    else:
        browser.client.headers["Origin"] = "https://foreign.invalid"
    response = browser.client.post(PREFIX + "/cycle", json={})
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"limit": True},
        {"limit": 0},
        {"limit": 65},
        {"runtime": "live"},
        {"provider": "arbitrary"},
        {"factory": {}},
        {"bearer": "forged"},
        {"facts": {"authority": True}},
        {"success": True},
        {"limit": "1"},
    ],
)
def test_http_cannot_inject_authority_executor_success_or_unbounded_work(browser, payload):
    authenticate(browser)
    before = original_state(browser.engine)
    response = browser.client.post(PREFIX + "/cycle", json=payload)
    assert response.status_code == 422, response.text
    assert original_state(browser.engine) == before


@pytest.mark.parametrize("kind", ["large", "media", "malformed"])
def test_bounded_json_rejects_invalid_transport_without_original_side_effect(browser, kind):
    authenticate(browser)
    headers = {"content-type": "text/plain" if kind == "media" else "application/json"}
    content = b"{" if kind == "malformed" else (b"x" * 17000 if kind == "large" else b"{}")
    before = original_state(browser.engine)
    response = browser.client.post(PREFIX + "/cycle", content=content, headers=headers)
    assert response.status_code == 422, response.text
    assert original_state(browser.engine) == before
