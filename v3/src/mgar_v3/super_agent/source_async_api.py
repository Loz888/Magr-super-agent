"""The twelve agreed metadata routes on the original authenticated application."""

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.responses import Response

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_async_domain import (
    AsyncAdmitRequest,
    AsyncJobCancelRequest,
    AsyncModel,
    AsyncRestrictRequest,
    AsyncWorkerJobCancelRequest,
    AsyncWorkerRequest,
)
from mgar_v3.super_agent.source_coding_api import _worker_bearer
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession

PREFIX = "/api/v3/source-async"


def _invalid() -> DomainError:
    return DomainError("SOURCE_ASYNC_REQUEST_INVALID", "Exact Async metadata is required", 422)


def _body[Command: AsyncModel](model: type[Command]) -> Callable[[Request], Awaitable[Command]]:
    async def parse(request: Request) -> Command:
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "application/json" and not (
            media.startswith("application/") and media.endswith("+json")
        ):
            raise _invalid()
        try:
            return model.model_validate_json(await request.body())
        except ValidationError:
            raise _invalid() from None

    return parse


def _same(path: UUID, command: UUID) -> None:
    if path != command:
        raise _invalid()


def _worker_query(
    coding_id: UUID, run_id: UUID, assignment_id: UUID, session_id: UUID, epoch: int
) -> AsyncWorkerRequest:
    try:
        return AsyncWorkerRequest(
            request_id=uuid4(),
            coding_id=coding_id,
            run_id=run_id,
            assignment_id=assignment_id,
            session_id=session_id,
            epoch=epoch,
            expected_revision=1,
        )
    except ValidationError:
        raise _invalid() from None


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    @app.middleware("http")
    async def private_cache(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        result = await call_next(request)
        if request.url.path == PREFIX or request.url.path.startswith(PREFIX + "/"):
            result.headers["Cache-Control"] = "no-store"
        return result

    def response(value: object) -> JSONResponse:
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    @app.get(PREFIX + "/capabilities")
    def capabilities(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(engine.source_async.capabilities(request.cookies.get(SESSION_COOKIE, "")))

    @app.post(PREFIX + "/admissions")
    def admit(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[AsyncAdmitRequest, Depends(_body(AsyncAdmitRequest))],
    ) -> JSONResponse:
        return response(engine.source_async.admit(request.cookies.get(SESSION_COOKIE, ""), body))

    @app.get(PREFIX + "/bindings/{coding_id}")
    def binding(
        coding_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(engine.source_async.get(request.cookies.get(SESSION_COOKIE, ""), coding_id))

    @app.post(PREFIX + "/bindings/{coding_id}/restrictions")
    def restrict(
        coding_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[AsyncRestrictRequest, Depends(_body(AsyncRestrictRequest))],
    ) -> JSONResponse:
        _same(coding_id, body.coding_id)
        return response(engine.source_async.restrict(request.cookies.get(SESSION_COOKIE, ""), body))

    @app.get(PREFIX + "/bindings/{coding_id}/inputs")
    def inputs(
        coding_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=64)] = 64,
    ) -> JSONResponse:
        return response(
            engine.source_async.inputs(
                request.cookies.get(SESSION_COOKIE, ""),
                coding_id,
                after=after_sequence,
                limit=limit,
            )
        )

    @app.get(PREFIX + "/bindings/{coding_id}/jobs")
    def jobs(
        coding_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        after_job_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=64)] = 64,
    ) -> JSONResponse:
        return response(
            engine.source_async.jobs(
                request.cookies.get(SESSION_COOKIE, ""), coding_id, after=after_job_id, limit=limit
            )
        )

    @app.get(PREFIX + "/bindings/{coding_id}/events")
    def events(
        coding_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        after_event_id: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=64)] = 64,
    ) -> JSONResponse:
        return response(
            engine.source_async.events(
                request.cookies.get(SESSION_COOKIE, ""),
                coding_id,
                after=after_event_id,
                limit=limit,
            )
        )

    @app.post(PREFIX + "/bindings/{coding_id}/jobs/{job_id}/cancel")
    def cancel(
        coding_id: UUID,
        job_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[AsyncJobCancelRequest, Depends(_body(AsyncJobCancelRequest))],
    ) -> JSONResponse:
        _same(coding_id, body.coding_id)
        _same(job_id, body.job_id)
        return response(engine.source_async.cancel(request.cookies.get(SESSION_COOKIE, ""), body))

    @app.post(PREFIX + "/worker/{coding_id}/start")
    def start(
        coding_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        body: Annotated[AsyncWorkerRequest, Depends(_body(AsyncWorkerRequest))],
    ) -> JSONResponse:
        _same(coding_id, body.coding_id)
        return response(engine.source_async.start(_worker_bearer(request), body))

    @app.get(PREFIX + "/worker/{coding_id}/state")
    def state(
        coding_id: UUID,
        run_id: UUID,
        assignment_id: UUID,
        session_id: UUID,
        epoch: Annotated[int, Query(ge=1)],
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
    ) -> JSONResponse:
        return response(
            engine.source_async.state(
                _worker_bearer(request),
                _worker_query(coding_id, run_id, assignment_id, session_id, epoch),
            )
        )

    @app.get(PREFIX + "/worker/{coding_id}/jobs/{job_id}")
    def job(
        coding_id: UUID,
        job_id: UUID,
        run_id: UUID,
        assignment_id: UUID,
        session_id: UUID,
        epoch: Annotated[int, Query(ge=1)],
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
    ) -> JSONResponse:
        return response(
            engine.source_async.state(
                _worker_bearer(request),
                _worker_query(coding_id, run_id, assignment_id, session_id, epoch),
                job_id=job_id,
            )
        )

    @app.post(PREFIX + "/worker/{coding_id}/jobs/{job_id}/cancel")
    def worker_cancel(
        coding_id: UUID,
        job_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        body: Annotated[AsyncWorkerJobCancelRequest, Depends(_body(AsyncWorkerJobCancelRequest))],
    ) -> JSONResponse:
        _same(coding_id, body.coding_id)
        _same(job_id, body.job_id)
        return response(engine.source_async.worker_cancel(_worker_bearer(request), body))
