"""Only new failed live intake boundaries; historical PASS targets are excluded."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sa06_support import payload, setup, signed
from sa22_model_support import model_environment

from mgar_v3.sources.ingress_domain import GapDeclaration
from mgar_v3.super_agent.source_case_experiments import canonical_request


@pytest.mark.parametrize("transport", ["fresh_socket", "webhook", "old_socket", "shadow_socket"])
def test_unreplayable_gap_only_allows_fresh_explicit_request_on_current_authenticated_socket(
    engine, transport
):
    env = setup(engine, mode="shadow" if transport == "shadow_socket" else "auto_investigate")
    engine.source_ingress.gap(
        env.source_id,
        GapDeclaration(
            stream_id="feishu-socket",
            epoch="old-connection",
            reason="no_replay",
            started_at=datetime.now(UTC) - timedelta(minutes=2),
            ended_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
    )
    raw = payload(message_id="om_new_live_gap_" + uuid4().hex)
    if transport == "old_socket":
        old = str(int((datetime.now(UTC) - timedelta(seconds=301)).timestamp() * 1000))
        raw["header"]["create_time"] = old
        raw["event"]["message"]["create_time"] = old
    if transport == "webhook":
        receipt = env.runtime.receive(signed(env, raw))
    else:
        epoch = env.runtime.socket_connected()
        receipt = env.runtime.receive_socket(
            json.dumps(raw).encode(), "new-frame-" + uuid4().hex, epoch
        )
    assert receipt.committed
    with engine.transaction() as conn:
        rows = conn.execute("SELECT context_only FROM app.source_ingress_inputs").fetchall()
        assert rows and all(row["context_only"] == (transport != "fresh_socket") for row in rows)
        gaps = conn.execute("SELECT resolved_at FROM app.source_ingress_gaps").fetchall()
        assert gaps and all(row["resolved_at"] is None for row in gaps)


@pytest.mark.parametrize("fragmented", ["保留正 常、零、空向量", "非法值 抛 RRF_K_INVALID"])
def test_native_rich_text_chinese_span_spaces_keep_exact_case_identity(fragmented):
    assert canonical_request(fragmented) == canonical_request(fragmented.replace(" ", "", 1))
    assert canonical_request("file one.py") != canonical_request("fileone.py")
    assert canonical_request("foo bar") != canonical_request("foobar")


@pytest.mark.parametrize("registered", [True, False])
def test_only_registered_explicit_case_can_resolve_model_answer_intent_to_coding(
    engine, registered
):
    def model(_):
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"intent": "answer", "answer": "这是实现建议，不代表已执行。"}
                            ),
                        },
                    }
                ],
            },
        )

    env = model_environment(engine, handler=model, question="请修复已登记的受控代码目标 @_user_1")
    calls = []
    env.service.coding = lambda identifier: (
        calls.append(identifier) or "受控编码端口已接收，未宣称通过。"
    )
    env.service.deterministic_coding = lambda identifier: registered and identifier == env.input_id
    try:
        env.service.execute(env.input_id)
        assert calls == ([env.input_id] if registered else [])
        with engine.transaction() as conn:
            row = conn.execute(
                "SELECT result->>'intent' AS intent,status FROM app.usage_calls"
            ).fetchone()
            assert row == {"intent": "code" if registered else "answer", "status": "known"}
            event = conn.execute(
                "SELECT safe_payload FROM app.run_events WHERE kind='source_explicit_code_dispatch'"
            ).fetchall()
            assert bool(event) == registered
        assert len(env.model_calls) == 1
    finally:
        env.close()
