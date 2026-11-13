"""Owner-only Source subscriptions and bounded, bodyless mailbox references."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ConfigDict, Field, ValidationError
from starlette.responses import Response

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Model
from mgar_v3.super_agent import source_subscription
from mgar_v3.super_agent.source_correlation_domain import TargetRef
from mgar_v3.super_agent.source_subscription_domain import (
    SubscriptionAckRequest,
    SubscriptionCloseRequest,
    SubscriptionDispatchRequest,
    SubscriptionHandbackRequest,
    SubscriptionPrepareRequest,
    SubscriptionRebindRequest,
    SubscriptionRenewRequest,
    SubscriptionSaveRequest,
)
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession

# FastAPI parses JSON into Python values before validation. Permit transport
# UUID/array decoding, then validate the entire strict domain request in JSON
# mode. Explicit strict scalar fields and all domain validators remain active.
TRANSPORT = ConfigDict(extra="forbid", frozen=True, strict=False)


class TargetBody(TargetRef):
    model_config = TRANSPORT
    allowed: bool = Field(strict=True)


class SaveBody(SubscriptionSaveRequest):
    model_config = TRANSPORT
    target: TargetBody


class RenewBody(SubscriptionRenewRequest):
    model_config = TRANSPORT


class RebindBody(SubscriptionRebindRequest):
    model_config = TRANSPORT
    target: TargetBody


class CloseBody(SubscriptionCloseRequest):
    model_config = TRANSPORT


class HandbackBody(SubscriptionHandbackRequest):
    model_config = TRANSPORT


class DispatchBody(SubscriptionDispatchRequest):
    model_config = TRANSPORT


class PrepareBody(SubscriptionPrepareRequest):
    model_config = TRANSPORT


class AckBody(SubscriptionAckRequest):
    model_config = TRANSPORT


def _command[Command: Model](model: type[Command], body: Model) -> Command:
    try:
        return model.model_validate_json(body.model_dump_json())
    except ValidationError:
        raise DomainError(
            "SOURCE_SUBSCRIPTION_REQUEST_INVALID", "Exact existing references are required", 422,
        ) from None


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    prefix = "/api/v3/source-subscriptions"

    @app.middleware("http")
    async def private_cache_policy(
        request: Request, call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        result = await call_next(request)
        if request.url.path == prefix or request.url.path.startswith(prefix + "/"):
            result.headers["Cache-Control"] = "no-store"
        return result

    def response(value: object) -> JSONResponse:
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    @app.post(prefix + "/subscriptions")
    def create(
        body: SaveBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_subscription.SourceSubscriptions(engine).save(
            request.cookies.get(SESSION_COOKIE, ""), uuid4(),
            _command(SubscriptionSaveRequest, body),
        ))

    @app.post(prefix + "/subscriptions/{subscription_id}/renew")
    def renew(
        subscription_id: UUID,
        body: RenewBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_subscription.SourceSubscriptions(engine).renew(
            request.cookies.get(SESSION_COOKIE, ""), subscription_id,
            _command(SubscriptionRenewRequest, body),
        ))

    @app.post(prefix + "/subscriptions/{subscription_id}/rebind")
    def rebind(
        subscription_id: UUID,
        body: RebindBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_subscription.SourceSubscriptions(engine).rebind(
            request.cookies.get(SESSION_COOKIE, ""), subscription_id,
            _command(SubscriptionRebindRequest, body),
        ))

    @app.post(prefix + "/subscriptions/{subscription_id}/unsubscribe")
    def unsubscribe(
        subscription_id: UUID,
        body: CloseBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_subscription.SourceSubscriptions(engine).close(
            request.cookies.get(SESSION_COOKIE, ""), subscription_id,
            _command(SubscriptionCloseRequest, body),
        ))

    @app.post(prefix + "/subscriptions/{subscription_id}/handback")
    def handback(
        subscription_id: UUID,
        body: HandbackBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_subscription.SourceSubscriptions(engine).handback(
            request.cookies.get(SESSION_COOKIE, ""), subscription_id,
            _command(SubscriptionHandbackRequest, body),
        ))

    @app.get(prefix + "/subscriptions/{subscription_id}")
    def get(
        subscription_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_subscription.SourceSubscriptions(engine).get(
            request.cookies.get(SESSION_COOKIE, ""), subscription_id,
        ))

    @app.get(prefix + "/sources/{source_id}/subscriptions")
    def subscriptions(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> JSONResponse:
        return response(source_subscription.SourceSubscriptions(engine).list(
            request.cookies.get(SESSION_COOKIE, ""), source_id, limit,
        ))

    @app.post(prefix + "/dispatch")
    def dispatch(
        body: DispatchBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_subscription.SourceSubscriptions(engine).dispatch(
            request.cookies.get(SESSION_COOKIE, ""),
            _command(SubscriptionDispatchRequest, body),
        ))

    @app.post(prefix + "/runs/{run_id}/prepare")
    def prepare(
        run_id: UUID,
        body: PrepareBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_subscription.SourceSubscriptions(engine).prepare(
            request.cookies.get(SESSION_COOKIE, ""), run_id,
            _command(SubscriptionPrepareRequest, body),
        ))

    @app.post(prefix + "/runs/{run_id}/ack")
    def acknowledge(
        run_id: UUID,
        body: AckBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_subscription.SourceSubscriptions(engine).acknowledge(
            request.cookies.get(SESSION_COOKIE, ""), run_id,
            _command(SubscriptionAckRequest, body),
        ))

    @app.get(prefix + "/runs/{run_id}/mailbox")
    def mailbox(
        run_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        lane: Literal["primary", "context_only", "observer"] = "primary",
        cursor: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
        limit: Annotated[int, Query(ge=1, le=16)] = 16,
    ) -> JSONResponse:
        return response(source_subscription.SourceSubscriptions(engine).mailbox(
            request.cookies.get(SESSION_COOKIE, ""), run_id, lane=lane, cursor=cursor, limit=limit,
        ))
