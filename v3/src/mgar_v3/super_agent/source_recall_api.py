"""The nine agreed read-only Recall routes on the original API application."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.responses import Response

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_coding_api import _worker_bearer
from mgar_v3.super_agent.source_recall_access import RecallCaller
from mgar_v3.super_agent.source_recall_domain import (
    RecallModel,
    RecallPageRequest,
    RecallResolveRequest,
    RecallSearchRequest,
    RecallWalkRequest,
    WorkerRecallPage,
    WorkerRecallResolve,
    WorkerRecallSearch,
    WorkerRecallWalk,
)
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession

PREFIX = "/api/v3/source-recall"


def _invalid() -> DomainError:
    return DomainError(
        "SOURCE_RECALL_REQUEST_INVALID", "Exact bounded Recall references required", 422
    )


def _body[T: RecallModel](model: type[T]) -> Callable[[Request], Awaitable[T]]:
    async def parse(request: Request) -> T:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json" and not (
            media_type.startswith("application/") and media_type.endswith("+json")
        ):
            raise _invalid()
        # The transport body is finite; authority/runtime fields are forbidden by DTOs.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16384:
                raise _invalid()
        try:
            return model.model_validate_json(bytes(raw))
        except ValidationError:
            raise _invalid() from None

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
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
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
        return _response(engine.source_recall.capabilities(request.cookies.get(SESSION_COOKIE, "")))

    @app.post(PREFIX + "/resolve")
    def resolve(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[RecallResolveRequest, Depends(_body(RecallResolveRequest))],
    ) -> JSONResponse:
        return _response(
            engine.source_recall.resolve(
                RecallCaller("owner", request.cookies.get(SESSION_COOKIE, "")),
                body,
            )
        )

    @app.post(PREFIX + "/walk")
    def walk(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[RecallWalkRequest, Depends(_body(RecallWalkRequest))],
    ) -> JSONResponse:
        return _response(
            engine.source_recall.walk(
                RecallCaller("owner", request.cookies.get(SESSION_COOKIE, "")),
                body,
            )
        )

    @app.post(PREFIX + "/page")
    def page(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[RecallPageRequest, Depends(_body(RecallPageRequest))],
    ) -> JSONResponse:
        return _response(
            engine.source_recall.page(
                RecallCaller("owner", request.cookies.get(SESSION_COOKIE, "")),
                body,
            )
        )

    @app.post(PREFIX + "/search")
    def search(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[RecallSearchRequest, Depends(_body(RecallSearchRequest))],
    ) -> JSONResponse:
        return _response(
            engine.source_recall.search(
                RecallCaller("owner", request.cookies.get(SESSION_COOKIE, "")),
                body,
            )
        )

    @app.post(PREFIX + "/worker/resolve")
    def worker_resolve(
        engine: Annotated[Engine, Depends(authority)],
        bearer: Annotated[str, Depends(_worker_bearer)],
        body: Annotated[WorkerRecallResolve, Depends(_body(WorkerRecallResolve))],
    ) -> JSONResponse:
        return _response(
            engine.source_recall.resolve(RecallCaller("worker", bearer, body.worker), body)
        )

    @app.post(PREFIX + "/worker/walk")
    def worker_walk(
        engine: Annotated[Engine, Depends(authority)],
        bearer: Annotated[str, Depends(_worker_bearer)],
        body: Annotated[WorkerRecallWalk, Depends(_body(WorkerRecallWalk))],
    ) -> JSONResponse:
        return _response(
            engine.source_recall.walk(RecallCaller("worker", bearer, body.worker), body)
        )

    @app.post(PREFIX + "/worker/page")
    def worker_page(
        engine: Annotated[Engine, Depends(authority)],
        bearer: Annotated[str, Depends(_worker_bearer)],
        body: Annotated[WorkerRecallPage, Depends(_body(WorkerRecallPage))],
    ) -> JSONResponse:
        return _response(
            engine.source_recall.page(RecallCaller("worker", bearer, body.worker), body)
        )

    @app.post(PREFIX + "/worker/search")
    def worker_search(
        engine: Annotated[Engine, Depends(authority)],
        bearer: Annotated[str, Depends(_worker_bearer)],
        body: Annotated[WorkerRecallSearch, Depends(_body(WorkerRecallSearch))],
    ) -> JSONResponse:
        return _response(
            engine.source_recall.search(RecallCaller("worker", bearer, body.worker), body)
        )
