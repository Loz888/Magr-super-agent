"""Authenticated installation/grant management. No token or raw snapshot endpoint."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from mgar_v3.coordination.engine import Engine
from mgar_v3.sources.github_domain import GitHubConfigure, GitHubRevoke, SnapshotSave
from mgar_v3.sources.github_runtime import GitHubRuntime
from mgar_v3.sources.github_store import GitHubSources
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
    runtime: GitHubRuntime | None = None,
) -> None:
    prefix = "/api/v3/github-sources"

    def response(value: object) -> JSONResponse:
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    @app.put(prefix + "/{source_id}")
    def configure(
        source_id: UUID,
        body: GitHubConfigure,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            GitHubSources(engine).configure(
                request.cookies.get(SESSION_COOKIE, ""), source_id, body
            )
        )

    @app.get(prefix + "/{source_id}")
    def inspect(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        result = GitHubSources(engine).inspect(request.cookies.get(SESSION_COOKIE, ""), source_id)
        if runtime is not None and runtime.source_id == source_id:
            result.update(runtime.status())
        return response(result)

    @app.put(prefix + "/{source_id}/snapshot-grants/{grant_id}")
    def grant(
        source_id: UUID,
        grant_id: UUID,
        body: SnapshotSave,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            GitHubSources(engine).save_grant(
                request.cookies.get(SESSION_COOKIE, ""), source_id, grant_id, body
            )
        )

    @app.post(prefix + "/{source_id}/snapshot-grants/{grant_id}/revoke")
    def revoke(
        source_id: UUID,
        grant_id: UUID,
        body: GitHubRevoke,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            GitHubSources(engine).revoke(
                request.cookies.get(SESSION_COOKIE, ""), source_id, grant_id, body
            )
        )
