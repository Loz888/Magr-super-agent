"""Formal Source-native delivery and consumption on the real original Engine."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sa05_support import payload, signed
from sa11_support import TABLES, ack_request, business, deliveries, followup, prepare, setup

from mgar_v3.domain import DomainError
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.super_agent.fast_path_domain import FastPathConfigure, FastPathSettings
from mgar_v3.super_agent.source_subscription_domain import (
    SourceIntakeSnapshot,
    SubscriptionCloseRequest,
    SubscriptionDispatchRequest,
    SubscriptionHandbackRequest,
    SubscriptionPrepareRequest,
)


def parsed(page):
    value = page.get("snapshot", page) if isinstance(page, dict) else page
    return (
        value
        if isinstance(value, SourceIntakeSnapshot)
        else (SourceIntakeSnapshot.model_validate_json(json.dumps(value, default=str)))
    )


def close_request(env):
    view = env.service.get(env.cookie, env.subscription_id)
    return SubscriptionCloseRequest(
        expected_revision=view["revision"],
        snapshot_sha256=view["snapshot_sha256"],
        review_ref="SA11-explicit-owner-close",
    )


def source_counts(env):
    with env.engine.transaction() as conn:
        return {
            t: conn.execute("SELECT count(*) AS n FROM app." + t).fetchone()["n"] for t in TABLES
        }


def test_formal_ingress_delivers_existing_run_without_business_or_plaintext_copy(engine):
    env = setup(engine)
    before = business(env)
    secret = "/mgar investigate SA11-UNIQUE-PRIVATE-NATIVE-CONTEXT"
    input_id = followup(env, secret)
    rows = deliveries(env)
    assert len(rows) == 1 and rows[0]["input_id"] == input_id
    assert rows[0]["run_id"] == env.run_id
    assert business(env) == before
    with engine.transaction() as conn:
        for table in TABLES:
            raw = conn.execute(
                "SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb) AS v FROM app." + table + " t"
            ).fetchone()["v"]
            assert secret not in json.dumps(raw, default=str)


@pytest.mark.parametrize("role", ["primary", "context_only", "observer"])
def test_independent_lanes_are_bounded_ref_only_and_cas_consumed(engine, role):
    env = setup(engine, role=role)
    ids = [followup(env, f"/mgar investigate SA11-LANE-{n}") for n in range(3)]
    page = parsed(prepare(env, limit=2))
    assert len(page.items) == 2 and page.lane == role
    assert [x.input.input_id for x in page.items] == ids[:2]
    assert all(x.lane == role for x in page.items)
    before = business(env)
    request = ack_request(page)
    env.service.acknowledge(env.cookie, env.run_id, request)
    assert business(env) == before
    tail = parsed(prepare(env, cursor=page.through_cursor, limit=2))
    assert [x.input.input_id for x in tail.items] == ids[2:]
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.sa_source_subscription_consumptions"
            ).fetchone()["n"]
            == 1
        )


def test_prepare_and_ack_idempotently_resume_same_durable_page(engine):
    env = setup(engine)
    followup(env)
    page = parsed(prepare(env))
    again = parsed(prepare(env))
    assert page.snapshot_id == again.snapshot_id
    request = ack_request(page)
    one = env.service.acknowledge(env.cookie, env.run_id, request)
    two = env.service.acknowledge(env.cookie, env.run_id, request)
    assert one == two
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.sa_source_subscription_consumptions"
            ).fetchone()["n"]
            == 1
        )


def test_stale_epoch_page_cannot_consume_and_fresh_epoch_can_prepare(engine):
    env = setup(engine)
    followup(env)
    old = parsed(prepare(env))
    started = engine.begin_run(env.run_id)
    assert started["execution_epoch"] > old.epoch
    before = source_counts(env)
    with pytest.raises(DomainError):
        env.service.acknowledge(env.cookie, env.run_id, ack_request(old))
    assert source_counts(env) == before
    fresh = parsed(prepare(env))
    assert fresh.epoch == started["execution_epoch"]
    env.service.acknowledge(env.cookie, env.run_id, ack_request(fresh))


def test_unsubscribe_fences_prepared_page_and_keeps_closed_followup_off_run(engine):
    env = setup(engine)
    followup(env)
    page = parsed(prepare(env))
    env.service.close(env.cookie, env.subscription_id, close_request(env))
    before = source_counts(env)
    with pytest.raises(DomainError):
        env.service.acknowledge(env.cookie, env.run_id, ack_request(page))
    assert source_counts(env) == before
    original = business(env)
    followup(env, "/mgar status SA11-original-existing-task")
    assert len(deliveries(env)) == 1
    assert business(env) == original
    assert env.service.get(env.cookie, env.subscription_id)["state"] == "unsubscribed"


def test_current_identity_withdrawal_invalidates_ready_snapshot(engine):
    env = setup(engine)
    followup(env)
    page = parsed(prepare(env))
    origin = env.source.sourceenv
    origin.identities.roles(
        env.cookie,
        origin.binding["binding_id"],
        RoleChange(
            expected_revision=origin.binding["revision"],
            roles=("context_provider",),
            review_ref="SA11-withdraw-controller-after-prepare",
        ),
    )
    before = source_counts(env)
    with pytest.raises(DomainError):
        env.service.acknowledge(env.cookie, env.run_id, ack_request(page))
    assert source_counts(env) == before


def test_expired_source_cannot_materialize_or_ack_old_page_but_owner_can_close(engine):
    env = setup(engine)
    input_id = followup(env)
    page = parsed(prepare(env))
    with engine.transaction() as conn:
        conn.execute("SELECT app.expire_source_input(%s,'INGRESS_BODY_EXPIRED')", (input_id,))
    before = source_counts(env)
    with pytest.raises(DomainError):
        env.service.materialize(env.cookie, env.run_id, ack_request(page))
    with pytest.raises(DomainError):
        env.service.acknowledge(env.cookie, env.run_id, ack_request(page))
    assert source_counts(env) == before
    env.service.close(env.cookie, env.subscription_id, close_request(env))
    assert env.service.get(env.cookie, env.subscription_id)["state"] == "unsubscribed"


def test_blocked_metadata_can_be_drained_without_materializing_expired_text(engine, monkeypatch):
    import sa10_support

    original_setup = sa10_support.github_setup

    def model_read_source(engine, *, receive):
        # Grant this synthetic positive read through the normal owner policy setup.
        return original_setup(engine, receive={**receive, "model_input": True})

    monkeypatch.setattr(sa10_support, "github_setup", model_read_source)
    env = setup(engine, role="context_only")
    first = followup(env, "/mgar investigate SA11-EXPIRED-PRIVATE")
    with engine.transaction() as conn:
        conn.execute("SELECT app.expire_source_input(%s,'INGRESS_BODY_EXPIRED')", (first,))
    second = followup(env, "/mgar investigate SA11-NEXT-VALID")
    page = parsed(prepare(env))
    assert [x.input.input_id for x in page.items] == [first, second]
    assert page.items[0].disposition == "blocked"
    assert page.items[1].disposition == "ready"
    material = env.service.materialize(env.cookie, env.run_id, ack_request(page))
    assert all(x.input_id != first for x in material.items)
    assert [x.input_id for x in material.items] == [second]
    assert material.items[0].text == "/mgar investigate SA11-NEXT-VALID"
    assert "SA11-EXPIRED-PRIVATE" not in repr(material)
    env.service.acknowledge(env.cookie, env.run_id, ack_request(page))


def test_observer_never_materializes_worker_context(engine):
    env = setup(engine, role="observer")
    followup(env)
    page = parsed(prepare(env))
    with pytest.raises(DomainError):
        env.service.materialize(env.cookie, env.run_id, ack_request(page))


def test_wrong_resource_number_and_other_problem_do_not_join_subscription(engine):
    env = setup(engine)
    before = source_counts(env)
    followup(env, number=8)
    assert source_counts(env) == before
    assert deliveries(env) == []


def test_canonical_redelivery_never_queues_twice(engine):
    env = setup(engine)
    raw = payload("issue_comment")
    delivery = "SA11-same-authenticated-delivery"
    first = followup(env, raw=raw, delivery=delivery)
    origin = env.source.sourceenv
    second = origin.runtime.receive(
        signed(raw, family="issue_comment", delivery=delivery),
        origin.generation,
    ).input_ids[0]
    assert first == second
    assert len(deliveries(env)) == 1
    before = source_counts(env)
    env.service.dispatch(env.cookie, SubscriptionDispatchRequest(input_ids=(first,)))
    assert source_counts(env) == before


def test_parallel_dispatch_and_ack_have_single_committed_fact(engine):
    env = setup(engine)
    input_id = followup(env)
    request = SubscriptionDispatchRequest(input_ids=(input_id,))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: env.service.dispatch(env.cookie, request), range(2)))
    assert len(results) == 2 and len(deliveries(env)) == 1
    page = parsed(prepare(env))
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(
                lambda _: env.service.acknowledge(env.cookie, env.run_id, ack_request(page)),
                range(2),
            )
        )
    assert receipts[0] == receipts[1]
    assert source_counts(env)["sa_source_subscription_consumptions"] == 1


def test_outer_transaction_rollback_removes_uncommitted_delivery(engine):
    env = setup(engine)
    before = source_counts(env)
    with pytest.raises(RuntimeError, match="ROLLBACK"):
        with engine.transaction():
            # The public ingress correctly refuses nested non-durable ACK. Exercise
            # the original service transaction with an existing canonical input.
            env.service.dispatch(
                env.cookie,
                SubscriptionDispatchRequest(
                    input_ids=(env.native_input_id,),
                ),
            )
            raise RuntimeError("ROLLBACK")
    assert source_counts(env) == before


def test_terminal_run_followup_does_not_reopen_or_create_attempt(engine):
    env = setup(engine)
    engine.fail_run(env.run_id, "SA11-controlled-terminal")
    original = business(env)
    followup(env, "/mgar status SA11-terminal-task")
    assert business(env) == original
    assert deliveries(env) == []
    with engine.transaction() as conn:
        outcome = conn.execute("SELECT outcome FROM app.sa_source_subscription_routes").fetchone()
        assert outcome["outcome"] == "closed_followup"


def test_subscribed_followup_has_no_semantic_candidate_or_router_cost(engine):
    env = setup(engine)
    engine.fast_path.configure(
        env.cookie,
        env.source_id,
        FastPathConfigure(
            expected_revision=0,
            settings=FastPathSettings(enabled=True, cooldown_seconds=0),
            review_ref="SA11-current-deterministic-subscription-bypass",
        ),
    )
    input_id = followup(env)
    with engine.transaction() as conn:
        d = conn.execute(
            "SELECT disposition,reason_code,batch_id FROM "
            "app.sa_fast_path_decisions WHERE input_id=%s",
            (input_id,),
        ).fetchone()
        assert d["disposition"] == "context"
        assert d["reason_code"] == "FAST_PATH_SUBSCRIPTION_ROUTED"
        assert d["batch_id"] is None
        assert conn.execute("SELECT count(*) AS n FROM app.router_calls").fetchone()["n"] == 0
        assert (
            conn.execute("SELECT count(*) AS n FROM app.sa_source_eval_snapshots").fetchone()["n"]
            == 0
        )


def test_handback_is_exactly_once_ref_only_and_never_reposts_model(engine):
    env = setup(engine)
    input_id = followup(env)
    view = env.service.get(env.cookie, env.subscription_id)
    req = SubscriptionHandbackRequest(
        expected_revision=view["revision"],
        snapshot_sha256=view["snapshot_sha256"],
        review_ref="SA11-reviewed-handback",
        input_ids=(input_id,),
        reason_code="NEEDS_ROUTING",
    )
    before = business(env)
    env.service.handback(env.cookie, env.subscription_id, req)
    assert business(env) == before
    with pytest.raises(DomainError):
        env.service.handback(env.cookie, env.subscription_id, req)
    assert source_counts(env)["sa_source_subscription_handbacks"] == 1
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT outcome,payload FROM app.sa_source_subscription_handbacks"
        ).fetchone()
        assert row["outcome"] == "needs_routing"
        assert conn.execute("SELECT count(*) AS n FROM app.router_calls").fetchone()["n"] == 0


def test_cursor_and_project_cannot_be_supplied_as_authority(engine):
    env = setup(engine)
    followup(env)
    before = source_counts(env)
    with pytest.raises(DomainError):
        env.service.prepare(
            env.cookie,
            env.run_id,
            SubscriptionPrepareRequest(
                epoch=0,
                expected_cursor=123456,
                lane="primary",
            ),
        )
    with pytest.raises(DomainError):
        env.service.prepare(
            env.cookie,
            uuid4(),
            SubscriptionPrepareRequest(
                epoch=0,
                expected_cursor=0,
                lane="primary",
            ),
        )
    assert source_counts(env) == before
