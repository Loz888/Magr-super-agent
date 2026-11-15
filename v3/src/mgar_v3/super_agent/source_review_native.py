"""Link trusted SA05 code/version facts to the original reviewed Ticket only."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import digest
from mgar_v3.super_agent.source_review_bundle import denied
from mgar_v3.super_agent.source_review_domain import ReviewContext
from mgar_v3.super_agent.source_review_store import Row

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection
    from mgar_v3.super_agent.source_correlation import _Input
    from mgar_v3.super_agent.source_reviews import SourceReviews


def link_proof(
    service: SourceReviews,
    conn: Connection,
    review: Row,
    context: ReviewContext,
    source: _Input,
    pull: Row,
) -> Row:
    original = service.engine.source_correlations._input(conn, context.origin.input.input_id)
    if original.ref != context.origin.input:
        raise denied("ORIGIN_NO_LONGER_READABLE", 403)
    target = service.engine.source_correlations._target(conn, context.target.ticket_id)
    if target.run_id != context.target.run_id or target.thread_id != context.target.thread_id:
        raise denied("ORIGINAL_TICKET_CHANGED")
    service.engine.source_correlations._context(source, target)
    native = [
        p
        for p in source.native
        if p.proof_kind == "verified_resource"
        and p.resource.kind == "pull_request"
        and p.resource.project_id == service.project
        and p.resource.repository == context.publication.repository
        and p.resource.repository_id == context.publication.repository_id
        and p.resource.resource_id == str(pull["number"])
    ]
    if len(native) != 1:
        raise denied("EXACT_NATIVE_PR_REQUIRED")
    proof = native[0]
    code = proof.code_ref
    if (
        code is None
        or code.head_sha != pull["head_sha"]
        or code.base_sha != pull["base_sha"]
        or code.head_repository_id not in {None, context.publication.repository_id}
    ):
        raise denied("NATIVE_HEAD_CHANGED")
    kind = source.item.event.event_kind.split(".", 1)[0]
    if kind not in {"workflow_run", "check_run", "check_suite", "pull_request"}:
        raise denied("NATIVE_EVENT_KIND_DENIED")
    if kind == "workflow_run" and (code.run_id is None or code.run_attempt is None):
        raise denied("NATIVE_CI_ATTEMPT_REQUIRED")
    facts = {
        "mode": "fixture",
        "source_kind": "fixture_git",
        "repository": context.publication.repository,
        "repository_id": context.publication.repository_id,
        "number": pull["number"],
        "head_sha": code.head_sha,
        "base_sha": code.base_sha,
        "ticket_id": str(context.target.ticket_id),
        "run_id": str(context.target.run_id),
        "source_input": source.ref.model_dump(mode="json"),
        "resource_proof": proof.model_dump(mode="json"),
        "workflow_id": code.run_id,
        "run_attempt": code.run_attempt,
    }
    receipt = digest(facts)
    conn.execute(
        "INSERT INTO app.sa_source_review_links(link_id,review_id,kind,receipt_sha256,facts) "
        "VALUES(%s,%s,'ci',%s,%s) ON CONFLICT(review_id,kind,receipt_sha256) DO NOTHING",
        (uuid4(), context.review_id, receipt, Jsonb(facts)),
    )
    service._event(conn, review, "native_linked", "EXACT_HEAD_ATTEMPT_LINKED")
    return {
        "review_id": context.review_id,
        "ticket_id": context.target.ticket_id,
        "receipt_sha256": receipt,
        "kind": kind,
        "mode": "fixture",
    }
