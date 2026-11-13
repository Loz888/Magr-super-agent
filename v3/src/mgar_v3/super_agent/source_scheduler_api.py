"""Strict Scheduler metadata HTTP; owner cookies and independent Worker bearers."""

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.responses import Response

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.super_agent import source_scheduler
from mgar_v3.super_agent.source_scheduler_domain import (
    SchedulerClaimRequest,
    SchedulerConfigureRequest,
    SchedulerEnqueueRequest,
    SchedulerHeartbeatRequest,
    SchedulerModel,
    SchedulerOwnerDrainRequest,
    SchedulerQueueCancelRequest,
    SchedulerReadyRequest,
    SchedulerReconcileRequest,
    SchedulerReportRequest,
    SchedulerSessionRegisterRequest,
    SchedulerSessionRevokeRequest,
)
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession

PREFIX = "/api/v3/source-scheduler"


def _invalid() -> DomainError:
    return DomainError(
        "SOURCE_SCHEDULER_REQUEST_INVALID", "Exact scheduling metadata is required", 422,
    )


def _body[Command: SchedulerModel](model: type[Command]) -> Callable[[Request], Awaitable[Command]]:
    async def parse(request: Request) -> Command:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json" and not (
            media_type.startswith("application/") and media_type.endswith("+json")
        ):
            raise _invalid()
        try:
            # Validate the original JSON once. Python-mode transport coercions
            # must not turn strings/bools into strict domain numbers or flags.
            return model.model_validate_json(await request.body())
        except ValidationError:
            raise _invalid() from None

    return parse


def _same_reference(path_id: UUID, command_id: UUID) -> None:
    if path_id != command_id:
        raise _invalid()


def _worker_bearer(request: Request) -> str:
    values = request.headers.getlist("authorization")
    if len(values) == 1:
        scheme, separator, token = values[0].partition(" ")
        if (
            separator
            and scheme.casefold() == "bearer"
            and 1 <= len(token) <= 512
            and all(33 <= ord(character) <= 126 for character in token)
        ):
            return token
    raise DomainError(
        "SOURCE_SCHEDULER_WORKER_AUTH_REQUIRED", "A current Worker session bearer is required", 401,
    )


def _service(engine: Engine) -> source_scheduler.SourceScheduler:
    # Preserve the original Engine's trusted driver/profile injection. Creating
    # a new Scheduler per HTTP call would discard that server-owned boundary.
    return engine.source_scheduler


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    @app.middleware("http")
    async def private_cache_policy(
        request: Request, call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        result = await call_next(request)
        if request.url.path == PREFIX or request.url.path.startswith(PREFIX + "/"):
            result.headers["Cache-Control"] = "no-store"
        return result

    def response(value: object) -> JSONResponse:
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    @app.put(PREFIX + "/config")
    def configure(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[SchedulerConfigureRequest, Depends(_body(SchedulerConfigureRequest))],
    ) -> JSONResponse:
        return response(_service(engine).configure(request.cookies.get(SESSION_COOKIE, ""), body))

    @app.get(PREFIX + "/state")
    def state(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        run_id: UUID | None = None,
    ) -> JSONResponse:
        service, cookie = _service(engine), request.cookies.get(SESSION_COOKIE, "")
        return response({
            "capacity": service.capacity(cookie),
            "target": service.target(cookie, run_id) if run_id is not None else None,
        })

    @app.post(PREFIX + "/queue")
    def enqueue(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[SchedulerEnqueueRequest, Depends(_body(SchedulerEnqueueRequest))],
    ) -> JSONResponse:
        return response(_service(engine).enqueue(request.cookies.get(SESSION_COOKIE, ""), body))

    @app.get(PREFIX + "/queue")
    def queue(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> JSONResponse:
        return response(_service(engine).queue(
            request.cookies.get(SESSION_COOKIE, ""), limit=limit,
        ))

    @app.post(PREFIX + "/queue/{queue_id}/cancel")
    def cancel_queue(
        queue_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[SchedulerQueueCancelRequest, Depends(_body(SchedulerQueueCancelRequest))],
    ) -> JSONResponse:
        _same_reference(queue_id, body.queue_id)
        return response(_service(engine).cancel_queue(
            request.cookies.get(SESSION_COOKIE, ""), body,
        ))

    @app.post(PREFIX + "/sessions")
    def register_session(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[
            SchedulerSessionRegisterRequest, Depends(_body(SchedulerSessionRegisterRequest)),
        ],
    ) -> JSONResponse:
        return response(_service(engine).register_session(
            request.cookies.get(SESSION_COOKIE, ""), body,
        ))

    @app.post(PREFIX + "/sessions/{session_id}/revoke")
    def revoke_session(
        session_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[
            SchedulerSessionRevokeRequest, Depends(_body(SchedulerSessionRevokeRequest)),
        ],
    ) -> JSONResponse:
        _same_reference(session_id, body.session_id)
        return response(_service(engine).revoke_session(
            request.cookies.get(SESSION_COOKIE, ""), body,
        ))

    @app.get(PREFIX + "/assignments")
    def assignments(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> JSONResponse:
        return response(_service(engine).assignments(
            request.cookies.get(SESSION_COOKIE, ""), limit=limit,
        ))

    @app.get(PREFIX + "/assignments/{assignment_id}")
    def get_assignment(
        assignment_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(_service(engine).get_assignment(
            request.cookies.get(SESSION_COOKIE, ""), assignment_id,
        ))

    @app.post(PREFIX + "/assignments/{assignment_id}/drain")
    def drain(
        assignment_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[SchedulerOwnerDrainRequest, Depends(_body(SchedulerOwnerDrainRequest))],
    ) -> JSONResponse:
        _same_reference(assignment_id, body.assignment_id)
        return response(_service(engine).drain(request.cookies.get(SESSION_COOKIE, ""), body))

    @app.post(PREFIX + "/assignments/{assignment_id}/reconcile")
    def reconcile(
        assignment_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[SchedulerReconcileRequest, Depends(_body(SchedulerReconcileRequest))],
    ) -> JSONResponse:
        _same_reference(assignment_id, body.assignment_id)
        return response(_service(engine).reconcile(request.cookies.get(SESSION_COOKIE, ""), body))

    @app.post(PREFIX + "/worker/claim")
    def claim(
        engine: Annotated[Engine, Depends(authority)],
        bearer: Annotated[str, Depends(_worker_bearer)],
        body: Annotated[SchedulerClaimRequest, Depends(_body(SchedulerClaimRequest))],
    ) -> JSONResponse:
        return response(_service(engine).claim(bearer, body))

    @app.post(PREFIX + "/worker/{assignment_id}/heartbeat")
    def heartbeat(
        assignment_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        bearer: Annotated[str, Depends(_worker_bearer)],
        body: Annotated[SchedulerHeartbeatRequest, Depends(_body(SchedulerHeartbeatRequest))],
    ) -> JSONResponse:
        _same_reference(assignment_id, body.assignment_id)
        return response(_service(engine).heartbeat(bearer, body))

    @app.post(PREFIX + "/worker/{assignment_id}/ready")
    def ready(
        assignment_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        bearer: Annotated[str, Depends(_worker_bearer)],
        body: Annotated[SchedulerReadyRequest, Depends(_body(SchedulerReadyRequest))],
    ) -> JSONResponse:
        _same_reference(assignment_id, body.assignment_id)
        return response(_service(engine).ready(bearer, body))

    @app.post(PREFIX + "/worker/{assignment_id}/report")
    def report(
        assignment_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        bearer: Annotated[str, Depends(_worker_bearer)],
        body: Annotated[SchedulerReportRequest, Depends(_body(SchedulerReportRequest))],
    ) -> JSONResponse:
        _same_reference(assignment_id, body.assignment_id)
        return response(_service(engine).report(bearer, body))
