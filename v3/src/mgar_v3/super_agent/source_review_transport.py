"""Adapt the original three-step Workbench protocol to original draft_pr Actions.

Unknown writes never re-enter send. The metadata-only reader also works after
Source content erasure, and verifies the whole Git tree and unique base parent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.effects.dispatcher import DefinitiveFailure
from mgar_v3.repair.pr_delivery import DraftPullRequest, ReviewBundle
from mgar_v3.super_agent.source_review_bundle import denied, sha256
from mgar_v3.super_agent.source_review_domain import ReviewContext
from mgar_v3.super_agent.source_review_store import Row, decode
from mgar_v3.workbench.publication import (
    PublicationUncertain,
    StepName,
    WorkbenchDraftTransport,
    WorkbenchPublicationLedger,
)

if TYPE_CHECKING:
    from mgar_v3.super_agent.source_review_ports import ReviewProfile
    from mgar_v3.super_agent.source_reviews import SourceReviews


class ReviewLedger(WorkbenchPublicationLedger):
    def __init__(self, service: SourceReviews) -> None:
        super().__init__(service.engine)
        self.service = service

    def begin_step(
        self, publication_id: Any, lease_token: Any, ordinal: int, kind: StepName, request_hash: str
    ) -> str:
        with self.engine.transaction() as conn:
            review = self.service._review(conn, publication_id)
            action = self.service._action_current(conn, review, require_approval=True)
            if action["status"] != "dispatching" or action["lease_token"] != lease_token:
                raise denied("LEASE_FENCED")
            step = conn.execute(
                "SELECT * FROM app.repair_publication_steps WHERE action_id=%s"
                " AND ordinal=%s FOR UPDATE",
                (action["action_id"], ordinal),
            ).fetchone()
            if step:
                if step["kind"] != kind or step["request_hash"] != request_hash:
                    raise denied("STEP_REQUEST_CHANGED")
                if step["status"] == "succeeded":
                    return "succeeded"
                raise PublicationUncertain("Original publication step is unknown; query only")
            conn.execute(
                "INSERT INTO app.repair_publication_steps(action_id,ordinal,kind,"
                "request_hash,status) "
                "VALUES(%s,%s,%s,%s,'dispatching')",
                (action["action_id"], ordinal, kind, request_hash),
            )
        return "dispatching"

    def receipt_step(
        self, publication_id: Any, ordinal: int, kind: StepName, receipt: Mapping[str, Any]
    ) -> None:
        with self.engine.transaction() as conn:
            review = self.service._review(conn, publication_id)
            step = conn.execute(
                "SELECT * FROM app.repair_publication_steps WHERE action_id=%s AND ordinal=%s",
                (review["action_id"], ordinal),
            ).fetchone()
            if step and step["kind"] != kind:
                raise denied("STEP_REQUEST_CHANGED")
            conn.execute(
                "INSERT INTO app.repair_publication_steps(action_id,ordinal,kind,"
                "request_hash,status,receipt) "
                "VALUES(%s,%s,%s,%s,'succeeded',%s) ON CONFLICT(action_id,ordinal) DO UPDATE "
                "SET status='succeeded',receipt=EXCLUDED.receipt",
                (review["action_id"], ordinal, kind, digest(dict(receipt)), Jsonb(dict(receipt))),
            )

    def attach_pull_request(self, publication_id: Any, pull: DraftPullRequest) -> None:
        with self.engine.transaction() as conn:
            review = self.service._review(conn, publication_id)
            context = decode(ReviewContext, review["context"])
            meta = review["publication_metadata"]
            if not matches_pull(meta, pull, pull.head_sha):
                raise denied("REMOTE_DRAFT_MISMATCH")
            facts = {
                "mode": "fixture",
                "source_kind": "fixture_git",
                "repository": pull.repository,
                "repository_id": context.publication.repository_id,
                "number": pull.number,
                "head_sha": pull.head_sha,
                "base_sha": pull.base_sha,
                "base_branch": pull.base_ref,
                "branch": pull.head_ref,
                "draft": True,
                "ticket_id": str(context.target.ticket_id),
                "run_id": str(context.target.run_id),
            }
            receipt = digest(facts)
            conn.execute(
                "INSERT INTO app.sa_source_review_links(link_id,review_id,kind,"
                "receipt_sha256,facts) "
                "VALUES(%s,%s,'draft_pr',%s,%s) ON CONFLICT(review_id,kind,"
                "receipt_sha256) DO NOTHING",
                (uuid4(), publication_id, receipt, Jsonb(facts)),
            )
            self.service._event(conn, review, "published", "CONTROLLED_DRAFT_VERIFIED")


def matches_pull(meta: Row, pull: DraftPullRequest, head: str) -> bool:
    return (
        pull.draft is True
        and pull.repository == meta["repository"]
        and pull.head_ref == meta["branch"]
        and pull.head_sha == head
        and pull.base_ref == meta["base_branch"]
        and pull.base_sha == meta["base_sha"]
        and sha256(pull.title) == meta["title_sha256"]
        and sha256(pull.body) == meta["body_sha256"]
    )


def matches_commit(profile: ReviewProfile, meta: Row, head: str) -> bool:
    identity = profile.reader.commit_identity(str(meta["repository"]), head)
    return (
        identity is not None
        and identity == (meta["git_tree"], (meta["base_sha"],))
        and profile.reader.commit_blobs(str(meta["repository"]), head) == meta["git_blobs"]
    )


class _Draft(WorkbenchDraftTransport):
    def __init__(self, service: SourceReviews, review: Row, profile: ReviewProfile) -> None:
        super().__init__(
            ReviewLedger(service), mode="fixture", reader=profile.reader, writer=profile.writer
        )
        self.service, self.review, self.profile = service, review, profile

    def _commit_matches(self, bundle: ReviewBundle, sha: str) -> bool:
        return matches_commit(self.profile, self.review["publication_metadata"], sha)

    def _remote_current(self, kind: StepName) -> None:
        meta = self.review["publication_metadata"]
        if self.reader.branch_sha(meta["repository"], meta["base_branch"]) != meta["base_sha"]:
            raise denied("REMOTE_BASE_CHANGED")
        head = self.reader.branch_sha(meta["repository"], meta["branch"])
        if kind == "branch" and head is not None:
            raise denied("REMOTE_BRANCH_CHANGED")
        if kind == "commit" and head != meta["base_sha"]:
            raise denied("REMOTE_BRANCH_CHANGED")
        if kind == "pull_request" and (
            head is None or not matches_commit(self.profile, meta, head)
        ):
            raise denied("REMOTE_BRANCH_CHANGED")

    def _write(
        self,
        publication_id: Any,
        lease: Any,
        ordinal: int,
        kind: StepName,
        request: Mapping[str, Any],
        write: Any,
    ) -> None:
        state = self.ledger.begin_step(publication_id, lease, ordinal, kind, digest(dict(request)))
        if state == "succeeded":
            return
        meta = self.review["publication_metadata"]
        with self.service.io_guard(publication_id, lease, lambda: self._remote_current(kind)):
            if kind == "branch":
                # Pin the exact reviewed commit rather than resolving a moving
                # base branch inside the remote create operation.
                self.writer.create_branch(meta["repository"], meta["branch"], meta["base_sha"])
            else:
                write()
        self._receipt(publication_id, ordinal, kind, {"already_present": False})


class ReviewTransport:
    mode = "fixture"
    action_types = ("draft_pr",)

    def __init__(self, service: SourceReviews) -> None:
        self.service = service

    def send(self, action: Row) -> str:
        try:
            with self.service.engine._mutex:
                with self.service.engine.transaction() as conn:
                    if not self.service.handles(action):
                        raise denied("ACTION_TARGET_CHANGED")
                    review = self.service._review(conn, UUID(action["payload"]["review_id"]))
                    actual = self.service._action_current(conn, review, require_approval=True)
                    if (
                        actual["status"] != "dispatching"
                        or actual["lease_token"] != action["lease_token"]
                    ):
                        raise denied("LEASE_FENCED")
                    context = decode(ReviewContext, review["context"])
                    profile = self.service._profile(conn, context.profile_sha256)
                    body = self.service._body_row(conn, review)
                    approval = conn.execute(
                        "SELECT publication_approval FROM app.sa_source_review_approvals "
                        "WHERE review_id=%s",
                        (context.review_id,),
                    ).fetchone()
                    assert approval is not None
                bundle = self.service._decrypt(review, body)
                if self.service._capture(context, profile) != bundle:
                    raise denied("ACTUAL_BUNDLE_CHANGED")
                enriched = {
                    **approval["publication_approval"],
                    "bundle": bundle.model_dump(mode="json"),
                    "publication_id": context.review_id,
                    "lease_token": action["lease_token"],
                }
                return _Draft(self.service, review, profile).send(enriched)
        except DomainError as exc:
            raise DefinitiveFailure(
                "Source review was fenced before a new remote write: " + exc.code
            ) from None

    def find(self, action: Row) -> str | None:
        # Reconciliation uses frozen hashes and the independent reader; no
        # decrypt, workspace capture, writer, approval renewal or retry.
        with self.service.engine.transaction() as conn:
            if not self.service.handles(action) or action["status"] != "outcome_unknown":
                raise denied("NOT_UNKNOWN")
            review = self.service._review(conn, UUID(action["payload"]["review_id"]))
            if review["action_id"] != action["action_id"]:
                raise denied("ACTION_TARGET_CHANGED")
            profile = self.service._profile(conn, review["profile_sha256"])
            approval = conn.execute(
                "SELECT 1 FROM app.sa_source_review_approvals WHERE review_id=%s",
                (review["review_id"],),
            ).fetchone()
            if approval is None:
                raise denied("EXACT_OWNER_APPROVAL_REQUIRED")
            meta = review["publication_metadata"]
        head = profile.reader.branch_sha(meta["repository"], meta["branch"])
        if head is None or head == meta["base_sha"] or not matches_commit(profile, meta, head):
            return None
        pull = profile.reader.find_draft(meta["repository"], meta["branch"], meta["base_branch"])
        if pull is None or not matches_pull(meta, pull, head):
            return None
        ledger = ReviewLedger(self.service)
        for ordinal, kind in enumerate(("branch", "commit", "pull_request")):
            ledger.receipt_step(
                review["review_id"],
                ordinal,
                cast(StepName, kind),
                {"already_present": True, "sha": head, "number": pull.number},
            )
        ledger.attach_pull_request(review["review_id"], pull)
        return str(pull.number)
