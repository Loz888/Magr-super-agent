"""HTTPS-only reverse proxy with no Engine, Settings, database or platform secrets.

Upstream is one fixed local API. Component, Watchdog, MCP and Worker bearer
routes are unavailable on the public listener, including normalized aliases.
"""

import argparse
import posixpath
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import Field, field_validator
from starlette.background import BackgroundTask

from mgar_v3.domain import Model
from mgar_v3.super_agent.source_operations_client import private_model
from mgar_v3.super_agent.source_operations_domain import https_origin, local_api


class GatewayConfiguration(Model):
    public_origin: str
    api_url: str = "http://127.0.0.1:18080"
    source_relay_url: str | None = None
    certificate: Path
    private_key: Path
    listen_host: str = "127.0.0.1"
    listen_port: Annotated[int, Field(strict=True, ge=1, le=65535)] = 18443
    max_body_bytes: Annotated[int, Field(strict=True, ge=1024, le=1048576)] = 1048576

    @field_validator("public_origin")
    @classmethod
    def origin(cls, value: str) -> str:
        return https_origin(value)

    @field_validator("api_url")
    @classmethod
    def upstream(cls, value: str) -> str:
        return local_api(value)

    @field_validator("source_relay_url")
    @classmethod
    def relay(cls, value: str | None) -> str | None:
        return local_api(value) if value is not None else None

    @field_validator("certificate", "private_key")
    @classmethod
    def absolute(cls, path: Path) -> Path:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Absolute TLS file paths required")
        return path


PRIVATE = (
    "/api/v3/source-operations/components",
    "/api/v3/watchdog",
    "/mcp",
    "/api/v3/source-scheduler/worker",
    "/api/v3/source-coding/worker",
    "/api/v3/source-async/worker",
    "/api/v3/harness",
)
HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
SENSITIVE = {
    "authorization",
    "forwarded",
    "x-mgar-token",
    "x-mgar-watchdog-token",
    "x-mgar-operations-token",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
}


def create_gateway(configuration: GatewayConfiguration) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            base_url=configuration.api_url,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(60, connect=3),
        ) as client:
            app.state.upstream = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    authority = urlsplit(configuration.public_origin).netloc

    @app.middleware("http")
    async def proxy(request: Request, call_next: object) -> Response:
        security = {
            "Cache-Control": "no-store",
            "Strict-Transport-Security": "max-age=31536000",
            "X-Content-Type-Options": "nosniff",
        }

        def reject(code: int) -> JSONResponse:
            return JSONResponse(
                {"code": "GATEWAY_REQUEST_DENIED"}, status_code=code, headers=security
            )

        if request.url.scheme != "https" or request.headers.get("host") != authority:
            return reject(403)
        path = request.scope["path"]
        normalized = posixpath.normpath(path)
        if normalized != path.rstrip("/") and path != "/":
            return reject(404)
        if path.startswith("//") or any(path == p or path.startswith(p + "/") for p in PRIVATE):
            return reject(404)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > configuration.max_body_bytes:
                return reject(413)
        extra_hop = {x.strip().lower() for x in request.headers.get("connection", "").split(",")}
        headers = [
            (k, v)
            for k, v in request.headers.items()
            if k.lower() not in HOP | SENSITIVE | extra_hop
            and not k.lower().startswith(("x-mgar-", "x-forwarded-"))
        ]
        headers.append(("Host", authority))
        target = path + (
            "?" + request.scope["query_string"].decode("ascii")
            if request.scope["query_string"]
            else ""
        )
        if path.startswith("/callbacks/"):
            if configuration.source_relay_url is None:
                return reject(503)
            target = configuration.source_relay_url + target
        client: httpx.AsyncClient = request.app.state.upstream
        try:
            outgoing = client.build_request(
                request.method, target, headers=headers, content=bytes(raw)
            )
            upstream = await client.send(outgoing, stream=True)
        except httpx.HTTPError:
            return reject(503)
        result = StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            background=BackgroundTask(upstream.aclose),
        )
        result.raw_headers = [
            (k, v) for k, v in upstream.headers.raw if k.decode().lower() not in HOP
        ]
        for k, v in security.items():
            result.headers[k] = v
        return result

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = private_model(args.config, GatewayConfiguration)
    import os
    import stat

    info = config.private_key.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or (info.st_uid != os.geteuid() or info.st_nlink != 1)
    ):
        parser.exit(1, "A private TLS key is required.\n")
    uvicorn.run(
        create_gateway(config),
        host=config.listen_host,
        port=config.listen_port,
        ssl_certfile=str(config.certificate),
        ssl_keyfile=str(config.private_key),
        proxy_headers=False,
        access_log=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
