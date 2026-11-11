"""Authenticated Feishu settings and optional default-disabled callback transport."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.sources.feishu_domain import FeishuConfigure
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.sources.feishu_store import FeishuSources
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
    runtime: FeishuRuntime | None = None,
) -> None:
    prefix = "/api/v3/feishu-sources"

    def response(value: object) -> JSONResponse:
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    @app.put(prefix + "/{source_id}")
    def configure(
        source_id: UUID,
        body: FeishuConfigure,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            FeishuSources(engine).configure(
                request.cookies.get(SESSION_COOKIE, ""), source_id, body
            )
        )

    @app.get(prefix + "/{source_id}")
    def inspect(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        result = FeishuSources(engine).inspect(request.cookies.get(SESSION_COOKIE, ""), source_id)
        if (
            runtime is not None
            and runtime.source_id == source_id
            and runtime.sources.engine is engine
        ):
            result.update(runtime.status())
        return response(result)

    @app.post("/api/v3/webhooks/feishu")
    async def webhook(
        request: Request, engine: Annotated[Engine, Depends(authority)]
    ) -> JSONResponse:
        if runtime is None or runtime.sources.engine is not engine:
            raise DomainError(
                "FEISHU_RUNTIME_DISABLED", "No Feishu callback runtime is enabled", 503
            )
        headers: dict[str, str] = {}
        try:
            for raw_key, raw_value in request.headers.raw:
                key = raw_key.decode("ascii").lower()
                if key in {"x-lark-request-timestamp", "x-lark-request-nonce", "x-lark-signature"}:
                    if key in headers:
                        raise DomainError(
                            "FEISHU_DUPLICATE_HEADER", "Duplicate authentication header", 403
                        )
                    headers[key] = raw_value.decode("ascii")
        except UnicodeError:
            raise DomainError(
                "FEISHU_INVALID_HEADER", "Malformed authentication header", 403
            ) from None
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > 262144:
                raise DomainError("INGRESS_ENVELOPE_BOUND", "Raw callback exceeds 256 KiB", 413)
            payload.extend(chunk)
        try:
            return response(runtime.webhook(TransportEnvelope(headers, bytes(payload))))
        except psycopg.Error:
            raise DomainError(
                "FEISHU_STORAGE_UNAVAILABLE", "Callback is not durably committed", 503
            ) from None
