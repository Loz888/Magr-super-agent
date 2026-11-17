"""Single-attempt bounded model port for the explicitly authorized SA22 trial."""

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from time import monotonic
from typing import Any, Literal

import httpx
from pydantic import Field

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.contracts import (
    CancellationToken,
    HarnessToolCall,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from mgar_v3.harness.model import _message_payload
from mgar_v3.super_agent.budget_domain import PriceQuote
from mgar_v3.super_agent.source_coding_runtime import ExperimentalCodingModel
from mgar_v3.super_agent.source_model_json import decode_model_json

ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
_SEND_GUARD: ContextVar[Callable[[], AbstractContextManager[Any]] | None] = ContextVar(
    "sa22_trial_model_send_guard", default=None
)
_REASONING: ContextVar[tuple[tuple[str, str], ...]] = ContextVar(
    "sa22_trial_reasoning_history", default=()
)


class TrialModelPreflightError(DomainError):
    """Proves this adapter rejected locally before invoking physical HTTP."""


class TrialModelResponse(ModelResponse):
    # Kept only in the existing encrypted, scoped, expiring model-response journal.
    reasoning_content: str = Field(default="", max_length=262144, repr=False)


def restore_trial_response(raw: bytes) -> ModelResponse:
    value = decode_model_json(raw)
    model = TrialModelResponse if "reasoning_content" in value else ModelResponse
    return model.model_validate(value)


def reasoning_key(message: ModelMessage) -> str:
    return digest(
        {
            "content": message.content,
            "tool_calls": [call.model_dump(mode="json") for call in message.tool_calls],
        }
    )


@contextmanager
def trial_reasoning_history(replies: tuple[tuple[str, str], ...]) -> Iterator[None]:
    token = _REASONING.set(replies)
    try:
        yield
    finally:
        _REASONING.reset(token)


@contextmanager
def guard_trial_model_send(guard: Callable[[], AbstractContextManager[Any]]) -> Iterator[None]:
    token = _SEND_GUARD.set(guard)
    try:
        yield
    finally:
        _SEND_GUARD.reset(token)


class ExperimentalDeepSeekModel(ExperimentalCodingModel):
    def __init__(
        self,
        client: httpx.Client,
        api_key: str,
        price: PriceQuote,
        *,
        context_tokens: int = 32768,
        controlled_transport: bool = False,
        thinking_mode: Literal["enabled", "disabled"] = "disabled",
        reasoning_effort: Literal["low", "high", "max"] = "max",
    ):
        if (
            not api_key
            or client.event_hooks.get("request")
            or isinstance(client._transport, httpx.MockTransport) is not controlled_transport
            or not 4096 <= context_tokens <= 262144
            or thinking_mode not in {"enabled", "disabled"}
            or reasoning_effort not in {"low", "high", "max"}
        ):
            raise ValueError("Explicit bounded experimental model transport required")
        self.client, self.api_key, self.price = client, api_key, price
        self.context_tokens = context_tokens
        self.thinking_mode, self.reasoning_effort = thinking_mode, reasoning_effort

    def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        cancellation.ensure_active()
        if (
            request.model_id != self.price.model_id
            or request.price_version != self.price.price_version
            or self.client.event_hooks.get("request")
        ):
            raise DomainError("SOURCE_CODING_MODEL_PROFILE", "Frozen model profile required", 403)
        body: dict[str, Any] = {
            "model": self.price.model_id,
            "max_tokens": request.max_output_tokens,
            "stream": False,
            "thinking": {"type": self.thinking_mode},
            "messages": [_message_payload(message) for message in request.messages],
        }
        if self.thinking_mode == "enabled":
            body["reasoning_effort"] = self.reasoning_effort
            if request.tools:
                replies = iter(_REASONING.get())
                for original, wire in zip(request.messages, body["messages"], strict=True):
                    if original.role != "assistant":
                        continue
                    key = reasoning_key(original)
                    for prior_key, reasoning in replies:
                        if prior_key == key:
                            wire["reasoning_content"] = reasoning
                            break
                    else:
                        raise TrialModelPreflightError(
                            "SOURCE_CODING_REASONING_HISTORY_REQUIRED",
                            "Exact scoped thinking history required before HTTP",
                            403,
                        )
        else:
            body["temperature"] = 0
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
        if len(json.dumps(body, ensure_ascii=False).encode()) + 1024 > self.context_tokens:
            raise TrialModelPreflightError(
                "SOURCE_CODING_MODEL_INPUT_BOUND", "Model input bound exceeded", 413
            )
        # The original Coding journal and usage reservation already committed.
        # The caller holds the original final authority guard at this entry.
        raw = bytearray()
        response = None
        started = monotonic()
        try:
            guard = _SEND_GUARD.get()
            if guard is None:
                raise DomainError(
                    "SOURCE_CODING_MODEL_GUARD_REQUIRED", "Final authority required", 403
                )
            with guard():
                response = self.client.send(
                    self.client.build_request(
                        "POST",
                        ENDPOINT,
                        json=body,
                        timeout=60,
                        headers={"Authorization": "Bearer " + self.api_key},
                    ),
                    stream=True,
                    follow_redirects=False,
                    auth=None,
                )
            status = response.status_code
            for block in response.iter_bytes():
                raw.extend(block)
                if len(raw) > 262144 or monotonic() - started > 60:
                    raise ValueError
            payload = decode_model_json(raw)
            usage = payload["usage"]
            counts = [usage["prompt_tokens"], usage["completion_tokens"]]
            if any(type(value) is not int or value < 0 for value in counts):
                raise ValueError
            cost = (
                counts[0] * self.price.input_micros_per_million
                + counts[1] * self.price.output_micros_per_million
                + 999999
            ) // 1000000
            if cost > request.reservation_micros:
                raise ValueError
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return ModelResponse(
                finish_reason="error",
                error_code="EXPERIMENTAL_PROVIDER_USAGE_UNKNOWN",
                usage=ModelUsage(known=False),
                provider="deepseek",
                model_id=self.price.model_id,
                price_version=self.price.price_version,
            )
        finally:
            if response is not None:
                response.close()
        bill = ModelUsage(input_tokens=counts[0], output_tokens=counts[1], cost_micros=cost)
        try:
            choice = payload["choices"][0]
            message = choice["message"]
            if status != 200 or choice["finish_reason"] not in {"stop", "tool_calls", "length"}:
                raise ValueError
            text = message.get("content") or ""
            if type(text) is not str or len(text.encode()) > 65536:
                raise ValueError
            calls: list[HarnessToolCall] = []
            for call in message.get("tool_calls") or []:
                if len(calls) >= 16 or call["type"] != "function":
                    raise ValueError
                arguments = decode_model_json(call["function"]["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError
                calls.append(
                    HarnessToolCall(
                        call_id=call["id"], name=call["function"]["name"], arguments=arguments
                    )
                )
            reasoning = message.get("reasoning_content", "")
            if not isinstance(reasoning, str):
                raise ValueError
            response_type = TrialModelResponse if self.thinking_mode == "enabled" else ModelResponse
            extra = {"reasoning_content": reasoning} if self.thinking_mode == "enabled" else {}
            return response_type(
                text=text,
                tool_calls=tuple(calls),
                finish_reason=choice["finish_reason"],
                usage=bill,
                provider="deepseek",
                model_id=self.price.model_id,
                price_version=self.price.price_version,
                **extra,
            )
        except (ValueError, KeyError, TypeError, IndexError):
            return ModelResponse(
                finish_reason="error",
                error_code="EXPERIMENTAL_PROVIDER_OUTPUT_INVALID",
                usage=bill,
                provider="deepseek",
                model_id=self.price.model_id,
                price_version=self.price.price_version,
            )
