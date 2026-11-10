"""Cookie/Origin/CSRF policy management; no source bearer or execution endpoint."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from mgar_v3.coordination.engine import Engine
from mgar_v3.super_agent.policy_domain import PolicyPreview, PolicyRevoke, PolicySave
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    prefix = "/api/v3/source-policies"

    @app.put(prefix + "/{policy_id}")
    def save(
        policy_id: UUID,
        body: PolicySave,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return JSONResponse(
            jsonable_encoder(
                engine.source_policies.save(
                    request.cookies.get(SESSION_COOKIE, ""), policy_id, body
                )
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.post(prefix + "/{policy_id}/preview")
    def preview(
        policy_id: UUID,
        body: PolicyPreview,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return JSONResponse(
            jsonable_encoder(
                engine.source_policies.preview(
                    request.cookies.get(SESSION_COOKIE, ""), policy_id, body
                )
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.post(prefix + "/{policy_id}/revoke")
    def revoke(
        policy_id: UUID,
        body: PolicyRevoke,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return JSONResponse(
            jsonable_encoder(
                engine.source_policies.revoke(
                    request.cookies.get(SESSION_COOKIE, ""), policy_id, body
                )
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.get(prefix + "/{policy_id}")
    def read(
        policy_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return JSONResponse(
            jsonable_encoder(
                engine.source_policies.read(request.cookies.get(SESSION_COOKIE, ""), policy_id)
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.get(prefix + "/{policy_id}/history")
    def history(
        policy_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return JSONResponse(
            jsonable_encoder(
                engine.source_policies.history(request.cookies.get(SESSION_COOKIE, ""), policy_id)
            ),
            headers={"Cache-Control": "no-store"},
        )
