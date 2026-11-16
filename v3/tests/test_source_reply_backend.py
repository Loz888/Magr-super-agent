"""New SA22 checks only; earlier accepted targets are not repeated."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sa22_reply_support import compose

from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.super_agent.policy_domain import PolicyRevoke
from mgar_v3.super_agent.source_feishu_channel import FeishuNotificationChannel
from mgar_v3.super_agent.source_replies import GRAPH, queue_reply


def action(engine, identifier):
    with engine.transaction() as conn:
        return engine._action(conn, identifier)


def test_real_original_outbox_reply_has_no_source_or_coding_claim(engine):
    env = compose(engine)
    plan = queue_reply(engine, env.source.cookie, env.input_id)
    assert not plan["coding_task"]
    assert Dispatcher(engine, env.transport).tick() == plan["action_id"]
    row, run = action(engine, plan["action_id"])
    assert row["status"] == "succeeded" and row["external_id"] == "om_controlled_reply"
    assert run["graph_version"] == GRAPH and run["status"] == "waiting_input"
    assert len(env.calls) == 2
    assert env.calls[-1].url.path.endswith("/Om_Message-AbC/reply")
    request = json.loads(env.calls[-1].content)
    assert "已收到" in json.loads(request["content"])["text"]
    assert "尚未开启自动编码" in request["content"] and "PRIVATE-SOURCE" not in request["content"]
    assert len(request["uuid"]) == 36
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.sa_scheduler_assignments").fetchone()["n"] == 0
        assert conn.execute("SELECT consumed_at FROM app.source_ingress_inputs WHERE input_id=%s",
                            (env.input_id,)).fetchone()["consumed_at"] is not None


def test_duplicate_concurrent_claim_creates_one_original_workflow_and_action(engine):
    env = compose(engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        plans = list(pool.map(lambda _:queue_reply(engine, env.source.cookie, env.input_id), range(2)))
    assert plans[0]["action_id"] == plans[1]["action_id"]
    assert sorted(p["created"] for p in plans) == [False, True]
    with engine.transaction() as conn:
        for table in ["runs", "tickets", "threads", "actions", "outbox", "approvals"]:
            assert conn.execute("SELECT count(*) AS n FROM app."+table).fetchone()["n"] == 1


@pytest.mark.parametrize("outcome", ["lost", "malformed"])
def test_unknown_delivery_is_never_resent(engine, outcome):
    env = compose(engine, outcome=outcome)
    plan = queue_reply(engine, env.source.cookie, env.input_id)
    dispatcher = Dispatcher(engine, env.transport)
    dispatcher.tick()
    assert action(engine, plan["action_id"])[0]["status"] == "outcome_unknown"
    assert dispatcher.tick() is None
    assert dispatcher.reconcile(plan["action_id"]) is False
    assert len(env.calls) == 2
    repeated = queue_reply(engine, env.source.cookie, env.input_id)
    assert not repeated["created"] and repeated["action_id"] == plan["action_id"]
    assert dispatcher.tick() is None and len(env.calls) == 2


def test_committed_policy_revocation_during_token_prevents_message_request(engine):
    def revoke(env, request):
        if request.url.path.endswith("/tenant_access_token/internal"):
            engine.source_policies.revoke(env.cookie, env.policy_id,
                PolicyRevoke(expected_revision=2, review_ref="SA22-mid-mint-revocation"))
    env = compose(engine, hook=revoke)
    plan = queue_reply(engine, env.source.cookie, env.input_id)
    Dispatcher(engine, env.transport).tick()
    assert len(env.calls) == 1
    assert action(engine, plan["action_id"])[0]["status"] in {"failed", "cancelled"}


def test_platform_definitive_rejection_not_reported_as_delivery(engine):
    env = compose(engine, outcome="rejected")
    plan = queue_reply(engine, env.source.cookie, env.input_id)
    Dispatcher(engine, env.transport).tick()
    assert action(engine, plan["action_id"])[0]["status"] == "failed"
    assert len(env.calls) == 2


def test_expired_source_blocks_queued_reply_before_any_http(engine, monkeypatch):
    env = compose(engine)
    plan = queue_reply(engine, env.source.cookie, env.input_id)
    monkeypatch.setattr(engine.source_ingress, "now", lambda conn:datetime.now(UTC)+timedelta(hours=1))
    assert Dispatcher(engine, env.transport).tick() is None
    assert env.calls == []
    assert action(engine, plan["action_id"])[0]["status"] in {"expired", "cancelled"}


def test_foreign_input_denial_rolls_back_without_workflow(engine):
    env = compose(engine)
    with pytest.raises(DomainError):
        queue_reply(engine, env.source.cookie, uuid4())
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 0


def test_fixture_profile_cannot_be_relabeled_live(engine):
    env = compose(engine)
    with pytest.raises(ValueError):
        replace(env.profile, mode="live").validate()
    with pytest.raises(ValueError):
        FeishuNotificationChannel(engine, env.profile.scope, lambda:env.source.secrets,
                                  mode="live", transport=httpx.MockTransport(lambda r:None))


@pytest.mark.parametrize("proxy", ["http://evil.example:8080", "http://user:pass@localhost:8080"])
def test_proxy_cannot_redirect_platform_credentials(engine, proxy):
    env = compose(engine)
    with pytest.raises(ValueError):
        FeishuNotificationChannel(engine, env.profile.scope, lambda:env.source.secrets, proxy=proxy)
