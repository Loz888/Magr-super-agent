"""Read an opt-in frozen trial artifact under the existing Owner and Recall ACL."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_recall_access import RecallAccess, RecallCaller
from mgar_v3.super_agent.source_recall_rules import denied
from mgar_v3.super_agent.source_recall_store import RecallStore
from mgar_v3.web_access import SESSION_COOKIE

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine

HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _read_pinned(path: Path, expected: str, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if not data or len(data) > limit or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Frozen trial artifact does not match the authorized bytes")
    return data


@dataclass(frozen=True)
class TrialReview:
    run_id: UUID
    manifest_sha256: str
    diff_sha256: str
    final_tree_sha256: str
    verification: dict[str, Any]
    text: str

    @classmethod
    def load(cls, directory: Path, manifest_sha256: str) -> TrialReview:
        metadata_bytes = _read_pinned(directory / "verification.json", manifest_sha256, 32768)
        metadata = json.loads(metadata_bytes)
        verification = metadata["public_verification"]
        run_id = UUID(metadata["run_id"])
        final_tree = metadata["final_tree_sha256"]
        if (
            verification["tree_sha256"] != final_tree
            or verification["result"] != "incomplete"
            or metadata["formal_verifier"] != "incomplete"
            or metadata["original_git_publication"] is not False
        ):
            raise ValueError("Read-only trial manifest must match the recorded final tree")
        patch = _read_pinned(directory / "patch.diff", metadata["diff_sha256"], 262144)
        return cls(
            run_id, manifest_sha256, metadata["diff_sha256"], final_tree, verification,
            "SA22 试验审查\n\n" + metadata_bytes.decode() + "\n" + patch.decode(),
        )

    def view(self, engine: Engine, cookie: str) -> str:
        with engine.transaction() as conn:
            access = RecallAccess(engine, conn, RecallCaller("owner", cookie))
            store = RecallStore(access)
            store._run(self.run_id, content=True)
            row = conn.execute(
                "SELECT v.result,v.tree_sha256,v.tests_run,v.tests_failed "
                "FROM app.sa_coding_verifications v JOIN app.sa_coding_bindings b USING(coding_id) "
                "WHERE b.run_id=%s AND b.project_id=%s AND b.state='admitted' "
                "ORDER BY v.observed_at DESC,v.verification_id DESC LIMIT 1",
                (self.run_id, engine.settings.project_id),
            ).fetchone()
            artifact = conn.execute(
                "SELECT safe_payload->>'patch_sha256' AS sha FROM app.run_events "
                "WHERE run_id=%s AND kind='source_trial_artifact_available' "
                "ORDER BY event_id DESC LIMIT 1", (self.run_id,),
            ).fetchone()
            if (
                row is None or artifact is None or artifact["sha"] != self.diff_sha256
                or any(row[key] != self.verification[key] for key in row)
            ):
                raise denied()
            access.check()
            engine._event(
                conn, self.run_id, "source_trial_review_viewed",
                manifest_sha256=self.manifest_sha256, diff_sha256=self.diff_sha256,
                tree_sha256=self.final_tree_sha256,
            )
        return self.text


def register_trial_review(app: FastAPI, engine: Engine, artifact: TrialReview) -> None:
    """Register one explicitly configured route on the private trial application."""

    @app.get(f"/source-trial-review/{artifact.run_id}", include_in_schema=False)
    def trial_review(request: Request) -> PlainTextResponse:
        cookie = request.cookies.get(SESSION_COOKIE)
        if not cookie or request.headers.get("sec-fetch-site") == "cross-site":
            return PlainTextResponse("需要在本机登录后打开此页面。", 401, headers=HEADERS)
        try:
            result = artifact.view(engine, cookie)
        except DomainError as error:
            return PlainTextResponse("当前授权无法查看此试验产物。", error.status, headers=HEADERS)
        return PlainTextResponse(result, headers=HEADERS)
