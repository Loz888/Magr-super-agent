"""Source evaluation against real disposable PG; all providers/platforms synthetic."""

import json
from datetime import timedelta

import httpx
import pytest
from router_helpers import setup_router
from sa07_support import definition
from sa07_support import save as budget_save
from sa08_support import configure_fast_path, original_state, policy
from sa09_support import (
    apply,
    configure_eval,
    enqueue,
    fast_batch,
    ingest_case,
    prepared,
    proposals,
    setup,
)
from test_super_agent_sa08 import controller

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_eval import SourceEvalProvider
from mgar_v3.super_agent.source_eval_domain import SourceEvalProfile
from mgar_v3.super_agent.source_eval_model import SourceEvalModel


@pytest.mark.parametrize("action", ["ignore", "observe", "create", "ask"])
def test_fixture_decisions_are_current_bodyless_and_have_no_business_effects(engine, action):
    env = setup(engine)
    before = original_state(engine)
    batch, decisions = apply(env, action=action)
    assert original_state(engine) == before
    assert len(decisions) == 1 and decisions[0].eligible_for_execution is False
    assert decisions[0].intended_action == "would_" + action
    assert env.service.prepare() is None
    assert env.service.commit(batch["batch_id"], decisions) == [
        decisions[0].model_dump(mode="json")
    ]
    with engine.transaction() as conn:
        result = conn.execute(
            "SELECT * FROM app.sa_source_eval_results WHERE batch_id=%s", (batch["batch_id"],),
        ).fetchone()
        assert result["call_id"] is None and result["usage_known"] is False
        assert conn.execute("SELECT count(*) AS n FROM app.router_calls").fetchone()["n"] == 0
    assert env.service.preview(env.cookie, env.installation.scope.source_instance_id)[
        "evaluations"
    ][0]["current"]


def test_precise_current_controller_target_is_only_hypothetical_attachment(engine, trigger):
    env = setup(engine)
    task = controller(env, trigger)
    before = original_state(engine)
    batch = prepared(env)
    targets = batch["source_snapshot"]["groups"][0]["targets"]
    assert len(targets) == 1 and targets[0]["ticket_id"] == str(task.ticket_id)
    _, decisions = apply(env, batch, action="attach", target=task.ticket_id)
    assert decisions[0].target.ticket_id == task.ticket_id
    assert original_state(engine) == before


@pytest.mark.parametrize("change", [
    "eval-disable", "fast-disable", "policy", "budget", "member", "delete", "expiry",
])
def test_each_current_authority_or_member_change_rejects_original_snapshot(
    engine, monkeypatch, change,
):
    env = setup(engine)
    configure_fast_path(env, env.fast_settings.model_copy(update={"merge_window_seconds": 300}))
    receipt = ingest_case(env, topic="topic-source-eval")
    batch = prepared(env, receipt)
    proposed = proposals(batch)
    if change == "eval-disable":
        configure_eval(env, env.eval_settings.model_copy(update={"enabled": False}))
    elif change == "fast-disable":
        configure_fast_path(env, env.fast_settings.model_copy(update={"enabled": False}))
    elif change == "policy":
        policy(env)
    elif change == "budget":
        budget_save(env.budget, "project", engine.settings.project_id, definition(
            organization_id="sa07-controlled-organization", quotas={"candidates_hour": 99},
        ), revision=1)
    elif change == "member":
        added = ingest_case(env, entity="second", delivery="second", topic="topic-source-eval")
        assert fast_batch(env, receipt) == fast_batch(env, added)
    elif change == "delete":
        ingest_case(env, delivery="delete", event="delete", revision="2", order=2,
                    operation="delete", body=None, topic="topic-source-eval")
    else:
        monkeypatch.setattr(
            env.service, "now", lambda conn: batch["expires_at"]+timedelta(seconds=1)
        )
    before = original_state(engine)
    with pytest.raises(DomainError):
        env.service.wrap(batch, proposed)
    assert original_state(engine) == before
    view = env.service.preview(env.cookie, env.installation.scope.source_instance_id)
    assert not view["evaluations"][0]["current"]


def test_duplicate_enqueue_reuses_same_snapshot_and_does_not_charge(engine):
    env = setup(engine)
    receipt = ingest_case(env)
    first = enqueue(env, receipt)
    second = enqueue(env, receipt)
    assert first["evaluations"][0]["batch_id"] == second["evaluations"][0]["batch_id"]
    assert second["evaluations"][0]["duplicate"]
    assert first["semantic_evaluation"] == "NOT_RUN"
    assert env.service.metrics(env.cookie, env.installation.scope.source_instance_id)[
        "recorded_model_attempts"
    ] == 0


def test_enqueued_snapshot_and_original_batch_roll_back_in_same_engine_transaction(engine):
    env = setup(engine)
    receipt = ingest_case(env)
    with pytest.raises(RuntimeError):
        with engine.transaction():
            queued = enqueue(env, receipt)
            assert queued["evaluations"][0]["status"] == "prepared"
            raise RuntimeError("synthetic-outer-rollback")
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.router_batches").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT count(*) AS n FROM app.sa_source_eval_snapshots"
        ).fetchone()["n"] == 0


def test_missing_signed_repository_keeps_fast_candidate_without_model_job(engine):
    env = setup(engine)
    policy(env, execution={"enabled": False, "repositories": [], "capabilities": [],
                           "network_hosts": []})
    queued = enqueue(env)
    assert queued["evaluations"][0]["status"] == "pending"
    assert queued["evaluations"][0]["reason_code"] == "SOURCE_EVAL_REPOSITORY_REQUIRED"
    assert env.service.prepare() is None


def test_high_priority_control_is_not_a_semantic_model_candidate(engine, trigger):
    env = setup(engine)
    task = controller(env, trigger)
    receipt = ingest_case(env, body=f"/mgar pause {task.ticket_id}")
    queued = enqueue(env, receipt)
    assert queued["evaluations"][0]["status"] == "pending"
    assert queued["evaluations"][0]["reason_code"] == "SOURCE_EVAL_FAST_BATCH_STALE"


def test_aggregate_context_hard_bound_is_fixed_pending_without_validation_text(engine):
    env = setup(engine)
    configure_fast_path(env, env.fast_settings.model_copy(update={"merge_window_seconds": 300}))
    receipt = ingest_case(env, body="SYNTHETIC_PRIVATE_BOUND_" + "x" * 40000, topic="large-topic")
    added = ingest_case(env, entity="large-2", delivery="large-2", event="large-2",
                        body="SYNTHETIC_PRIVATE_BOUND_" + "y" * 40000, topic="large-topic")
    assert fast_batch(env, receipt) == fast_batch(env, added)
    result = enqueue(env, receipt)
    assert result["evaluations"][0]["status"] == "pending"
    assert result["evaluations"][0]["reason_code"] == "SOURCE_EVAL_CONTEXT_BOUND"
    assert env.service.prepare() is None


def live(env):
    _, permit = setup_router(env.engine)
    profile = SourceEvalProfile(
        provider_id="deepseek", profile_ref=permit.profile.profile_hash,
        price=env.eval_settings.profile.price,
        prompt_version=env.eval_settings.profile.prompt_version,
        prompt_sha256=env.eval_settings.profile.prompt_sha256,
    )
    configure_eval(env, env.eval_settings.model_copy(update={"profile": profile}))
    return permit, profile


def frames(group_id, *, invalid=False):
    value = ({"invalid": True} if invalid else {
        "schema_version": "source-router-proposals-v1", "proposals": [{
            "group_id": group_id, "action": "create", "target_id": None,
            "reason_code": "ACTIONABLE",
        }],
    })
    payload = [
        {"choices": [{"index": 0, "delta": {"content": json.dumps(value)}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
    ]
    return b"".join(("data: " + json.dumps(item) + "\n\n").encode() for item in payload) + (
        b"data: [DONE]\n\n"
    )


@pytest.mark.parametrize("outcome", ["ok", "invalid", "unknown"])
def test_explicit_live_port_uses_original_ledger_and_never_reposts_original_input(engine, outcome):
    env = setup(engine)
    permit, profile = live(env)
    receipt = ingest_case(env, body="PRIVATE_SYNTHETIC_SOURCE_EVAL_NO_EGRESS")
    batch = prepared(env, receipt, mode="live")
    before = original_state(engine)
    posts = []

    def respond(request):
        posts.append(request)
        if outcome == "unknown":
            raise httpx.ReadTimeout("synthetic-response-unknown")
        return httpx.Response(
            200, content=frames(str(batch["source_snapshot"]["groups"][0]["group_id"]),
                                invalid=outcome == "invalid"),
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = SourceEvalModel(
            profile, permit, client=client, api_key="synthetic-not-a-credential"
        )
        provider = SourceEvalProvider(env.service, model)
        if outcome == "ok":
            result = provider.propose(batch)
            decisions = env.service.wrap(batch, result)
            env.service.save(batch["batch_id"], decisions)
            env.service.commit(batch["batch_id"], decisions)
            assert provider.propose(batch) == result
        else:
            with pytest.raises(DomainError):
                provider.propose(batch)
            with pytest.raises(DomainError):
                provider.propose(batch)
        assert len(posts) == 1
    with engine.transaction() as conn:
        call = conn.execute("SELECT * FROM app.router_calls").fetchone()
        assert call["response_hash"] is None and call["result"] is None
        assert call["status"] == ("unknown" if outcome == "unknown" else "known")
        assert call["settled_micros"] == (None if outcome == "unknown" else 120)
        assert conn.execute("SELECT count(*) AS n FROM app.sa_budget_calls").fetchone()["n"] == 1
    after = original_state(engine)
    assert {k: v for k, v in before.items() if k != "router_calls"} == {
        k: v for k, v in after.items() if k != "router_calls"
    }
    configure_eval(env, env.eval_settings)
    repeated = enqueue(env, receipt, mode="live")
    assert repeated["evaluations"][0]["reason_code"] == "SOURCE_EVAL_INPUT_ALREADY_ATTEMPTED"
