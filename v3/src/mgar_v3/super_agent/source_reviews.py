"""Owner review commands composed over the original Engine and publication ledger."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.repair.pr_delivery import PublicationApproval, ReviewBundle
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.source_review_bundle import (
    build_bundle,
    denied,
    encode_bundle,
    publication_metadata,
    sha256,
    validate_approval,
)
from mgar_v3.super_agent.source_review_domain import (
    PRESENTATION_SECONDS,
    REVIEW_VERSION,
    ReviewApprove,
    ReviewContext,
    ReviewExact,
    ReviewNativeLink,
    ReviewPrepare,
    ReviewPresentation,
)
from mgar_v3.super_agent.source_review_ports import ReviewProfile
from mgar_v3.super_agent.source_review_store import ReviewStore, Row, decode

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine


class SourceReviews(ReviewStore):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._profiles: dict[str, ReviewProfile] = {}
        self.cipher: BodyCipher | None = None

    def register_profile(self, profile: ReviewProfile) -> None:
        if not isinstance(profile, ReviewProfile):
            raise denied("SERVER_PROFILE_REQUIRED", 403)
        with self.engine._mutex:
            if profile.sha256 in self._profiles and self._profiles[profile.sha256] is not profile:
                raise denied("PROFILE_IMMUTABLE")
            self._profiles[profile.sha256] = profile

    def _cipher(self) -> BodyCipher:
        cipher = self.cipher or self.engine.source_coding.cipher
        if cipher is None:
            raise denied("ENCRYPTION_NOT_CONFIGURED", 503)
        return cipher

    def capabilities(self, cookie: str) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return {
                "schema_version": REVIEW_VERSION,
                "profiles": sorted(self._profiles),
                "engineering": "fixture_only",
                "live_publication": "NOT_RUN",
                "paid": False,
                "merge": False,
                "deploy": False,
            }

    @staticmethod
    def _binding(review: Row) -> str:
        return REVIEW_VERSION + ":" + str(review["review_id"]) + ":" + str(review["bundle_sha256"])

    @staticmethod
    def _exact(review: Row, request: ReviewExact) -> None:
        if (
            review["bundle_sha256"] != request.bundle_sha256
            or review["action_sha256"] != request.action_sha256
        ):
            raise denied("EXACT_HASH_REQUIRED", 403)

    def _decrypt(self, review: Row, body: Row) -> ReviewBundle:
        clear = self._cipher().decrypt(bytes(body["ciphertext"]), self._binding(review))
        if len(clear) != body["plaintext_bytes"] or sha256(clear) != body["content_sha256"]:
            raise denied("BODY_INTEGRITY_INVALID")
        bundle = ReviewBundle.model_validate_json(clear)
        if bundle.payload_hash != review["bundle_sha256"]:
            raise denied("BODY_INTEGRITY_INVALID")
        return bundle

    def _capture(self, context: ReviewContext, profile: ReviewProfile) -> ReviewBundle:
        before, after = profile.capture.capture(context)
        # Preparation already checked the complete path set against the frozen
        # Mandate. Every caller compares this exact bundle and rechecks current
        # authority after capture, before presentation/approval/physical IO.
        return build_bundle(context, before, after)

    def prepare(self, cookie: str, request: ReviewPrepare) -> Row:
        with self.engine._mutex:
            request_hash = digest(request.model_dump(mode="json"))
            with self.engine.transaction() as conn:
                self._console(conn, cookie)
                previous = conn.execute(
                    "SELECT * FROM app.sa_source_reviews WHERE project_id=%s "
                    "AND (request_id=%s OR verification_id=%s) ORDER BY created_at LIMIT 1",
                    (self.project, request.request_id, request.verification_id),
                ).fetchone()
                if previous is not None:
                    if (
                        previous["request_id"] == request.request_id
                        and previous["request_sha256"] != request_hash
                    ):
                        raise denied("REQUEST_ID_CONFLICT")
                    self._current(conn, decode(ReviewContext, previous["context"]))
                    return {**self._public(conn, previous), "created": False}
                context = self._prepare_context(conn, cookie, request)
                profile = self._profile(conn, context.profile_sha256)
            before, after = profile.capture.capture(context)
            bundle = build_bundle(context, before, after)
            raw = encode_bundle(bundle)
            review = {"review_id": context.review_id, "bundle_sha256": bundle.payload_hash}
            cipher = self._cipher().encrypt(raw, self._binding(review))
            with self.engine.transaction() as conn:
                if self._console(conn, cookie) != context.prepared_by:
                    raise denied("OWNER_CHANGED", 403)
                # Source Coding owns an original Thread/Run but does not create
                # a legacy publication subscription. This explicit Owner
                # preparation may create the first one; it never revives an
                # inactive row or replaces another primary. Every subsequent
                # use, including the scope check below, requires it to exist.
                subscription = conn.execute(
                    "SELECT active,role FROM app.subscriptions WHERE thread_id=%s AND run_id=%s",
                    (context.target.thread_id, context.target.run_id),
                ).fetchone()
                if subscription is None:
                    other = conn.execute(
                        "SELECT 1 FROM app.subscriptions WHERE thread_id=%s AND active"
                        " AND role='primary'",
                        (context.target.thread_id,),
                    ).fetchone()
                    if other:
                        raise denied("PRIMARY_REQUIRED")
                    conn.execute(
                        "INSERT INTO app.subscriptions(thread_id,run_id,role) VALUES(%s,"
                        "%s,'primary')",
                        (context.target.thread_id, context.target.run_id),
                    )
                self._capture_scope(conn, context, before, after)
                run = self.engine._run(conn, context.target.run_id)
                busy = conn.execute(
                    "SELECT 1 FROM app.actions WHERE run_id=%s AND action_type='draft_pr' "
                    "AND status IN ('proposed','ready','dispatching','outcome_unknown') LIMIT 1",
                    (run["run_id"],),
                ).fetchone()
                if busy:
                    raise denied("OTHER_PUBLICATION_UNRESOLVED")
                target = f"github:{bundle.repository}:draft:{bundle.base_branch}"
                grant = conn.execute(
                    "SELECT * FROM app.target_write_grants WHERE target=%s FOR UPDATE", (target,)
                ).fetchone()
                if grant and grant["active"] and grant["run_id"] != run["run_id"]:
                    raise denied("TARGET_WRITE_CONFLICT")
                fencing = int(grant["fencing_token"]) + 1 if grant else 1
                conn.execute(
                    "INSERT INTO app.target_write_grants(target,run_id,"
                    "execution_epoch,fencing_token) "
                    "VALUES(%s,%s,%s,%s) ON CONFLICT(target) DO UPDATE SET run_id=EXCLUDED.run_id,"
                    "execution_epoch=EXCLUDED.execution_epoch,"
                    "fencing_token=EXCLUDED.fencing_token,active=true",
                    (target, run["run_id"], context.epoch, fencing),
                )
                payload = {
                    "scope_kind": REVIEW_VERSION,
                    "review_id": str(context.review_id),
                    "bundle_sha256": bundle.payload_hash,
                    "context_sha256": context.sha256,
                    "profile_sha256": profile.sha256,
                    "draft": True,
                }
                action_hash = digest(
                    {
                        "type": "draft_pr",
                        "target": target,
                        "payload": payload,
                        "base_revision": context.input_revision,
                    }
                )
                action_id = uuid4()
                conn.execute(
                    "INSERT INTO app.actions(action_id,run_id,action_type,target,"
                    "payload,payload_hash,"
                    "base_revision,execution_epoch,fencing_token,dedupe_key,status,expires_at) "
                    "VALUES(%s,%s,'draft_pr',%s,%s,%s,%s,%s,%s,%s,'proposed',%s)",
                    (
                        action_id,
                        run["run_id"],
                        target,
                        Jsonb(payload),
                        action_hash,
                        context.input_revision,
                        context.epoch,
                        fencing,
                        "source-review:" + str(context.verification.verification_id),
                        context.expires_at,
                    ),
                )
                conn.execute(
                    "UPDATE app.runs SET status='waiting_approval',"
                    "reason_code='SOURCE_REVIEW_REQUIRED' "
                    "WHERE run_id=%s",
                    (run["run_id"],),
                )
                values = {
                    "review_id": context.review_id,
                    "project_id": self.project,
                    "request_id": request.request_id,
                    "request_sha256": request_hash,
                    "coding_id": context.coding_id,
                    "attempt_id": context.attempt_id,
                    "verification_id": context.verification.verification_id,
                    "run_id": run["run_id"],
                    "action_id": action_id,
                    "prepared_by": context.prepared_by,
                    "profile_sha256": profile.sha256,
                    "context_sha256": context.sha256,
                    "bundle_sha256": bundle.payload_hash,
                    "action_sha256": action_hash,
                    "context": Jsonb(context.model_dump(mode="json")),
                    "publication_metadata": Jsonb(publication_metadata(bundle, after)),
                    "created_at": context.prepared_at,
                    "expires_at": context.expires_at,
                }
                conn.execute(
                    "INSERT INTO app.sa_source_reviews("
                    + ",".join(values)
                    + ") VALUES("
                    + ",".join("%s" for _ in values)
                    + ")",
                    tuple(values.values()),
                )
                conn.execute(
                    "INSERT INTO app.sa_source_review_payloads(review_id,"
                    "bundle_sha256,content_sha256,"
                    "cipher_sha256,ciphertext,plaintext_bytes,expires_at) VALUES(%s,"
                    "%s,%s,%s,%s,%s,%s)",
                    (
                        context.review_id,
                        bundle.payload_hash,
                        sha256(raw),
                        sha256(cipher),
                        cipher,
                        len(raw),
                        context.expires_at,
                    ),
                )
                saved = self._review(conn, context.review_id)
                self._event(conn, saved, "prepared", "ACTUAL_VERIFIED_BUNDLE")
                return {**self._public(conn, saved), "created": True}

    def _public(self, conn: Connection, review: Row) -> Row:
        action, _ = self.engine._action(conn, review["action_id"])
        context = decode(ReviewContext, review["context"])
        links = conn.execute(
            "SELECT kind,facts,receipt_sha256,observed_at FROM app.sa_source_review_links "
            "WHERE review_id=%s ORDER BY observed_at LIMIT 64",
            (review["review_id"],),
        ).fetchall()
        for link in links:
            if link["kind"] == "draft_pr":
                f = link["facts"]
                link["controlled_receipt_uri"] = (
                    f"fixture://github/{f['repository']}/pull/{f['number']}"
                )
        return {
            "review_id": review["review_id"],
            "coding_id": review["coding_id"],
            "action_id": action["action_id"],
            "run_id": context.target.run_id,
            "ticket_id": context.target.ticket_id,
            "status": action["status"],
            "reason_code": review["reason_code"] or action["reason_code"],
            "bundle_sha256": review["bundle_sha256"],
            "action_sha256": review["action_sha256"],
            "expires_at": review["expires_at"],
            "invalidated_at": review["invalidated_at"],
            "target": context.publication.model_dump(mode="json"),
            "verification": context.verification.model_dump(mode="json"),
            "links": links,
            "mode": "fixture",
            "real_publication": "NOT_RUN",
        }

    def get(self, cookie: str, identifier: UUID) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return self._public(conn, self._review(conn, identifier))

    def list(self, cookie: str, limit: int = 32) -> Row:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            rows = conn.execute(
                "SELECT * FROM app.sa_source_reviews WHERE project_id=%s "
                "ORDER BY created_at DESC,review_id LIMIT %s",
                (self.project, limit + 1),
            ).fetchall()
            return {
                "reviews": [self._public(conn, r) for r in rows[:limit]],
                "partial": len(rows) > limit,
            }

    @staticmethod
    def _limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 32:
            raise denied("LIMIT_INVALID", 422)

    def _material(self, cookie: str, identifier: UUID) -> tuple[Row, ReviewBundle, ReviewContext]:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            review = self._review(conn, identifier)
            context = decode(ReviewContext, review["context"])
            if user != context.prepared_by:
                raise denied("OWNER_CHANGED", 403)
            self._action_current(conn, review, require_approval=False)
            body = self._body_row(conn, review)
            profile = self._profile(conn, context.profile_sha256)
        bundle = self._decrypt(review, body)
        if self._capture(context, profile) != bundle:
            raise denied("ACTUAL_BUNDLE_CHANGED")
        return review, bundle, context

    def inspect(self, cookie: str, identifier: UUID) -> Row:
        with self.engine._mutex:
            review, bundle, context = self._material(cookie, identifier)
            with self.engine.transaction() as conn:
                self._console(conn, cookie)
                self._action_current(conn, self._review(conn, identifier), require_approval=False)
                now = self.now(conn)
                p = ReviewPresentation(
                    presentation_id=uuid4(),
                    review_id=identifier,
                    user_id=context.prepared_by,
                    session_sha256=sha256(cookie),
                    context_sha256=context.sha256,
                    bundle_sha256=review["bundle_sha256"],
                    action_sha256=review["action_sha256"],
                    presented_at=now,
                    expires_at=min(
                        now + timedelta(seconds=PRESENTATION_SECONDS), context.expires_at
                    ),
                )
                values = p.model_dump()
                conn.execute(
                    "INSERT INTO app.sa_source_review_presentations("
                    + ",".join(values)
                    + ") VALUES("
                    + ",".join("%s" for _ in values)
                    + ")",
                    tuple(values.values()),
                )
                self._event(conn, review, "presented", "COMPLETE_BUNDLE_PRESENTED")
                return {
                    "review": self._public(conn, review),
                    "bundle": bundle.model_dump(mode="json"),
                    "context_sha256": context.sha256,
                    "presentation": p.model_dump(mode="json"),
                    "complete": True,
                    "write_configured": self._profiles[context.profile_sha256].write_authorized,
                }

    def approve(self, cookie: str, identifier: UUID, request: ReviewApprove) -> Row:
        with self.engine._mutex:
            review, bundle, context = self._material(cookie, identifier)
            self._exact(review, request)
            with self.engine.transaction() as conn:
                user = self._console(conn, cookie)
                review = self._review(conn, identifier)
                action = self._action_current(conn, review, require_approval=False)
                existing = conn.execute(
                    "SELECT * FROM app.sa_source_review_approvals WHERE review_id=%s", (identifier,)
                ).fetchone()
                if existing:
                    if existing["presentation_id"] != request.presentation_id:
                        raise denied("ALREADY_APPROVED_ANOTHER_PRESENTATION")
                    return self._public(conn, review)
                row = conn.execute(
                    "SELECT * FROM app.sa_source_review_presentations WHERE presentation_id=%s",
                    (request.presentation_id,),
                ).fetchone()
                if row is None:
                    raise denied("PRESENTATION_REQUIRED", 403)
                p = decode(ReviewPresentation, row)
                now = self.now(conn)
                validate_approval(
                    context,
                    self._current(conn, context),
                    p,
                    request,
                    user_id=user,
                    session_sha256=sha256(cookie),
                    bundle_sha256=review["bundle_sha256"],
                    action_sha256=review["action_sha256"],
                    at=now,
                )
                approval = PublicationApproval.grant(
                    bundle, actor=context.actor, approved_at=now.isoformat()
                )
                approval_id = uuid4()
                conn.execute(
                    "INSERT INTO app.approvals(approval_id,action_id,actor,"
                    "payload_hash,approved_at) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (approval_id, action["action_id"], context.actor, review["action_sha256"], now),
                )
                conn.execute(
                    "INSERT INTO app.sa_source_review_approvals(review_id,"
                    "presentation_id,approval_id,"
                    "user_id,session_sha256,bundle_sha256,action_sha256,"
                    "publication_approval,approved_at,"
                    "expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        identifier,
                        p.presentation_id,
                        approval_id,
                        user,
                        sha256(cookie),
                        review["bundle_sha256"],
                        review["action_sha256"],
                        Jsonb(approval.model_dump(mode="json")),
                        now,
                        context.expires_at,
                    ),
                )
                conn.execute(
                    "UPDATE app.actions SET status='ready' WHERE action_id=%s",
                    (action["action_id"],),
                )
                conn.execute(
                    "INSERT INTO app.outbox(outbox_id,action_id,status) VALUES(%s,%s,'ready')",
                    (uuid4(), action["action_id"]),
                )
                self._event(conn, review, "approved", "OWNER_EXACT_HASH_APPROVED")
                return self._public(conn, review)

    def validate_action(self, conn: Connection, action: Row, run: Row) -> None:
        if not self._ready(conn) or not self.handles(action):
            raise denied("NOT_WIRED")
        review = self._review(conn, UUID(action["payload"]["review_id"]))
        if review["action_id"] != action["action_id"] or review["run_id"] != run["run_id"]:
            raise denied("ACTION_TARGET_CHANGED")
        # A generic Engine.approve call lacks this Owner presentation/approval.
        self._action_current(conn, review, require_approval=True)

    @contextmanager
    def io_guard(self, identifier: UUID, lease: UUID) -> Iterator[None]:
        with self.engine._mutex:
            with self.engine.transaction() as conn:
                review = self._review(conn, identifier)
                action = self._action_current(conn, review, require_approval=True)
                if action["status"] != "dispatching" or action["lease_token"] != lease:
                    raise denied("LEASE_FENCED")
                context = decode(ReviewContext, review["context"])
                profile = self._profile(conn, context.profile_sha256)
                body = self._body_row(conn, review)
            if self._capture(context, profile) != self._decrypt(review, body):
                raise denied("ACTUAL_BUNDLE_CHANGED")
            # Last authority read occurs after both step reservation and capture.
            with self.engine.transaction() as conn:
                review = self._review(conn, identifier)
                action = self._action_current(conn, review, require_approval=True)
                if action["status"] != "dispatching" or action["lease_token"] != lease:
                    raise denied("LEASE_FENCED")
            yield

    def cancel(self, cookie: str, identifier: UUID, request: ReviewExact) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            review = self._review(conn, identifier)
            self._exact(review, request)
            action, _ = self.engine._action(conn, review["action_id"])
            if action["status"] not in {"proposed", "ready", "approved"}:
                raise denied("INFLIGHT_OR_SETTLED")
            conn.execute(
                "SELECT app.sa_source_review_invalidate(%s,%s)",
                (identifier, "SOURCE_REVIEW_OWNER_CANCELLED"),
            )
            return self._public(conn, self._review(conn, identifier))

    def patch(self, cookie: str, identifier: UUID) -> str:
        with self.engine._mutex:
            _, bundle, _ = self._material(cookie, identifier)
            return bundle.diff

    def _maintain(self, conn: Connection, limit: int) -> Row:
        rows = conn.execute(
            "SELECT * FROM app.sa_source_reviews WHERE project_id=%s AND invalidated_at IS NULL "
            "ORDER BY created_at,review_id LIMIT %s",
            (self.project, limit),
        ).fetchall()
        invalidated = 0
        for review in rows:
            try:
                self._current(conn, decode(ReviewContext, review["context"]))
            except DomainError:
                conn.execute(
                    "SELECT app.sa_source_review_invalidate(%s,%s)",
                    (review["review_id"], "SOURCE_REVIEW_AUTHORITY_EXPIRED"),
                )
                invalidated += 1
        return {"checked": len(rows), "invalidated": invalidated, "remote_calls": 0}

    def maintain(self, cookie: str, limit: int = 8) -> Row:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return self._maintain(conn, limit)

    def recover_conn(self, conn: Connection) -> None:
        if self._ready(conn):
            self._maintain(conn, 32)

    def events(self, cookie: str, limit: int = 32) -> Row:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return {
                "events": conn.execute(
                    "SELECT review_id,kind,reason_code,created_at FROM app.sa_source_review_events "
                    "WHERE project_id=%s ORDER BY event_id DESC LIMIT %s",
                    (self.project, limit),
                ).fetchall()
            }

    def tick(self, cookie: str, limit: int = 8) -> Row:
        from mgar_v3.effects.dispatcher import Dispatcher
        from mgar_v3.super_agent.source_review_transport import ReviewTransport

        self._limit(limit)
        identifiers = []
        with self.engine._mutex:
            with self.engine.transaction() as conn:
                self._console(conn, cookie)
                # Original Dispatcher claims draft_pr. Never consume another
                # subsystem's pending Draft in this explicit fixture endpoint.
                other = conn.execute(
                    "SELECT 1 FROM app.outbox o JOIN app.actions a USING(action_id) "
                    "JOIN app.runs r USING(run_id) WHERE o.status='ready' AND a.status='ready' "
                    "AND a.action_type='draft_pr' AND r.execution_mode='fixture' "
                    "AND o.next_retry_at<=now() AND a.payload->>'scope_kind' IS"
                    " DISTINCT FROM %s LIMIT 1",
                    (REVIEW_VERSION,),
                ).fetchone()
                if other:
                    return {"actions": [], "reason_code": "SOURCE_REVIEW_OTHER_DISPATCHER_REQUIRED"}
            dispatcher = Dispatcher(self.engine, ReviewTransport(self))
            for _ in range(limit):
                identifier = dispatcher.tick()
                if identifier is None:
                    break
                identifiers.append(identifier)
        return {"actions": identifiers, "mode": "fixture", "real_publication": "NOT_RUN"}

    def reconcile(self, cookie: str, identifier: UUID) -> Row:
        from mgar_v3.effects.dispatcher import Dispatcher
        from mgar_v3.super_agent.source_review_transport import ReviewTransport

        with self.engine._mutex:
            with self.engine.transaction() as conn:
                self._console(conn, cookie)
                review = self._review(conn, identifier)
                action, _ = self.engine._action(conn, review["action_id"])
                if action["status"] != "outcome_unknown":
                    raise denied("NOT_UNKNOWN")
            known = Dispatcher(self.engine, ReviewTransport(self)).reconcile(review["action_id"])
            return {**self.get(cookie, identifier), "reconciled": known}

    def link_native(self, cookie: str, identifier: UUID, request: ReviewNativeLink) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            review = self._review(conn, identifier)
            self._exact(review, request)
            context = decode(ReviewContext, review["context"])
            source = self.engine.source_correlations._input(conn, request.source_input.input_id)
            if source.ref != request.source_input or source.ref.project_id != self.project:
                raise denied("NATIVE_SOURCE_CHANGED")
            # ResourceProof is produced by the trusted SA05 normalizer, not by
            # these HTTP fields. Relations never grant access to Source bodies.
            links = conn.execute(
                "SELECT facts FROM app.sa_source_review_links WHERE review_id=%s "
                "AND kind='draft_pr' ORDER BY observed_at LIMIT 1",
                (identifier,),
            ).fetchone()
            if not source.native or links is None:
                raise denied("EXACT_NATIVE_PROOF_REQUIRED")
            from mgar_v3.super_agent.source_review_native import link_proof

            return link_proof(self, conn, review, context, source, links["facts"])
