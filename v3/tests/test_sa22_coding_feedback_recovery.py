"""An expired question cannot suppress the unrelated completed trial's feedback."""

import json
from uuid import uuid4

import pytest
from sa06_support import payload, signed
from sa22_coding_support import coding_environment

from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.super_agent.source_conversations import ConversationPermit
from mgar_v3.super_agent.source_feishu_channel import FeishuNotificationTransport


def test_expired_history_recovery_uses_only_the_exact_completed_input_once(engine, tmp_path):
    env = coding_environment(engine, tmp_path, task_status=True)
    try:
        service = env.conversation.service
        service.profile = service.profile.model_copy(
            update={
                "repository_base_sha": env.experiment.profile.repository.base_commit,
                "context_path": "src/calc.py",
                "history_turns": 3,
            }
        )
        service.permit = ConversationPermit(engine, service.profile)

        def receive(message_id, body):
            receipt = env.runtime.receive(
                signed(
                    env.source,
                    payload(
                        message_id=message_id,
                        body=body,
                    ),
                )
            )
            assert receipt.disposition == "accepted"
            return receipt.input_ids[0]

        earlier = receive("om_earlier_" + uuid4().hex, "之前的只读问题 @_user_1")
        dispatcher = Dispatcher(engine, FeishuNotificationTransport(engine.source_notifications))
        assert dispatcher.tick() is None
        prior_action = service.process(earlier)["action_id"]
        assert dispatcher.tick() == prior_action

        message_id = "om_completed_" + uuid4().hex
        question = "请修复 src/calc.py 中 total 的计算错误，保持测试不变 @_user_1"
        input_id = receive(message_id, question)
        original = service._prepare(input_id)
        target = env.experiment.submit(input_id)
        result = env.experiment.run_one()
        assert result and result["verification"] == "incomplete" and result["changed"]
        with engine.transaction() as conn:
            refs = conn.execute(
                "SELECT scope->'conversation_dependencies' AS refs FROM app.mandates "
                "WHERE mandate_id=%s",
                (original["mandate_id"],),
            ).fetchone()["refs"]
            assert [r["input_id"] for r in refs] == [str(earlier)]
            conn.execute(
                "UPDATE app.source_ingress_bodies SET expires_at=clock_timestamp()-interval '1s' "
                "WHERE input_id=%s",
                (earlier,),
            )
        assert (
            engine.source_ingress.purge(env.source.cookie, env.source.source_id)["expired_bodies"]
            == 1
        )
        with engine.transaction() as conn, pytest.raises(DomainError) as denied:
            service._source(conn, original)
        assert denied.value.code == "SOURCE_CHAT_SOURCE"

        service.coding = env.experiment
        service.deterministic_coding = lambda identifier: identifier == input_id
        fresh = receive("om_next_" + uuid4().hex, question)
        service.deterministic_coding = lambda identifier: identifier in (input_id, fresh)
        with pytest.raises(DomainError):
            service.recover_coding_feedback(fresh, target["run_id"])
        posts = len(env.requests), len(env.conversation.model_calls)
        action = service.recover_coding_feedback(input_id, target["run_id"])["action_id"]
        assert service.recover_coding_feedback(input_id, target["run_id"])["action_id"] == action
        assert dispatcher.tick() == action and dispatcher.tick() is None
        [reply] = [
            p for p in env.conversation.platform_calls if p["path"].endswith(f"/{message_id}/reply")
        ]
        text = json.loads(reply["body"]["content"])["text"]
        assert "incomplete" in text and "失败 0 项" in text and "之前的只读问题" not in text
        assert posts == (len(env.requests), len(env.conversation.model_calls))
        with engine.transaction() as conn:
            assert conn.execute(
                "SELECT status,attempts FROM app.outbox WHERE action_id=%s",
                (action,),
            ).fetchone() == {"status": "succeeded", "attempts": 1}
    finally:
        env.close()
