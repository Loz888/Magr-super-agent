from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness import (
    CAPABILITIES,
    DEEPSEEK_CHAT,
    MODEL_PROTOCOL_VERSION,
    AnthropicMessagesAdapter,
    BudgetLimits,
    CachingModelAdapter,
    CancellationToken,
    CapabilityGrant,
    DeepSeekChatAdapter,
    HarnessRuntime,
    HarnessToolRegistry,
    InMemoryBudgetLedger,
    InMemoryEventSink,
    LocalExecutionBroker,
    LocalVerifier,
    LocalWorkspace,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    SimpleContextManager,
    TaskBinding,
    ToolSpec,
    UnknownModelOutcome,
    VerificationRequirement,
    adapter_for,
)
from mgar_v3.harness.model import (
    ModelProtocolError,
    ModelProviderRejected,
    SSEDecoder,
)

ROOT = Path(__file__).resolve().parents[1]


def _task() -> TaskBinding:
    return TaskBinding(
        task_id="model-task",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=1,
        actor="owner",
        repository="example/project",
        base_commit="a" * 40,
        workspace_id="ws-1",
        acceptance_revision="1",
        objective="Use the model contract",
        requirements=(VerificationRequirement(requirement_id="unit"),),
    )


def _request(
    provider: str = "deepseek",
    *,
    messages: tuple[ModelMessage, ...] | None = None,
    tools: tuple[ToolSpec, ...] = (),
    max_output_tokens: int = 2048,
    reservation_micros: int = 10000,
    model_id: str | None = None,
    price_version: str | None = None,
) -> ModelRequest:
    capabilities = CAPABILITIES[provider]
    return ModelRequest(
        operation_id="model-op",
        task=_task(),
        messages=messages
        or (
            ModelMessage(role="system", content="Use tools."),
            ModelMessage(role="user", content="Read calc.py"),
        ),
        tools=tools,
        model_id=model_id or capabilities.model_id,
        price_version=price_version or capabilities.price.version,
        max_output_tokens=max_output_tokens,
        reservation_micros=reservation_micros,
        context_revision="rev-1",
        workspace_tree="b" * 64,
    )


def _tool() -> ToolSpec:
    return ToolSpec(
        name="read_file",
        description="Read one file.",
        input_schema={"type": "object", "required": ["path"]},
    )


def _deepseek_sse(
    *,
    text: str = "读取",
    arguments: str = '{"path":"calc.py"}',
    finish: str = "tool_calls",
    include_usage: bool = True,
    include_finish: bool = True,
) -> bytes:
    chunks: list[dict[str, Any]] = [
        {"choices": [{"delta": {"content": text}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {
                                    "name": "read_file",
                                    "arguments": arguments[:5],
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": arguments[5:]},
                            }
                        ]
                    }
                }
            ]
        },
    ]
    if include_finish:
        chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    if include_usage:
        chunks.append(
            {
                "choices": [],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        )
    return (
        b"".join(
            f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            for chunk in chunks
        )
        + b"data: [DONE]\n\n"
    )


def _anthropic_sse(
    *,
    text: str = "读取",
    arguments: str = '{"path":"calc.py"}',
    include_usage: bool = True,
) -> bytes:
    cut = 5
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 100}},
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        (
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "read_file",
                },
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": arguments[:cut],
                },
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": arguments[cut:],
                },
            },
        ),
        (
            "content_block_stop",
            {"type": "content_block_stop", "index": 1},
        ),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 20} if include_usage else {},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(
        (
            f"event: {name}\n"
            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        ).encode()
        for name, payload in events
    )


class _Chunks(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _streaming_client(payload: bytes, size: int = 3) -> httpx.Client:
    chunks = [payload[index : index + size] for index in range(0, len(payload), size)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Chunks(chunks))

    return _client(handler)


def test_capability_manifest_matches_adapters() -> None:
    manifest = json.loads(
        (ROOT / "experiments/harness-v4/model-capabilities-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["version"] == MODEL_PROTOCOL_VERSION
    assert manifest["status"] == "FROZEN"
    assert set(manifest["providers"]) == set(CAPABILITIES)
    for provider, capabilities in CAPABILITIES.items():
        entry = manifest["providers"][provider]
        assert entry["protocol"] == capabilities.protocol
        assert entry["model_id"] == capabilities.model_id
        assert entry["context_tokens"] == capabilities.context_tokens
        assert entry["max_output_tokens"] == capabilities.max_output_tokens
        assert entry["price"] == capabilities.price.model_dump(mode="json")
    assert manifest["evidence"]["real_provider"] == "NOT_RUN_NO_AUTHORIZED_PAID_CALL"


@pytest.mark.parametrize(
    ("provider", "payload", "expected_id"),
    [
        ("deepseek", _deepseek_sse(), "call_1"),
        ("anthropic", _anthropic_sse(), "tool_1"),
    ],
)
def test_two_adapters_share_the_tool_contract(
    provider: str, payload: bytes, expected_id: str
) -> None:
    capabilities = CAPABILITIES[provider]
    adapter = adapter_for(
        capabilities,
        api_key="controlled-test-key",
        client=_client(lambda request: httpx.Response(200, content=payload)),
    )
    response = adapter.complete(_request(provider, tools=(_tool(),)), CancellationToken())
    assert response.text == "读取"
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.call_id == expected_id
    assert call.name == "read_file"
    assert call.arguments == {"path": "calc.py"}
    assert response.finish_reason == "tool_calls"
    assert response.usage.known is True
    assert response.usage.cost_micros > 0
    assert response.provider == provider
    assert response.model_id == capabilities.model_id
    assert response.price_version == capabilities.price.version


def test_fragmented_utf8_and_json_are_reassembled() -> None:
    payload = _deepseek_sse(text="读取 calc.py")
    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key",
        client=_streaming_client(payload, size=2),
    )
    response = adapter.complete(_request(tools=(_tool(),)), CancellationToken())
    assert response.text == "读取 calc.py"
    assert response.tool_calls[0].arguments == {"path": "calc.py"}
    assert response.finish_reason == "tool_calls"


def test_sse_decoder_keeps_multibyte_characters_across_chunks() -> None:
    decoder = SSEDecoder()
    encoded = 'data: {"text":"读取"}\n\n'.encode()
    events = []
    for index in range(0, len(encoded), 2):
        events.extend(decoder.feed(encoded[index : index + 2]))
    assert events == [(None, '{"text":"读取"}')]


def test_sse_crlf_boundary_across_single_byte_chunks() -> None:
    decoder = SSEDecoder()
    events = []
    for byte in b'data: {"ok":true}\r\n\r\n':
        events.extend(decoder.feed(bytes([byte])))
    assert events == [(None, '{"ok":true}')]


def test_read_timeout_after_http_200_is_not_retried() -> None:
    class Broken(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            raise httpx.ReadTimeout("response body lost after POST was accepted")
            yield b""  # pragma: no cover - makes this a generator

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, stream=Broken())

    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key", client=_client(handler)
    )
    response = adapter.complete(_request(), CancellationToken())
    assert response.finish_reason == "error"
    assert response.error_code == "MODEL_STREAM_INTERRUPTED"
    assert response.usage.known is False
    assert calls == ["POST"]


def test_invalid_deepseek_usage_is_never_repriced_as_known_zero() -> None:
    payload = _deepseek_sse().replace(
        b'"prompt_tokens": 100, "completion_tokens": 20',
        b'"prompt_tokens": "invalid", "completion_tokens": 20',
    )
    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key",
        client=_client(lambda request: httpx.Response(200, content=payload)),
    )
    response = adapter.complete(_request(), CancellationToken())
    assert response.usage.known is False
    assert response.usage.cost_micros == 0


def test_anthropic_missing_final_output_usage_is_unknown() -> None:
    payload = _anthropic_sse(include_usage=False)
    adapter = AnthropicMessagesAdapter(
        api_key="controlled-test-key",
        client=_client(lambda request: httpx.Response(200, content=payload)),
    )
    response = adapter.complete(_request("anthropic"), CancellationToken())
    assert response.usage.known is False
    assert response.usage.output_tokens == 0


def _cancel_after_first_chunk(token: CancellationToken) -> httpx.Client:
    class CancelledStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"generated"}}]}\n\n'
            token.cancel()
            yield b"data: [DONE]\n\n"

    return _client(lambda request: httpx.Response(200, stream=CancelledStream()))


def test_cancel_after_generation_keeps_the_reservation(tmp_path: Path) -> None:
    token = CancellationToken()
    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key", client=_cancel_after_first_chunk(token)
    )
    runtime, ledger = _runtime(tmp_path, adapter)
    runtime.model_id = DEEPSEEK_CHAT.model_id
    runtime.price_version = DEEPSEEK_CHAT.price.version
    runtime.reservation_micros = 10000
    result = runtime.run(_task(), _grant(), cancellation=token)
    assert result.status == "unknown"
    assert ledger.snapshot("model-task").cost_micros == 10000


def test_malformed_sse_after_send_keeps_the_reservation(tmp_path: Path) -> None:
    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key",
        client=_client(
            lambda request: httpx.Response(200, content=b"data: {invalid-json}\n\n")
        ),
    )
    runtime, ledger = _runtime(tmp_path, adapter)
    runtime.model_id = DEEPSEEK_CHAT.model_id
    runtime.price_version = DEEPSEEK_CHAT.price.version
    runtime.reservation_micros = 10000
    result = runtime.run(_task(), _grant())
    assert result.status == "unknown"
    assert ledger.snapshot("model-task").cost_micros == 10000


@pytest.mark.parametrize("provider", ["deepseek", "anthropic"])
def test_tool_roundtrip_preserves_native_history(
    tmp_path: Path, provider: str
) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            payload = _deepseek_sse() if provider == "deepseek" else _anthropic_sse()
            return httpx.Response(200, content=payload)
        messages = body["messages"]
        preceding = messages[-2]
        latest = messages[-1]
        if provider == "deepseek":
            native_calls = preceding.get("tool_calls", [])
            valid = any(
                call.get("id") == latest.get("tool_call_id")
                for call in native_calls
            )
        else:
            content = preceding.get("content", [])
            valid = isinstance(content, list) and any(
                block.get("type") == "tool_use"
                and block.get("id") == latest["content"][0].get("tool_use_id")
                for block in content
            )
        if not valid:
            return httpx.Response(400, text="missing native tool history")
        terminal = (
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
            if provider == "deepseek"
            else (
                b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n\n'
                b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                b'"usage":{"output_tokens":1}}\n\n'
            )
        )
        return httpx.Response(200, content=terminal)

    adapter = adapter_for(
        CAPABILITIES[provider],
        api_key="controlled-test-key",
        client=_client(handler),
    )
    runtime, _ = _runtime(tmp_path, adapter)
    runtime.model_id = CAPABILITIES[provider].model_id
    runtime.price_version = CAPABILITIES[provider].price.version
    runtime.reservation_micros = 10000
    runtime.max_steps = 2
    (tmp_path / "workspace" / "calc.py").write_text("pass\n", encoding="utf-8")
    result = runtime.run(_task(), _grant())
    assert result.status in {"incomplete", "verified"}
    assert len(bodies) == 2
    assert bodies[1]["messages"][-2].get("role") == "assistant"
    assert bodies[1]["messages"][-1].get("role") in {"tool", "user"}



def test_context_overflow_is_rejected_before_any_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_deepseek_sse())

    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key", client=_client(handler)
    )
    messages = tuple(
        ModelMessage(role="user", content="x" * 200000) for _ in range(3)
    )
    with pytest.raises(DomainError) as error:
        adapter.complete(
            _request(messages=messages, max_output_tokens=8192), CancellationToken()
        )
    assert error.value.code == "MODEL_CONTEXT_EXCEEDED"
    assert calls == 0


def test_model_and_price_versions_are_frozen() -> None:
    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key",
        client=_client(lambda request: httpx.Response(200, content=_deepseek_sse())),
    )
    with pytest.raises(DomainError) as model_error:
        adapter.complete(_request(model_id="another-model"), CancellationToken())
    assert model_error.value.code == "MODEL_VERSION_MISMATCH"
    with pytest.raises(DomainError) as price_error:
        adapter.complete(_request(price_version="another-price"), CancellationToken())
    assert price_error.value.code == "MODEL_PRICE_MISMATCH"


def test_missing_usage_is_reported_as_unknown() -> None:
    payload = _deepseek_sse(include_usage=False)
    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key",
        client=_client(lambda request: httpx.Response(200, content=payload)),
    )
    response = adapter.complete(_request(), CancellationToken())
    assert response.finish_reason == "tool_calls"
    assert response.usage.known is False


def test_runtime_keeps_reservation_when_usage_is_missing(tmp_path: Path) -> None:
    class MissingUsage:
        def complete(
            self, request: ModelRequest, cancellation: CancellationToken
        ) -> ModelResponse:
            return ModelResponse(usage=ModelUsage(known=False))

    runtime, ledger = _runtime(tmp_path, MissingUsage())
    result = runtime.run(_task(), _grant())
    assert result.status == "unknown"
    assert ledger.snapshot("model-task").cost_micros == 100


def test_incomplete_tool_arguments_are_an_error_not_a_tool_call() -> None:
    payload = _deepseek_sse(arguments='{"path":', finish="length")
    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key",
        client=_client(lambda request: httpx.Response(200, content=payload)),
    )
    response = adapter.complete(_request(tools=(_tool(),)), CancellationToken())
    assert response.finish_reason == "error"
    assert response.error_code == "MODEL_TOOL_ARGUMENTS_INVALID"
    assert response.tool_calls == ()


def test_stream_without_finish_is_unknown() -> None:
    payload = _deepseek_sse(include_finish=False)
    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key",
        client=_client(lambda request: httpx.Response(200, content=payload)),
    )
    with pytest.raises(UnknownModelOutcome):
        adapter.complete(_request(), CancellationToken())


def test_known_429_is_retried_once_without_switching_provider() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(429, text="retry later")
        return httpx.Response(200, content=_deepseek_sse())

    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key", client=_client(handler)
    )
    response = adapter.complete(_request(), CancellationToken())
    assert response.finish_reason == "tool_calls"
    assert len(requests) == 2
    assert {request["model"] for request in requests} == {DEEPSEEK_CHAT.model_id}


def test_explicit_rejection_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="unauthorized")

    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key", client=_client(handler)
    )
    with pytest.raises(ModelProviderRejected):
        adapter.complete(_request(), CancellationToken())
    assert calls == 1


def test_cancellation_happens_before_the_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_deepseek_sse())

    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key", client=_client(handler)
    )
    token = CancellationToken()
    token.cancel()
    with pytest.raises(DomainError) as error:
        adapter.complete(_request(), token)
    assert error.value.code == "HARNESS_CANCELLED"
    assert calls == 0


def test_invalid_sse_json_is_a_protocol_error() -> None:
    payload = b"data: {not-json}\n\n"
    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key",
        client=_client(lambda request: httpx.Response(200, content=payload)),
    )
    with pytest.raises(ModelProtocolError):
        adapter.complete(_request(), CancellationToken())


def test_cache_hit_is_not_billed_twice(tmp_path: Path) -> None:
    class CostModel:
        def __init__(self) -> None:
            self.calls = 0

        def complete(
            self, request: ModelRequest, cancellation: CancellationToken
        ) -> ModelResponse:
            self.calls += 1
            return ModelResponse(
                usage=ModelUsage(
                    input_tokens=100, output_tokens=20, cost_micros=54
                )
            )

    inner = CostModel()
    cached = CachingModelAdapter(inner)
    runtime, ledger = _runtime(tmp_path, cached)
    first = runtime.run(_task(), _grant())
    second = runtime.run(_task(), _grant())
    assert first.status == "incomplete"
    assert second.status == "incomplete"
    assert inner.calls == 1
    assert ledger.snapshot("model-task").cost_micros == 54
    assert ledger.snapshot("model-task").model_calls == 2


@pytest.mark.parametrize(
    ("provider", "payload", "error_code"),
    [
        ("deepseek", _deepseek_sse(finish="content_filter"), "MODEL_CONTENT_FILTER"),
        (
            "anthropic",
            _anthropic_sse().replace(
                b'"stop_reason": "tool_use"', b'"stop_reason": "refusal"'
            ),
            "MODEL_REFUSAL",
        ),
    ],
)
def test_error_finish_never_executes_proposed_tools(
    tmp_path: Path, provider: str, payload: bytes, error_code: str
) -> None:
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.method)
        return httpx.Response(200, content=payload)

    adapter = adapter_for(
        CAPABILITIES[provider],
        api_key="controlled-test-key",
        client=_client(handler),
    )
    response = adapter.complete(
        _request(provider, tools=(_tool(),)), CancellationToken()
    )
    assert posts == ["POST"]
    assert response.finish_reason == "error"
    assert response.error_code == error_code
    assert response.tool_calls == ()
    assert response.usage.known is True
    runtime, ledger = _runtime(tmp_path, adapter)
    runtime.model_id = CAPABILITIES[provider].model_id
    runtime.price_version = CAPABILITIES[provider].price.version
    runtime.reservation_micros = 10000
    (tmp_path / "workspace" / "calc.py").write_text("pass\n", encoding="utf-8")
    result = runtime.run(_task(), _grant())
    assert posts == ["POST", "POST"]
    assert result.status == "incomplete"
    assert result.tool_calls == 0
    snapshot = ledger.snapshot("model-task")
    assert snapshot.tool_calls == 0
    assert snapshot.cost_micros == response.usage.cost_micros


def test_length_finish_never_executes_parallel_tool_calls(tmp_path: Path) -> None:
    parallel = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": f"call_{index}",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"calc.py"}',
                            },
                        }
                        for index in range(2)
                    ]
                }
            }
        ]
    }
    chunks = [
        parallel,
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
    ]
    payload = (
        b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        + b"data: [DONE]\n\n"
    )
    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key",
        client=_client(lambda request: httpx.Response(200, content=payload)),
    )
    response = adapter.complete(_request(tools=(_tool(),)), CancellationToken())
    assert response.finish_reason == "length"
    assert response.tool_calls == ()
    runtime, ledger = _runtime(tmp_path, adapter)
    runtime.model_id = DEEPSEEK_CHAT.model_id
    runtime.price_version = DEEPSEEK_CHAT.price.version
    runtime.reservation_micros = 10000
    (tmp_path / "workspace" / "calc.py").write_text("pass\n", encoding="utf-8")
    result = runtime.run(_task(), _grant())
    assert result.status == "incomplete"
    assert result.tool_calls == 0
    assert ledger.snapshot("model-task").tool_calls == 0


def test_timeout_without_authoritative_finish_never_executes_tools(
    tmp_path: Path,
) -> None:
    payload = _deepseek_sse(include_finish=False).replace(b"data: [DONE]\n\n", b"")

    posts: list[str] = []

    class Broken(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield payload
            raise httpx.ReadTimeout("transport lost without a finish event")

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.method)
        return httpx.Response(200, stream=Broken())

    adapter = DeepSeekChatAdapter(
        api_key="controlled-test-key",
        client=_client(handler),
    )
    response = adapter.complete(_request(tools=(_tool(),)), CancellationToken())
    assert posts == ["POST"]
    assert response.finish_reason == "error"
    assert response.error_code == "MODEL_STREAM_INTERRUPTED"
    assert response.tool_calls == ()
    assert response.usage.known is True
    runtime, ledger = _runtime(tmp_path, adapter)
    runtime.model_id = DEEPSEEK_CHAT.model_id
    runtime.price_version = DEEPSEEK_CHAT.price.version
    runtime.reservation_micros = 10000
    (tmp_path / "workspace" / "calc.py").write_text("pass\n", encoding="utf-8")
    result = runtime.run(_task(), _grant())
    assert posts == ["POST", "POST"]
    assert result.status == "incomplete"
    assert result.tool_calls == 0
    snapshot = ledger.snapshot("model-task")
    assert snapshot.tool_calls == 0
    assert snapshot.cost_micros == response.usage.cost_micros


@pytest.mark.parametrize(
    ("field", "value"),
    [("input_tokens", 100), ("output_tokens", 20)],
)
def test_anthropic_negative_usage_is_unknown_and_reservation_is_retained(
    tmp_path: Path, field: str, value: int
) -> None:
    payload = _anthropic_sse().replace(
        f'"{field}": {value}'.encode(), f'"{field}": -1'.encode()
    )
    adapter = AnthropicMessagesAdapter(
        api_key="controlled-test-key",
        client=_client(lambda request: httpx.Response(200, content=payload)),
    )
    response = adapter.complete(
        _request("anthropic", tools=(_tool(),)), CancellationToken()
    )
    assert response.usage.known is False
    runtime, ledger = _runtime(tmp_path, adapter)
    runtime.model_id = CAPABILITIES["anthropic"].model_id
    runtime.price_version = CAPABILITIES["anthropic"].price.version
    runtime.reservation_micros = 100
    (tmp_path / "workspace" / "calc.py").write_text("pass\n", encoding="utf-8")
    result = runtime.run(_task(), _grant())
    assert result.status == "unknown"
    assert result.tool_calls == 0
    snapshot = ledger.snapshot("model-task")
    assert snapshot.cost_micros == 100
    assert snapshot.tool_calls == 0
    assert [item.status for item in ledger._reservations.values()] == ["unknown"]


def _grant() -> CapabilityGrant:
    return CapabilityGrant(
        actor="owner",
        workspace_id="ws-1",
        repository="example/project",
        read_paths=("",),
        write_paths=(),
        execute=False,
    )


def _runtime(
    tmp_path: Path, model: Any
) -> tuple[HarnessRuntime, InMemoryBudgetLedger]:
    workspace = LocalWorkspace(tmp_path / "workspace", workspace_id="ws-1")
    events = InMemoryEventSink()
    ledger = InMemoryBudgetLedger(
        BudgetLimits(max_model_calls=8, max_tool_calls=8, max_cost_micros=100000)
    )
    broker = LocalExecutionBroker(workspace)
    runtime = HarnessRuntime(
        model=model,
        tools=HarnessToolRegistry(workspace, broker),
        workspace=workspace,
        broker=broker,
        context=SimpleContextManager(),
        verifier=LocalVerifier(),
        events=events,
        budgets=ledger,
        reservation_micros=100,
        max_steps=1,
    )
    return runtime, ledger
