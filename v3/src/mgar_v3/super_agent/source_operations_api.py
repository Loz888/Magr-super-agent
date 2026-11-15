"""Owner administration and local per-component credentials; no admin-token shortcut."""

import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette._utils import get_route_path
from starlette.responses import Response

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import Model
from mgar_v3.settings import Settings
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.super_agent.source_operations import denied
from mgar_v3.super_agent.source_operations_client import private_model
from mgar_v3.super_agent.source_operations_domain import (
    ComponentHeartbeat,
    ComponentOpen,
    ComponentTick,
    IncidentDecision,
    OperationsSave,
    ServiceSecrets,
    SignalSubmission,
)
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession

PREFIX = "/api/v3/source-operations"


def body_parser[T: Model](model: type[T]) -> Callable[[Request], Awaitable[T]]:
    async def parse(request: Request) -> T:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise denied("REQUEST_INVALID", 422)
        try:
            import json

            from mgar_v3.super_agent.source_operations_client import _unique

            raw = await request.body()
            json.loads(raw, object_pairs_hook=_unique)
            return model.model_validate_json(raw)
        except (ValidationError, ValueError):
            raise denied("REQUEST_INVALID", 422) from None

    return parse


def response(value: Any) -> JSONResponse:
    return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})


def register(
    app: FastAPI,
    settings: Settings,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
    *,
    github_runtime: Any = None,
    feishu_runtime: Any = None,
) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with original_lifespan(application):
            # Original Engine has already obtained its sole writer lock and
            # performed business recovery. No competing controller is started.
            operations = application.state.engine.source_operations
            operations.recover()
            from mgar_v3.super_agent.source_operations_runtime import (
                OriginalDispatcherPort,
                OriginalSchedulerPort,
            )

            for port in (
                OriginalSchedulerPort(operations.engine),
                OriginalDispatcherPort(operations.engine),
            ):
                if port.role not in operations._ports:
                    operations.bind(port)
            yield

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def cache(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        result = await call_next(request)
        path = get_route_path(request.scope)
        if path == PREFIX or path.startswith(PREFIX + "/"):
            result.headers["Cache-Control"] = "no-store"
        return result

    def machine(request: Request, component_id: UUID) -> UUID:
        # Request.client is the transport peer, not X-Forwarded-For. Public
        # reverse-proxy configuration blocks this entire machine sub-tree.
        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            raise denied("LOCAL_COMPONENT_REQUIRED", 403)
        if request.headers.get("origin") or request.cookies.get(SESSION_COOKIE):
            raise denied("COMPONENT_AUTH_REQUIRED", 401)
        supplied = request.headers.getlist("X-MGAR-Operations-Token")
        if len(supplied) != 1 or settings.operations_service_config is None:
            raise denied("COMPONENT_AUTH_REQUIRED", 401)
        try:
            credentials = private_model(settings.operations_service_config, ServiceSecrets)
        except ValueError:
            raise denied("COMPONENT_AUTH_REQUIRED", 401) from None
        if not any(
            c.component_id == component_id
            and hmac.compare_digest(supplied[0].encode(), c.token.get_secret_value().encode())
            for c in credentials.components
        ):
            raise denied("COMPONENT_AUTH_REQUIRED", 401)
        return component_id

    @app.get(PREFIX)
    def read(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(engine.source_operations.read(request.cookies.get(SESSION_COOKIE, "")))

    @app.put(PREFIX)
    def save(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[OperationsSave, Depends(body_parser(OperationsSave))],
    ) -> JSONResponse:
        return response(
            engine.source_operations.save(request.cookies.get(SESSION_COOKIE, ""), body)
        )

    @app.get(PREFIX + "/metrics")
    def metrics(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(engine.source_operations.snapshot(request.cookies.get(SESSION_COOKIE, "")))

    @app.get(PREFIX + "/incidents")
    def incidents(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(engine.source_operations.incidents(request.cookies.get(SESSION_COOKIE, "")))

    @app.post(PREFIX + "/incidents/{incident_key}/decision")
    def decision(
        incident_key: str,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[IncidentDecision, Depends(body_parser(IncidentDecision))],
    ) -> JSONResponse:
        return response(
            engine.source_operations.decide(
                request.cookies.get(SESSION_COOKIE, ""),
                incident_key,
                body,
            )
        )

    @app.get(PREFIX + "/components/{component_id}/snapshot")
    def snapshot(
        component_id: Annotated[UUID, Depends(machine)],
        engine: Annotated[Engine, Depends(authority)],
    ) -> JSONResponse:
        return response(engine.source_operations.service_view(component_id))

    @app.post(PREFIX + "/components/{component_id}/open")
    def open_component(
        component_id: Annotated[UUID, Depends(machine)],
        engine: Annotated[Engine, Depends(authority)],
        body: Annotated[ComponentOpen, Depends(body_parser(ComponentOpen))],
    ) -> JSONResponse:
        return response(engine.source_operations.open(component_id, body))

    @app.post(PREFIX + "/components/{component_id}/heartbeat")
    def heartbeat(
        component_id: Annotated[UUID, Depends(machine)],
        engine: Annotated[Engine, Depends(authority)],
        body: Annotated[ComponentHeartbeat, Depends(body_parser(ComponentHeartbeat))],
    ) -> JSONResponse:
        return response(engine.source_operations.heartbeat(component_id, body))

    @app.post(PREFIX + "/components/{component_id}/tick")
    def tick(
        request: Request,
        component_id: Annotated[UUID, Depends(machine)],
        engine: Annotated[Engine, Depends(authority)],
        body: Annotated[ComponentTick, Depends(body_parser(ComponentTick))],
    ) -> JSONResponse:
        return response(
            engine.source_operations.tick(
                component_id, body, authenticate=lambda: machine(request, component_id) and None
            )
        )

    @app.post(PREFIX + "/components/{component_id}/observations")
    def observations(
        component_id: Annotated[UUID, Depends(machine)],
        engine: Annotated[Engine, Depends(authority)],
        body: Annotated[SignalSubmission, Depends(body_parser(SignalSubmission))],
    ) -> JSONResponse:
        return response(engine.source_operations.observe(component_id, body))

    @app.post(PREFIX + "/components/{component_id}/relay/{source_id}")
    async def relay(
        source_id: UUID,
        request: Request,
        component_id: Annotated[UUID, Depends(machine)],
        engine: Annotated[Engine, Depends(authority)],
    ) -> JSONResponse:
        payload = await request.body()
        # No caller-supplied socket proof or trusted-source JSON is accepted.
        allowed = {
            "content-type",
            "x-github-delivery",
            "x-github-event",
            "x-hub-signature-256",
            "x-lark-request-timestamp",
            "x-lark-request-nonce",
            "x-lark-signature",
        }
        headers = {k: v for k, v in request.headers.items() if k.lower() in allowed}
        envelope = TransportEnvelope(headers=headers, payload=payload)
        with engine._mutex:
            with engine.transaction() as conn:
                _, _, grant = engine.source_operations._grant(conn, component_id, role="source")
                try:
                    generation = int(request.headers.get("X-MGAR-Operations-Generation", ""))
                    boot = UUID(request.headers.get("X-MGAR-Operations-Boot", ""))
                except ValueError:
                    raise denied("FENCED") from None
                engine.source_operations._lease(conn, component_id, generation, boot)
                if source_id not in grant.source_ids:
                    raise denied("SCOPE_DENIED", 403)
                owner = conn.execute(
                    "SELECT generation FROM app.source_ingress_controls WHERE source_i"
                    "nstance_id=%s "
                    "AND project_id=%s",
                    (source_id, engine.settings.project_id),
                ).fetchone()
                if owner is None:
                    raise denied("SOURCE_UNAVAILABLE", 503)
            machine(request, component_id)  # Re-read file credentials after reservation reads.
            if github_runtime is not None and github_runtime.source_id == source_id:
                if github_runtime.broker.sources.engine is not engine:
                    raise denied("SOURCE_UNAVAILABLE", 503)
                result = github_runtime.receive(envelope, owner["generation"]).model_dump(
                    mode="json"
                )
            elif feishu_runtime is not None and feishu_runtime.source_id == source_id:
                if feishu_runtime.sources.engine is not engine:
                    raise denied("SOURCE_UNAVAILABLE", 503)
                result = feishu_runtime.webhook(envelope)
            else:
                raise denied("SOURCE_UNAVAILABLE", 503)
            # Gateway enforces durable outer commit before returning a receipt.
            return response(result)
