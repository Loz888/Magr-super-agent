"""Separate bounded raw-callback relay. Only original Engine Runtime verifies/ACKs."""

import argparse
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, field_validator

from mgar_v3.domain import Model
from mgar_v3.super_agent.source_operations_client import (
    LocalOperationsClient,
    ServiceUnavailable,
    private_model,
)
from mgar_v3.super_agent.source_operations_domain import ServiceClient


class RelayConfiguration(Model):
    client: ServiceClient
    listen_host: str = "127.0.0.1"
    listen_port: Annotated[int, Field(strict=True, ge=1, le=65535)] = 18082

    @field_validator("client")
    @classmethod
    def source_only(cls, value: ServiceClient) -> ServiceClient:
        if value.role != "source":
            raise ValueError("Source relay requires a Source credential")
        return value


SIGNATURES = {
    "content-type",
    "x-github-delivery",
    "x-github-event",
    "x-hub-signature-256",
    "x-lark-request-timestamp",
    "x-lark-request-nonce",
    "x-lark-signature",
}


def create_relay(configuration: RelayConfiguration) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = LocalOperationsClient(configuration.client)
        stopped = asyncio.Event()

        async def poll() -> None:
            while not stopped.is_set():
                try:
                    await asyncio.to_thread(client.poll)
                except (ServiceUnavailable, ValueError):
                    pass
                try:
                    await asyncio.wait_for(
                        stopped.wait(), timeout=configuration.client.interval_seconds
                    )
                except TimeoutError:
                    pass

        task = asyncio.create_task(poll())
        app.state.client = client
        try:
            yield
        finally:
            stopped.set()
            await task
            client.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/callbacks/{source_id}")
    async def receive(source_id: UUID, request: Request) -> Response:
        def unavailable() -> JSONResponse:
            return JSONResponse(
                {"code": "SOURCE_NOT_DURABLY_ACKNOWLEDGED"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )

        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            return unavailable()
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > 262144:
                return Response(status_code=413, headers={"Cache-Control": "no-store"})
        headers = {}
        for name in SIGNATURES:
            values = request.headers.getlist(name)
            if len(values) > 1:
                return Response(status_code=403, headers={"Cache-Control": "no-store"})
            if values:
                headers[name] = values[0]
        client: LocalOperationsClient = request.app.state.client
        try:
            from mgar_v3.super_agent.source_operations_client import ServiceView

            view = ServiceView.model_validate(await asyncio.to_thread(client.request, "snapshot"))
            component = next(
                (
                    c
                    for c in view.snapshot.components
                    if c.component_id == configuration.client.component_id
                ),
                None,
            )
            if component is None or component.boot_id != client.boot_id:
                return unavailable()
            headers.update(
                {
                    "X-MGAR-Operations-Token": configuration.client.token.get_secret_value(),
                    "X-MGAR-Operations-Boot": str(client.boot_id),
                    "X-MGAR-Operations-Generation": str(component.generation),
                }
            )
            path = (
                f"/api/v3/source-operations/components/{configuration.client.component_id}"
                f"/relay/{source_id}"
            )
            async with httpx.AsyncClient(
                base_url=configuration.client.api_url,
                timeout=10,
                follow_redirects=False,
                trust_env=False,
            ) as transport:
                async with transport.stream(
                    "POST", path, headers=headers, content=bytes(payload)
                ) as reply:
                    if reply.status_code != 200:
                        return unavailable()
                    raw = bytearray()
                    async for chunk in reply.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            return unavailable()
                    # Returning a successful ACK requires the original Engine's
                    # verifier/runtime response, never a relay-generated success.
                    return Response(
                        bytes(raw),
                        media_type="application/json",
                        headers={"Cache-Control": "no-store"},
                    )
        except (ServiceUnavailable, ValueError, httpx.HTTPError):
            return unavailable()

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    configuration = private_model(args.config, RelayConfiguration)
    uvicorn.run(
        create_relay(configuration),
        host=configuration.listen_host,
        port=configuration.listen_port,
        proxy_headers=False,
        access_log=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
