"""Single-attempt bounded model port for the explicitly authorized SA22 trial."""

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from time import monotonic
from typing import Any

import httpx

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import (
    CancellationToken,
    HarnessToolCall,
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
    ):
        if (
            not api_key
            or client.event_hooks.get("request")
            or isinstance(client._transport, httpx.MockTransport) is not controlled_transport
            or not 4096 <= context_tokens <= 65536
        ):
            raise ValueError("Explicit bounded experimental model transport required")
        self.client, self.api_key, self.price = client, api_key, price
        self.context_tokens = context_tokens

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
            "thinking": {"type": "disabled"},
            "temperature": 0,
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
        if len(json.dumps(body, ensure_ascii=False).encode()) + 1024 > self.context_tokens:
            raise DomainError("SOURCE_CODING_MODEL_INPUT_BOUND", "Model input bound exceeded", 413)
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
            calls = []
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
            return ModelResponse(
                text=text,
                tool_calls=tuple(calls),
                finish_reason=choice["finish_reason"],
                usage=bill,
                provider="deepseek",
                model_id=self.price.model_id,
                price_version=self.price.price_version,
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
