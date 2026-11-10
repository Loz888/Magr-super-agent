"""Console-only management. No route accepts platform assertions or issues source bearers."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from mgar_v3.coordination.engine import Engine
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.identity_domain import (
    BindIdentity,
    ControllerGrant,
    RestoreBinding,
    RestoreIdentity,
    RevisionCommand,
    RoleChange,
)
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession


def _response(value: Any) -> JSONResponse:
    return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    prefix = "/api/v3/source-identities"

    @app.get(prefix + "/evidence")
    def evidence(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(SourceIdentities(engine).evidence(request.cookies.get(SESSION_COOKIE, "")))

    @app.get(prefix + "/bindings")
    def bindings(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(SourceIdentities(engine).bindings(request.cookies.get(SESSION_COOKIE, "")))

    @app.get(prefix + "/principals")
    def principals(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(
            SourceIdentities(engine).principals(request.cookies.get(SESSION_COOKIE, ""))
        )

    @app.get(prefix + "/audit")
    def audit(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(SourceIdentities(engine).audit(request.cookies.get(SESSION_COOKIE, "")))

    @app.post(prefix + "/bindings")
    def bind(
        body: BindIdentity,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(
            SourceIdentities(engine).bind(request.cookies.get(SESSION_COOKIE, ""), body)
        )

    @app.put(prefix + "/bindings/{binding_id}/roles")
    def roles(
        binding_id: UUID,
        body: RoleChange,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(
            SourceIdentities(engine).roles(
                request.cookies.get(SESSION_COOKIE, ""), binding_id, body
            )
        )

    @app.post(prefix + "/bindings/{binding_id}/revoke")
    def revoke_binding(
        binding_id: UUID,
        body: RevisionCommand,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(
            SourceIdentities(engine).revoke_binding(
                request.cookies.get(SESSION_COOKIE, ""), binding_id, body
            )
        )

    @app.post(prefix + "/bindings/{binding_id}/restore")
    def restore_binding(
        binding_id: UUID,
        body: RestoreBinding,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(
            SourceIdentities(engine).restore_binding(
                request.cookies.get(SESSION_COOKIE, ""), binding_id, body
            )
        )

    @app.post(prefix + "/identities/{identity_id}/revoke")
    def revoke_identity(
        identity_id: UUID,
        body: RevisionCommand,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(
            SourceIdentities(engine).revoke_identity(
                request.cookies.get(SESSION_COOKIE, ""), identity_id, body
            )
        )

    @app.post(prefix + "/identities/{identity_id}/restore")
    def restore_identity(
        identity_id: UUID,
        body: RestoreIdentity,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(
            SourceIdentities(engine).restore_identity(
                request.cookies.get(SESSION_COOKIE, ""), identity_id, body
            )
        )

    @app.post(prefix + "/bindings/{binding_id}/controllers")
    def grant_controller(
        binding_id: UUID,
        body: ControllerGrant,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(
            SourceIdentities(engine).grant_controller(
                request.cookies.get(SESSION_COOKIE, ""), binding_id, body
            )
        )

    @app.post(prefix + "/bindings/{binding_id}/controllers/{ticket_id}/revoke")
    def revoke_controller(
        binding_id: UUID,
        ticket_id: UUID,
        body: RevisionCommand,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return _response(
            SourceIdentities(engine).revoke_controller(
                request.cookies.get(SESSION_COOKIE, ""), binding_id, ticket_id, body
            )
        )
