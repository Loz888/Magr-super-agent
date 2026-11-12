"""Owner-only budget management; reservation and settlement stay server-internal."""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Path, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from mgar_v3.coordination.engine import Engine
from mgar_v3.super_agent.budget_domain import BudgetSave
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession

ScopeType = Literal["organization", "project"]


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    prefix = "/api/v3/project-budgets"

    def response(value: object) -> JSONResponse:
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    @app.get(prefix)
    def current_project(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            engine.project_budgets.inspect(
                request.cookies.get(SESSION_COOKIE, ""), "project", engine.settings.project_id
            )
        )

    @app.put(prefix + "/{scope_type}/{scope_id}")
    def save(
        scope_type: ScopeType,
        scope_id: Annotated[str, Path(min_length=1, max_length=200)],
        body: BudgetSave,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            engine.project_budgets.save(
                request.cookies.get(SESSION_COOKIE, ""), scope_type, scope_id, body
            )
        )

    @app.get(prefix + "/{scope_type}/{scope_id}")
    def inspect(
        scope_type: ScopeType,
        scope_id: Annotated[str, Path(min_length=1, max_length=200)],
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            engine.project_budgets.inspect(
                request.cookies.get(SESSION_COOKIE, ""), scope_type, scope_id
            )
        )
