"""SA14 reference inbox and original resource observations on the held Engine conn.

There is no default runner, thread, provider, listener or second business writer.
Bodies are materialized only after current Source authorization and never stored
in these tables. Original Coding/SA12/usage remain execution and cost authority.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.super_agent.source_async_domain import (
    ASYNC_VERSION,
    AsyncAdmitRequest,
    AsyncCheckpoint,
    AsyncCurrentFacts,
    AsyncInputFacts,
    AsyncInputReceipt,
    AsyncJobCancelRequest,
    AsyncRestriction,
    AsyncRestrictRequest,
    AsyncScope,
    AsyncWorkerJobCancelRequest,
    AsyncWorkerRequest,
    SafePointKind,
)
from mgar_v3.super_agent.source_async_plan import PendingInput, plan_safe_point
from mgar_v3.super_agent.source_async_rules import classify_input, validate_mutation, validate_view
from mgar_v3.super_agent.source_coding_domain import CodingRunRequest, within
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.super_agent.source_correlation import _Input

Row = dict[str, Any]


def _deny(code: str, status: int = 409) -> DomainError:
    return DomainError(code, "Async work requires current exact original scoped facts", status)


def _decode[T: Model](model: type[T], value: Any) -> T:
    return model.model_validate_json(json.dumps(value, default=str))


class AsyncRunner(Protocol):
    def start(self, bearer: str, request: CodingRunRequest, async_id: UUID) -> None: ...


@dataclass(frozen=True)
class AsyncControlProfile:
    """Explicit immutable server configuration; never deserialized from HTTP."""

    database_restriction: AsyncRestriction

    @property
    def profile_sha256(self) -> str:
        return digest(
            {
                "version": ASYNC_VERSION,
                "database": self.database_restriction.model_dump(mode="json"),
            }
        )


class SourceAsync:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.runtime: AsyncRunner | None = None
        self._profiles: dict[str, AsyncControlProfile] = {}
        self._executing: ContextVar[UUID | None] = ContextVar(
            "sa14_original_execution_scope", default=None
        )

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    def _conn(self, conn: Connection) -> None:
        if (
            conn is not self.engine.connection
            or conn.info.transaction_status != TransactionStatus.INTRANS
        ):
            raise _deny("SOURCE_ASYNC_ORIGINAL_TRANSACTION_REQUIRED", 403)

    def _ready(self, conn: Connection) -> bool:
        self._conn(conn)
        row = conn.execute(
            "SELECT to_regclass('app.sa_async_bindings') IS NOT NULL AS ready"
        ).fetchone()
        return bool(row and row["ready"])

    def now(self, conn: Connection) -> datetime:
        self._conn(conn)
        row = conn.execute("SELECT clock_timestamp() AS at").fetchone()
        assert row is not None
        return cast(datetime, row["at"])

    def _owner(self, conn: Connection, cookie: str) -> UUID:
        self._conn(conn)
        user = self.engine.source_coding._console(conn, cookie)
        if not self._ready(conn):
            raise _deny("SOURCE_ASYNC_NOT_MIGRATED")
        return user

    def register_profile(self, coding_profile_sha256: str, profile: AsyncControlProfile) -> None:
        original = self.engine.source_coding._profiles.get(coding_profile_sha256)
        if (
            not isinstance(profile, AsyncControlProfile)
            or original is None
            or any(
                not within(path, original.grant.write_paths)
                for path in profile.database_restriction.deny_write_paths
            )
            or not set(profile.database_restriction.deny_commands)
            <= set(original.grant.allowed_commands)
        ):
            raise _deny("SOURCE_ASYNC_PROFILE_INVALID", 422)
        with self.engine._mutex:
            previous = self._profiles.get(coding_profile_sha256)
            if previous is not None and previous != profile:
                raise _deny("SOURCE_ASYNC_PROFILE_IMMUTABLE")
            self._profiles[coding_profile_sha256] = profile

    def _profile(self, row: Row) -> AsyncControlProfile:
        profile = self._profiles.get(row["profile_sha256"])
        if profile is None:
            raise _deny("SOURCE_ASYNC_PROFILE_REQUIRED")
        return profile

    def capabilities(self, cookie: str) -> Row:
        with self.engine.transaction() as conn:
            self._owner(conn, cookie)
            return {
                "version": ASYNC_VERSION,
                "runtime_configured": self.runtime is not None,
                "profiles_registered": len(self._profiles),
                "default_running": False,
                "controlled_fixture_only": True,
                "live_agent": "NOT_RUN",
                "paid": "NOT_RUN",
            }

    def managed(self, conn: Connection, coding_id: UUID) -> bool:
        self._conn(conn)
        if not self._ready(conn):
            return False
        return (
            conn.execute(
                "SELECT 1 FROM app.sa_async_bindings WHERE coding_id=%s AND project_id=%s",
                (coding_id, self.project),
            ).fetchone()
            is not None
        )

    @contextmanager
    def execution_scope(self, async_id: UUID) -> Iterator[None]:
        # This process-local marker only prevents legacy entrypoint bypass.
        # Every operation still validates PG authority and the exact scope.
        token = self._executing.set(async_id)
        try:
            yield
        finally:
            self._executing.reset(token)

    def check_managed_access(self, conn: Connection, coding_id: UUID) -> None:
        if not self.managed(conn, coding_id):
            return
        row = self.binding(conn, coding_id)
        if self._executing.get() != row["async_id"]:
            raise _deny("SOURCE_ASYNC_EXPLICIT_START_REQUIRED", 403)
        if (
            row["state"] != "active"
            or row["expires_at"] <= self.now(conn)
            or self._scope(conn, row).scope_sha256 != row["scope_sha256"]
        ):
            raise _deny("SOURCE_ASYNC_AUTHORITY_FENCED", 403)
        if row["current_checkpoint_id"]:
            self._dependencies(conn, row, self._checkpoint(conn, row, row["current_checkpoint_id"]))

    def binding(self, conn: Connection, coding_id: UUID) -> Row:
        self._conn(conn)
        row = conn.execute(
            "SELECT * FROM app.sa_async_bindings WHERE coding_id=%s AND project_id=%s FOR UPDATE",
            (coding_id, self.project),
        ).fetchone()
        if row is None:
            raise _deny("SOURCE_ASYNC_BINDING_NOT_FOUND", 404)
        # The original authority guard joins many small scoped tables. Repeated
        # JIT compilation is counterproductive for this bounded control path
        # and can consume its short lease/TTL. This is transaction-local and
        # changes neither the predicates nor other Engine transactions.
        conn.execute("SET LOCAL jit = off")
        return row

    def _coding_request(self, row: Row, *, request_id: UUID | None = None) -> CodingRunRequest:
        return CodingRunRequest(
            request_id=request_id or row["request_id"],
            coding_id=row["coding_id"],
            run_id=row["run_id"],
            assignment_id=row["assignment_id"],
            session_id=row["session_id"],
            epoch=row["execution_epoch"],
            expected_revision=row["coding_revision"],
        )

    def _scope(self, conn: Connection, row: Row) -> AsyncScope:
        mandate = self.engine.source_coding.mandate(conn, row["coding_id"])
        return AsyncScope(
            coding_id=row["coding_id"],
            coding_revision=row["coding_revision"],
            target=mandate.target,
            assignment_id=row["assignment_id"],
            session_id=row["session_id"],
            workspace_id=row["workspace_id"],
            epoch=row["execution_epoch"],
            mandate_sha256=row["mandate_sha256"],
            profile_sha256=row["profile_sha256"],
            initial_context_sha256=row["initial_context_sha256"],
            control_profile_sha256=self._profile(row).profile_sha256,
        )

    @staticmethod
    def _cas(row: Row, revision: int) -> None:
        if row["revision"] != revision:
            raise _deny("SOURCE_ASYNC_REVISION_CONFLICT")

    def _worker(self, conn: Connection, bearer: str, row: Row, request: AsyncWorkerRequest) -> None:
        self.engine.source_scheduler._session(conn, request.session_id, bearer)
        if any(
            (
                request.coding_id != row["coding_id"],
                request.run_id != row["run_id"],
                request.assignment_id != row["assignment_id"],
                request.session_id != row["session_id"],
                request.epoch != row["execution_epoch"],
            )
        ):
            raise _deny("SOURCE_ASYNC_WORKER_SCOPE_DENIED", 403)

    @staticmethod
    def _view(row: Row) -> Row:
        return {
            key: row[key]
            for key in (
                "async_id",
                "coding_id",
                "run_id",
                "assignment_id",
                "session_id",
                "execution_epoch",
                "state",
                "reason_code",
                "revision",
                "input_revision",
                "received_sequence",
                "applied_sequence",
                "restriction",
                "expires_at",
            )
        }

    def _event(
        self,
        conn: Connection,
        row: Row,
        kind: str,
        *,
        input_id: UUID | None = None,
        job_id: UUID | None = None,
        checkpoint_id: UUID | None = None,
        reason: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO "
            "app.sa_async_events(async_id,project_id,kind,input_id,job_id,checkpoint_id,"
            "input_revision,reason_code) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row["async_id"],
                self.project,
                kind,
                input_id,
                job_id,
                checkpoint_id,
                row["input_revision"],
                reason,
            ),
        )

    def admit(self, cookie: str, request: AsyncAdmitRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._owner(conn, cookie)
            original = self.engine.source_coding.binding(conn, request.coding_id)
            if (
                original["run_id"] != request.run_id
                or original["revision"] != request.expected_coding_revision
            ):
                raise _deny("SOURCE_ASYNC_TARGET_STALE")
            profile = self._profile(original)
            fingerprint = digest(
                {
                    "request": request.model_dump(mode="json"),
                    "control_profile": profile.profile_sha256,
                }
            )
            previous = conn.execute(
                "SELECT * FROM app.sa_async_bindings WHERE coding_id=%s AND project_id=%s FOR "
                "UPDATE",
                (request.coding_id, self.project),
            ).fetchone()
            if previous is not None:
                if (
                    previous["request_id"] != request.request_id
                    or previous["request_sha256"] != fingerprint
                ):
                    raise _deny("SOURCE_ASYNC_ADMISSION_CONFLICT")
                return self._view(previous)
            assignment = conn.execute(
                "SELECT * FROM app.sa_scheduler_assignments WHERE run_id=%s AND project_id=%s "
                "AND state='active'",
                (request.run_id, self.project),
            ).fetchone()
            if assignment is None or assignment["execution_epoch"] != request.expected_epoch:
                raise _deny("SOURCE_ASYNC_ASSIGNMENT_REQUIRED")
            if conn.execute(
                "SELECT 1 FROM app.sa_coding_attempts WHERE coding_id=%s", (request.coding_id,)
            ).fetchone():
                raise _deny("SOURCE_ASYNC_ADMIT_BEFORE_WORKER_REQUIRED")
            mandate = self.engine.source_coding.mandate(conn, request.coding_id)
            self.engine.source_coding._source(conn, mandate)
            self.engine.source_coding.fresh_context(conn, mandate)
            if not self.engine.source_coding._budget_current(conn, mandate):
                raise _deny("SOURCE_ASYNC_BUDGET_FENCED", 403)
            scope = AsyncScope(
                coding_id=request.coding_id,
                coding_revision=original["revision"],
                target=mandate.target,
                assignment_id=assignment["assignment_id"],
                session_id=assignment["session_id"],
                workspace_id=assignment["workspace_id"],
                epoch=assignment["execution_epoch"],
                mandate_sha256=mandate.mandate_sha256,
                profile_sha256=mandate.worker_profile_sha256,
                initial_context_sha256=mandate.context_sha256,
                control_profile_sha256=profile.profile_sha256,
            )
            row = conn.execute(
                "INSERT INTO "
                "app.sa_async_bindings(async_id,coding_id,project_id,run_id,coding_revision,"
                "assignment_id,session_id,workspace_id,execution_epoch,mandate_sha256,"
                "profile_sha256,initial_context_sha256,"
                "scope_sha256,request_id,request_sha256,signed_by,review_sha256,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    uuid4(),
                    request.coding_id,
                    self.project,
                    request.run_id,
                    original["revision"],
                    scope.assignment_id,
                    scope.session_id,
                    scope.workspace_id,
                    scope.epoch,
                    scope.mandate_sha256,
                    scope.profile_sha256,
                    scope.initial_context_sha256,
                    scope.scope_sha256,
                    request.request_id,
                    fingerprint,
                    user,
                    digest(request.review_ref),
                    min(mandate.valid_until, mandate.origin.expires_at),
                ),
            ).fetchone()
            assert row is not None
            self._event(conn, row, "admitted")
            return self._view(row)

    def get(self, cookie: str, coding_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self._owner(conn, cookie)
            return self._view(self.binding(conn, coding_id))

    def _append(
        self,
        conn: Connection,
        row: Row,
        *,
        request_id: UUID,
        request_sha256: str,
        facts: AsyncInputFacts,
        source: _Input | None = None,
        owner_id: UUID | None = None,
        resource_operation_id: UUID | None = None,
    ) -> Row:
        previous = conn.execute(
            "SELECT * FROM app.sa_async_inputs WHERE async_id=%s AND request_id=%s",
            (row["async_id"], request_id),
        ).fetchone()
        if previous is not None:
            if previous["request_sha256"] != request_sha256:
                raise _deny("SOURCE_ASYNC_INPUT_CONFLICT")
            return previous
        ordinary = conn.execute(
            "SELECT count(*) AS n FROM app.sa_async_inputs WHERE async_id=%s", (row["async_id"],)
        ).fetchone()
        assert ordinary is not None
        if ordinary["n"] >= 256:
            conn.execute(
                "SELECT app.sa_async_invalidate(%s,%s)",
                (row["async_id"], "SOURCE_ASYNC_INPUT_LIMIT"),
            )
            return {
                "state": "blocked",
                "reason_code": "SOURCE_ASYNC_INPUT_LIMIT",
                "async_id": row["async_id"],
            }
        now = self.now(conn)
        valid = (
            classify_input(facts, at=now) != "blocked"
            and row["state"] == "active"
            and row["expires_at"] > now
        )
        state = "received" if valid else "blocked"
        reason = None if valid else "SOURCE_ASYNC_INPUT_DENIED"
        updated = conn.execute(
            "UPDATE app.sa_async_bindings SET received_sequence=received_sequence+1,"
            "input_revision=input_revision+%s,revision=revision+1,updated_at=clock_timestamp() "
            "WHERE async_id=%s RETURNING *",
            (int(facts.kind != "job_progress"), row["async_id"]),
        ).fetchone()
        assert updated is not None
        row = updated
        role = None
        if source is not None:
            role = conn.execute(
                "SELECT binding_id,revision FROM app.source_role_bindings WHERE identity_id=%s "
                "AND conversation_key=%s AND project_id=%s",
                (
                    source.row["identity_id"],
                    source.item.event.source_thread.conversation_key,
                    self.project,
                ),
            ).fetchone()
        identifier = uuid4()
        input_row = conn.execute(
            "INSERT INTO app.sa_async_inputs(input_id,async_id,project_id,scope_sha256,sequence,"
            "input_revision,request_id,request_sha256,kind,origin,state,source_input_id,"
            "source_instance_id,version_fingerprint,"
            "source_state_sha256,identity_id,identity_revision,policy_id,policy_revision,"
            "role_binding_id,role_revision,owner_id,"
            "resource_operation_id,restriction,reason_code,received_at,expires_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s) RETURNING *",
            (
                identifier,
                row["async_id"],
                self.project,
                row["scope_sha256"],
                row["received_sequence"],
                row["input_revision"],
                request_id,
                request_sha256,
                facts.kind,
                facts.source,
                state,
                source.ref.input_id if source else None,
                source.ref.source_instance_id if source else None,
                source.ref.version_fingerprint if source else None,
                source.ref.state_sha256 if source else None,
                source.row["identity_id"] if source else None,
                source.row["identity_revision"] if source else None,
                source.row["policy_id"] if source else None,
                source.row["policy_revision"] if source else None,
                role["binding_id"] if role else None,
                role["revision"] if role else None,
                owner_id,
                resource_operation_id,
                Jsonb(facts.restriction.model_dump(mode="json")) if facts.restriction else None,
                reason,
                now,
                min(facts.expires_at, now + timedelta(hours=1))
                if valid
                else now + timedelta(seconds=1),
            ),
        ).fetchone()
        assert input_row is not None
        self._event(conn, row, state, input_id=identifier, reason=reason)
        return input_row

    def restrict(self, cookie: str, request: AsyncRestrictRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._owner(conn, cookie)
            row = self.binding(conn, request.coding_id)
            if row["run_id"] != request.run_id or row["execution_epoch"] != request.expected_epoch:
                raise _deny("SOURCE_ASYNC_TARGET_STALE")
            fingerprint = digest(request.model_dump(mode="json"))
            old = conn.execute(
                "SELECT * FROM app.sa_async_inputs WHERE async_id=%s AND request_id=%s",
                (row["async_id"], request.request_id),
            ).fetchone()
            if old is not None:
                if old["request_sha256"] != fingerprint:
                    raise _deny("SOURCE_ASYNC_INPUT_CONFLICT")
                return self._input_view(old)
            self._cas(row, request.expected_revision)
            if row["state"] != "active":
                raise _deny("SOURCE_ASYNC_AUTHORITY_FENCED")
            facts = AsyncInputFacts(
                kind="restriction",
                source="owner",
                exact_target=True,
                owner_session_current=True,
                restriction=request.restriction,
                expires_at=min(row["expires_at"], self.now(conn) + timedelta(minutes=30)),
            )
            return self._input_view(
                self._append(
                    conn,
                    row,
                    request_id=request.request_id,
                    request_sha256=fingerprint,
                    facts=facts,
                    owner_id=user,
                )
            )

    @staticmethod
    def _input_view(row: Row) -> Row:
        keys = (
            "input_id",
            "async_id",
            "sequence",
            "kind",
            "origin",
            "state",
            "input_revision",
            "restriction",
            "applied_checkpoint_id",
            "reason_code",
            "expires_at",
        )
        return {key: row[key] for key in keys if key in row}

    def _source_facts(
        self,
        conn: Connection,
        row: Row,
        source: _Input,
        kind: Literal["context", "restriction"],
        restriction: AsyncRestriction | None = None,
    ) -> AsyncInputFacts:
        roles, controlled = self.engine.source_policies._roles(
            conn,
            source.item.event.sender,
            source.item.event.source_thread,
            self.engine.source_coding.mandate(conn, row["coding_id"]).target.ticket_id,
        )
        return AsyncInputFacts(
            kind=kind,
            source="source",
            source_input=source.ref,
            expires_at=source.expires,
            receive_current=True,
            identity_current=True,
            policy_current=True,
            canonical_current=True,
            exact_target=True,
            read_allowed=source.definition.receive.model_input and "context_provider" in roles,
            context_only=source.row["context_only"],
            human=source.item.event.sender.actor_kind == "human",
            task_controller="task_controller" in roles,
            ticket_membership=controlled,
            restriction=restriction,
        )

    def _source(self, conn: Connection, row: Row, receipt: Row) -> tuple[_Input, AsyncInputFacts]:
        source = self.engine.source_correlations._input(conn, receipt["source_input_id"])
        ref = CorrelationInputRef(
            input_id=receipt["source_input_id"],
            source_instance_id=receipt["source_instance_id"],
            project_id=self.project,
            version_fingerprint=receipt["version_fingerprint"],
            state_sha256=receipt["source_state_sha256"],
        )
        if source.ref != ref:
            raise _deny("SOURCE_ASYNC_SOURCE_STALE", 403)
        return source, self._source_facts(conn, row, source, "context")

    def accepted_conn(self, conn: Connection, input_id: UUID, *, receipt_id: UUID) -> None:
        self._conn(conn)
        incoming = conn.execute(
            "SELECT source_instance_id FROM app.source_ingress_inputs WHERE input_id=%s AND "
            "project_id=%s",
            (input_id, self.project),
        ).fetchone()
        if incoming is None:
            return
        # Scope by canonical installation first; an entire chat is never a Task.
        candidates = conn.execute(
            "SELECT a.* FROM app.sa_async_bindings a JOIN app.sa_coding_bindings c "
            "USING(coding_id) "
            "WHERE a.project_id=%s AND a.state='active' AND a.expires_at>clock_timestamp() "
            "AND c.source_instance_id=%s ORDER BY a.created_at LIMIT 129",
            (self.project, incoming["source_instance_id"]),
        ).fetchall()
        if not candidates or len(candidates) > 128:
            return
        try:
            source = self.engine.source_correlations._input(conn, input_id)
        except DomainError:
            return
        event = source.item.event
        targets = []
        for row in candidates:
            mandate = self.engine.source_coding.mandate(conn, row["coding_id"])
            if input_id == mandate.origin.input.input_id:
                continue
            try:
                original = self.engine.source_correlations._input(
                    conn, mandate.origin.input.input_id
                )
            except DomainError:
                conn.execute(
                    "SELECT app.sa_async_invalidate(%s,%s)",
                    (row["async_id"], "SOURCE_ASYNC_SOURCE_INVALIDATED"),
                )
                continue
            first = original.item.event
            if first.source_thread.conversation_key != event.source_thread.conversation_key:
                continue
            exact_reply = event.reply_to_entity_id == first.entity_id
            exact_thread = (
                first.source_thread.native_thread_id is not None
                and first.source_thread.topic_key == event.source_thread.topic_key
            )
            delivered = (
                conn.execute(
                    "SELECT 1 FROM app.sa_source_subscription_deliveries WHERE input_id=%s AND "
                    "run_id=%s LIMIT 1",
                    (input_id, row["run_id"]),
                ).fetchone()
                if self.engine.source_subscriptions._ready(conn)
                else None
            )
            if exact_reply or exact_thread or delivered:
                targets.append(row)
        if len(targets) != 1:
            return
        row = targets[0]
        row = self.binding(conn, row["coding_id"])
        if conn.execute(
            "SELECT 1 FROM app.sa_async_inputs WHERE async_id=%s AND source_input_id=%s",
            (row["async_id"], input_id),
        ).fetchone():
            return
        restriction = None
        kind: Literal["context", "restriction"] = "context"
        if self._restriction_text(source):
            kind = "restriction"
            restriction = self._profile(row).database_restriction
        facts = self._source_facts(conn, row, source, kind, restriction)
        if (
            kind == "restriction"
            and restriction is not None
            and not (restriction.deny_write_paths or restriction.deny_commands)
        ):
            facts = facts.model_copy(update={"exact_target": False})
        self._append(
            conn,
            row,
            request_id=input_id,
            request_sha256=digest(
                {
                    "version": ASYNC_VERSION,
                    "source_input": source.ref.model_dump(mode="json"),
                    "receipt_id": str(receipt_id),
                    "async_id": str(row["async_id"]),
                }
            ),
            facts=facts,
            source=source,
        )

    @staticmethod
    def _restriction_text(source: _Input) -> bool:
        if source.text.strip() != "不要改数据库":
            return False
        if not source.clear.lstrip().startswith(b"{"):
            return True
        raw = json.loads(source.clear)
        if raw.get("schema_version") == "github-content-v1":
            return bool(raw.get("text", "").strip() == "不要改数据库")
        return bool(
            raw.get("schema_version") == "feishu-content-v1" and raw.get("message_type") == "text"
        )

    def current(
        self, conn: Connection, bearer: str, request: CodingRunRequest, tree: str
    ) -> tuple[Row, AsyncCurrentFacts]:
        row = self.binding(conn, request.coding_id)
        if any(
            (
                request.run_id != row["run_id"],
                request.assignment_id != row["assignment_id"],
                request.session_id != row["session_id"],
                request.epoch != row["execution_epoch"],
            )
        ):
            raise _deny("SOURCE_ASYNC_SCOPE_FENCED", 403)
        with self.execution_scope(row["async_id"]):
            mandate, original = self.engine.source_coding.current(
                conn, bearer, request, tree_sha256=tree
            )
        scope = self._scope(conn, row)
        if scope.scope_sha256 != row["scope_sha256"]:
            raise _deny("SOURCE_ASYNC_PROFILE_CHANGED", 403)
        facts = AsyncCurrentFacts(
            async_id=row["async_id"],
            scope=scope,
            revision=row["revision"],
            input_revision=row["input_revision"],
            restriction=_decode(AsyncRestriction, row["restriction"]),
            tree_sha256=tree,
            environment_sha256=mandate.grant.environment_sha256,
            expires_at=min(
                row["expires_at"],
                original.lease_until,
                original.session_expires_at,
                original.configuration_expires_at,
                mandate.origin.expires_at,
            ),
            original_coding_current=True,
            assignment_current=True,
            session_current=True,
            sources_current=True,
            budget_current=True,
            active=row["state"] == "active",
            pending_invalidation=row["state"] != "active",
        )
        if not facts.active or self.now(conn) >= facts.expires_at:
            raise _deny("SOURCE_ASYNC_AUTHORITY_FENCED", 403)
        return row, facts

    def _checkpoint(self, conn: Connection, row: Row, identifier: UUID) -> AsyncCheckpoint:
        cp = conn.execute(
            "SELECT * FROM app.sa_async_checkpoints WHERE checkpoint_id=%s AND async_id=%s",
            (identifier, row["async_id"]),
        ).fetchone()
        if cp is None:
            raise _deny("SOURCE_ASYNC_CHECKPOINT_REQUIRED")
        refs = conn.execute(
            "SELECT * FROM app.sa_async_inputs WHERE async_id=%s AND origin='source' AND "
            "state='applied' "
            "AND sequence<=%s ORDER BY source_input_id",
            (row["async_id"], cp["through_sequence"]),
        ).fetchall()
        return AsyncCheckpoint(
            checkpoint_id=identifier,
            async_id=row["async_id"],
            scope=self._scope(conn, row),
            stage=cp["stage"],
            input_revision=cp["input_revision"],
            through_sequence=cp["through_sequence"],
            restriction=_decode(AsyncRestriction, cp["restriction"]),
            source_refs=tuple(
                CorrelationInputRef(
                    input_id=r["source_input_id"],
                    source_instance_id=r["source_instance_id"],
                    project_id=self.project,
                    version_fingerprint=r["version_fingerprint"],
                    state_sha256=r["source_state_sha256"],
                )
                for r in refs
            ),
            context_sha256=cp["context_sha256"],
            tree_sha256=cp["tree_sha256"],
            environment_sha256=cp["environment_sha256"],
            created_at=cp["created_at"],
            expires_at=cp["expires_at"],
        )

    def _dependencies(
        self, conn: Connection, row: Row, cp: AsyncCheckpoint
    ) -> tuple[AsyncInputFacts, ...]:
        facts = []
        for ref in cp.source_refs:
            receipt = conn.execute(
                "SELECT * FROM app.sa_async_inputs WHERE async_id=%s AND source_input_id=%s",
                (row["async_id"], ref.input_id),
            ).fetchone()
            assert receipt is not None
            _, current = self._source(conn, row, receipt)
            if classify_input(current, at=self.now(conn)) != "context":
                raise _deny("SOURCE_ASYNC_CONTEXT_REVOKED", 403)
            facts.append(current)
        return tuple(facts)

    def safe_point(
        self, bearer: str, request: CodingRunRequest, *, tree: str, stage: SafePointKind
    ) -> AsyncCheckpoint:
        with self.engine.transaction() as conn:
            row, current = self.current(conn, bearer, request, tree)
            previous = (
                self._checkpoint(conn, row, row["current_checkpoint_id"])
                if row["current_checkpoint_id"]
                else None
            )
            retained = self._dependencies(conn, row, previous) if previous else ()
            if (
                previous is not None
                and previous.through_sequence == row["received_sequence"]
                and previous.input_revision == current.input_revision
                and previous.tree_sha256 == tree
                and previous.environment_sha256 == current.environment_sha256
                and previous.restriction == current.restriction
                and previous.expires_at > self.now(conn)
            ):
                validate_view(previous, current, at=self.now(conn))
                return previous
            raw = conn.execute(
                "SELECT * FROM app.sa_async_inputs WHERE async_id=%s AND sequence>%s ORDER BY "
                "sequence LIMIT 257",
                (row["async_id"], previous.through_sequence if previous else 0),
            ).fetchall()
            # Terminal blocked inputs are acknowledged as blocked in the same new view.
            pending = []
            for item in raw:
                if item["state"] == "applied":
                    raise _deny("SOURCE_ASYNC_INPUT_CURSOR_CONFLICT")
                if item["origin"] == "source":
                    try:
                        source, facts = self._source(conn, row, item)
                        facts = self._source_facts(
                            conn,
                            row,
                            source,
                            item["kind"],
                            _decode(AsyncRestriction, item["restriction"])
                            if item["restriction"]
                            else None,
                        )
                    except DomainError:
                        facts = AsyncInputFacts(
                            kind=item["kind"],
                            source="source",
                            expires_at=item["expires_at"],
                            source_input=CorrelationInputRef(
                                input_id=item["source_input_id"],
                                source_instance_id=item["source_instance_id"],
                                project_id=self.project,
                                version_fingerprint=item["version_fingerprint"],
                                state_sha256=item["source_state_sha256"],
                            ),
                            restriction=_decode(AsyncRestriction, item["restriction"])
                            if item["restriction"]
                            else None,
                        )
                else:
                    observed = (
                        item["origin"] == "job"
                        and conn.execute(
                            "SELECT 1 FROM app.sa_scheduler_operations WHERE operation_id=%s "
                            "AND assignment_id=%s AND state='known_terminal'",
                            (item["resource_operation_id"], row["assignment_id"]),
                        ).fetchone()
                        is not None
                    )
                    facts = AsyncInputFacts(
                        kind=item["kind"],
                        source=item["origin"],
                        expires_at=item["expires_at"],
                        exact_target=True,
                        owner_session_current=item["origin"] == "owner",
                        trusted_observation=bool(observed),
                        restriction=_decode(AsyncRestriction, item["restriction"])
                        if item["restriction"]
                        else None,
                    )
                if item["state"] == "blocked":
                    facts = facts.model_copy(update={"exact_target": False})
                pending.append(
                    PendingInput(
                        receipt=AsyncInputReceipt(
                            input_id=item["input_id"],
                            async_id=row["async_id"],
                            scope_sha256=row["scope_sha256"],
                            sequence=item["sequence"],
                            kind=item["kind"],
                            state="received",
                            input_revision=item["input_revision"],
                            request_sha256=item["request_sha256"],
                            source_input=facts.source_input,
                            restriction=facts.restriction,
                        ),
                        facts=facts,
                    )
                )
            now = self.now(conn)
            plan = plan_safe_point(
                checkpoint_id=uuid4(),
                current=current,
                stage=stage,
                at=now,
                received_sequence=row["received_sequence"],
                pending=tuple(pending),
                previous=previous,
                retained_facts=retained,
            )
            cp = plan.checkpoint
            conn.execute(
                "INSERT INTO "
                "app.sa_async_checkpoints(checkpoint_id,async_id,project_id,scope_sha256,"
                "request_id,stage,"
                "input_revision,through_sequence,restriction,context_sha256,tree_sha256,"
                "environment_sha256,created_at,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    cp.checkpoint_id,
                    row["async_id"],
                    self.project,
                    row["scope_sha256"],
                    cp.checkpoint_id,
                    stage,
                    cp.input_revision,
                    cp.through_sequence,
                    Jsonb(cp.restriction.model_dump(mode="json")),
                    cp.context_sha256,
                    tree,
                    cp.environment_sha256,
                    now,
                    cp.expires_at,
                ),
            )
            for item in raw:
                if item["state"] == "blocked":
                    continue
                applied = item["input_id"] in plan.applied_ids
                conn.execute(
                    "UPDATE app.sa_async_inputs SET "
                    "state=%s,applied_checkpoint_id=%s,reason_code=%s,"
                    "updated_at=clock_timestamp() WHERE input_id=%s",
                    (
                        "applied" if applied else "blocked",
                        cp.checkpoint_id if applied else None,
                        None if applied else "SOURCE_ASYNC_INPUT_DENIED",
                        item["input_id"],
                    ),
                )
                self._event(
                    conn,
                    row,
                    "applied" if applied else "blocked",
                    input_id=item["input_id"],
                    checkpoint_id=cp.checkpoint_id if applied else None,
                    reason=None if applied else "SOURCE_ASYNC_INPUT_DENIED",
                )
            conn.execute(
                "UPDATE app.sa_async_bindings SET "
                "current_checkpoint_id=%s,applied_sequence=%s,restriction=%s,"
                "revision=revision+1,updated_at=clock_timestamp() WHERE async_id=%s",
                (
                    cp.checkpoint_id,
                    cp.through_sequence,
                    Jsonb(cp.restriction.model_dump(mode="json")),
                    row["async_id"],
                ),
            )
            return cp

    def validate(
        self,
        conn: Connection,
        bearer: str,
        request: CodingRunRequest,
        cp: AsyncCheckpoint,
        *,
        tree: str,
        capability: Literal["read", "model", "write", "execute", "verify"] = "read",
        paths: tuple[str, ...] = (),
        command: str | None = None,
    ) -> AsyncCurrentFacts:
        row, facts = self.current(conn, bearer, request, tree)
        self._dependencies(conn, row, cp)
        if capability in {"write", "execute"}:
            validate_mutation(
                cp,
                facts,
                at=self.now(conn),
                capability=cast(Literal["write", "execute"], capability),
                paths=paths,
                command=command,
            )
        else:
            validate_view(cp, facts, at=self.now(conn))
        return facts

    @contextmanager
    def guard(
        self,
        bearer: str,
        request: CodingRunRequest,
        cp: AsyncCheckpoint,
        *,
        tree: str,
        capability: Literal["read", "model", "write", "execute", "verify"] = "read",
        paths: tuple[str, ...] = (),
        command: str | None = None,
    ) -> Iterator[None]:
        with self.engine._mutex:
            with self.engine.transaction() as conn:
                self.validate(
                    conn,
                    bearer,
                    request,
                    cp,
                    tree=tree,
                    capability=capability,
                    paths=paths,
                    command=command,
                )
            yield

    def materialize(
        self, bearer: str, request: CodingRunRequest, cp: AsyncCheckpoint, *, tree: str
    ) -> tuple[str, ...]:
        with self.engine.transaction() as conn:
            self.validate(conn, bearer, request, cp, tree=tree)
            bodies = tuple(
                self.engine.source_correlations._input(conn, ref.input_id).text
                for ref in cp.source_refs
            )
            if sum(len(text.encode()) for text in bodies) > 65536:
                raise _deny("SOURCE_ASYNC_CONTEXT_LIMIT")
            return bodies

    def payload_expiry(
        self, conn: Connection, bearer: str, request: CodingRunRequest, operation: Row
    ) -> datetime | None:
        if not self.managed(conn, request.coding_id):
            return None
        row = self.binding(conn, request.coding_id)
        view = conn.execute(
            "SELECT * FROM app.sa_async_operation_views WHERE operation_id=%s AND async_id=%s",
            (operation["operation_id"], row["async_id"]),
        ).fetchone()
        if view is None:
            raise _deny("SOURCE_ASYNC_OPERATION_VIEW_REQUIRED")
        cp = self._checkpoint(conn, row, view["checkpoint_id"])
        self.validate(conn, bearer, request, cp, tree=operation["tree_sha256"])
        return cast(datetime, view["expires_at"])

    def maintain(self, coding_id: UUID) -> None:
        with self.engine.transaction() as conn:
            if not self.managed(conn, coding_id):
                return
            row = self.binding(conn, coding_id)
            if row["state"] == "active" and row["current_checkpoint_id"]:
                try:
                    cp = self._checkpoint(conn, row, row["current_checkpoint_id"])
                    self._dependencies(conn, row, cp)
                except DomainError:
                    conn.execute(
                        "SELECT app.sa_async_invalidate(%s,%s)",
                        (row["async_id"], "SOURCE_ASYNC_CONTEXT_REVOKED"),
                    )
            conn.execute(
                "UPDATE app.sa_coding_payloads p SET "
                "ciphertext=NULL,unavailable_at=clock_timestamp(),"
                "reason_code='SOURCE_ASYNC_VIEW_UNAVAILABLE',revision=p.revision+1,"
                "updated_at=clock_timestamp() "
                "FROM app.sa_async_operation_views v JOIN app.sa_async_bindings b ON "
                "b.async_id=v.async_id "
                "WHERE p.operation_id=v.operation_id AND b.coding_id=%s AND p.unavailable_at IS "
                "NULL "
                "AND (b.state<>'active' OR v.expires_at<=clock_timestamp() OR "
                "v.input_revision<>b.input_revision)",
                (coding_id,),
            )

    def inputs(self, cookie: str, coding_id: UUID, *, after: int = 0, limit: int = 64) -> Row:
        with self.engine.transaction() as conn:
            self._owner(conn, cookie)
            row = self.binding(conn, coding_id)
            self._page(after, limit)
            items = conn.execute(
                "SELECT * FROM app.sa_async_inputs WHERE async_id=%s AND sequence>%s ORDER BY "
                "sequence LIMIT %s",
                (row["async_id"], after, limit),
            ).fetchall()
            return {"coding_id": coding_id, "inputs": [self._input_view(item) for item in items]}

    @staticmethod
    def _page(after: int, limit: int) -> None:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 64:
            raise _deny("SOURCE_ASYNC_PAGE_INVALID", 422)

    def events(self, cookie: str, coding_id: UUID, *, after: int = 0, limit: int = 64) -> Row:
        with self.engine.transaction() as conn:
            self._owner(conn, cookie)
            row = self.binding(conn, coding_id)
            self._page(after, limit)
            events = conn.execute(
                "SELECT "
                "event_id,kind,input_id,job_id,checkpoint_id,input_revision,reason_code,created_at "
                "FROM app.sa_async_events WHERE async_id=%s AND event_id>%s ORDER BY event_id "
                "LIMIT %s",
                (row["async_id"], after, limit),
            ).fetchall()
            return {"coding_id": coding_id, "events": events}

    @staticmethod
    def _job_view(row: Row) -> Row:
        return {
            k: row[k]
            for k in (
                "job_id",
                "async_id",
                "coding_id",
                "run_id",
                "assignment_id",
                "session_id",
                "execution_epoch",
                "revision",
                "state",
                "result_code",
                "receipt_sha256",
                "observed_at",
                "input_revision",
            )
        }

    def jobs(
        self, cookie: str, coding_id: UUID, *, after: UUID | None = None, limit: int = 64
    ) -> Row:
        with self.engine.transaction() as conn:
            self._owner(conn, cookie)
            row = self.binding(conn, coding_id)
            self._page(0, limit)
            items = conn.execute(
                "SELECT * FROM app.sa_async_jobs WHERE async_id=%s AND (%s::uuid IS NULL OR "
                "job_id>%s) "
                "ORDER BY job_id LIMIT %s",
                (row["async_id"], after, after, limit),
            ).fetchall()
            return {"coding_id": coding_id, "jobs": [self._job_view(item) for item in items]}

    def _job(self, conn: Connection, row: Row, job_id: UUID) -> Row:
        job = conn.execute(
            "SELECT * FROM app.sa_async_jobs WHERE job_id=%s AND async_id=%s FOR UPDATE",
            (job_id, row["async_id"]),
        ).fetchone()
        if job is None:
            raise _deny("SOURCE_ASYNC_JOB_NOT_FOUND", 404)
        return job

    def _cancel(self, conn: Connection, row: Row, job_id: UUID, revision: int) -> Row:
        job = self._job(conn, row, job_id)
        if job["state"] in {"stop_requested", "unknown", "known"}:
            return self._job_view(job)
        self._cas(job, revision)
        updated = conn.execute(
            "UPDATE app.sa_async_jobs SET "
            "state='stop_requested',revision=revision+1,updated_at=clock_timestamp() "
            "WHERE job_id=%s RETURNING *",
            (job_id,),
        ).fetchone()
        assert updated is not None
        job = updated
        self._event(conn, row, "job_stop_requested", job_id=job_id)
        return self._job_view(job)

    def cancel(self, cookie: str, request: AsyncJobCancelRequest) -> Row:
        with self.engine.transaction() as conn:
            self._owner(conn, cookie)
            row = self.binding(conn, request.coding_id)
            if row["run_id"] != request.run_id:
                raise _deny("SOURCE_ASYNC_TARGET_STALE")
            return self._cancel(conn, row, request.job_id, request.expected_revision)

    def worker_cancel(self, bearer: str, request: AsyncWorkerJobCancelRequest) -> Row:
        with self.engine.transaction() as conn:
            row = self.binding(conn, request.coding_id)
            self._worker(conn, bearer, row, request)
            return self._cancel(conn, row, request.job_id, request.expected_revision)

    def state(self, bearer: str, request: AsyncWorkerRequest, *, job_id: UUID | None = None) -> Row:
        with self.engine.transaction() as conn:
            row = self.binding(conn, request.coding_id)
            self._worker(conn, bearer, row, request)
            if job_id is not None:
                return self._job_view(self._job(conn, row, job_id))
            attempts = conn.execute(
                "SELECT attempt_id,state,revision,reason_code FROM app.sa_coding_attempts "
                "WHERE coding_id=%s ORDER BY created_at DESC LIMIT 1",
                (request.coding_id,),
            ).fetchall()
            return {**self._view(row), "attempts": attempts}

    def start(self, bearer: str, request: AsyncWorkerRequest) -> Row:
        from mgar_v3.super_agent.source_async_runtime import SourceAsyncRuntime

        with self.engine._mutex:
            with self.engine.transaction() as conn:
                row = self.binding(conn, request.coding_id)
                self._worker(conn, bearer, row, request)
                coding = self._coding_request(row)
                self.current(
                    conn,
                    bearer,
                    coding,
                    self.engine.source_coding.mandate(
                        conn, row["coding_id"]
                    ).repository.materialized_tree_sha256,
                )
                existing = conn.execute(
                    "SELECT 1 FROM app.sa_coding_attempts WHERE assignment_id=%s",
                    (row["assignment_id"],),
                ).fetchone()
                if existing:
                    return {**self._view(row), "already_started": True}
                self._cas(row, request.expected_revision)
                if not isinstance(self.runtime, SourceAsyncRuntime):
                    raise _deny("SOURCE_ASYNC_RUNTIME_REQUIRED")
                with self.execution_scope(row["async_id"]):
                    self.engine.source_coding.begin_attempt(conn, bearer, coding)
            self.runtime.start(bearer, coding, row["async_id"])
            return {**self._view(row), "already_started": False}

    def job_prepared(self, operation: Row) -> UUID:
        with self.engine.transaction() as conn:
            row = self.binding(conn, operation["coding_id"])
            view = conn.execute(
                "SELECT * FROM app.sa_async_operation_views WHERE operation_id=%s",
                (operation["operation_id"],),
            ).fetchone()
            if view is None:
                raise _deny("SOURCE_ASYNC_OPERATION_VIEW_REQUIRED")
            identifier = uuid4()
            conn.execute(
                "INSERT INTO "
                "app.sa_async_jobs(job_id,async_id,coding_id,project_id,run_id,assignment_id,"
                "session_id,execution_epoch,"
                "checkpoint_id,coding_operation_id,resource_operation_id,scope_sha256,"
                "input_revision,request_sha256,tree_sha256,environment_sha256) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    identifier,
                    row["async_id"],
                    row["coding_id"],
                    self.project,
                    row["run_id"],
                    row["assignment_id"],
                    row["session_id"],
                    row["execution_epoch"],
                    view["checkpoint_id"],
                    operation["operation_id"],
                    operation["resource_operation_id"],
                    row["scope_sha256"],
                    view["input_revision"],
                    operation["request_sha256"],
                    operation["tree_sha256"],
                    operation["environment_sha256"],
                ),
            )
            self._event(conn, row, "job_prepared", job_id=identifier)
            return identifier

    def job_running(self, coding_id: UUID, identifier: UUID) -> None:
        with self.engine.transaction() as conn:
            row = self.binding(conn, coding_id)
            job = self._job(conn, row, identifier)
            if job["state"] != "prepared":
                raise _deny("SOURCE_ASYNC_JOB_STOPPED")
            conn.execute(
                "UPDATE app.sa_async_jobs SET state='running',revision=revision+1,"
                "updated_at=clock_timestamp() WHERE job_id=%s",
                (identifier,),
            )
            self._event(conn, row, "job_running", job_id=identifier)

    def job_observed(self, coding_id: UUID, identifier: UUID) -> Row:
        with self.engine.transaction() as conn:
            row = self.binding(conn, coding_id)
            job = self._job(conn, row, identifier)
            original = conn.execute(
                "SELECT * FROM app.sa_scheduler_operations WHERE operation_id=%s",
                (job["resource_operation_id"],),
            ).fetchone()
            assert original is not None
            if job["state"] != "known" and original["state"] == "known_terminal":
                updated = conn.execute(
                    "UPDATE app.sa_async_jobs SET "
                    "state='known',result_code=%s,receipt_sha256=%s,observed_at=%s,"
                    "revision=revision+1,updated_at=clock_timestamp() WHERE job_id=%s RETURNING *",
                    (
                        original["result_code"],
                        original["result_sha256"],
                        original["observed_at"],
                        identifier,
                    ),
                ).fetchone()
                assert updated is not None
                job = updated
                self._event(conn, row, "job_known", job_id=identifier)
                facts = AsyncInputFacts(
                    kind="job_progress",
                    source="job",
                    exact_target=True,
                    trusted_observation=True,
                    expires_at=self.now(conn) + timedelta(minutes=30),
                )
                self._append(
                    conn,
                    row,
                    request_id=identifier,
                    request_sha256=digest(
                        {"job_id": str(identifier), "receipt": original["result_sha256"]}
                    ),
                    facts=facts,
                    resource_operation_id=original["operation_id"],
                )
            return self._job_view(job)

    def job_unknown(self, coding_id: UUID, identifier: UUID) -> None:
        with self.engine.transaction() as conn:
            row = self.binding(conn, coding_id)
            job = self._job(conn, row, identifier)
            if job["state"] not in {"unknown", "known"}:
                conn.execute(
                    "UPDATE app.sa_async_jobs SET state='unknown',revision=revision+1,"
                    "updated_at=clock_timestamp() WHERE job_id=%s",
                    (identifier,),
                )
                self._event(conn, row, "job_unknown", job_id=identifier)

    def job_should_stop(self, coding_id: UUID, identifier: UUID) -> bool:
        with self.engine.transaction() as conn:
            row = self.binding(conn, coding_id)
            return row["state"] != "active" or self._job(conn, row, identifier)["state"] in {
                "stop_requested",
                "unknown",
            }

    def recover_conn(self, conn: Connection) -> None:
        self._conn(conn)
        if not self._ready(conn):
            return
        rows = conn.execute(
            "SELECT * FROM app.sa_async_bindings WHERE project_id=%s AND state='active' ORDER BY "
            "async_id FOR UPDATE",
            (self.project,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "SELECT app.sa_async_invalidate(%s,%s)",
                (row["async_id"], "SOURCE_ASYNC_ENGINE_RECOVERY"),
            )
        observed = conn.execute(
            "SELECT j.coding_id,j.job_id FROM app.sa_async_jobs j JOIN "
            "app.sa_scheduler_operations r "
            "ON r.operation_id=j.resource_operation_id WHERE j.project_id=%s AND "
            "j.state<>'known' AND r.state='known_terminal'",
            (self.project,),
        ).fetchall()
        for job in observed:
            self.job_observed(job["coding_id"], job["job_id"])
        conn.execute(
            "UPDATE app.sa_async_jobs SET "
            "state='unknown',revision=revision+1,updated_at=clock_timestamp() "
            "WHERE project_id=%s AND state IN ('prepared','running','stop_requested')",
            (self.project,),
        )

    def reconcile_jobs(self, coding_id: UUID) -> None:
        with self.engine.transaction() as conn:
            row = self.binding(conn, coding_id)
            jobs = conn.execute(
                "SELECT j.job_id FROM app.sa_async_jobs j JOIN app.sa_scheduler_operations r "
                "ON r.operation_id=j.resource_operation_id WHERE j.async_id=%s AND "
                "j.state<>'known' "
                "AND r.state='known_terminal' ORDER BY j.job_id LIMIT 128",
                (row["async_id"],),
            ).fetchall()
            for job in jobs:
                self.job_observed(coding_id, job["job_id"])
