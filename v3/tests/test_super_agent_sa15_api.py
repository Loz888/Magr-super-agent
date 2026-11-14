"""Formal original-app Recall routes and independent Owner/Worker boundaries."""

import json
from uuid import uuid4

import pytest
from sa15_support import path, setup
from test_super_agent_sa13_api import authenticate
from test_super_agent_sa13_api import browser as browser

from mgar_v3.super_agent.source_recall_api import PREFIX
from mgar_v3.web_access import SESSION_COOKIE

OWNER = [
    ("GET", "/capabilities"),
    *[("POST", "/" + op) for op in ("resolve", "walk", "page", "search")],
]
WORKER = [("POST", "/worker/" + op) for op in ("resolve", "walk", "page", "search")]


def body(env, route):
    result = (
        {"query": "登录"}
        if route.endswith("/search")
        else {
            "path": path(env.source_ref).model_dump(mode="json"),
        }
    )
    if "/worker/" in route:
        result["worker"] = env.run_request.model_dump(mode="json")
    return result


def test_original_engine_and_exact_nine_routes_with_no_new_migration(browser):
    actual = {
        (method, route.path)
        for route in browser.app.routes
        if hasattr(route, "methods")
        for method in route.methods
        if route.path.startswith(PREFIX + "/")
    }
    assert actual == {(method, PREFIX + route) for method, route in OWNER + WORKER}
    assert browser.engine.source_recall.engine is browser.engine
    assert browser.engine.source_coding.runtime is None
    assert browser.engine.source_async.runtime is None
    from pathlib import Path

    sql = list(Path("migrations").glob("*.sql"))
    assert len(sql) == 44
    assert not list(Path("migrations").glob("0048*"))


@pytest.mark.parametrize("method,route", OWNER)
def test_owner_requires_cookie_and_cannot_use_worker_or_operator(browser, tmp_path, method, route):
    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    browser.client.headers.update(
        {
            "Authorization": "Bearer " + env.worker["bearer"],
            "X-MGAR-Token": browser.engine.settings.local_token.get_secret_value(),
        }
    )
    value = browser.client.request(
        method, PREFIX + route, **({"json": body(env, route)} if method == "POST" else {})
    )
    assert value.status_code == 401, value.text
    assert value.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("fault", ["origin", "csrf", "foreign_origin"])
def test_owner_post_reads_require_original_origin_csrf(browser, tmp_path, fault):
    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    authenticate(browser)
    if fault == "origin":
        del browser.client.headers["Origin"]
    elif fault == "csrf":
        del browser.client.headers["X-CSRF-Token"]
    else:
        browser.client.headers["Origin"] = "https://foreign.invalid"
    value = browser.client.post(PREFIX + "/resolve", json=body(env, "/resolve"))
    assert value.status_code == 403, value.text
    assert value.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,route", OWNER)
def test_authenticated_original_owner_read_routes(browser, tmp_path, method, route):
    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    authenticate(browser)
    value = browser.client.request(
        method, PREFIX + route, **({"json": body(env, route)} if method == "POST" else {})
    )
    assert value.status_code == 200, value.text
    assert value.json()["read_only"] is True
    assert value.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,route", WORKER)
def test_worker_cookie_does_not_substitute_for_independent_bearer(browser, tmp_path, method, route):
    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    authenticate(browser)
    value = browser.client.post(PREFIX + route, json=body(env, route))
    assert value.status_code == 401, value.text
    assert value.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,route", WORKER)
def test_current_worker_can_read_original_paths_without_owner_cookie(
    browser, tmp_path, method, route
):
    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    browser.client.headers["Authorization"] = "Bearer " + env.worker["bearer"]
    value = browser.client.post(PREFIX + route, json=body(env, route))
    assert value.status_code == 200, value.text
    assert value.json()["read_only"] is True
    assert value.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "field",
    [
        "actor",
        "trusted",
        "provider",
        "token",
        "grant",
        "paid",
        "host_path",
        "url",
        "body",
        "task_succeeded",
    ],
)
def test_caller_authority_and_runtime_fields_are_rejected(browser, tmp_path, field):
    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    authenticate(browser)
    command = body(env, "/resolve")
    command[field] = str(uuid4())
    value = browser.client.post(PREFIX + "/resolve", json=command)
    assert value.status_code == 422, value.text
    assert value.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "payload,content_type",
    [
        ("null", "application/json"),
        ("[1]", "application/json"),
        ("{}", "text/plain"),
        ("x" * 17000, "application/json"),
    ],
)
def test_malformed_and_oversize_transport_closed(browser, payload, content_type):
    authenticate(browser)
    value = browser.client.post(
        PREFIX + "/resolve", content=payload, headers={"Content-Type": content_type}
    )
    assert value.status_code == 422, value.text
    assert value.headers["cache-control"] == "no-store"


def test_logout_between_owner_pages_does_not_restore_grant(browser, tmp_path):
    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    authenticate(browser)
    command = body(env, "/page") | {"max_bytes": 128}
    first = browser.client.post(PREFIX + "/page", json=command)
    assert first.status_code == 200, first.text
    with browser.engine.transaction() as conn:
        conn.execute("DELETE FROM app.web_sessions")
    value = browser.client.post(PREFIX + "/page", json=command | {"cursor": first.json()["cursor"]})
    assert value.status_code == 401, value.text
    assert env.worker["bearer"] not in json.dumps(value.json())
    assert SESSION_COOKIE
