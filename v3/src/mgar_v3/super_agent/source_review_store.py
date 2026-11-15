"""SQL-only SA18 authority on the original Engine connection.

There is no worker bearer or new writer here. Completed worker facts are checked
against the current Owner/Source/Mandate and original publication fences.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.pq import TransactionStatus

from mgar_v3.domain import Model, digest
from mgar_v3.super_agent.source_coding_domain import within
from mgar_v3.super_agent.source_review_bundle import denied, sha256, validate_current
from mgar_v3.super_agent.source_review_domain import (
    REVIEW_VERSION,
    ReviewContext,
    ReviewCurrent,
    ReviewPrepare,
    ReviewTarget,
    ReviewVerification,
)
from mgar_v3.super_agent.source_review_ports import ReviewProfile

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine

Row = dict[str, Any]


def decode[T: Model](model: type[T], value: Any) -> T:
    # All stored contracts are strict. JSON is the explicitly typed boundary.
    return model.model_validate_json(json.dumps(value, default=str))


class ReviewStore:
    engine: Engine
    _profiles: dict[str, ReviewProfile]

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    def _conn(self, conn: Connection) -> None:
        if (
            conn is not self.engine.connection
            or conn.info.transaction_status != TransactionStatus.INTRANS
        ):
            raise denied("ORIGINAL_TRANSACTION_REQUIRED")

    def _ready(self, conn: Connection) -> bool:
        self._conn(conn)
        row = conn.execute("SELECT to_regclass('app.sa_source_reviews') AS t").fetchone()
        return bool(row and row["t"])

    def _console(self, conn: Connection, cookie: str) -> UUID:
        user = self.engine.source_correlations.identities._console(conn, cookie)
        if not self._ready(conn):
            raise denied("NOT_MIGRATED")
        return user

    def now(self, conn: Connection) -> datetime:
        self._conn(conn)
        return self.engine.source_ingress.now(conn)

    @staticmethod
    def handles(action: Row) -> bool:
        return (
            action.get("action_type") == "draft_pr"
            and (action.get("payload") or {}).get("scope_kind") == REVIEW_VERSION
        )

    def _review(self, conn: Connection, identifier: UUID) -> Row:
        self._conn(conn)
        row = conn.execute(
            "SELECT * FROM app.sa_source_reviews WHERE review_id=%s AND project_id=%s FOR UPDATE",
            (identifier, self.project),
        ).fetchone()
        if row is None:
            raise denied("NOT_FOUND", 404)
        return row

    def _event(self, conn: Connection, review: Row, kind: str, reason: str) -> None:
        self._conn(conn)
        conn.execute(
            "INSERT INTO app.sa_source_review_events(review_id,project_id,kind,reason_code) "
            "VALUES(%s,%s,%s,%s)",
            (review["review_id"], self.project, kind, "SOURCE_REVIEW_" + reason),
        )

    def _profile(self, conn: Connection, identifier: str) -> ReviewProfile:
        self._conn(conn)
        profile = self._profiles.get(identifier)
        if profile is None or profile.sha256 != identifier:
            raise denied("PROFILE_NOT_CONFIGURED")
        if profile.valid_until <= self.now(conn):
            raise denied("PROFILE_EXPIRED", 403)
        if profile.actor not in self.engine.settings.allowed_actors:
            raise denied("ACTOR_DENIED", 403)
        return profile

    def _latest(self, conn: Connection, coding_id: UUID) -> tuple[Row, ReviewVerification]:
        attempt = conn.execute(
            "SELECT * FROM app.sa_coding_attempts WHERE coding_id=%s AND project_id=%s "
            "ORDER BY created_at DESC,attempt_id DESC LIMIT 1 FOR UPDATE",
            (coding_id, self.project),
        ).fetchone()
        verification = conn.execute(
            "SELECT * FROM app.sa_coding_verifications WHERE coding_id=%s AND project_id=%s "
            "ORDER BY created_at DESC,verification_id DESC LIMIT 1",
            (coding_id, self.project),
        ).fetchone()
        if (
            attempt is None
            or verification is None
            or attempt["state"] != "result_recorded"
            or attempt["fenced_at"] is not None
            or verification["attempt_id"] != attempt["attempt_id"]
        ):
            raise denied("VERIFIED_FINAL_ATTEMPT_REQUIRED")
        try:
            fields = ReviewVerification.model_fields
            v = decode(ReviewVerification, {k: verification[k] for k in fields})
        except (ValueError, TypeError):
            raise denied("VERIFIED_FINAL_ATTEMPT_REQUIRED") from None
        pending = conn.execute(
            "SELECT 1 FROM app.sa_coding_operations WHERE coding_id=%s "
            "AND state IN ('queued','running','unknown') LIMIT 1",
            (coding_id,),
        ).fetchone()
        if pending:
            raise denied("WORKER_OPERATION_UNRESOLVED")
        return attempt, v

    def _async(
        self, conn: Connection, coding_id: UUID, verification: ReviewVerification
    ) -> tuple[int, int]:
        row = conn.execute(
            "SELECT * FROM app.sa_async_bindings WHERE coding_id=%s FOR UPDATE", (coding_id,)
        ).fetchone()
        if row is None:
            return 0, 0
        view = conn.execute(
            "SELECT * FROM app.sa_async_operation_views WHERE operation_id=%s",
            (verification.operation_id,),
        ).fetchone()
        if (
            row["state"] != "active"
            or row["expires_at"] <= self.now(conn)
            or view is None
            or view["async_id"] != row["async_id"]
            or view["input_revision"] != row["input_revision"]
            or view["expires_at"] <= self.now(conn)
            or row["applied_sequence"] != row["received_sequence"]
        ):
            raise denied("ASYNC_INPUT_CHANGED")
        pending = conn.execute(
            "SELECT 1 FROM app.sa_async_jobs WHERE coding_id=%s AND state<>'known' LIMIT 1",
            (coding_id,),
        ).fetchone()
        if pending:
            raise denied("JOB_UNRESOLVED")
        asynchronous = self.engine.source_async
        if asynchronous._scope(conn, row).scope_sha256 != row["scope_sha256"]:
            raise denied("ASYNC_SCOPE_CHANGED")
        checkpoint = asynchronous._checkpoint(conn, row, view["checkpoint_id"])
        asynchronous._dependencies(conn, row, checkpoint)
        return int(row["revision"]), int(row["input_revision"])

    def _base_facts(
        self,
        conn: Connection,
        coding_id: UUID,
        profile_sha256: str,
        user: UUID,
        *,
        require_primary: bool = True,
    ) -> tuple[Row, Row, Any, ReviewVerification, ReviewProfile, int, int]:
        coding = self.engine.source_coding
        binding = coding.binding(conn, coding_id)
        run, mandate = coding._mandate(conn, binding["run_id"])
        source = coding._source(conn, mandate)
        if (
            binding["state"] != "admitted"
            or binding["mandate_sha256"] != mandate.mandate_sha256
            or binding["revision"] != mandate.revision
            or mandate.signed_by != user
            or run["ticket_status"] != "open"
            or run["status"] not in {"running", "waiting_approval"}
            or run["execution_mode"] != "fixture"
            or run["input_revision"] != run["thread_revision"]
            or not mandate.valid_from <= self.now(conn) < mandate.valid_until
            or source.ref != mandate.origin.input
            or coding.context_sha256(source) != mandate.context_sha256
        ):
            raise denied("SOURCE_MANDATE_FENCED", 403)
        attempt, v = self._latest(conn, coding_id)
        profile = self._profile(conn, profile_sha256)
        if (
            profile.worker_profile_sha256 != mandate.worker_profile_sha256
            or profile.repository != mandate.repository.repository
            or profile.repository_id != mandate.repository.repository_id
            or mandate.repository.source_kind != "fixture_git"
            or attempt["execution_epoch"] != run["execution_epoch"]
            or attempt["initial_tree_sha256"] != mandate.repository.materialized_tree_sha256
            or v.mandate_sha256 != mandate.mandate_sha256
            or v.environment_sha256 != mandate.grant.environment_sha256
            or v.acceptance_sha256 != mandate.acceptance_sha256
            or v.profile_sha256 != mandate.worker_profile_sha256
        ):
            raise denied("TARGET_VERIFICATION_CHANGED")
        primary = conn.execute(
            "SELECT run_id FROM app.subscriptions WHERE thread_id=%s AND active AND role='primary'",
            (run["thread_id"],),
        ).fetchone()
        if (primary and primary["run_id"] != run["run_id"]) or (require_primary and not primary):
            raise denied("PRIMARY_REQUIRED")
        ar, ai = self._async(conn, coding_id, v)
        return run, attempt, mandate, v, profile, ar, ai

    def _prepare_context(
        self,
        conn: Connection,
        cookie: str,
        request: ReviewPrepare,
    ) -> ReviewContext:
        user = self._console(conn, cookie)
        run, attempt, mandate, v, profile, ar, ai = self._base_facts(
            conn, request.coding_id, request.profile_sha256, user, require_primary=False
        )
        if (
            request.verification_id != v.verification_id
            or request.expected_epoch != run["execution_epoch"]
            or request.expected_input_revision != run["thread_revision"]
        ):
            raise denied("REQUEST_VERSION_CONFLICT")
        now = self.now(conn)
        expires = min(
            now + timedelta(minutes=10),
            mandate.valid_until,
            mandate.origin.expires_at,
            profile.valid_until,
            v.observed_at + timedelta(minutes=15),
        )
        if expires <= now + timedelta(seconds=2):
            raise denied("EXPIRED")
        identifier = uuid4()
        return ReviewContext(
            review_id=identifier,
            coding_id=mandate.coding_id,
            target=mandate.target,
            origin=mandate.origin,
            attempt_id=attempt["attempt_id"],
            assignment_id=attempt["assignment_id"],
            session_id=attempt["session_id"],
            workspace_id=attempt["workspace_id"],
            epoch=run["execution_epoch"],
            input_revision=run["thread_revision"],
            async_revision=ar,
            async_input_revision=ai,
            mandate_sha256=mandate.mandate_sha256,
            coding_revision=mandate.revision,
            worker_profile_sha256=mandate.worker_profile_sha256,
            profile_sha256=profile.sha256,
            environment_sha256=mandate.grant.environment_sha256,
            acceptance_sha256=mandate.acceptance_sha256,
            baseline_tree_sha256=mandate.repository.materialized_tree_sha256,
            registration_sha256=mandate.repository.registration_sha256,
            context_sha256=mandate.context_sha256,
            publication=ReviewTarget(
                repository=profile.repository,
                repository_id=profile.repository_id,
                base_branch=profile.base_branch,
                base_sha=mandate.repository.materialized_commit,
                branch="sa18/" + identifier.hex,
            ),
            verification=v,
            actor=profile.actor,
            prepared_by=user,
            prepared_at=now,
            expires_at=expires,
        )

    def _current(self, conn: Connection, context: ReviewContext) -> ReviewCurrent:
        run, attempt, mandate, v, profile, ar, ai = self._base_facts(
            conn, context.coding_id, context.profile_sha256, context.prepared_by
        )
        if (
            attempt["attempt_id"] != context.attempt_id
            or attempt["assignment_id"] != context.assignment_id
            or attempt["session_id"] != context.session_id
            or attempt["workspace_id"] != context.workspace_id
            or v != context.verification
            or mandate.target != context.target
            or mandate.origin != context.origin
            or mandate.mandate_sha256 != context.mandate_sha256
            or mandate.revision != context.coding_revision
            or mandate.repository.materialized_commit != context.publication.base_sha
            or mandate.repository.registration_sha256 != context.registration_sha256
            or profile.base_branch != context.publication.base_branch
        ):
            raise denied("ORIGINAL_TARGET_CHANGED")
        facts = ReviewCurrent(
            context_sha256=mandate.context_sha256,
            epoch=run["execution_epoch"],
            input_revision=run["thread_revision"],
            async_revision=ar,
            async_input_revision=ai,
            tree_sha256=v.tree_sha256,
            profile_sha256=profile.sha256,
            user_id=context.prepared_by,
            actor=profile.actor,
            run_state=run["status"],
            source_current=True,
            roles_current=True,
            policy_current=True,
            target_current=True,
            verification_current=True,
            primary_current=True,
            write_grant_current=profile.write_authorized and profile.writer.scope == "write",
            no_running_jobs=True,
            owner_current=True,
        )
        validate_current(context, facts, at=self.now(conn))
        return facts

    def _action_current(self, conn: Connection, review: Row, *, require_approval: bool) -> Row:
        self._conn(conn)
        context = decode(ReviewContext, review["context"])
        if review["invalidated_at"] is not None or context.sha256 != review["context_sha256"]:
            raise denied("INVALIDATED")
        facts = self._current(conn, context)
        action, run = self.engine._action(conn, review["action_id"])
        expected_payload = {
            "scope_kind": REVIEW_VERSION,
            "review_id": str(context.review_id),
            "bundle_sha256": review["bundle_sha256"],
            "context_sha256": context.sha256,
            "profile_sha256": context.profile_sha256,
            "draft": True,
        }
        target = f"github:{context.publication.repository}:draft:{context.publication.base_branch}"
        expected = digest(
            {
                "type": "draft_pr",
                "target": target,
                "payload": expected_payload,
                "base_revision": context.input_revision,
            }
        )
        if (
            not action["unexpired"]
            or action["run_id"] != context.target.run_id
            or action["action_type"] != "draft_pr"
            or action["target"] != target
            or action["payload"] != expected_payload
            or action["payload_hash"] != expected
            or action["payload_hash"] != review["action_sha256"]
            or action["execution_epoch"] != context.epoch
            or action["base_revision"] != context.input_revision
            or action["status"] not in {"proposed", "ready", "dispatching"}
            or run["status"] != "waiting_approval"
        ):
            raise denied("ACTION_CHANGED")
        grant = conn.execute(
            "SELECT 1 FROM app.target_write_grants WHERE target=%s AND active AND run_id=%s "
            "AND execution_epoch=%s AND fencing_token=%s",
            (target, action["run_id"], context.epoch, action["fencing_token"]),
        ).fetchone()
        if not grant:
            raise denied("WRITE_FENCE_LOST", 403)
        if require_approval:
            approval = conn.execute(
                "SELECT a.*,p.session_sha256,p.expires_at AS review_expires FROM app.approvals a "
                "JOIN app.sa_source_review_approvals p USING(approval_id) "
                "JOIN app.web_sessions s ON s.token_hash=p.session_sha256 "
                "AND s.user_id=p.user_id AND s.expires_at>clock_timestamp() "
                "WHERE a.action_id=%s AND p.review_id=%s AND p.user_id=%s",
                (action["action_id"], context.review_id, context.prepared_by),
            ).fetchone()
            if (
                not facts.write_grant_current
                or approval is None
                or approval["payload_hash"] != expected
                or approval["actor"] != context.actor
                or approval["review_expires"] <= self.now(conn)
            ):
                raise denied("EXACT_OWNER_APPROVAL_REQUIRED", 403)
        return action

    def _capture_scope(
        self,
        conn: Connection,
        context: ReviewContext,
        before: dict[str, str],
        after: dict[str, str],
    ) -> None:
        self._current(conn, context)
        mandate = self.engine.source_coding.mandate(conn, context.coding_id)
        if any(not within(p, mandate.grant.read_paths) for p in before):
            raise denied("COMPLETE_TREE_READ_SCOPE_REQUIRED", 403)
        changed = [p for p in before if before[p] != after.get(p)]
        if any(
            not within(p, mandate.grant.write_paths) or within(p, mandate.grant.protected_paths)
            for p in changed
        ):
            raise denied("PATCH_SCOPE_CHANGED", 403)

    def _body_row(self, conn: Connection, review: Row) -> Row:
        row = conn.execute(
            "SELECT * FROM app.sa_source_review_payloads WHERE review_id=%s", (review["review_id"],)
        ).fetchone()
        if (
            row is None
            or row["unavailable_at"] is not None
            or row["ciphertext"] is None
            or row["expires_at"] <= self.now(conn)
            or sha256(bytes(row["ciphertext"])) != row["cipher_sha256"]
        ):
            raise denied("BODY_UNAVAILABLE")
        return row
