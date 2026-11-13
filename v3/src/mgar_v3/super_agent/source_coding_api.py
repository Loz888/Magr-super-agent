"""Coding metadata routes on the original app and its current authority."""

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
from mgar_v3.super_agent import source_coding
from mgar_v3.super_agent.source_coding_domain import (
    CodingAdmitRequest,
    CodingCancelRequest,
    CodingInspectRequest,
    CodingModel,
    CodingRunRequest,
)
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession

PREFIX = "/api/v3/source-coding"


def _invalid() -> DomainError:
    return DomainError(
        "SOURCE_CODING_REQUEST_INVALID", "Exact Coding metadata is required", 422,
    )


def _body[Command: CodingModel](model: type[Command]) -> Callable[[Request], Awaitable[Command]]:
    async def parse(request: Request) -> Command:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json" and not (
            media_type.startswith("application/") and media_type.endswith("+json")
        ):
            raise _invalid()
        try:
            # Validate transport JSON directly: UUID strings are transport
            # values, while strict integer/string/boolean types stay strict.
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
        "SOURCE_CODING_WORKER_AUTH_REQUIRED", "A current Worker session bearer is required", 401,
    )


def _service(engine: Engine) -> source_coding.SourceCoding:
    # Keep server-owned runtime injection and the original held connection.
    return engine.source_coding


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

    @app.get(PREFIX + "/capabilities")
    def capabilities(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(_service(engine).capabilities(request.cookies.get(SESSION_COOKIE, "")))

    @app.post(PREFIX + "/admissions")
    def admit(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[CodingAdmitRequest, Depends(_body(CodingAdmitRequest))],
    ) -> JSONResponse:
        return response(_service(engine).admit(request.cookies.get(SESSION_COOKIE, ""), body))

    @app.get(PREFIX + "/bindings/{coding_id}")
    def get_binding(
        coding_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(_service(engine).get(request.cookies.get(SESSION_COOKIE, ""), coding_id))

    @app.post(PREFIX + "/bindings/{coding_id}/cancel")
    def cancel(
        coding_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[CodingCancelRequest, Depends(_body(CodingCancelRequest))],
    ) -> JSONResponse:
        _same_reference(coding_id, body.coding_id)
        return response(_service(engine).cancel(request.cookies.get(SESSION_COOKIE, ""), body))

    @app.post(PREFIX + "/worker/{coding_id}/run")
    def run(
        coding_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        bearer: Annotated[str, Depends(_worker_bearer)],
        body: Annotated[CodingRunRequest, Depends(_body(CodingRunRequest))],
    ) -> JSONResponse:
        _same_reference(coding_id, body.coding_id)
        return response(_service(engine).run(bearer, body))

    @app.get(PREFIX + "/worker/{coding_id}/state")
    def state(
        coding_id: UUID,
        run_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        bearer: Annotated[str, Depends(_worker_bearer)],
    ) -> JSONResponse:
        if list(request.query_params) != ["run_id"] or len(
            request.query_params.getlist("run_id")
        ) != 1:
            raise _invalid()
        return response(_service(engine).state(
            bearer, CodingInspectRequest(coding_id=coding_id, run_id=run_id),
        ))
