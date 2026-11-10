"""Cookie/Origin/CSRF management only. There is no raw source/identity assertion endpoint."""

from collections.abc import Callable
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from mgar_v3.coordination.engine import Engine
from mgar_v3.sources.ingress_domain import CursorReset, IngressConfigure, ReplayRequest
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    prefix = "/api/v3/source-ingress"

    def response(value: object) -> JSONResponse:
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    @app.put(prefix + "/{source_id}")
    def configure(
        source_id: UUID,
        body: IngressConfigure,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            engine.source_ingress.configure(
                request.cookies.get(SESSION_COOKIE, ""), source_id, body
            )
        )

    @app.get(prefix + "/{source_id}/view/{kind}")
    def inspect(
        source_id: UUID,
        kind: Literal["receipts", "inputs", "gaps", "cursors", "conflicts", "audit"],
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            engine.source_ingress.inspect(request.cookies.get(SESSION_COOKIE, ""), source_id, kind)
        )

    @app.post(prefix + "/{source_id}/replay")
    def replay(
        source_id: UUID,
        body: ReplayRequest,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            engine.source_ingress.replay(request.cookies.get(SESSION_COOKIE, ""), source_id, body)
        )

    @app.post(prefix + "/{source_id}/purge")
    def purge(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            engine.source_ingress.purge(request.cookies.get(SESSION_COOKIE, ""), source_id)
        )

    @app.post(prefix + "/{source_id}/cursor/{stream_id}/reset")
    def reset(
        source_id: UUID,
        stream_id: str,
        body: CursorReset,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            engine.source_ingress.reset_cursor(
                request.cookies.get(SESSION_COOKIE, ""), source_id, stream_id, body
            )
        )
