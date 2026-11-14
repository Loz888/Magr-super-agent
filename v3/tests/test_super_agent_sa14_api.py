"""First formal twelve-route HTTP integration and principal separation."""

from uuid import uuid4

import pytest
from sa14_support import setup, worker_request
from test_super_agent_sa13_api import authenticate
from test_super_agent_sa13_api import browser as browser

from mgar_v3.super_agent.source_async_domain import (
    AsyncJobCancelRequest,
    AsyncRestriction,
    AsyncRestrictRequest,
)

PREFIX = "/api/v3/source-async"
ROUTES = [
    ("GET", "/capabilities"),
    ("POST", "/admissions"),
    ("GET", "/bindings/{coding_id}"),
    ("POST", "/bindings/{coding_id}/restrictions"),
    ("GET", "/bindings/{coding_id}/inputs"),
    ("GET", "/bindings/{coding_id}/jobs"),
    ("GET", "/bindings/{coding_id}/events"),
    ("POST", "/bindings/{coding_id}/jobs/{job_id}/cancel"),
    ("POST", "/worker/{coding_id}/start"),
    ("GET", "/worker/{coding_id}/state"),
    ("GET", "/worker/{coding_id}/jobs/{job_id}"),
    ("POST", "/worker/{coding_id}/jobs/{job_id}/cancel"),
]


def path(route, coding_id, job_id=None):
    return PREFIX + route.format(coding_id=coding_id, job_id=job_id or uuid4())


def request_payload(route, env):
    if route == "/admissions":
        return env.async_admit.model_dump(mode="json")
    if route.endswith("/restrictions"):
        current = env.async_service.get(env.cookie, env.mandate.coding_id)
        return AsyncRestrictRequest(
            request_id=uuid4(),
            coding_id=env.mandate.coding_id,
            run_id=env.target.run_id,
            expected_epoch=env.run_request.epoch,
            expected_revision=current["revision"],
            restriction=AsyncRestriction(deny_write_paths=("src/calc.py",)),
            review_ref="SA14-HTTP-owner-deny",
        ).model_dump(mode="json")
    result = worker_request(env).model_dump(mode="json")
    if route.endswith("/cancel"):
        if route.startswith("/bindings"):
            return AsyncJobCancelRequest(
                request_id=uuid4(),
                coding_id=env.mandate.coding_id,
                run_id=env.target.run_id,
                job_id=uuid4(),
                expected_revision=1,
                review_ref="SA14-HTTP-stop",
            ).model_dump(mode="json")
        result["job_id"] = str(uuid4())
    return result


def test_original_app_registers_exact_twelve_agreed_async_routes(browser):
    actual = {
        (method, route.path)
        for route in browser.app.routes
        if hasattr(route, "methods")
        for method in route.methods
        if route.path.startswith(PREFIX + "/")
    }
    assert actual == {(method, PREFIX + route) for method, route in ROUTES}
    assert browser.engine.source_async.runtime is None


@pytest.mark.parametrize("method,route", ROUTES[:8])
def test_owner_routes_never_accept_worker_or_operator_as_owner(browser, method, route, tmp_path):
    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    browser.client.headers.update(
        {
            "Authorization": "Bearer " + env.worker["bearer"],
            "X-MGAR-Token": browser.engine.settings.local_token.get_secret_value(),
        }
    )
    response = browser.client.request(
        method,
        path(route, env.mandate.coding_id),
        **({"json": request_payload(route, env)} if method == "POST" else {}),
    )
    assert response.status_code == 401, (route, response.text)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("fault", ["origin", "csrf", "cross_origin"])
def test_owner_mutations_require_current_origin_and_csrf(browser, tmp_path, fault):
    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    authenticate(browser)
    if fault == "origin":
        del browser.client.headers["Origin"]
    if fault == "csrf":
        del browser.client.headers["X-CSRF-Token"]
    if fault == "cross_origin":
        browser.client.headers["Origin"] = "https://foreign.invalid"
    response = browser.client.post(
        path("/bindings/{coding_id}/restrictions", env.mandate.coding_id),
        json=request_payload("/bindings/{coding_id}/restrictions", env),
    )
    assert response.status_code == 403, response.text
    assert env.async_service.inputs(env.cookie, env.mandate.coding_id)["inputs"] == []


@pytest.mark.parametrize(
    "field",
    [
        "body",
        "argv",
        "provider",
        "token",
        "paid",
        "trusted",
        "task_succeeded",
        "host_path",
        "grant",
    ],
)
def test_authenticated_owner_cannot_submit_runtime_or_authority_assertions(
    browser, tmp_path, field
):
    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    authenticate(browser)
    body = request_payload("/bindings/{coding_id}/restrictions", env)
    body[field] = None
    response = browser.client.post(
        path("/bindings/{coding_id}/restrictions", env.mandate.coding_id), json=body
    )
    assert response.status_code == 422, response.text
    assert env.async_service.inputs(env.cookie, env.mandate.coding_id)["inputs"] == []


def test_owner_receives_received_applied_blocked_metadata_without_source_body_or_worker_secret(
    browser, tmp_path
):
    from sa14_support import followup, point

    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    authenticate(browser)
    response = browser.client.post(
        path("/bindings/{coding_id}/restrictions", env.mandate.coding_id),
        json=request_payload("/bindings/{coding_id}/restrictions", env),
    )
    assert response.status_code == 200 and response.json()["state"] == "received", response.text
    followup(env, body="不要改数据库", sender="Alice-AbC")
    point(env)
    for method, route in ROUTES[:8]:
        if method != "GET":
            continue
        value = browser.client.get(path(route, env.mandate.coding_id))
        assert value.status_code == 200, (route, value.text)
        assert value.headers["cache-control"] == "no-store"
        for secret in [
            env.cookie,
            env.worker["bearer"],
            "SA13 synthetic private repair request",
            "ciphertext",
        ]:
            assert secret not in value.text
    result = browser.client.get(path("/bindings/{coding_id}/inputs", env.mandate.coding_id)).json()
    assert {item["state"] for item in result["inputs"]} == {"applied", "blocked"}


def test_worker_uses_own_bearer_and_exact_original_assignment_after_owner_signout(
    browser, tmp_path
):
    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    query = {
        key: str(getattr(env.run_request, key))
        for key in ["run_id", "assignment_id", "session_id", "epoch"]
    }
    route = path("/worker/{coding_id}/state", env.mandate.coding_id)
    authenticate(browser)
    assert browser.client.get(route, params=query).status_code == 401
    browser.client.cookies.clear()
    browser.client.headers.clear()
    browser.client.headers["Authorization"] = "Bearer " + env.worker["bearer"]
    result = browser.client.get(route, params=query)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    query["assignment_id"] = str(uuid4())
    assert browser.client.get(route, params=query).status_code == 403


@pytest.mark.parametrize("query", [{"limit": 65}, {"after_sequence": -1}])
def test_metadata_pagination_is_bounded(browser, tmp_path, query):
    env = setup(browser.engine, tmp_path, cookie=browser.cookie)
    authenticate(browser)
    response = browser.client.get(
        path("/bindings/{coding_id}/inputs", env.mandate.coding_id), params=query
    )
    assert response.status_code == 422
