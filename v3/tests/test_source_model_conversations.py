import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sa06_support import payload, signed
from sa07_support import definition, save
from sa22_model_support import model_environment

from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_feishu_channel import FeishuNotificationTransport


def test_model_answer_uses_original_money_and_encrypted_outbox(engine):
    env = model_environment(engine)
    try:
        plan = env.service.execute(env.input_id)
        with engine.transaction() as c:
            call = c.execute("SELECT * FROM app.usage_calls").fetchone()
            action = c.execute(
                "SELECT * FROM app.actions WHERE action_id=%s", (plan["action_id"],)
            ).fetchone()
            attributed = c.execute("SELECT count(*) AS n FROM app.sa_budget_calls").fetchone()["n"]
        assert call["status"] == "known" and call["settled_micros"] == 120
        assert attributed == 1 and len(env.model_calls) == 1
        assert "授权代码说明" not in json.dumps(call["result"], ensure_ascii=False)
        assert action["payload"]["body"].startswith("source-chat-output-v1:")
        assert "授权代码说明" not in json.dumps(action["payload"], ensure_ascii=False)
        Dispatcher(engine, FeishuNotificationTransport(engine.source_notifications)).tick()
        assert len(env.platform_calls) == 2
        assert (
            json.loads(env.platform_calls[-1]["body"]["content"])["text"]
            == "模型回答：\n授权代码说明。"
        )
        assert env.service.execute(env.input_id)["action_id"] == plan["action_id"]
        assert len(env.model_calls) == 1
    finally:
        env.close()


@pytest.mark.parametrize("failure", ["lost", "missing_usage"])
def test_unknown_model_is_never_retried(engine, failure):
    def handler(request):
        if failure == "lost":
            raise httpx.ReadTimeout("controlled", request=request)
        return httpx.Response(200, json={"choices": []})

    env = model_environment(engine, handler=handler)
    try:
        with pytest.raises(DomainError):
            env.service.execute(env.input_id)
        with pytest.raises(DomainError):
            env.service.execute(env.input_id)
        assert len(env.model_calls) == 1
        with engine.transaction() as c:
            call = c.execute("SELECT status,reserved_micros FROM app.usage_calls").fetchone()
            assert call["status"] == "unknown" and call["reserved_micros"] > 0
            assert c.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 0
    finally:
        env.close()


def test_revocation_after_reservation_prevents_physical_model_post(engine, monkeypatch):
    env = model_environment(engine)
    reserve = engine.reserve_call

    def revoked(*args, **kwargs):
        call = reserve(*args, **kwargs)
        current = engine.source_policies.read(env.source.cookie, env.source.policy_id)
        raw = current["definition"]
        raw["receive"]["model_input"] = False
        engine.source_policies.save(
            env.source.cookie,
            env.source.policy_id,
            PolicySave(
                expected_revision=3,
                definition=PolicyDefinition.model_validate(raw),
                review_ref="revoke-before-send",
            ),
        )
        return call

    monkeypatch.setattr(engine, "reserve_call", revoked)
    try:
        with pytest.raises(DomainError):
            env.service.execute(env.input_id)
        assert env.model_calls == [] and env.platform_calls == []
    finally:
        env.close()


def test_concurrent_same_input_cannot_duplicate_paid_request(engine):
    env = model_environment(engine)

    def execute():
        try:
            return env.service.execute(env.input_id)
        except DomainError as error:
            return error.code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: execute(), range(2)))
        assert any(isinstance(value, dict) for value in values)
        assert len(env.model_calls) == 1
        with engine.transaction() as c:
            assert c.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 1
            assert c.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 1
    finally:
        env.close()


def test_expired_reply_key_cannot_disclose_and_is_removed(engine):
    env = model_environment(engine)
    try:
        plan = env.service.execute(env.input_id)
        key = next(env.keys.root.glob("*.json"))
        record = json.loads(key.read_text())
        record["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        key.write_text(json.dumps(record))
        with engine.transaction() as c:
            ref = c.execute(
                "SELECT payload FROM app.actions WHERE action_id=%s", (plan["action_id"],)
            ).fetchone()["payload"]["body"]
        with pytest.raises(DomainError):
            env.service.resolve_body(plan["action_id"], ref)
        assert env.keys.purge() == 1 and not key.exists()
    finally:
        env.close()


def test_known_invalid_answer_settles_bill_without_resend(engine):
    env = model_environment(
        engine,
        handler=lambda r: httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                "choices": [{"finish_reason": "stop", "message": {"content": "invalid-json"}}],
            },
        ),
    )
    try:
        for _ in range(2):
            with pytest.raises(DomainError):
                env.service.execute(env.input_id)
        with engine.transaction() as c:
            call = c.execute("SELECT * FROM app.usage_calls").fetchone()
            assert (
                call["status"] == "known"
                and call["settled_micros"] == 20
                and call["result"] is None
            )
        assert len(env.model_calls) == 1
    finally:
        env.close()


def test_authority_change_during_platform_token_prevents_answer_send(engine):
    def revoke(source):
        current = engine.source_policies.read(source.cookie, source.policy_id)
        raw = current["definition"]
        raw["notification"]["enabled"] = False
        engine.source_policies.save(
            source.cookie,
            source.policy_id,
            PolicySave(
                expected_revision=3,
                definition=PolicyDefinition.model_validate(raw),
                review_ref="revoke-after-token",
            ),
        )

    env = model_environment(engine, token_hook=revoke)
    try:
        env.service.execute(env.input_id)
        Dispatcher(engine, FeishuNotificationTransport(engine.source_notifications)).tick()
        assert len(env.platform_calls) == 1
        with engine.transaction() as c:
            assert c.execute("SELECT status FROM app.actions").fetchone()["status"] == "failed"
    finally:
        env.close()


def test_existing_outer_transaction_cannot_send_or_reserve(engine):
    env = model_environment(engine)
    try:
        with engine.transaction() as c:
            with pytest.raises(DomainError, match="Current model conversation"):
                env.service.execute(env.input_id)
            assert c.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0
        assert env.model_calls == []
    finally:
        env.close()


def test_new_question_does_not_reset_shared_project_budget(engine):
    env = model_environment(engine)
    try:
        env.service.execute(env.input_id)
        budget = type(
            "BudgetOwner", (), {"service": engine.project_budgets, "cookie": env.source.cookie}
        )()
        save(
            budget,
            "project",
            engine.settings.project_id,
            definition(
                organization_id="sa22-organization",
                caps={"total_micros": 120, "hour_micros": 120, "day_micros": 120},
            ),
            revision=1,
        )
        received = env.runtime.receive(
            signed(
                env.source,
                payload(message_id="Om-second-budget-question", body="请再解释一下 @_user_1"),
            )
        )
        assert received.disposition == "accepted"
        with pytest.raises(DomainError):
            env.service.execute(received.input_ids[0])
        assert len(env.model_calls) == 1
        with engine.transaction() as c:
            assert c.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 1
    finally:
        env.close()


def test_unknown_bill_can_send_fixed_failure_status_without_faking_answer(engine):
    def lost(request):
        raise httpx.ReadTimeout("controlled", request=request)

    env = model_environment(engine, handler=lost)
    try:
        env.service.process(env.input_id)
        Dispatcher(engine, FeishuNotificationTransport(engine.source_notifications)).tick()
        assert len(env.model_calls) == 1
        text = json.loads(env.platform_calls[-1]["body"]["content"])["text"]
        assert "不会自动重复调用" in text
        with engine.transaction() as c:
            assert c.execute("SELECT status FROM app.usage_calls").fetchone()["status"] == "unknown"
            assert c.execute("SELECT status FROM app.actions").fetchone()["status"] == "succeeded"
    finally:
        env.close()
