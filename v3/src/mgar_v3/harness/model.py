"""Model adapters for the Harness V4 model port.

Two wire protocols are implemented behind the same ``ModelAdapter`` contract:
OpenAI-compatible chat-completions SSE (DeepSeek) and Anthropic Messages SSE.
Tests use ``httpx.MockTransport`` and never touch a real provider.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any, Literal, cast

import httpx
from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.harness.contracts import (
    CancellationToken,
    HarnessToolCall,
    ModelAdapter,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
    ToolSpec,
)
from mgar_v3.harness.runtime import UnknownModelOutcome

MODEL_PROTOCOL_VERSION = "harness-model-v1"


class ModelPrice(Model):
    version: str = Field(min_length=1, max_length=100)
    input_micros_per_million: int = Field(gt=0)
    output_micros_per_million: int = Field(gt=0)


class ModelCapabilities(Model):
    provider: Literal["deepseek", "anthropic"]
    model_id: str = Field(min_length=1, max_length=100)
    protocol: Literal["openai-chat-sse", "anthropic-messages-sse"]
    context_tokens: int = Field(ge=4096, le=1048576)
    max_output_tokens: int = Field(ge=512, le=8192)
    supports_tools: bool = True
    supports_parallel_tools: bool = True
    supports_streaming: bool = True
    supports_structured_output: bool = False
    tokenizer: str = "conservative-chars-div-4"
    price: ModelPrice


DEEPSEEK_CHAT = ModelCapabilities(
    provider="deepseek",
    model_id="deepseek-chat",
    protocol="openai-chat-sse",
    context_tokens=131072,
    max_output_tokens=8192,
    supports_structured_output=True,
    price=ModelPrice(
        version="deepseek-2026-11",
        input_micros_per_million=300000,
        output_micros_per_million=1200000,
    ),
)

ANTHROPIC_MESSAGES = ModelCapabilities(
    provider="anthropic",
    model_id="claude-sonnet-4-5",
    protocol="anthropic-messages-sse",
    context_tokens=200000,
    max_output_tokens=8192,
    supports_structured_output=True,
    price=ModelPrice(
        version="anthropic-2026-11",
        input_micros_per_million=3000000,
        output_micros_per_million=15000000,
    ),
)

CAPABILITIES: dict[str, ModelCapabilities] = {
    "deepseek": DEEPSEEK_CHAT,
    "anthropic": ANTHROPIC_MESSAGES,
}


def estimate_tokens(text: str) -> int:
    """Conservative token estimate used before any provider request."""
    return max(1, (len(text) + 3) // 4)


def _price_usage(
    input_tokens: int,
    output_tokens: int,
    request: ModelRequest,
    capabilities: ModelCapabilities,
) -> ModelUsage:
    if input_tokens < 0 or output_tokens < 0:
        # Negative provider counters are a protocol error, not a proved spend.
        # Keep the usage unknown so the runtime retains the reservation.
        return ModelUsage(known=False)
    numerator = (
        input_tokens * capabilities.price.input_micros_per_million
        + output_tokens * capabilities.price.output_micros_per_million
    )
    cost = (numerator + 999999) // 1000000
    known = cost <= request.reservation_micros
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_micros=cost if known else 0,
        known=known,
    )


class ModelProtocolError(UnknownModelOutcome):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "MODEL_PROTOCOL_ERROR"


class ModelProviderRejected(DomainError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__("MODEL_PROVIDER_REJECTED", message, 502)
        self.status_code = status_code


class SSEDecoder:
    """Incremental SSE decoder that never splits a multi-byte UTF-8 character."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[tuple[str | None, str]]:
        self._buffer += chunk
        self._buffer = self._buffer.replace(b"\r\n", b"\n")
        events: list[tuple[str | None, str]] = []
        while b"\n\n" in self._buffer:
            block, self._buffer = self._buffer.split(b"\n\n", 1)
            event_name: str | None = None
            data_lines: list[str] = []
            for raw_line in block.split(b"\n"):
                line = raw_line.decode("utf-8")
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                events.append((event_name, "\n".join(data_lines)))
        return events

    def flush(self) -> list[tuple[str | None, str]]:
        if not self._buffer.strip():
            self._buffer = b""
            return []
        block = self._buffer
        self._buffer = b""
        try:
            return self.feed(block + b"\n\n")
        except UnicodeDecodeError as exc:
            raise ModelProtocolError("SSE stream ended with invalid UTF-8") from exc


def _sse_payloads(
    response: httpx.Response, cancellation: CancellationToken
) -> Iterator[tuple[str | None, dict[str, Any]]]:
    decoder = SSEDecoder()
    for chunk in response.iter_bytes():
        if cancellation.cancelled:
            raise UnknownModelOutcome("Cancelled after the request was sent")
        try:
            events = decoder.feed(chunk)
        except UnicodeDecodeError as exc:
            raise ModelProtocolError("SSE stream contains invalid UTF-8") from exc
        for event_name, data in events:
            if data.strip() == "[DONE]":
                return
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                raise ModelProtocolError("SSE data is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise ModelProtocolError("SSE data is not a JSON object")
            yield event_name, payload


def _message_payload(message: ModelMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.name:
        payload["name"] = message.name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            }
            for call in message.tool_calls
        ]
    return payload


class _BaseSSEAdapter:
    capabilities: ModelCapabilities

    def __init__(
        self,
        capabilities: ModelCapabilities,
        *,
        api_key: str | None,
        client: httpx.Client | None = None,
        endpoint: str,
    ) -> None:
        self.capabilities = capabilities
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=60)
        self.endpoint = endpoint

    def _validate(self, request: ModelRequest) -> None:
        if self.api_key is None:
            raise DomainError(
                "MODEL_CREDENTIAL_REQUIRED", "Model credential is missing", 403
            )
        if request.model_id != self.capabilities.model_id:
            raise DomainError(
                "MODEL_VERSION_MISMATCH", "Request model is not the frozen adapter model", 409
            )
        if request.price_version != self.capabilities.price.version:
            raise DomainError(
                "MODEL_PRICE_MISMATCH", "Request price version is not frozen", 409
            )
        if request.max_output_tokens > self.capabilities.max_output_tokens:
            raise DomainError(
                "MODEL_OUTPUT_LIMIT", "Request output exceeds the model capability", 413
            )
        encoded = json.dumps(
            {
                "messages": [message.model_dump(mode="json") for message in request.messages],
                "tools": [tool.model_dump(mode="json") for tool in request.tools],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        input_bound = estimate_tokens(encoded) + 1024
        if input_bound + request.max_output_tokens > self.capabilities.context_tokens:
            raise DomainError(
                "MODEL_CONTEXT_EXCEEDED", "Request exceeds the model context window", 413
            )

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        raise NotImplementedError

    def _parse(
        self,
        payload: dict[str, Any],
        request: ModelRequest,
        state: dict[str, int | None],
    ) -> list[ModelStreamEvent]:
        raise NotImplementedError

    def _merge_usage(
        self,
        current: ModelUsage | None,
        event_usage: ModelUsage | None,
        request: ModelRequest,
    ) -> ModelUsage | None:
        if event_usage is None:
            return current
        if event_usage.known is False:
            return ModelUsage(known=False)
        if current is not None and current.known is False:
            return current
        input_tokens = event_usage.input_tokens
        output_tokens = event_usage.output_tokens
        if current is not None:
            input_tokens = max(input_tokens, current.input_tokens)
            output_tokens = max(output_tokens, current.output_tokens)
        return _price_usage(input_tokens, output_tokens, request, self.capabilities)

    def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> Iterator[ModelStreamEvent]:
        self._validate(request)
        body = self._body(request)
        headers = self._headers()
        state: dict[str, int | None] = {"input_tokens": None, "output_tokens": None}
        for attempt in range(2):
            cancellation.ensure_active()
            try:
                with self.client.stream(
                    "POST", self.endpoint, json=body, headers=headers
                ) as response:
                    if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                        response.read()
                        continue
                    if not response.is_success:
                        snippet = response.read().decode("utf-8", "replace")[:300]
                        raise ModelProviderRejected(
                            response.status_code,
                            f"Model provider rejected the request: {snippet}",
                        )
                    finish_seen = False
                    for _event_name, payload in _sse_payloads(response, cancellation):
                        for event in self._parse(payload, request, state):
                            if event.type == "finish":
                                finish_seen = True
                            yield event
                    if not finish_seen:
                        raise UnknownModelOutcome(
                            "Model stream ended without a finish event"
                        )
                    return
            except httpx.HTTPError:
                # A response may have reached the provider even if no SSE event
                # arrived; never turn this into a second POST. The error finish
                # carries unknown usage so the runtime keeps the reservation.
                yield ModelStreamEvent(
                    type="finish",
                    finish_reason="error",
                    error_code="MODEL_STREAM_INTERRUPTED",
                )
                return
        raise UnknownModelOutcome("Model stream ended without a response")

    def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        text_parts: list[str] = []
        tool_buffers: dict[int, dict[str, str]] = {}
        usage: ModelUsage | None = None
        finish: str | None = None
        error_code = ""
        for event in self.stream(request, cancellation):
            if event.type == "text_delta":
                text_parts.append(event.text)
            elif event.type == "tool_call_start":
                buffer = tool_buffers.setdefault(event.tool_index, {})
                if event.tool_call_id:
                    buffer["id"] = event.tool_call_id
                if event.tool_name:
                    buffer["name"] = event.tool_name
            elif event.type == "tool_call_delta":
                buffer = tool_buffers.setdefault(event.tool_index, {})
                buffer["arguments"] = buffer.get("arguments", "") + event.arguments_delta
            elif event.type == "usage":
                usage = self._merge_usage(usage, event.usage, request)
            elif event.type == "finish":
                finish = event.finish_reason
                if event.error_code:
                    error_code = event.error_code
        if finish is None:
            raise UnknownModelOutcome("Model stream did not produce a finish reason")
        calls: list[HarnessToolCall] = []
        for index in sorted(tool_buffers):
            buffer = tool_buffers[index]
            name = buffer.get("name", "")
            raw = buffer.get("arguments", "") or "{}"
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                return self._response(
                    text_parts,
                    (),
                    "error",
                    usage,
                    request,
                    error_code="MODEL_TOOL_ARGUMENTS_INVALID",
                )
            if not isinstance(arguments, dict) or not name:
                return self._response(
                    text_parts,
                    (),
                    "error",
                    usage,
                    request,
                    error_code="MODEL_TOOL_ARGUMENTS_INVALID",
                )
            calls.append(
                HarnessToolCall(
                    call_id=buffer.get("id") or f"call-{index}",
                    name=name,
                    arguments=cast(dict[str, Any], arguments),
                )
            )
        if finish != "tool_calls":
            # Only an authoritative tool_calls finish makes buffered proposals
            # executable; error/length/stop responses must never dispatch them.
            calls = []
        if finish == "tool_calls" and not calls:
            error_code = "MODEL_TOOL_CALL_MISSING"
        if finish == "error" and not error_code:
            error_code = "MODEL_FINISH_ERROR"
        return self._response(
            text_parts,
            tuple(calls),
            cast(Any, "error" if error_code else finish),
            usage,
            request,
            error_code=error_code,
        )

    def _response(
        self,
        text_parts: Sequence[str],
        calls: tuple[HarnessToolCall, ...],
        finish: str,
        usage: ModelUsage | None,
        request: ModelRequest,
        *,
        error_code: str = "",
    ) -> ModelResponse:
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=calls,
            finish_reason=cast(Any, finish),
            usage=usage or ModelUsage(known=False),
            provider=self.capabilities.provider,
            model_id=self.capabilities.model_id,
            price_version=self.capabilities.price.version,
            error_code=error_code,
        )


class DeepSeekChatAdapter(_BaseSSEAdapter):
    def __init__(
        self,
        *,
        api_key: str | None,
        client: httpx.Client | None = None,
        capabilities: ModelCapabilities = DEEPSEEK_CHAT,
        endpoint: str = "https://api.deepseek.com/v1/chat/completions",
    ) -> None:
        super().__init__(
            capabilities, api_key=api_key, client=client, endpoint=endpoint
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + cast(str, self.api_key),
            "Content-Type": "application/json",
        }

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.capabilities.model_id,
            "max_tokens": request.max_output_tokens,
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [_message_payload(message) for message in request.messages],
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
        return body

    def _parse(
        self,
        payload: dict[str, Any],
        request: ModelRequest,
        state: dict[str, int | None],
    ) -> list[ModelStreamEvent]:
        events: list[ModelStreamEvent] = []
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, dict):
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        events.append(ModelStreamEvent(type="text_delta", text=content))
                    tool_calls = delta.get("tool_calls")
                    if isinstance(tool_calls, list):
                        for item in tool_calls:
                            if not isinstance(item, dict):
                                continue
                            index = item.get("index", 0)
                            if not isinstance(index, int):
                                index = 0
                            function = item.get("function")
                            name = ""
                            arguments = ""
                            if isinstance(function, dict):
                                raw_name = function.get("name")
                                raw_args = function.get("arguments")
                                name = raw_name if isinstance(raw_name, str) else ""
                                arguments = raw_args if isinstance(raw_args, str) else ""
                            call_id = item.get("id")
                            events.append(
                                ModelStreamEvent(
                                    type="tool_call_start",
                                    tool_index=index,
                                    tool_call_id=call_id if isinstance(call_id, str) else None,
                                    tool_name=name or None,
                                )
                            )
                            if arguments:
                                events.append(
                                    ModelStreamEvent(
                                        type="tool_call_delta",
                                        tool_index=index,
                                        arguments_delta=arguments,
                                    )
                                )
                finish = choice.get("finish_reason")
                if isinstance(finish, str):
                    mapped = {
                        "stop": "stop",
                        "tool_calls": "tool_calls",
                        "length": "length",
                        "content_filter": "error",
                    }.get(finish, "error")
                    events.append(
                        ModelStreamEvent(
                            type="finish",
                            finish_reason=cast(Any, mapped),
                            error_code=(
                                ""
                                if finish in {"stop", "tool_calls", "length"}
                                else "MODEL_CONTENT_FILTER"
                            ),
                        )
                    )
        usage = payload.get("usage")
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
                events.append(
                    ModelStreamEvent(
                        type="usage",
                        usage=_price_usage(prompt, completion, request, self.capabilities),
                    )
                )
            else:
                events.append(
                    ModelStreamEvent(type="usage", usage=ModelUsage(known=False))
                )
        return events


class AnthropicMessagesAdapter(_BaseSSEAdapter):
    def __init__(
        self,
        *,
        api_key: str | None,
        client: httpx.Client | None = None,
        capabilities: ModelCapabilities = ANTHROPIC_MESSAGES,
        endpoint: str = "https://api.anthropic.com/v1/messages",
        api_version: str = "2023-06-01",
    ) -> None:
        super().__init__(
            capabilities, api_key=api_key, client=client, endpoint=endpoint
        )
        self.api_version = api_version

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": cast(str, self.api_key),
            "anthropic-version": self.api_version,
            "Content-Type": "application/json",
        }

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        system = "\n".join(
            message.content for message in request.messages if message.role == "system"
        )
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content,
                }
                previous = messages[-1] if messages else None
                if (
                    previous is not None
                    and previous.get("role") == "user"
                    and isinstance(previous.get("content"), list)
                    and any(
                        isinstance(item, dict) and item.get("type") == "tool_result"
                        for item in previous["content"]
                    )
                ):
                    previous["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
            elif message.role == "assistant" and message.tool_calls:
                content: list[dict[str, Any]] = []
                if message.content:
                    content.append({"type": "text", "text": message.content})
                content.extend(
                    {
                        "type": "tool_use",
                        "id": call.call_id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                    for call in message.tool_calls
                )
                messages.append({"role": "assistant", "content": content})
            else:
                messages.append(
                    {"role": message.role, "content": message.content}
                )
        body: dict[str, Any] = {
            "model": self.capabilities.model_id,
            "max_tokens": request.max_output_tokens,
            "stream": True,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if request.tools:
            body["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in request.tools
            ]
        return body

    def _parse(
        self,
        payload: dict[str, Any],
        request: ModelRequest,
        state: dict[str, int | None],
    ) -> list[ModelStreamEvent]:
        event_type = payload.get("type")
        events: list[ModelStreamEvent] = []
        if event_type == "message_start":
            message = payload.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if (
                isinstance(usage, dict)
                and type(usage.get("input_tokens")) is int
                and cast(int, usage["input_tokens"]) >= 0
            ):
                state["input_tokens"] = cast(int, usage["input_tokens"])
        elif event_type == "content_block_start":
            index = payload.get("index", 0)
            block = payload.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                events.append(
                    ModelStreamEvent(
                        type="tool_call_start",
                        tool_index=index if isinstance(index, int) else 0,
                        tool_call_id=block.get("id")
                        if isinstance(block.get("id"), str)
                        else None,
                        tool_name=block.get("name")
                        if isinstance(block.get("name"), str)
                        else None,
                    )
                )
        elif event_type == "content_block_delta":
            index = payload.get("index", 0)
            delta = payload.get("delta")
            if isinstance(delta, dict):
                if delta.get("type") == "text_delta" and isinstance(
                    delta.get("text"), str
                ):
                    events.append(
                        ModelStreamEvent(type="text_delta", text=cast(str, delta["text"]))
                    )
                elif delta.get("type") == "input_json_delta" and isinstance(
                    delta.get("partial_json"), str
                ):
                    events.append(
                        ModelStreamEvent(
                            type="tool_call_delta",
                            tool_index=index if isinstance(index, int) else 0,
                            arguments_delta=cast(str, delta["partial_json"]),
                        )
                    )
        elif event_type == "message_delta":
            delta = payload.get("delta")
            stop_reason = delta.get("stop_reason") if isinstance(delta, dict) else None
            if not isinstance(stop_reason, str):
                stop_reason = ""
            mapped = {
                "end_turn": "stop",
                "tool_use": "tool_calls",
                "max_tokens": "length",
                "stop_sequence": "stop",
                "refusal": "error",
            }.get(stop_reason, "error")
            events.append(
                ModelStreamEvent(
                    type="finish",
                    finish_reason=cast(Any, mapped),
                    error_code="" if mapped != "error" else "MODEL_REFUSAL",
                )
            )
            usage = payload.get("usage")
            if (
                isinstance(usage, dict)
                and type(usage.get("output_tokens")) is int
                and cast(int, usage["output_tokens"]) >= 0
            ):
                state["output_tokens"] = cast(int, usage["output_tokens"])
            input_tokens = state["input_tokens"]
            output_tokens = state["output_tokens"]
            if input_tokens is not None and output_tokens is not None:
                events.append(
                    ModelStreamEvent(
                        type="usage",
                        usage=_price_usage(
                            input_tokens,
                            output_tokens,
                            request,
                            self.capabilities,
                        ),
                    )
                )
            else:
                events.append(
                    ModelStreamEvent(type="usage", usage=ModelUsage(known=False))
                )
        return events


class CachingModelAdapter:
    """Cache known, successful model results so a retry is not billed twice."""

    def __init__(self, inner: ModelAdapter) -> None:
        self.inner = inner
        self._cache: dict[str, ModelResponse] = {}

    def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        key = digest(
            {
                "model_id": request.model_id,
                "price_version": request.price_version,
                "max_output_tokens": request.max_output_tokens,
                "messages": [
                    message.model_dump(mode="json") for message in request.messages
                ],
                "tools": [tool.model_dump(mode="json") for tool in request.tools],
            }
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached.model_copy(
                update={"cached": True, "usage": ModelUsage(known=True)}
            )
        response = self.inner.complete(request, cancellation)
        if response.usage.known and response.finish_reason != "error":
            self._cache[key] = response
        return response


def adapter_for(
    capabilities: ModelCapabilities,
    *,
    api_key: str | None,
    client: httpx.Client | None = None,
) -> _BaseSSEAdapter:
    if capabilities.protocol == "openai-chat-sse":
        return DeepSeekChatAdapter(
            api_key=api_key, client=client, capabilities=capabilities
        )
    return AnthropicMessagesAdapter(
        api_key=api_key, client=client, capabilities=capabilities
    )


def tool_specs(values: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [value.model_dump(mode="json") for value in values]
