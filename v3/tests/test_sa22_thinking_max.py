"""New thinking/max protocol checks; historical PASS targets are excluded."""

import json
from contextlib import nullcontext
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from sa07_support import PRICE
from sa13_runtime_support import BAD, GOOD
from sa22_coding_support import coding_environment
from sa22_model_support import model_environment

from mgar_v3.harness.contracts import (
    CancellationToken,
    HarnessToolCall,
    ModelMessage,
    ModelRequest,
    TaskBinding,
    ToolSpec,
)
from mgar_v3.super_agent.source_coding_live_model import (
    ExperimentalDeepSeekModel,
    TrialModelPreflightError,
    guard_trial_model_send,
)
from mgar_v3.super_agent.source_coding_runtime import SourceCodingRuntime
from mgar_v3.super_agent.source_conversations import ConversationPermit


def test_owner_question_uses_max_and_bills_all_completion_tokens_without_exposing_reasoning(engine):
    seen = []

    def supplier(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 200,
                    "completion_tokens_details": {"reasoning_tokens": 140},
                },
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "reasoning_content": "private synthetic reasoning must stay private",
                            "content": json.dumps({"intent": "answer", "answer": "4"}),
                        },
                    }
                ],
            },
        )

    env = model_environment(engine, handler=supplier, question="2+2等于多少？ @_user_1")
    try:
        env.service.profile = env.service.profile.model_copy(
            update={
                "thinking_mode": "enabled",
                "reasoning_effort": "max",
                "max_output_tokens": 4096,
            }
        )
        env.service.permit = ConversationPermit(engine, env.service.profile)
        env.service.execute(env.input_id)
        assert len(seen) == 1 and seen[0]["thinking"] == {"type": "enabled"}
        assert seen[0]["reasoning_effort"] == "max" and seen[0]["max_tokens"] == 4096
        assert "temperature" not in seen[0]
        with engine.transaction() as c:
            row = c.execute("SELECT * FROM app.usage_calls").fetchone()
        expected = (
            100 * PRICE.input_micros_per_million + 200 * PRICE.output_micros_per_million + 999999
        ) // 1000000
        assert row["status"] == "known" and row["settled_micros"] == expected
        clear = env.service.keys.open(row["call_id"], row["result"]["ciphertext"])
        assert "4" in clear and "private synthetic reasoning" not in clear
    finally:
        env.close()


def test_code_thinking_replays_exact_scoped_encrypted_history_across_actual_tool_rounds(
    engine, tmp_path
):
    seen = []
    reasonings = []
    encrypted_observed = []

    def supplier(request):
        body = json.loads(request.content)
        seen.append(body)
        assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "max"
        assistants = [m for m in body["messages"] if m["role"] == "assistant"]
        assert [m["reasoning_content"] for m in assistants] == reasonings
        if len(seen) > 1:
            with engine.transaction() as c:
                rows = c.execute(
                    "SELECT ciphertext FROM app.sa_coding_payloads "
                    "WHERE payload_kind='model_response' AND ciphertext IS NOT NULL"
                ).fetchall()
            assert rows and all(
                b"private-code-thought" not in bytes(row["ciphertext"]) for row in rows
            )
            encrypted_observed.append(True)
        name, args = "read_file", {"path": "src/calc.py"}
        if len(seen) == 2:
            tool = next(m for m in reversed(body["messages"]) if m["role"] == "tool")
            data = json.loads(tool["content"])
            assert data["content"] == BAD
            name, args = (
                "apply_edits",
                {
                    "expected_tree": data["tree_hash"],
                    "edits": [
                        {"path": data["path"], "expected_sha256": data["sha256"], "content": GOOD}
                    ],
                },
            )
        elif len(seen) == 3:
            scope = next(
                m["content"]
                for m in body["messages"]
                if m["role"] == "system" and m["content"].startswith("Trusted execution scope")
            )
            spec = json.loads(scope.split("\n")[1])
            name, args = "run_tests", {"argv": spec["test_argv"], "cwd": spec["test_cwd"]}
        reason = "private-code-thought-" + str(len(seen))
        reasonings.append(reason)
        message = {
            "role": "assistant",
            "content": None,
            "reasoning_content": reason,
            "tool_calls": [
                {
                    "id": "thinking-tool-" + str(len(seen)),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        }
        if len(seen) > 3:
            message = {
                "role": "assistant",
                "content": "Isolated proposal; independent acceptance pending.",
                "reasoning_content": reason,
            }
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 200},
                "choices": [
                    {"finish_reason": "stop" if len(seen) > 3 else "tool_calls", "message": message}
                ],
            },
        )

    env = coding_environment(engine, tmp_path, supplier_hook=supplier)
    try:
        model = ExperimentalDeepSeekModel(
            env.client,
            "controlled-not-a-key",
            PRICE,
            controlled_transport=True,
            thinking_mode="enabled",
            reasoning_effort="max",
        )
        runtime = replace(env.experiment.runtime_profile, model=model)
        env.experiment.runtime_profile = runtime
        engine.source_coding.runtime = SourceCodingRuntime(engine.source_coding, runtime)
        env.experiment(env.input_id)
        result = json.loads(next(env.experiment.artifacts.glob("*/result.json")).read_text())
        assert 4 <= len(seen) <= 8 and encrypted_observed
        assert result["tree_before"] != result["tree_after"]
        assert result["verification"] == "incomplete" and result["business_task_completed"] is False
        with engine.transaction() as c:
            assert (
                c.execute("SELECT count(*)n FROM app.usage_calls WHERE status<>'known'").fetchone()[
                    "n"
                ]
                == 0
            )
    finally:
        env.close()


def test_missing_thinking_history_fails_before_any_physical_http():
    calls = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: calls.append(request)), trust_env=False
    )
    model = ExperimentalDeepSeekModel(
        client,
        "controlled-not-a-key",
        PRICE,
        controlled_transport=True,
        thinking_mode="enabled",
        reasoning_effort="max",
    )
    task = TaskBinding(
        task_id="scope-task",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=1,
        actor="sa_test",
        repository="example/test",
        base_commit="a" * 40,
        workspace_id="workspace",
        acceptance_revision="acceptance",
        objective="controlled protocol",
        task_kind="feature",
        requirements=(
            {"requirement_id": "controlled", "command_contains": ("python3",), "min_tests": 1},
        ),
    )
    message = ModelMessage(
        role="assistant",
        content="",
        tool_calls=(
            HarnessToolCall(
                call_id="prior-call", name="read_file", arguments={"path": "src/calc.py"}
            ),
        ),
    )
    request = ModelRequest(
        operation_id="next-call",
        task=task,
        messages=(message,),
        tools=(
            ToolSpec(name="read_file", description="controlled", input_schema={"type": "object"}),
        ),
        model_id=PRICE.model_id,
        price_version=PRICE.price_version,
        max_output_tokens=8192,
        reservation_micros=20000,
        context_revision="one",
        workspace_tree="b" * 64,
    )
    try:
        with guard_trial_model_send(nullcontext), pytest.raises(TrialModelPreflightError):
            model.complete(request, CancellationToken())
        assert calls == []
    finally:
        client.close()


def test_local_model_input_rejection_is_known_zero_without_refunding_any_remote_unknown(
    engine, tmp_path
):
    env = coding_environment(engine, tmp_path)
    try:
        model = ExperimentalDeepSeekModel(
            env.client,
            "controlled-not-a-key",
            PRICE,
            context_tokens=4096,
            controlled_transport=True,
            thinking_mode="enabled",
            reasoning_effort="max",
        )
        runtime = replace(env.experiment.runtime_profile, model=model)
        env.experiment.runtime_profile = runtime
        engine.source_coding.runtime = SourceCodingRuntime(engine.source_coding, runtime)
        env.experiment(env.input_id)
        assert env.requests == []
        with engine.transaction() as c:
            rows = c.execute("SELECT status,settled_micros FROM app.usage_calls").fetchall()
            assert rows == [{"status": "known", "settled_micros": 0}]
            event = c.execute(
                "SELECT safe_payload FROM app.run_events WHERE kind='source_trial_model_not_sent'"
            ).fetchone()
            assert event["safe_payload"]["physical_http"] is False
            assert event["safe_payload"]["reason"] == "SOURCE_CODING_MODEL_INPUT_BOUND"
    finally:
        env.close()
