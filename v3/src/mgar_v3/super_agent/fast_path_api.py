"""Owner-only observation controls; source content and authority assertions stay internal."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from mgar_v3.coordination.engine import Engine
from mgar_v3.super_agent.fast_path import FastPath
from mgar_v3.super_agent.fast_path_domain import FastPathConfigure, ObserveRequest
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    prefix = "/api/v3/source-fast-path"

    def response(value: object) -> JSONResponse:
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    @app.put(prefix + "/{source_id}")
    def configure(
        source_id: UUID,
        body: FastPathConfigure,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(FastPath(engine).configure(
            request.cookies.get(SESSION_COOKIE, ""), source_id, body
        ))

    @app.get(prefix + "/{source_id}")
    def read(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(FastPath(engine).read(request.cookies.get(SESSION_COOKIE, ""), source_id))

    @app.post(prefix + "/{source_id}/observe")
    def observe(
        source_id: UUID,
        body: ObserveRequest,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(FastPath(engine).observe(
            request.cookies.get(SESSION_COOKIE, ""), source_id, body
        ))

    @app.get(prefix + "/{source_id}/preview")
    def preview(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> JSONResponse:
        return response(FastPath(engine).preview(
            request.cookies.get(SESSION_COOKIE, ""), source_id, limit
        ))

    @app.get(prefix + "/{source_id}/decisions")
    def decisions(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> JSONResponse:
        return response(FastPath(engine).decisions(
            request.cookies.get(SESSION_COOKIE, ""), source_id, limit
        ))

    @app.get(prefix + "/{source_id}/metrics")
    def metrics(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(FastPath(engine).metrics(
            request.cookies.get(SESSION_COOKIE, ""), source_id
        ))
