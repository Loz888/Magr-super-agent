"""The thirteen agreed current-Owner controls; no executor/provider injection."""

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
from mgar_v3.super_agent.source_autonomy_domain import (
    AutonomyCycleRequest,
    AutonomyModel,
    AutonomyReopenRequest,
    AutonomyRevisionRequest,
    AutonomyRuleSave,
)
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession

PREFIX = "/api/v3/source-autonomy"


def _body[T: AutonomyModel](model: type[T]) -> Callable[[Request], Awaitable[T]]:
    async def parse(request: Request) -> T:
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "application/json" and not (
            media.startswith("application/") and media.endswith("+json")
        ):
            raise DomainError("AUTONOMY_REQUEST_INVALID", "Bounded JSON required", 422)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16384:
                raise DomainError("AUTONOMY_REQUEST_INVALID", "Bounded JSON required", 422)
        try:
            return model.model_validate_json(bytes(raw))
        except ValidationError:
            raise DomainError(
                "AUTONOMY_REQUEST_INVALID", "Exact reviewed fields required", 422
            ) from None

    return parse


def _response(value: object) -> JSONResponse:
    return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    @app.middleware("http")
    async def no_store(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        result = await call_next(request)
        if request.url.path == PREFIX or request.url.path.startswith(PREFIX + "/"):
            result.headers["Cache-Control"] = "no-store"
        return result

    @app.get(PREFIX + "/capabilities")
    def capabilities(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(
            engine.source_autonomy.capabilities(request.cookies.get(SESSION_COOKIE, ""))
        )

    @app.get(PREFIX + "/rules")
    def rules(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: int = 64,
    ) -> JSONResponse:
        return _response(
            engine.source_autonomy.rules(request.cookies.get(SESSION_COOKIE, ""), limit)
        )

    @app.put(PREFIX + "/rules/{rule_id}")
    def save_rule(
        request: Request,
        rule_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[AutonomyRuleSave, Depends(_body(AutonomyRuleSave))],
    ) -> JSONResponse:
        return _response(
            engine.source_autonomy.save_rule(request.cookies.get(SESSION_COOKIE, ""), rule_id, body)
        )

    @app.get(PREFIX + "/rules/{rule_id}")
    def get_rule(
        request: Request,
        rule_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(
            engine.source_autonomy.get_rule(request.cookies.get(SESSION_COOKIE, ""), rule_id)
        )

    @app.post(PREFIX + "/rules/{rule_id}/disable")
    def disable(
        request: Request,
        rule_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[AutonomyRevisionRequest, Depends(_body(AutonomyRevisionRequest))],
    ) -> JSONResponse:
        return _response(
            engine.source_autonomy.disable_rule(
                request.cookies.get(SESSION_COOKIE, ""), rule_id, body
            )
        )

    @app.get(PREFIX + "/claims")
    def claims(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: int = 64,
    ) -> JSONResponse:
        return _response(
            engine.source_autonomy.claims(request.cookies.get(SESSION_COOKIE, ""), limit)
        )

    @app.get(PREFIX + "/attempts")
    def attempts(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: int = 64,
    ) -> JSONResponse:
        return _response(
            engine.source_autonomy.attempts(request.cookies.get(SESSION_COOKIE, ""), limit)
        )

    @app.get(PREFIX + "/attempts/{attempt_id}")
    def get_attempt(
        request: Request,
        attempt_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(
            engine.source_autonomy.get_attempt(request.cookies.get(SESSION_COOKIE, ""), attempt_id)
        )

    @app.post(PREFIX + "/attempts/{attempt_id}/stop")
    def stop(
        request: Request,
        attempt_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[AutonomyRevisionRequest, Depends(_body(AutonomyRevisionRequest))],
    ) -> JSONResponse:
        return _response(
            engine.source_autonomy.stop(request.cookies.get(SESSION_COOKIE, ""), attempt_id, body)
        )

    @app.post(PREFIX + "/attempts/{attempt_id}/reopen")
    def reopen(
        request: Request,
        attempt_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[AutonomyReopenRequest, Depends(_body(AutonomyReopenRequest))],
    ) -> JSONResponse:
        return _response(
            engine.source_autonomy.reopen(request.cookies.get(SESSION_COOKIE, ""), attempt_id, body)
        )

    @app.post(PREFIX + "/maintain")
    def maintain(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[AutonomyCycleRequest, Depends(_body(AutonomyCycleRequest))],
    ) -> JSONResponse:
        return _response(
            engine.source_autonomy.maintain(request.cookies.get(SESSION_COOKIE, ""), body)
        )

    @app.post(PREFIX + "/tick")
    def tick(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[AutonomyCycleRequest, Depends(_body(AutonomyCycleRequest))],
    ) -> JSONResponse:
        return _response(engine.source_autonomy.tick(request.cookies.get(SESSION_COOKIE, ""), body))

    @app.post(PREFIX + "/cycle")
    def cycle(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[AutonomyCycleRequest, Depends(_body(AutonomyCycleRequest))],
    ) -> JSONResponse:
        return _response(
            engine.source_autonomy.cycle(request.cookies.get(SESSION_COOKIE, ""), body)
        )
