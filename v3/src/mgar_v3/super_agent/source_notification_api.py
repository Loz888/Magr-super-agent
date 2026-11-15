"""Eleven agreed Owner-only routes. Registration itself starts no channel or worker."""

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.responses import Response

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_notification_domain import (
    NotificationCancelRequest,
    NotificationConfigure,
    NotificationCycleRequest,
    NotificationModel,
    NotificationPlanRequest,
)
from mgar_v3.super_agent.source_notifications import SourceNotifications
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession

PREFIX = "/api/v3/source-notifications"


def _body[T: NotificationModel](model: type[T]) -> Callable[[Request], Awaitable[T]]:
    async def parse(request: Request) -> T:
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "application/json" and not (
            media.startswith("application/") and media.endswith("+json")
        ):
            raise DomainError("NOTIFICATION_REQUEST_INVALID", "Bounded JSON required", 422)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16384:
                raise DomainError("NOTIFICATION_REQUEST_INVALID", "Bounded JSON required", 422)
        try:
            return model.model_validate_json(bytes(raw))
        except ValidationError:
            raise DomainError(
                "NOTIFICATION_REQUEST_INVALID", "Exact reviewed fields required", 422
            ) from None

    return parse


def _response(value: object) -> JSONResponse:
    return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})


def _service(engine: Engine) -> SourceNotifications:
    service = getattr(engine, "source_notifications", None)
    if not isinstance(service, SourceNotifications):
        raise DomainError("NOTIFICATION_NOT_WIRED", "Formal composition is not installed", 409)
    return service


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    @app.middleware("http")
    async def no_store(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path == PREFIX or request.url.path.startswith(PREFIX + "/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get(PREFIX + "/capabilities")
    def capabilities(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(_service(engine).capabilities(request.cookies.get(SESSION_COOKIE, "")))

    @app.get(PREFIX + "/sources/{source_id}")
    def read(
        request: Request,
        source_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(_service(engine).read(request.cookies.get(SESSION_COOKIE, ""), source_id))

    @app.put(PREFIX + "/sources/{source_id}")
    def configure(
        request: Request,
        source_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[NotificationConfigure, Depends(_body(NotificationConfigure))],
    ) -> JSONResponse:
        return _response(
            _service(engine).configure(request.cookies.get(SESSION_COOKIE, ""), source_id, body)
        )

    @app.post(PREFIX + "/plans")
    def plan(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[NotificationPlanRequest, Depends(_body(NotificationPlanRequest))],
    ) -> JSONResponse:
        return _response(_service(engine).plan(request.cookies.get(SESSION_COOKIE, ""), body))

    @app.get(PREFIX + "/plans")
    def plans(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: int = 64,
    ) -> JSONResponse:
        return _response(_service(engine).plans(request.cookies.get(SESSION_COOKIE, ""), limit))

    @app.get(PREFIX + "/plans/{action_id}")
    def get(
        request: Request,
        action_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(_service(engine).get(request.cookies.get(SESSION_COOKIE, ""), action_id))

    @app.post(PREFIX + "/plans/{action_id}/cancel")
    def cancel(
        request: Request,
        action_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[NotificationCancelRequest, Depends(_body(NotificationCancelRequest))],
    ) -> JSONResponse:
        return _response(
            _service(engine).cancel(
                request.cookies.get(SESSION_COOKIE, ""), action_id, body.payload_sha256
            )
        )

    @app.post(PREFIX + "/tick")
    def tick(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[NotificationCycleRequest, Depends(_body(NotificationCycleRequest))],
    ) -> JSONResponse:
        return _response(_service(engine).tick(request.cookies.get(SESSION_COOKIE, ""), body.limit))

    @app.post(PREFIX + "/plans/{action_id}/reconcile")
    def reconcile(
        request: Request,
        action_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[NotificationCycleRequest, Depends(_body(NotificationCycleRequest))],
    ) -> JSONResponse:
        return _response(
            _service(engine).reconcile(request.cookies.get(SESSION_COOKIE, ""), action_id)
        )

    @app.post(PREFIX + "/maintain")
    def maintain(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[NotificationCycleRequest, Depends(_body(NotificationCycleRequest))],
    ) -> JSONResponse:
        return _response(
            _service(engine).maintain(request.cookies.get(SESSION_COOKIE, ""), body.limit)
        )

    @app.get(PREFIX + "/events")
    def events(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: int = 64,
    ) -> JSONResponse:
        return _response(_service(engine).events(request.cookies.get(SESSION_COOKIE, ""), limit))
