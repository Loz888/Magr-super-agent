"""SA11 formal owner HTTP routes on real PG, with source references only."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sa08_support import original_state
from sa10_support import link, link_id, resolved
from sa10_support import setup as native_setup
from sa11_support import followup, install_test_schema

from mgar_v3 import api
from mgar_v3.super_agent import source_subscription
from mgar_v3.super_agent.policy_domain import PolicyRevoke
from mgar_v3.super_agent.source_subscription_domain import (
    SubscriptionDispatchRequest,
    SubscriptionSaveRequest,
)
from mgar_v3.web_access import SESSION_COOKIE, WebAccounts

ORIGIN = "http://127.0.0.1:18080"
PREFIX = "/api/v3/source-subscriptions"
PRIVATE = "SA11-HTTP-PRIVATE-CURRENT-SOURCE"
MUTATIONS = ("create", "renew", "rebind", "unsubscribe", "handback", "dispatch", "prepare", "ack")
ACTIONS = (*MUTATIONS, "get", "subscriptions", "mailbox")
TABLES = (
    "sa_source_subscriptions", "sa_source_subscription_versions", "sa_source_subscription_routes",
    "sa_source_subscription_deliveries", "sa_source_subscription_intakes",
    "sa_source_subscription_consumptions", "sa_source_subscription_handbacks",
)


def formal_app(engine):
    """Use the original application's production registration without a test patch."""
    return api.create_app(engine.settings, engine, run_background=False)


@pytest.fixture
def browser(engine):
    install_test_schema(engine)
    engine.settings = engine.settings.model_copy(update={
        "allow_paid": False, "allow_external_write": False,
    })
    source = native_setup(engine)
    correlation = link(source)
    session = WebAccounts(engine).session(source.cookie)
    assert session is not None
    app = formal_app(engine)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        yield SimpleNamespace(
            engine=engine, source=source, client=client, app=app, cookie=source.cookie,
            csrf=session.csrf_token, owner=session.user_id,
            input_id=source.native_input_id, correlation_id=UUID(str(link_id(correlation))),
            subscription_id=uuid4(), run_id=source.target.run_id,
            source_id=source.sourceenv.source_id,
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


def save_payload(env, *, role="primary"):
    current = resolved(env.source)
    target = env.source.service.target(env.cookie, env.source.target.ticket_id)
    return {
        "correlation_id": str(env.correlation_id), "expected_correlation_revision": 1,
        "seed_input_id": str(env.input_id), "resource_key": env.source.resource_key,
        "target": target.model_dump(mode="json"), "role": role,
        "selector_kind": "resource", "ttl_seconds": 300,
        "snapshot_sha256": current["snapshot_sha256"], "expected_revision": 0,
        "review_ref": "SA11-HTTP-exact-existing-target",
    }


def payload(env, action):
    if action == "create":
        return save_payload(env)
    if action == "dispatch":
        return {"input_ids": [str(env.input_id)]}
    if action in {"prepare", "ack"}:
        value = {"epoch": 0, "expected_cursor": 0, "lane": "primary"}
        if action == "prepare":
            return {**value, "limit": 16, "max_bytes": 65536}
        return {**value, "snapshot_id": str(uuid4()), "snapshot_sha256": "a" * 64}
    value = {
        "expected_revision": 1, "snapshot_sha256": "a" * 64,
        "review_ref": "SA11-HTTP-reviewed-existing-subscription",
    }
    if action in {"renew", "rebind"}:
        value.update(seed_input_id=str(env.input_id), ttl_seconds=300)
    if action == "rebind":
        value["target"] = env.source.target.model_dump(mode="json")
    if action == "handback":
        value.update(input_ids=[str(env.input_id)], reason_code="NEEDS_ROUTING")
    return value


def path(env, action):
    if action == "create":
        return PREFIX + "/subscriptions"
    if action == "subscriptions":
        return PREFIX + f"/sources/{env.source_id}/subscriptions"
    if action == "dispatch":
        return PREFIX + "/dispatch"
    if action in {"prepare", "ack", "mailbox"}:
        return PREFIX + f"/runs/{env.run_id}/" + action
    value = PREFIX + f"/subscriptions/{env.subscription_id}"
    return value if action == "get" else value + "/" + action


def call(env, action):
    if action in {"get", "subscriptions", "mailbox"}:
        return env.client.get(path(env, action))
    return env.client.post(path(env, action), json=payload(env, action))


def assert_private_response(env, response):
    assert response.headers["cache-control"] == "no-store"
    for secret in (PRIVATE, "SA10_SYNTHETIC_PRIVATE_NATIVE_7", env.cookie, env.csrf, "ciphertext"):
        assert secret not in response.text


def test_formal_application_exposes_exact_eleven_bodyless_routes(browser):
    registered = [
        (method, route.path)
        for route in browser.app.routes if hasattr(route, "methods")
        for method in route.methods if getattr(route, "path", "").startswith(PREFIX + "/")
    ]
    routes = set(registered)
    assert len(registered) == 11
    assert routes == {
        ("POST", PREFIX + "/subscriptions"),
        ("POST", PREFIX + "/subscriptions/{subscription_id}/renew"),
        ("POST", PREFIX + "/subscriptions/{subscription_id}/rebind"),
        ("POST", PREFIX + "/subscriptions/{subscription_id}/unsubscribe"),
        ("POST", PREFIX + "/subscriptions/{subscription_id}/handback"),
        ("GET", PREFIX + "/subscriptions/{subscription_id}"),
        ("GET", PREFIX + "/sources/{source_id}/subscriptions"),
        ("POST", PREFIX + "/dispatch"),
        ("POST", PREFIX + "/runs/{run_id}/prepare"),
        ("POST", PREFIX + "/runs/{run_id}/ack"),
        ("GET", PREFIX + "/runs/{run_id}/mailbox"),
    }


def test_all_subscription_routes_require_cookie_even_with_operator_token(browser):
    env = browser
    before, business = persisted(env), original_state(env.engine)
    env.client.headers["X-MGAR-Token"] = env.engine.settings.local_token.get_secret_value()
    for action in ACTIONS:
        response = call(env, action)
        assert response.status_code == 401, (action, response.text)
        assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("action", MUTATIONS)
def test_subscription_mutations_require_origin_and_csrf(browser, action):
    env = browser
    before, business = persisted(env), original_state(env.engine)
    env.client.cookies.set(SESSION_COOKIE, env.cookie)
    response = call(env, action)
    assert response.status_code == 403
    assert_private_response(env, response)
    env.client.headers["Origin"] = ORIGIN
    response = call(env, action)
    assert response.status_code == 403
    assert_private_response(env, response)
    env.client.headers["X-CSRF-Token"] = env.csrf
    response = env.client.post(
        path(env, action), json=payload(env, action), headers={"Origin": "https://elsewhere.invalid"},
    )
    assert response.status_code == 403
    assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("action", ACTIONS)
def test_session_revocation_between_dependency_and_service_is_current(browser, monkeypatch, action):
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
    assert response.status_code == 401, response.text
    assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("action", MUTATIONS)
def test_http_cannot_supply_source_text_or_execution_authority(browser, action):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    for field, value in (
        ("body", PRIVATE), ("actor", "owner"), ("sender", {"actor_kind": "human"}),
        ("factory", "create_ticket"), ("allow_paid", True), ("model_input", True),
        ("eligible_for_execution", True), ("execution", {"repository": "other/repo"}),
        ("resource", {"repository_id": 1}),
    ):
        response = env.client.post(path(env, action), json={**payload(env, action), field: value})
        assert response.status_code == 422, (action, field, response.text)
        assert response.json()["code"] == "INVALID_INPUT"
        assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("action", ["create", "rebind"])
def test_target_json_preserves_exact_uuid_boolean_counter_and_scope(browser, action):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    for field, value in (
        ("allowed", "true"), ("allowed", 1), ("execution_epoch", True),
        ("thread_revision", 1.5), ("ticket_id", "not-a-uuid"), ("body", PRIVATE),
    ):
        command = payload(env, action)
        command["target"][field] = value
        response = env.client.post(path(env, action), json=command)
        assert response.status_code == 422, (action, field, response.text)
        assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


@pytest.mark.parametrize("action", ["dispatch", "handback"])
def test_dispatch_and_handback_require_distinct_bounded_existing_input_refs(browser, action):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    for values in ([], [str(env.input_id)] * 2, [str(uuid4()) for _ in range(17)],
                   ["not-a-uuid"], [True], "not-an-array"):
        response = env.client.post(
            path(env, action), json={**payload(env, action), "input_ids": values},
        )
        assert response.status_code == 422, response.text
        assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


def test_subscription_ttl_and_cas_values_are_strict_and_bounded(browser):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    for field, value in (
        ("ttl_seconds", 0), ("ttl_seconds", 604801), ("ttl_seconds", True),
        ("ttl_seconds", "300"), ("expected_revision", True),
        ("expected_correlation_revision", 0), ("expected_correlation_revision", "1"),
        ("seed_input_id", "not-a-uuid"), ("snapshot_sha256", "not-a-hash"),
        ("selector_kind", "conversation"),
    ):
        response = env.client.post(path(env, "create"), json={**save_payload(env), field: value})
        assert response.status_code == 422, (field, value, response.text)
        assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


def test_ref_only_mailbox_and_source_views_have_bounded_queries(browser):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    for limit in (0, 101, "1.5", "true"):
        response = env.client.get(path(env, "subscriptions"), params={"limit": limit})
        assert response.status_code == 422
        assert_private_response(env, response)
    for parameters in (
        {"limit": 0}, {"limit": 17}, {"cursor": -1}, {"cursor": "1.5"},
        {"cursor": "true"}, {"lane": "control"}, {"cursor": 9223372036854775808},
    ):
        response = env.client.get(path(env, "mailbox"), params=parameters)
        assert response.status_code == 422, response.text
        assert_private_response(env, response)
    for action in ("subscriptions", "mailbox"):
        response = call(env, action)
        assert response.status_code == 200, response.text
        assert_private_response(env, response)
    response = call(env, "get")
    assert response.status_code == 404
    assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


def test_valid_input_uuid_array_reaches_domain_and_existing_ref_service(browser, monkeypatch):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    observed = []
    original = source_subscription.SourceSubscriptions.dispatch

    def observe(service, cookie, command):
        assert type(command) is SubscriptionDispatchRequest
        assert type(command.input_ids) is tuple
        assert all(type(value) is UUID for value in command.input_ids)
        observed.append(command)
        return original(service, cookie, command)

    monkeypatch.setattr(source_subscription.SourceSubscriptions, "dispatch", observe)
    response = call(env, "dispatch")
    assert response.status_code == 200, response.text
    assert len(observed) == 1 and observed[0].input_ids == (env.input_id,)
    assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


def test_unknown_input_ref_cannot_be_dispatched_as_an_existing_canonical_input(browser):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    response = env.client.post(PREFIX + "/dispatch", json={"input_ids": [str(uuid4())]})
    assert response.status_code == 404, response.text
    assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


def test_private_cache_policy_does_not_change_neighbor_prefix_errors(browser):
    response = browser.client.get(PREFIX + "-other/not-a-subscription")
    assert response.status_code == 404
    assert "cache-control" not in response.headers


def test_http_has_no_source_body_materialization_or_execution_endpoint(browser):
    env = browser
    authenticate(env)
    before, business = persisted(env), original_state(env.engine)
    for suffix in ("materialize", "execute", "start"):
        response = env.client.post(
            PREFIX + f"/runs/{env.run_id}/" + suffix,
            json={"body": PRIVATE, "epoch": 0, "allow_paid": True},
        )
        assert response.status_code == 404
        assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business


def test_subscription_metadata_views_are_scoped_to_the_current_project(browser):
    env = browser
    authenticate(env)
    before = persisted(env)
    original = env.engine.settings
    try:
        env.engine.settings = original.model_copy(update={"project_id": "SA11-foreign-project"})
        for action in ("get", "subscriptions", "mailbox"):
            response = call(env, action)
            assert response.status_code == 404, (action, response.text)
            assert_private_response(env, response)
    finally:
        env.engine.settings = original
    assert persisted(env) == before


def test_http_create_renew_and_unsubscribe_use_current_refs_and_preserve_business(
    browser, monkeypatch,
):
    env = browser
    authenticate(env)
    business = original_state(env.engine)
    observed = []
    original = source_subscription.SourceSubscriptions.save

    def observe(service, cookie, identifier, command):
        assert type(identifier) is UUID and type(command) is SubscriptionSaveRequest
        assert type(command.seed_input_id) is UUID and type(command.target.run_id) is UUID
        observed.append(command)
        return original(service, cookie, identifier, command)

    monkeypatch.setattr(source_subscription.SourceSubscriptions, "save", observe)
    created = env.client.post(path(env, "create"), json=save_payload(env))
    assert created.status_code == 200, created.text
    assert len(observed) == 1
    env.subscription_id = UUID(created.json()["subscription_id"])
    initial = call(env, "get")
    assert initial.status_code == 200, initial.text
    assert initial.json()["revision"] == 1
    assert str(env.run_id) in initial.text
    listed = call(env, "subscriptions")
    assert listed.status_code == 200 and str(env.subscription_id) in listed.text

    renewal = payload(env, "renew")
    renewal["snapshot_sha256"] = resolved(env.source)["snapshot_sha256"]
    renewed = env.client.post(path(env, "renew"), json=renewal)
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["revision"] == 2
    after_renewal = persisted(env)
    stale = env.client.post(path(env, "renew"), json=renewal)
    assert stale.status_code == 409, stale.text
    assert persisted(env) == after_renewal

    current = call(env, "get")
    assert current.status_code == 200
    close = {
        "expected_revision": 2, "snapshot_sha256": current.json()["snapshot_sha256"],
        "review_ref": "SA11-HTTP-stop-current-head",
    }
    closed = env.client.post(path(env, "unsubscribe"), json=close)
    assert closed.status_code == 200, closed.text
    assert closed.json()["revision"] == 3
    final = call(env, "get")
    assert final.status_code == 200
    assert final.json()["state"] == "unsubscribed"
    for response in (created, initial, listed, renewed, stale, current, closed, final):
        assert_private_response(env, response)
    assert original_state(env.engine) == business
    assert not env.engine.settings.allow_paid and not env.engine.settings.allow_external_write


def create_subscription(env, *, role="context_only"):
    response = env.client.post(path(env, "create"), json=save_payload(env, role=role))
    assert response.status_code == 200, response.text
    assert_private_response(env, response)
    env.subscription_id = UUID(response.json()["subscription_id"])
    return response.json()


def prepare_page(env, epoch, *, cursor=0, limit=1):
    response = env.client.post(path(env, "prepare"), json={
        "epoch": epoch, "expected_cursor": cursor, "lane": "context_only",
        "limit": limit, "max_bytes": 65536,
    })
    assert response.status_code == 200, response.text
    assert_private_response(env, response)
    value = response.json()
    assert value["snapshot_id"] is not None and value["items"]
    return value


def ack_payload(page):
    snapshot = page["snapshot"]
    return {
        "epoch": snapshot["epoch"], "expected_cursor": snapshot["from_cursor"],
        "lane": snapshot["lane"], "snapshot_id": page["snapshot_id"],
        "snapshot_sha256": page["snapshot_sha256"],
    }


def test_formal_http_followups_prepare_and_ack_exact_bounded_source_pages(browser):
    env = browser
    authenticate(env)
    create_subscription(env)
    epoch = env.engine.begin_run(env.run_id)["execution_epoch"]
    first_input = followup(env, PRIVATE + "-first")
    second_input = followup(env, PRIVATE + "-second")
    business = original_state(env.engine)
    listed = env.client.get(path(env, "mailbox"), params={"lane": "context_only", "limit": 1})
    assert listed.status_code == 200, listed.text
    assert listed.json()["has_more"] is True and len(listed.json()["items"]) == 1
    assert_private_response(env, listed)

    first = prepare_page(env, epoch)
    assert len(first["items"]) == 1
    assert first["items"][0]["input"]["input_id"] == str(first_input)
    assert first["has_more"] is True
    acknowledged = env.client.post(path(env, "ack"), json=ack_payload(first))
    assert acknowledged.status_code == 200, acknowledged.text
    receipt = acknowledged.json()
    assert receipt["status"] == "acknowledged_refs"
    assert receipt["delivery_ids"] == [first["items"][0]["delivery_id"]]
    assert receipt["eligible_for_execution"] is False and receipt["calls_model"] is False
    assert receipt["external_writes"] == 0
    before_retry = persisted(env)
    retry = env.client.post(path(env, "ack"), json=ack_payload(first))
    assert retry.status_code == 200 and retry.json() == receipt
    assert persisted(env) == before_retry

    second = prepare_page(env, epoch, cursor=receipt["through_cursor"])
    assert len(second["items"]) == 1
    assert second["items"][0]["input"]["input_id"] == str(second_input)
    second_ack = env.client.post(path(env, "ack"), json=ack_payload(second))
    assert second_ack.status_code == 200, second_ack.text
    complete = env.client.get(path(env, "mailbox"), params={
        "lane": "context_only", "cursor": second_ack.json()["through_cursor"],
    })
    assert complete.status_code == 200 and complete.json()["items"] == []
    assert complete.json()["cursor"] == second_ack.json()["through_cursor"]
    for response in (acknowledged, retry, second_ack, complete):
        assert_private_response(env, response)
    assert original_state(env.engine) == business


def test_formal_http_old_epoch_ack_after_cancel_cannot_consume_or_reopen(browser):
    env = browser
    authenticate(env)
    create_subscription(env)
    epoch = env.engine.begin_run(env.run_id)["execution_epoch"]
    followup(env, PRIVATE + "-before-cancel")
    page = prepare_page(env, epoch)
    env.engine.cancel(env.run_id)
    before, business = persisted(env), original_state(env.engine)
    response = env.client.post(path(env, "ack"), json=ack_payload(page))
    assert response.status_code == 409, response.text
    assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business
    with env.engine.transaction() as conn:
        assert env.engine._run(conn, env.run_id)["status"] == "cancelled"
        assert conn.execute(
            "SELECT count(*) AS n FROM app.sa_source_subscription_consumptions"
        ).fetchone()["n"] == 0


def test_formal_http_withdrawal_blocks_old_ack_but_owner_can_close_metadata(browser):
    env = browser
    authenticate(env)
    create_subscription(env)
    epoch = env.engine.begin_run(env.run_id)["execution_epoch"]
    followup(env, PRIVATE + "-before-withdrawal")
    page = prepare_page(env, epoch)
    gh = env.source.sourceenv
    env.engine.source_policies.revoke(env.cookie, gh.policy_id, PolicyRevoke(
        expected_revision=gh.policy_revision, review_ref="SA11-HTTP-withdraw-before-ack",
    ))
    before, business = persisted(env), original_state(env.engine)
    response = env.client.post(path(env, "ack"), json=ack_payload(page))
    assert response.status_code in {403, 409}, response.text
    assert_private_response(env, response)
    assert persisted(env) == before and original_state(env.engine) == business
    current = call(env, "get")
    assert current.status_code == 200, current.text
    closed = env.client.post(path(env, "unsubscribe"), json={
        "expected_revision": current.json()["revision"],
        "snapshot_sha256": current.json()["snapshot_sha256"],
        "review_ref": "SA11-HTTP-owner-close-after-source-withdrawal",
    })
    assert closed.status_code == 200 and closed.json()["state"] == "unsubscribed"
    assert_private_response(env, closed)
    assert original_state(env.engine) == business
    with env.engine.transaction() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM app.sa_source_subscription_consumptions"
        ).fetchone()["n"] == 0


def test_formal_http_handback_records_once_without_model_or_new_task(browser):
    env = browser
    authenticate(env)
    create_subscription(env)
    input_id = followup(env, PRIVATE + "-handback")
    current = call(env, "get")
    assert current.status_code == 200
    command = {
        "expected_revision": current.json()["revision"],
        "snapshot_sha256": current.json()["snapshot_sha256"],
        "input_ids": [str(input_id)], "reason_code": "NEEDS_ROUTING",
        "review_ref": "SA11-HTTP-return-current-source-ref",
    }
    business = original_state(env.engine)
    response = env.client.post(path(env, "handback"), json=command)
    assert response.status_code == 200, response.text
    assert_private_response(env, response)
    current = call(env, "get")
    assert current.status_code == 200 and current.json()["state"] == "handed_back"
    with env.engine.transaction() as conn:
        rows = conn.execute(
            "SELECT input_id,outcome FROM app.sa_source_subscription_handbacks "
            "WHERE subscription_id=%s", (env.subscription_id,),
        ).fetchall()
    assert rows == [{"input_id": input_id, "outcome": "needs_routing"}]
    after = persisted(env)
    stale = env.client.post(path(env, "handback"), json=command)
    assert stale.status_code == 409, stale.text
    assert_private_response(env, stale)
    assert persisted(env) == after and original_state(env.engine) == business


def test_formal_http_rebind_selects_existing_new_run_without_moving_old_deliveries(browser):
    env = browser
    authenticate(env)
    create_subscription(env)
    followup(env, PRIVATE + "-old-run")
    with env.engine.transaction() as conn:
        old_rows = conn.execute(
            "SELECT delivery_id,run_id FROM app.sa_source_subscription_deliveries "
            "WHERE subscription_id=%s", (env.subscription_id,),
        ).fetchall()
    assert old_rows and all(row["run_id"] == env.run_id for row in old_rows)
    env.engine.begin_run(env.run_id)
    env.engine.fail_run(env.run_id, "GRAPH_VERSION_UNSUPPORTED")
    replacement = env.engine.resume(env.run_id)
    assert replacement != env.run_id
    target = env.source.service.target(env.cookie, env.source.target.ticket_id)
    assert target.run_id == replacement
    business = original_state(env.engine)
    command = {
        "expected_revision": 1, "seed_input_id": str(env.input_id), "ttl_seconds": 300,
        "snapshot_sha256": resolved(env.source)["snapshot_sha256"],
        "target": target.model_dump(mode="json"),
        "review_ref": "SA11-HTTP-reviewed-existing-replacement",
    }
    response = env.client.post(path(env, "rebind"), json=command)
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 2 and str(replacement) in response.text
    assert_private_response(env, response)
    assert original_state(env.engine) == business
    with env.engine.transaction() as conn:
        assert conn.execute(
            "SELECT delivery_id,run_id FROM app.sa_source_subscription_deliveries "
            "WHERE subscription_id=%s", (env.subscription_id,),
        ).fetchall() == old_rows
