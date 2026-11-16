import json
import time
from dataclasses import replace

import httpx
import pytest
from sa22_model_support import model_environment
from sa22_repository_support import answer, excerpts, receive, repository, request_data, response

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_conversations import SourceConversations
from mgar_v3.super_agent.source_feishu_channel import FeishuNotificationTransport


def environment(engine, tmp_path, handler=answer, history=3, token_hook=None):
    return model_environment(
        engine,
        repository=repository(tmp_path / "repo"),
        question="记忆功能如何实现？ @_user_1",
        handler=handler,
        history_turns=history,
        token_hook=token_hook,
    )


def dispatch(engine):
    return Dispatcher(engine, FeishuNotificationTransport(engine.source_notifications)).tick()


def expire(engine, input_id):
    with engine.transaction() as conn:
        conn.execute("SELECT app.expire_source_input(%s,'CONTROLLED_PRIVACY_TEST')", (input_id,))


def test_natural_question_reads_code_and_delivers_bound_citations(engine, tmp_path):
    env = environment(engine, tmp_path)
    try:
        plan = env.service.execute(env.input_id)
        supplied = excerpts(env.model_calls[0])
        assert any("MemoryRecord" in r["content"] for r in supplied)
        dispatch(engine)
        text = json.loads(env.platform_calls[-1]["body"]["content"])["text"]
        assert "源码依据" in text and "v3/src/memory/" in text and ":L" in text
        with engine.transaction() as conn:
            call = conn.execute("SELECT * FROM app.usage_calls").fetchone()
            action = conn.execute(
                "SELECT * FROM app.actions WHERE action_id=%s", (plan["action_id"],)
            ).fetchone()
            assert call["settled_micros"] == 120 and call["result"]["repository_reads"]
            assert "MemoryRecord" not in json.dumps(call["result"])
            assert action["status"] == "succeeded" and action["payload"]["body"].startswith(
                "source-chat-output-v1:"
            )
        env.service.execute(env.input_id)
        assert len(env.model_calls) == 1
    finally:
        env.close()


def test_model_searches_then_reads_long_file_tail_on_original_shared_budget(engine, tmp_path):
    rounds = []

    def handler(request):
        rounds.append(request_data(request))
        if len(rounds) == 1:
            return response(
                {"intent": "tool", "tool": "search_code", "arguments": {"query": "hidden_tail"}}
            )
        if len(rounds) == 2:
            return response(
                {
                    "intent": "tool",
                    "tool": "read_file",
                    "arguments": {
                        "path": "v3/src/context/long.py",
                        "start_line": 501,
                        "max_lines": 2,
                    },
                }
            )
        assert "actual tail implementation" in excerpts(request)[-1]["content"]
        return answer(request, "实际尾部函数返回该实现值。")

    env = environment(engine, tmp_path, handler)
    try:
        plan = env.service.execute(env.input_id)
        assert len(env.model_calls) == 3
        with engine.transaction() as conn:
            rows = conn.execute(
                "SELECT step_key,status,settled_micros,result FROM app.usage_calls ORDER BY step_key"
            ).fetchall()
            assert len(rows) == 3 and sum(r["settled_micros"] for r in rows) == 360
            assert all(r["status"] == "known" for r in rows)
            assert sum(r["result"] is not None for r in rows) == 1
            assert (
                conn.execute("SELECT count(*) AS n FROM app.sa_budget_calls").fetchone()["n"] == 3
            )
            assert (
                conn.execute("SELECT count(*) AS n FROM app.sa_coding_bindings").fetchone()["n"]
                == 0
            )
        assert env.service.execute(env.input_id)["action_id"] == plan["action_id"]
        assert len(env.model_calls) == 3
    finally:
        env.close()


def test_invalid_read_tool_can_be_corrected_without_host_io(engine, tmp_path):
    rounds = []

    def handler(request):
        rounds.append(request)
        if len(rounds) == 1:
            return response(
                {"intent": "tool", "tool": "read_file", "arguments": {"path": "/etc/passwd"}}
            )
        observations = request_data(request)["repository_context"]["observations"]
        assert observations[-1]["result"]["error"] == "INVALID_READ_ONLY_TOOL_REQUEST"
        return answer(request)

    env = environment(engine, tmp_path, handler)
    try:
        env.service.execute(env.input_id)
        assert len(rounds) == 2
        assert all("root:x:" not in r.content.decode() for r in rounds)
    finally:
        env.close()


def test_fabricated_citation_is_rejected_after_true_bill_is_settled(engine, tmp_path):
    env = environment(
        engine,
        tmp_path,
        lambda request: response(
            {
                "intent": "answer",
                "answer": "代码结论",
                "citations": [{"path": "v3/src/nonexistent.py", "start_line": 1, "end_line": 2}],
            }
        ),
    )
    try:
        with pytest.raises(DomainError, match="Current model conversation"):
            env.service.execute(env.input_id)
        with pytest.raises(DomainError):
            env.service.execute(env.input_id)
        with engine.transaction() as conn:
            row = conn.execute(
                "SELECT status,settled_micros,result FROM app.usage_calls"
            ).fetchone()
            assert (
                row["status"] == "known" and row["settled_micros"] == 120 and row["result"] is None
            )
            assert conn.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 0
        assert len(env.model_calls) == 1
    finally:
        env.close()


def test_tool_loop_has_a_physical_paid_request_ceiling(engine, tmp_path):
    env = environment(
        engine,
        tmp_path,
        lambda request: response({"intent": "tool", "tool": "list_files", "arguments": {}}),
    )
    try:
        with pytest.raises(DomainError):
            env.service.execute(env.input_id)
        assert len(env.model_calls) == 5
        with engine.transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 5
            assert conn.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 0
    finally:
        env.close()


def test_unknown_following_step_holds_money_without_replaying_the_question(engine, tmp_path):
    rounds = []

    def handler(request):
        rounds.append(request)
        if len(rounds) == 1:
            return response(
                {
                    "intent": "tool",
                    "tool": "read_file",
                    "arguments": {"path": "v3/src/memory/model.py"},
                }
            )
        raise httpx.ReadTimeout("controlled lost response", request=request)

    env = environment(engine, tmp_path, handler)
    try:
        for _ in range(2):
            with pytest.raises(DomainError):
                env.service.execute(env.input_id)
        assert len(rounds) == 2
        with engine.transaction() as conn:
            rows = conn.execute("SELECT status,reserved_micros FROM app.usage_calls").fetchall()
            assert len(rows) == 2 and sum(r["status"] == "unknown" for r in rows) == 1
            assert next(r for r in rows if r["status"] == "unknown")["reserved_micros"] > 0
    finally:
        env.close()


def test_policy_revocation_committed_before_second_send_prevents_code_disclosure(
    engine, tmp_path, monkeypatch
):
    env = environment(
        engine,
        tmp_path,
        lambda request: response(
            {"intent": "tool", "tool": "read_file", "arguments": {"path": "v3/src/memory/model.py"}}
        ),
    )
    reserve = engine.reserve_call
    calls = []

    def revoked(*args, **kwargs):
        call = reserve(*args, **kwargs)
        calls.append(call)
        if len(calls) == 2:
            current = engine.source_policies.read(env.source.cookie, env.source.policy_id)
            raw = current["definition"]
            raw["receive"]["model_input"] = False
            engine.source_policies.save(
                env.source.cookie,
                env.source.policy_id,
                PolicySave(
                    expected_revision=3,
                    definition=PolicyDefinition.model_validate(raw),
                    review_ref="controlled-revoke-before-following-send",
                ),
            )
        return call

    monkeypatch.setattr(engine, "reserve_call", revoked)
    try:
        with pytest.raises(DomainError):
            env.service.execute(env.input_id)
        assert len(env.model_calls) == 1
    finally:
        env.close()


def test_followup_loads_delivered_same_topic_original_records(engine, tmp_path):
    env = environment(engine, tmp_path)
    try:
        env.service.execute(env.input_id)
        dispatch(engine)
        second = receive(env, "那就查看完再回答我")
        env.service.execute(second)
        context = request_data(env.model_calls[-1])["repository_context"]
        assert len(context["recent_context"]) == 1
        assert "记忆功能如何实现" in context["recent_context"][0]["question"]
        assert any("MemoryRecord" in r["content"] for r in excerpts(env.model_calls[-1]))
        with engine.transaction() as conn:
            scopes = [
                row["scope"]
                for row in conn.execute("SELECT scope FROM app.mandates").fetchall()
                if row["scope"].get("conversation_context")
            ]
            assert len(scopes) == 1 and len(scopes[0]["conversation_dependencies"]) == 1
            assert "记忆功能" not in json.dumps(scopes, ensure_ascii=False)
    finally:
        env.close()


def test_expired_or_deleted_history_is_not_inserted_into_new_prompt(engine, tmp_path):
    env = environment(engine, tmp_path)
    try:
        env.service.execute(env.input_id)
        dispatch(engine)
        expire(engine, env.input_id)
        second = receive(env, "请介绍任务模块")
        env.service.execute(second)
        assert request_data(env.model_calls[-1])["repository_context"]["recent_context"] == []
    finally:
        env.close()


def test_history_deleted_after_reservation_blocks_physical_request(engine, tmp_path, monkeypatch):
    env = environment(engine, tmp_path)
    try:
        env.service.execute(env.input_id)
        dispatch(engine)
        second = receive(env, "那就读取源码再说明")
        reserve = engine.reserve_call

        def deleted(*args, **kwargs):
            call = reserve(*args, **kwargs)
            expire(engine, env.input_id)
            return call

        monkeypatch.setattr(engine, "reserve_call", deleted)
        with pytest.raises(DomainError):
            env.service.execute(second)
        assert len(env.model_calls) == 1
    finally:
        env.close()


def test_transitive_deleted_history_prevents_derived_answer_delivery(engine, tmp_path):
    env = environment(engine, tmp_path)
    try:
        env.service.execute(env.input_id)
        dispatch(engine)
        second = receive(env, "继续解释记忆的scope")
        env.service.execute(second)
        time.sleep(1.05)
        dispatch(engine)
        third = receive(env, "那再整理一下调用关系")
        plan = env.service.execute(third)
        expire(engine, env.input_id)
        dispatch(engine)
        with engine.transaction() as conn:
            action = conn.execute(
                "SELECT status FROM app.actions WHERE action_id=%s", (plan["action_id"],)
            ).fetchone()
            assert action["status"] != "succeeded"
        assert len([p for p in env.platform_calls if p["path"].endswith("/reply")]) == 2
    finally:
        env.close()


def test_history_does_not_include_future_or_other_native_topic_input(engine, tmp_path):
    env = environment(engine, tmp_path)
    try:
        earlier = receive(env, "请先说明预算")
        env.service.execute(earlier)
        dispatch(engine)
        env.service.execute(env.input_id)
        assert request_data(env.model_calls[-1])["repository_context"]["recent_context"] == []
        other = receive(env, "继续解释", thread_id="different-native-topic")
        count = len(env.model_calls)
        with pytest.raises(DomainError):
            env.service.execute(other)
        assert len(env.model_calls) == count
    finally:
        env.close()


def test_model_cannot_turn_followup_history_into_coding_authority(engine, tmp_path):
    env = environment(
        engine,
        tmp_path,
        lambda request: response({"intent": "code", "answer": "交给编码", "citations": []}),
    )
    executed = []
    env.service.coding = lambda input_id: executed.append(input_id) or "unauthorized"
    try:
        plan = env.service.execute(env.input_id)
        assert executed == []
        with engine.transaction() as conn:
            body = conn.execute(
                "SELECT payload FROM app.actions WHERE action_id=%s", (plan["action_id"],)
            ).fetchone()["payload"]["body"]
        assert "未启动编码" in env.service.resolve_body(plan["action_id"], body)
    finally:
        env.close()


def test_restart_reconstructs_context_from_original_pg_and_private_keys(engine, tmp_path):
    env = environment(engine, tmp_path)
    replacement = None
    try:
        env.service.execute(env.input_id)
        dispatch(engine)
        second = receive(env, "那就查看源码再回答")
        settings, cipher = engine.settings, engine.source_ingress.cipher
        profile = next(iter(engine.source_notifications._profiles.values()))
        engine.close()
        replacement = Engine(settings, ArtifactStore(settings.artifact_root))
        replacement.source_ingress.cipher = cipher
        old = env.service
        service = SourceConversations(
            replacement,
            old.cookie,
            old.profile,
            old.keys,
            client=old.client,
            api_key="controlled-not-a-credential",
            repository=old.repository,
            controlled_transport=True,
        )
        channel = type(profile.channel)(
            replacement,
            profile.scope,
            lambda: env.source.secrets,
            body_resolver=service.resolve_body,
        )
        replacement.source_notifications.register_profile(replace(profile, channel=channel))
        service.execute(second)
        history = request_data(env.model_calls[-1])["repository_context"]["recent_context"]
        assert len(history) == 1 and "记忆功能" in history[0]["question"]
    finally:
        if replacement:
            replacement.close()
        env.close()
