"""Only newly observed live mention and multi-read context failures."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sa06_support import SENDER_ID, payload, signed
from test_sa22_case_profiles import cases

from mgar_v3.harness.contracts import CancellationToken, ModelMessage, ModelRequest, TaskBinding
from mgar_v3.sources.feishu_domain import FeishuActivation
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.super_agent.source_case_experiments import build_case_experiment, canonical_request
from mgar_v3.super_agent.source_coding_live_model import (
    ExperimentalDeepSeekModel,
    TrialModelPreflightError,
    guard_trial_model_send,
)


def test_native_leading_bare_mention_reaches_exact_case_without_changing_objective(
    engine, tmp_path,
):
    env, _, _, manager = cases(engine, tmp_path)
    try:
        with engine.transaction() as conn:
            message = engine.source_correlations._input(conn, env.input_id).text
        runtime = FeishuRuntime(
            env.source.sources, env.source.source_id, 2, 4, env.source.secrets,
            FeishuActivation(
                env.source.source_id, 2, 4, datetime.now(UTC) + timedelta(minutes=5),
                frozenset({"webhook", "socket", "health"}), "new-live-bare-mention-boundary",
            ),
            transport=httpx.MockTransport(env.source.handler),
        )
        received = runtime.receive(signed(
            env.source, payload(message_id="bare-mention-" + uuid4().hex, body="@ " + message),
        ))
        selected = manager.case_for(received.input_ids[0])
        assert selected is not None and selected[0] == "L01"
        bound = manager.admit(received.input_ids[0])
        with engine.transaction() as conn:
            actual = engine.source_coding.binding(conn, bound["coding_id"])
            assert actual["input_id"] == received.input_ids[0]
            assert actual["profile_sha256"] == env.experiment.profile.profile_sha256
            assert conn.execute("SELECT count(*)n FROM app.usage_calls").fetchone()["n"] == 0
        assert canonical_request("@ " + message) == canonical_request(message)
        assert canonical_request("不要 " + message) != canonical_request(message)
        assert canonical_request("@@ " + message) != canonical_request(message)
        assert canonical_request("@ " + message + " 可以修改测试") != canonical_request(message)
        assert env.requests == []
    finally:
        env.close()


def test_larger_thinking_tool_context_keeps_full_bytes_and_conservative_original_reservation(
    engine, tmp_path,
):
    env, _, _, _ = cases(engine, tmp_path)
    sent = []

    def supplier(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 24000, "completion_tokens": 100},
            "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "Proposal only", "reasoning_content": "synthetic",
            }}],
        })

    client = httpx.Client(transport=httpx.MockTransport(supplier), trust_env=False)
    try:
        original = env.experiment
        model = ExperimentalDeepSeekModel(
            client, "controlled-not-a-key", original.runtime_profile.price,
            controlled_transport=True, context_tokens=131072, thinking_mode="enabled",
        )
        large = build_case_experiment(
            engine, env.source.cookie, source_id=env.source.source_id, actor_id=SENDER_ID,
            source_root=env.original, root=tmp_path / "large-context-case",
            upstream_commit=original.profile.repository.base_commit,
            upstream_tree=original.profile.repository.upstream_tree,
            image_id=original.runtime_profile.driver.profile.image_id,
            price=original.runtime_profile.price, model=model,
            expires=datetime.now(UTC) + timedelta(minutes=5), manifest_sha256="a" * 64,
            h12_evidence_sha256=original.profile.harness.h12.evidence_sha256,
            objective="controlled multi-read context boundary",
            baseline_command=original.runtime_profile.baseline_command,
            read_paths=original.profile.grant.read_paths,
            write_paths=original.profile.grant.write_paths, repository_id=1,
        )
        profile = large.runtime_profile
        expected = (
            131072 * profile.price.input_micros_per_million
            + 8192 * profile.price.output_micros_per_million + 999999
        ) // 1000000
        assert profile.reservation_micros == expected
        assert profile.reserved_tokens == 131072 + 8192
        assert profile.limits.max_cost_micros == 300000
        task = TaskBinding(
            task_id="large-context", thread_id=uuid4(), run_id=uuid4(), execution_epoch=1,
            actor="sa_test", repository="example/test", base_commit="a" * 40,
            workspace_id="controlled", acceptance_revision="frozen", objective="controlled",
            task_kind="feature", requirements=(
                {"requirement_id": "frozen", "command_contains": ("python3",), "min_tests": 1},
            ),
        )
        text = "source-line\n" * 8000
        request = ModelRequest(
            operation_id="large-context:model:1", task=task,
            messages=(ModelMessage(role="user", content=text),), tools=(),
            model_id=profile.price.model_id, price_version=profile.price.price_version,
            max_output_tokens=8192, reservation_micros=expected, context_revision="frozen",
            workspace_tree="b" * 64,
        )
        with guard_trial_model_send(nullcontext):
            response = model.complete(request, CancellationToken())
        assert response.usage.known and response.usage.cost_micros <= expected
        assert sent[0]["messages"][0]["content"] == text
        assert sent[0]["thinking"] == {"type": "enabled"}
        assert sent[0]["reasoning_effort"] == "max"
        too_large = request.model_copy(update={
            "messages": (ModelMessage(role="user", content=text * 2),),
        })
        with guard_trial_model_send(nullcontext), pytest.raises(TrialModelPreflightError):
            model.complete(too_large, CancellationToken())
        assert len(sent) == 1
    finally:
        client.close()
        env.close()
