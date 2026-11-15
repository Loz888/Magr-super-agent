"""The thirteen agreed Owner APIs and three dedicated review page routes."""

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import ValidationError
from starlette.responses import Response

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.settings import ROOT
from mgar_v3.super_agent.source_coding_domain import CodingModel
from mgar_v3.super_agent.source_review_domain import (
    ReviewApprove,
    ReviewCycle,
    ReviewExact,
    ReviewNativeLink,
    ReviewPrepare,
)
from mgar_v3.super_agent.source_reviews import SourceReviews
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession

PREFIX = "/api/v3/source-reviews"
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
}


def _body[T: CodingModel](model: type[T]) -> Callable[[Request], Awaitable[T]]:
    async def parse(request: Request) -> T:
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "application/json" and not (
            media.startswith("application/") and media.endswith("+json")
        ):
            raise DomainError("SOURCE_REVIEW_REQUEST_INVALID", "Bounded JSON required", 422)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16384:
                raise DomainError("SOURCE_REVIEW_REQUEST_INVALID", "Bounded JSON required", 422)
        try:
            return model.model_validate_json(bytes(raw))
        except ValidationError:
            raise DomainError(
                "SOURCE_REVIEW_REQUEST_INVALID", "Exact review fields required", 422
            ) from None

    return parse


def response(value: object) -> JSONResponse:
    return JSONResponse(jsonable_encoder(value), headers=HEADERS)


def service(engine: Engine) -> SourceReviews:
    result = getattr(engine, "source_reviews", None)
    if not isinstance(result, SourceReviews):
        raise DomainError("SOURCE_REVIEW_NOT_WIRED", "Formal composition is not installed", 409)
    return result


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    @app.middleware("http")
    async def no_store(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        result = await call_next(request)
        if (
            request.url.path == PREFIX
            or request.url.path.startswith(PREFIX + "/")
            or request.url.path == "/source-reviews"
            or request.url.path.startswith("/assets/source-reviews.")
        ):
            for key, value in HEADERS.items():
                result.headers[key] = value
        return result

    @app.get(PREFIX + "/capabilities")
    def capabilities(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(service(engine).capabilities(request.cookies.get(SESSION_COOKIE, "")))

    @app.get(PREFIX)
    def reviews(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: int = 32,
    ) -> JSONResponse:
        return response(service(engine).list(request.cookies.get(SESSION_COOKIE, ""), limit))

    @app.post(PREFIX)
    def prepare(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[ReviewPrepare, Depends(_body(ReviewPrepare))],
    ) -> JSONResponse:
        return response(service(engine).prepare(request.cookies.get(SESSION_COOKIE, ""), body))

    @app.get(PREFIX + "/events")
    def events(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: int = 32,
    ) -> JSONResponse:
        return response(service(engine).events(request.cookies.get(SESSION_COOKIE, ""), limit))

    @app.post(PREFIX + "/tick")
    def tick(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[ReviewCycle, Depends(_body(ReviewCycle))],
    ) -> JSONResponse:
        return response(service(engine).tick(request.cookies.get(SESSION_COOKIE, ""), body.limit))

    @app.post(PREFIX + "/maintain")
    def maintain(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[ReviewCycle, Depends(_body(ReviewCycle))],
    ) -> JSONResponse:
        return response(
            service(engine).maintain(request.cookies.get(SESSION_COOKIE, ""), body.limit)
        )

    @app.get(PREFIX + "/{review_id}")
    def get(
        request: Request,
        review_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(service(engine).get(request.cookies.get(SESSION_COOKIE, ""), review_id))

    @app.post(PREFIX + "/{review_id}/inspect")
    def inspect(
        request: Request,
        review_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(service(engine).inspect(request.cookies.get(SESSION_COOKIE, ""), review_id))

    @app.post(PREFIX + "/{review_id}/approve")
    def approve(
        request: Request,
        review_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[ReviewApprove, Depends(_body(ReviewApprove))],
    ) -> JSONResponse:
        return response(
            service(engine).approve(request.cookies.get(SESSION_COOKIE, ""), review_id, body)
        )

    @app.post(PREFIX + "/{review_id}/cancel")
    def cancel(
        request: Request,
        review_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[ReviewExact, Depends(_body(ReviewExact))],
    ) -> JSONResponse:
        return response(
            service(engine).cancel(request.cookies.get(SESSION_COOKIE, ""), review_id, body)
        )

    @app.get(PREFIX + "/{review_id}/patch")
    def patch(
        request: Request,
        review_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> PlainTextResponse:
        return PlainTextResponse(
            service(engine).patch(request.cookies.get(SESSION_COOKIE, ""), review_id),
            media_type="text/x-diff",
            headers={**HEADERS, "Content-Disposition": 'attachment; filename="review.patch"'},
        )

    @app.post(PREFIX + "/{review_id}/reconcile")
    def reconcile(
        request: Request,
        review_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(
            service(engine).reconcile(request.cookies.get(SESSION_COOKIE, ""), review_id)
        )

    @app.post(PREFIX + "/{review_id}/native-links")
    def link(
        request: Request,
        review_id: UUID,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        body: Annotated[ReviewNativeLink, Depends(_body(ReviewNativeLink))],
    ) -> JSONResponse:
        return response(
            service(engine).link_native(request.cookies.get(SESSION_COOKIE, ""), review_id, body)
        )

    def asset(request: Request, engine: Engine, filename: str, media: str) -> FileResponse:
        with engine.transaction() as conn:
            service(engine)._console(conn, request.cookies.get(SESSION_COOKIE, ""))
        return FileResponse(ROOT / "web" / filename, media_type=media, headers=HEADERS)

    @app.get("/source-reviews")
    def page(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> FileResponse:
        return asset(request, engine, "source-reviews.html", "text/html")

    @app.get("/assets/source-reviews.js")
    def javascript(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> FileResponse:
        return asset(request, engine, "source-reviews.js", "text/javascript")

    @app.get("/assets/source-reviews.css")
    def stylesheet(
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> FileResponse:
        return asset(request, engine, "source-reviews.css", "text/css")
