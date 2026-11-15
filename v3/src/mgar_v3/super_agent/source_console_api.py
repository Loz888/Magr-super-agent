"""Same-session console routes; new settings never activate a platform runtime."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.settings import ROOT
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.sources.github_runtime import GitHubRuntime
from mgar_v3.sources.ingress_domain import IngressConfigure
from mgar_v3.super_agent.policy_domain import PolicyPreview, PolicySave
from mgar_v3.super_agent.source_console import SourceConsole
from mgar_v3.super_agent.source_console_domain import (
    ConsoleCredentialSave,
    ConsoleDisableSources,
    ConsoleSourceRevoke,
    ConsoleSourceSave,
    ConsoleTaskStop,
)
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession

HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'",
}


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


class ConsoleRoute(APIRoute):
    def get_route_handler(self) -> Callable[..., Any]:
        original = super().get_route_handler()

        async def bounded(request: Request) -> Any:
            if request.method in {"POST", "PUT", "PATCH"}:
                if (
                    request.headers.get("content-type", "").split(";", 1)[0].lower()
                    != "application/json"
                ):
                    raise DomainError("CONSOLE_JSON_REQUIRED", "请提交JSON格式的设置。", 415)
                raw = bytearray()
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 65536:
                        raise DomainError(
                            "CONSOLE_BODY_BOUND", "来源设置过大，请减少选择范围。", 413
                        )
                    raw.extend(chunk)
                try:
                    json.loads(raw, object_pairs_hook=unique_object)
                except (ValueError, UnicodeError):
                    raise DomainError(
                        "CONSOLE_INVALID_JSON", "设置格式无效或存在重复字段。", 422
                    ) from None
                request._body = bytes(raw)
            return await original(request)

        return bounded


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
    *,
    github_runtime: GitHubRuntime | None = None,
    feishu_runtime: FeishuRuntime | None = None,
) -> None:
    router = APIRouter(prefix="/api/v3/source-console", route_class=ConsoleRoute)
    runtimes = tuple(runtime for runtime in (github_runtime, feishu_runtime) if runtime is not None)

    def console(engine: Engine) -> SourceConsole:
        service = getattr(engine, "source_console", None)
        if not isinstance(service, SourceConsole) or service.engine is not engine:
            raise DomainError("CONSOLE_NOT_WIRED", "来源控制台尚未完成接线。", 503)
        return service

    def response(value: Any) -> JSONResponse:
        return JSONResponse(jsonable_encoder(value), headers=HEADERS)

    def cookie(request: Request) -> str:
        return request.cookies.get(SESSION_COOKIE, "")

    @router.get("")
    def snapshot(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(console(engine).snapshot(cookie(request), runtimes))

    @router.get("/sources/{source_id}")
    def source(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(console(engine).source(cookie(request), source_id, runtimes))

    @router.put("/sources/{source_id}")
    def save_source(
        source_id: UUID,
        body: ConsoleSourceSave,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(console(engine).save_source(cookie(request), source_id, body))

    @router.put("/sources/{source_id}/credentials")
    def credentials(
        source_id: UUID,
        body: ConsoleCredentialSave,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(console(engine).save_credentials(cookie(request), source_id, body))

    @router.post("/sources/{source_id}/revoke")
    def revoke(
        source_id: UUID,
        body: ConsoleSourceRevoke,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(console(engine).revoke_source(cookie(request), source_id, body))

    @router.put("/sources/{source_id}/ingress")
    def ingress(
        source_id: UUID,
        body: IngressConfigure,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(console(engine).configure_ingress(cookie(request), source_id, body))

    @router.post("/disable-all")
    def disable(
        body: ConsoleDisableSources,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(console(engine).disable_all(cookie(request), body))

    @router.put("/policies/{policy_id}")
    def policy(
        policy_id: UUID,
        body: PolicySave,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(console(engine).save_policy(cookie(request), policy_id, body))

    @router.post("/policies/{policy_id}/preview")
    def preview(
        policy_id: UUID,
        body: PolicyPreview,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(console(engine).preview_policy(cookie(request), policy_id, body))

    @router.get("/tasks/{run_id}")
    def task(
        run_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(console(engine).task(cookie(request), run_id))

    @router.post("/tasks/{run_id}/stop")
    def stop(
        run_id: UUID,
        body: ConsoleTaskStop,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(console(engine).stop_task(cookie(request), run_id, body))

    @router.get("/decisions")
    def decisions(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: int = 50,
    ) -> JSONResponse:
        return response(console(engine).decisions(cookie(request), limit))

    # Keep the owned bounded routes directly visible to the original app and
    # its route inventory; do not defer them behind a lazy included router.
    for route in router.routes:
        assert isinstance(route, APIRoute)
        assert route.methods is not None
        app.router.add_api_route(
            route.path,
            route.endpoint,
            methods=list(route.methods),
            name=route.name,
            response_model=None,
            route_class_override=ConsoleRoute,
        )

    def file(request: Request, engine: Engine, path: Path) -> FileResponse:
        with engine.transaction() as conn:
            console(engine)._owner(conn, cookie(request))
        return FileResponse(path, headers=HEADERS)

    @app.get("/source-console", include_in_schema=False)
    def page(request: Request, engine: Annotated[Engine, Depends(authority)]) -> Any:
        try:
            return file(request, engine, ROOT / "web/source-console.html")
        except DomainError as exc:
            if exc.status == 401:
                return RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store"})
            raise

    @app.get("/static/source-console.js", include_in_schema=False)
    def script(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> FileResponse:
        return file(request, engine, ROOT / "web/source-console.js")

    @app.get("/static/source-console.css", include_in_schema=False)
    def style(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> FileResponse:
        return file(request, engine, ROOT / "web/source-console.css")

    # The existing /static mount precedes API registration. Put only these owned
    # resources ahead of that fallback so it cannot bypass their Owner checks.
    for resource in ("/static/source-console.js", "/static/source-console.css"):
        owned = next(r for r in app.router.routes if getattr(r, "path", None) == resource)
        app.router.routes.remove(owned)
        fallback = next(
            i for i, r in enumerate(app.router.routes) if getattr(r, "path", None) == "/static"
        )
        app.router.routes.insert(fallback, owned)

    @app.middleware("http")
    async def no_page_static_alias(request: Request, call_next: Callable[..., Any]) -> Any:
        if request.url.path == "/static/source-console.html":
            return JSONResponse({"code": "NOT_FOUND"}, status_code=404, headers=HEADERS)
        return await call_next(request)
