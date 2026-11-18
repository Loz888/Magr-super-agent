"""Exact task lookup from a fresh source message through original Outbox."""

import json
from uuid import uuid4

import pytest
from sa06_support import payload, signed
from sa22_coding_support import coding_environment

from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.super_agent.source_feishu_channel import FeishuNotificationTransport


@pytest.mark.parametrize("revoke_during_token", [False, True])
def test_task_status_reply_after_purge_revalidates_at_actual_send(
    engine, tmp_path, revoke_during_token,
):
    old_input = None

    def token_hook(source):
        if revoke_during_token:
            assert old_input is not None
            with engine.transaction() as conn:
                conn.execute(
                    "SELECT app.expire_source_input(%s,'INGRESS_ENTITY_DELETED')",
                    (old_input,),
                )

    env = coding_environment(engine, tmp_path, task_status=True, token_hook=token_hook)
    try:
        original = env.experiment.submit(env.input_id)
        old_input = env.input_id
        with engine.transaction() as conn:
            binding = conn.execute(
                "SELECT ticket_id FROM app.sa_coding_bindings WHERE run_id=%s",
                (original["run_id"],),
            ).fetchone()
            conn.execute(
                "UPDATE app.source_ingress_bodies SET expires_at=clock_timestamp()-interval '1s' "
                "WHERE input_id=%s", (old_input,),
            )
        purged = engine.source_ingress.purge(env.source.cookie, env.source.source_id)
        assert purged["expired_bodies"] == 1
        mid = "Om_Task_Status-" + uuid4().hex
        value = env.runtime.receive(signed(env.source, payload(
            message_id=mid,
            body=f"任务状态 {binding['ticket_id']} @_user_1",
        )))
        assert value.disposition == "accepted"
        query_id = value.input_ids[0]
        created = env.conversation.service.process(query_id)
        action_id = created["action_id"]
        assert env.conversation.service.process(query_id)["action_id"] == action_id
        dispatcher = Dispatcher(engine, FeishuNotificationTransport(engine.source_notifications))
        assert dispatcher.tick() == action_id
        assert dispatcher.tick() is None
        sends = [
            call for call in env.conversation.platform_calls if call["path"].endswith("/reply")
        ]
        with engine.transaction() as conn:
            action = conn.execute(
                "SELECT o.status,o.attempts FROM app.outbox o WHERE o.action_id=%s", (action_id,),
            ).fetchone()
            bindings = conn.execute("SELECT count(*) n FROM app.sa_coding_bindings").fetchone()
            assert bindings["n"] == 1
            assert conn.execute("SELECT count(*) n FROM app.usage_calls").fetchone()["n"] == 0
            assert conn.execute(
                "SELECT count(*) n FROM app.sa_coding_attempts",
            ).fetchone()["n"] == 0
        assert not env.conversation.model_calls and not env.requests
        assert action["attempts"] == 1
        if revoke_during_token:
            assert action["status"] == "failed" and sends == []
        else:
            [sent] = sends
            assert sent["path"].endswith(f"/{mid}/reply")
            reply = json.loads(sent["body"]["content"])["text"]
            assert str(binding["ticket_id"]) in reply
            assert "排队中" in reply and "尚无最终树验证记录" in reply
            assert "尚无原请求回复记录" in reply and "USD0.000000" in reply
            assert "total 的计算错误" not in reply
            assert action["status"] == "succeeded"
    finally:
        env.close()
