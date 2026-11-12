"""Owner-only Source evaluation configuration, queueing and bodyless observation."""

from collections.abc import Callable
from typing import Annotated, Self
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError, model_validator

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Model
from mgar_v3.super_agent import source_eval
from mgar_v3.super_agent.source_eval_domain import SourceEvalConfigure, SourceEvaluateRequest
from mgar_v3.web_access import SESSION_COOKIE, BrowserSession


class EvaluateBody(Model):
    """Decode JSON arrays/UUID strings before constructing the strict domain request."""

    fast_batch_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def distinct_batches(self) -> Self:
        if len(set(self.fast_batch_ids)) != len(self.fast_batch_ids):
            raise ValueError("Duplicate Fast Path batch reference")
        return self


def register(
    app: FastAPI,
    authority: Callable[[Request], Engine],
    browser_user: Callable[[Request], BrowserSession],
) -> None:
    prefix = "/api/v3/source-eval"

    def response(value: object, status_code: int = 200) -> JSONResponse:
        return JSONResponse(
            jsonable_encoder(value), status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    @app.put(prefix + "/{source_id}")
    def configure(
        source_id: UUID,
        body: SourceEvalConfigure,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_eval.SourceEval(engine).configure(
            request.cookies.get(SESSION_COOKIE, ""), source_id, body,
        ))

    @app.get(prefix + "/{source_id}")
    def read(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_eval.SourceEval(engine).read(
            request.cookies.get(SESSION_COOKIE, ""), source_id,
        ))

    @app.post(prefix + "/{source_id}/evaluate", status_code=202)
    def evaluate(
        source_id: UUID,
        body: EvaluateBody,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        try:
            command = SourceEvaluateRequest(fast_batch_ids=body.fast_batch_ids)
        except ValidationError:
            raise DomainError(
                "SOURCE_EVAL_REQUEST_INVALID",
                "Existing Fast Path batch references are required", 422,
            ) from None
        # Only enqueue here: model calls and business execution are not HTTP side effects.
        return response(source_eval.SourceEval(engine).enqueue(
            request.cookies.get(SESSION_COOKIE, ""), source_id, command,
        ), 202)

    @app.get(prefix + "/{source_id}/preview")
    def preview(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> JSONResponse:
        return response(source_eval.SourceEval(engine).preview(
            request.cookies.get(SESSION_COOKIE, ""), source_id, limit,
        ))

    @app.get(prefix + "/{source_id}/decisions")
    def decisions(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> JSONResponse:
        return response(source_eval.SourceEval(engine).decisions(
            request.cookies.get(SESSION_COOKIE, ""), source_id, limit,
        ))

    @app.get(prefix + "/{source_id}/metrics")
    def metrics(
        source_id: UUID,
        request: Request,
        engine: Annotated[Engine, Depends(authority)],
        user: Annotated[BrowserSession, Depends(browser_user)],
    ) -> JSONResponse:
        return response(source_eval.SourceEval(engine).metrics(
            request.cookies.get(SESSION_COOKIE, ""), source_id,
        ))
