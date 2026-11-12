"""Owner-only, bodyless Source relationships; HTTP cannot create or execute tasks."""

from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ConfigDict, Field, ValidationError
from starlette.responses import Response

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.super_agent import source_correlation
from mgar_v3.super_agent.source_correlation_domain import (
    CorrelationCorrectRequest,
    CorrelationLinkRequest,
    CorrelationModel,
    CorrelationRemoveRequest,
    CorrelationResolveRequest,
    TargetRef,
)
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession

# FastAPI validates parsed JSON in Python mode. Only the transport UUID/array
# coercions differ here; explicit strict scalar fields and all domain validators
# remain. Re-decode the complete domain request in JSON mode before dispatch.
TRANSPORT = ConfigDict(extra="forbid", frozen=True, strict=False)
Command = TypeVar("Command", bound=CorrelationModel)


class TargetBody(TargetRef):
    model_config = TRANSPORT
    allowed: bool = Field(strict=True)


class ResolveBody(CorrelationResolveRequest):
    model_config = TRANSPORT


class LinkBody(CorrelationLinkRequest):
    model_config = TRANSPORT
    target: TargetBody


class CorrectBody(CorrelationCorrectRequest):
    model_config = TRANSPORT
    target: TargetBody


class RemoveBody(CorrelationRemoveRequest):
    model_config = TRANSPORT


def _command(model: type[Command], body: CorrelationModel) -> Command:
    try:
        return model.model_validate_json(body.model_dump_json())
    except ValidationError:
        raise DomainError(
            "SOURCE_CORRELATION_REQUEST_INVALID", "Exact existing references are required", 422,
        ) from None


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    prefix = "/api/v3/source-correlations"

    @app.middleware("http")
    async def private_cache_policy(
        request: Request, call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        result = await call_next(request)
        if request.url.path == prefix or request.url.path.startswith(prefix + "/"):
            # Include handled validation/auth/conflict errors without changing
            # global handlers or the caching policy of any other API prefix.
            result.headers["Cache-Control"] = "no-store"
        return result

    def response(value: object) -> JSONResponse:
        return JSONResponse(
            jsonable_encoder(value), headers={"Cache-Control": "no-store"},
        )

    @app.post(prefix + "/resolve")
    def resolve(
        body: ResolveBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_correlation.SourceCorrelations(engine).resolve(
            request.cookies.get(SESSION_COOKIE, ""),
            _command(CorrelationResolveRequest, body),
        ))

    @app.post(prefix + "/links")
    def link(
        body: LinkBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_correlation.SourceCorrelations(engine).link(
            request.cookies.get(SESSION_COOKIE, ""),
            _command(CorrelationLinkRequest, body),
        ))

    @app.post(prefix + "/links/{correlation_id}/correct")
    def correct(
        correlation_id: UUID,
        body: CorrectBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_correlation.SourceCorrelations(engine).correct(
            request.cookies.get(SESSION_COOKIE, ""), correlation_id,
            _command(CorrelationCorrectRequest, body),
        ))

    @app.post(prefix + "/links/{correlation_id}/remove")
    def remove(
        correlation_id: UUID,
        body: RemoveBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_correlation.SourceCorrelations(engine).remove(
            request.cookies.get(SESSION_COOKIE, ""), correlation_id,
            _command(CorrelationRemoveRequest, body),
        ))

    @app.get(prefix + "/links/{correlation_id}")
    def get(
        correlation_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_correlation.SourceCorrelations(engine).get(
            request.cookies.get(SESSION_COOKIE, ""), correlation_id,
        ))

    @app.get(prefix + "/inputs/{input_id}/links")
    def links(
        input_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> JSONResponse:
        return response(source_correlation.SourceCorrelations(engine).links(
            request.cookies.get(SESSION_COOKIE, ""), input_id, limit,
        ))
