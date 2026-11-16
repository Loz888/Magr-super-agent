"""Bounded resource scheduling on the original Engine connection.

Assignments describe occupancy, never Task completion. Resource IO requires an
injected trusted driver and runs after committed intent, outside a transaction.
"""

from __future__ import annotations

import hmac
import json
import secrets
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef
from mgar_v3.super_agent.source_scheduler_domain import (
    AssignmentFacts,
    PendingWork,
    QueueCandidate,
    QuiescenceFacts,
    ResourceRequest,
    ResourceUsage,
    RunFacts,
    SchedulerAssignmentRequest,
    SchedulerClaimRequest,
    SchedulerConfigFacts,
    SchedulerConfiguration,
    SchedulerConfigureRequest,
    SchedulerEnqueueRequest,
    SchedulerHeartbeatRequest,
    SchedulerLimits,
    SchedulerOwnerDrainRequest,
    SchedulerQueueCancelRequest,
    SchedulerReadyRequest,
    SchedulerReconcileRequest,
    SchedulerReleaseRequest,
    SchedulerReportRequest,
    SchedulerSessionRegisterRequest,
    SchedulerSessionRevokeRequest,
    ServiceBucket,
    ServiceHistory,
    SessionFacts,
    SourceAuthorityFacts,
    SourceOccupancy,
)
from mgar_v3.super_agent.source_scheduler_rules import (
    choose_candidate,
    queue_quota_key,
    slot_quota_key,
    validate_lease,
    validate_release,
)

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine

Row = dict[str, Any]
OperationKind = Literal["prepare", "start", "job", "stop", "inspect"]


def _error(code: str, status: int = 409) -> DomainError:
    return DomainError(code, "Scheduling requires current exact resource authority", status)


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _hash(value: Any) -> str:
    return digest(_json(value))


def _decode[T: Model](model: type[T], value: Any) -> T:
    return model.model_validate_json(json.dumps(value, default=str))


@dataclass(frozen=True, repr=False)
class ResourceOperation:
    operation_id: UUID
    kind: OperationKind
    request_sha256: str
    state: str
    provider_operation_id: str | None
    result_sha256: str | None
    observed_at: datetime | None


@dataclass(frozen=True, repr=False)
class ResourceContext:
    project_id: str
    assignment_id: UUID
    queue_id: UUID
    run_id: UUID
    session_id: UUID
    workspace_id: UUID
    epoch: int
    operation_id: UUID
    kind: OperationKind
    request_sha256: str
    workspace_sha256: str
    profile_sha256: str
    target_sha256: str
    resources: ResourceRequest
    nonce: str = field(repr=False)
    provider_operation_id: str | None = None
    operations: tuple[ResourceOperation, ...] = ()
    cleanup_only: bool = False


@dataclass(frozen=True, repr=False)
class ResourceReceipt:
    project_id: str
    assignment_id: UUID
    run_id: UUID
    session_id: UUID
    workspace_id: UUID
    epoch: int
    operation_id: UUID
    request_sha256: str
    state: Literal["running", "unknown", "known_terminal"]
    observed_at: datetime
    result_code: Literal["succeeded", "failed", "cancelled", "absent"] | None = None
    provider_operation_id: str | None = None
    tree_sha256: str | None = None
    exit_code: int | None = None
    processes_stopped: bool = False
    workspace_fenced: bool = False
    related: tuple[ResourceReceipt, ...] = ()

    @property
    def result_sha256(self) -> str:
        return _hash(
            {
                "project_id": self.project_id,
                "assignment_id": self.assignment_id,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "workspace_id": self.workspace_id,
                "epoch": self.epoch,
                "operation_id": self.operation_id,
                "request_sha256": self.request_sha256,
                "state": self.state,
                "observed_at": self.observed_at.isoformat(),
                "result_code": self.result_code,
                "provider_operation_id": self.provider_operation_id,
                "tree_sha256": self.tree_sha256,
                "exit_code": self.exit_code,
                "processes_stopped": self.processes_stopped,
                "workspace_fenced": self.workspace_fenced,
                "related": [item.result_sha256 for item in self.related],
            }
        )


class SchedulerResourceDriver(Protocol):
    """Trusted installed port; receipts/registry must survive process restart.

    workspace_identity is a pure mapping over a fixed provider registration.
    inspect of a completed prepare reconstructs its original receipt unchanged.
    stop/other inspection produces fresh actual observations, never restamps.
    """

    profile_sha256: str

    def workspace_identity(
        self,
        project_id: str,
        assignment_id: UUID,
        workspace_id: UUID,
    ) -> str: ...

    def prepare(self, context: ResourceContext) -> ResourceReceipt: ...
    def inspect(self, context: ResourceContext) -> ResourceReceipt: ...
    def stop(self, context: ResourceContext) -> ResourceReceipt: ...


@dataclass(frozen=True)
class _Permit:
    service: object
    connection: object
    nonce: str
    kind: Literal["claim", "worker"]
    run_id: UUID
    queue_id: UUID
    session_id: UUID
    assignment_id: UUID
    old_epoch: int
    epoch: int


_PERMIT: ContextVar[_Permit | None] = ContextVar("source_scheduler_permit", default=None)


class SourceScheduler:
    """Original Run authority with two private, unforgeable server contexts.

    The claim permit exists only inside the held connection's outer transaction,
    after locked authority and SA07 slot reservation. It permits exactly the
    selected old/new epoch through begin_run's nested recovery heartbeat, before
    the Assignment INSERT. Deferred constraints and precommit lease validation
    still require a complete Assignment or roll back the entire claim.

    A worker permit is separate: after bearer/lease validation it lets the graph
    reenter begin_run at the same current epoch. Every later Engine operation
    rechecks PG. HTTP payloads and resource drivers cannot manufacture either
    permit or use an unclaimed managed Run through a legacy execution path.
    """

    def __init__(self, engine: Engine, driver: SchedulerResourceDriver | None = None):
        self.engine = engine
        self.driver = driver
        self._permits: dict[str, _Permit] = {}
        self._contexts: dict[UUID, ResourceContext] = {}

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    def _now(self, conn: Connection) -> datetime:
        row = conn.execute("SELECT clock_timestamp() AS at").fetchone()
        assert row is not None
        return cast(datetime, row["at"])

    def _conn(self, conn: Connection) -> None:
        if (
            conn is not self.engine.connection
            or conn.info.transaction_status != TransactionStatus.INTRANS
        ):
            raise _error("SOURCE_SCHEDULER_ORIGINAL_TRANSACTION_REQUIRED", 403)

    @staticmethod
    def _ready(conn: Connection) -> bool:
        row = conn.execute(
            "SELECT to_regclass('app.sa_scheduler_configs') IS NOT NULL AS ready"
        ).fetchone()
        return bool(row and row["ready"])

    def _console(self, conn: Connection, cookie: str) -> UUID:
        self._conn(conn)
        user = self.engine.source_subscriptions.identities._console(conn, cookie)
        if not self._ready(conn):
            raise _error("SOURCE_SCHEDULER_NOT_MIGRATED")
        return user

    def _event(self, conn: Connection, kind: str, row: Row, **details: Any) -> None:
        conn.execute(
            "INSERT INTO app.sa_scheduler_events(project_id,event_type,queue_id,session_id,"
            "assignment_id,operation_id,revision,reason_code,details) VALUES(%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s)",
            (
                self.project,
                kind,
                row.get("queue_id"),
                row.get("session_id"),
                row.get("assignment_id"),
                row.get("operation_id"),
                row["revision"],
                row.get("reason_code"),
                Jsonb(_json(details)),
            ),
        )

    def _run(self, conn: Connection, run_id: UUID) -> Row:
        scoped = conn.execute(
            "SELECT 1 FROM app.runs r JOIN app.tickets t USING(ticket_id) "
            "JOIN app.threads h USING(thread_id) WHERE r.run_id=%s AND h.project_id=%s",
            (run_id, self.project),
        ).fetchone()
        if scoped is None:
            raise _error("SOURCE_SCHEDULER_RUN_NOT_FOUND", 404)
        return self.engine._run(conn, run_id)

    def _target_hash(self, conn: Connection, run: Row) -> str:
        mandate = conn.execute(
            "SELECT to_jsonb(m) AS value FROM app.mandates m WHERE mandate_id=%s AND project_id=%s",
            (run["mandate_id"], self.project),
        ).fetchone()
        if mandate is None or run["repository"].lower() != self.engine.settings.repository.lower():
            raise _error("SOURCE_SCHEDULER_TARGET_DENIED", 403)
        return _hash(
            [
                self.project,
                run["repository"].lower(),
                run["thread_id"],
                run["ticket_id"],
                run["run_id"],
                run["mandate_id"],
                mandate["value"],
                _hash(run["question"]),
                run["input_revision"],
                run["thread_revision"],
            ]
        )

    def target(self, cookie: str, run_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            run = self._run(conn, run_id)
            return {
                "run_id": run_id,
                "epoch": run["execution_epoch"],
                "target_sha256": self._target_hash(conn, run),
            }

    def _configuration(self, conn: Connection, *, active: bool = False) -> SchedulerConfigFacts:
        row = conn.execute(
            "SELECT v.* FROM app.sa_scheduler_configs h "
            "JOIN app.sa_scheduler_config_versions v USING(project_id,revision) "
            "WHERE h.project_id=%s FOR UPDATE OF h",
            (self.project,),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_SCHEDULER_CONFIG_REQUIRED", 403)
        config = SchedulerConfiguration(
            enabled=row["enabled"],
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            limits=SchedulerLimits(
                slots=row["active_slots"],
                preparing=row["preparing_slots"],
                cpu_millis=row["total_cpu_millis"],
                memory_mib=row["total_memory_mib"],
                queue=row["max_queue"],
                source_slots=row["per_source_slots"],
                lease_seconds=row["lease_seconds"],
                preparation_seconds=row["preparation_seconds"],
                session_ttl_seconds=row["session_ttl_seconds"],
            ),
        )
        if digest(config.model_dump(mode="json")) != row["configuration_sha256"]:
            raise _error("SOURCE_SCHEDULER_CONFIG_INVALID", 403)
        if active and (
            not config.enabled or not config.valid_from <= self._now(conn) < config.valid_until
        ):
            raise _error("SOURCE_SCHEDULER_CONFIG_INACTIVE", 403)
        return SchedulerConfigFacts(
            project_id=self.project,
            revision=row["revision"],
            configuration_sha256=row["configuration_sha256"],
            configuration=config,
        )

    def configure(self, cookie: str, request: SchedulerConfigureRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            conn.execute(
                "INSERT INTO app.sa_scheduler_configs(project_id) VALUES(%s) "
                "ON CONFLICT DO NOTHING",
                (self.project,),
            )
            head = conn.execute(
                "SELECT * FROM app.sa_scheduler_configs WHERE project_id=%s FOR UPDATE",
                (self.project,),
            ).fetchone()
            assert head is not None
            if head["revision"] != request.expected_revision:
                raise _error("SOURCE_SCHEDULER_REVISION_CONFLICT")
            revision = head["revision"] + 1
            c, limits = request.configuration, request.configuration.limits
            conn.execute(
                "INSERT INTO app.sa_scheduler_config_versions(project_id,revision,"
                "previous_revision,"
                "enabled,active_slots,preparing_slots,total_cpu_millis,total_memory_mib,max_queue,"
                "per_source_slots,lease_seconds,preparation_seconds,session_ttl_seconds,valid_from,"
                "valid_until,configuration_sha256,review_sha256,signed_by) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    self.project,
                    revision,
                    revision - 1 if revision > 1 else None,
                    c.enabled,
                    limits.slots,
                    limits.preparing,
                    limits.cpu_millis,
                    limits.memory_mib,
                    limits.queue,
                    limits.source_slots,
                    limits.lease_seconds,
                    limits.preparation_seconds,
                    limits.session_ttl_seconds,
                    c.valid_from,
                    c.valid_until,
                    digest(c.model_dump(mode="json")),
                    digest(request.review_ref),
                    user,
                ),
            )
            conn.execute(
                "UPDATE app.sa_scheduler_configs SET revision=%s WHERE project_id=%s AND "
                "revision=%s",
                (revision, self.project, request.expected_revision),
            )
            for row in self._occupied(conn):
                self._drain(conn, row, "AUTHORITY_CHANGED")
            self._event(
                conn,
                "CONFIGURATION_SAVED",
                {"revision": revision},
                config_revision=revision,
            )
            return self._configuration(conn).model_dump(mode="json")

    def get_config(self, cookie: str) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return self._configuration(conn).model_dump(mode="json")

    def _budgets(self, conn: Connection) -> None:
        configs = self.engine.project_budgets._configs(conn)
        if not configs:
            raise _error("SOURCE_SCHEDULER_SHARED_BUDGET_REQUIRED", 403)
        for _, definition in configs:
            self.engine.project_budgets._active(definition, self._now(conn))

    def _queue_row(self, conn: Connection, queue_id: UUID) -> Row:
        row = conn.execute(
            "SELECT * FROM app.sa_scheduler_queue WHERE queue_id=%s AND project_id=%s FOR UPDATE",
            (queue_id, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_SCHEDULER_QUEUE_NOT_FOUND", 404)
        return row

    def _source(self, conn: Connection, row: Row, run: Row) -> SourceAuthorityFacts:
        coding = getattr(self.engine, "source_coding", None)
        if coding is not None and coding.managed(conn, run["run_id"]):
            return cast(SourceAuthorityFacts, coding.source_authority(conn, run, row))
        if row["input_id"] is None:
            self.engine.source_policies.check_run(conn, run)
            return SourceAuthorityFacts(required=False)
        loaded = self.engine.source_correlations._input(conn, row["input_id"])
        reference = CorrelationInputRef(
            input_id=row["input_id"],
            source_instance_id=row["source_instance_id"],
            project_id=self.project,
            version_fingerprint=row["version_fingerprint"],
            state_sha256=row["source_state_sha256"],
        )
        binding = self.engine.source_policies._bound(conn, run["run_id"])
        if (
            loaded.ref != reference
            or binding is None
            or binding["policy_id"] != loaded.row["policy_id"]
            or binding["identity_id"] != loaded.row["identity_id"]
        ):
            raise _error("SOURCE_SCHEDULER_SOURCE_EXECUTION_BRIDGE_REQUIRED", 403)
        self.engine.source_policies.check_run(conn, run)
        if loaded.definition.mode not in {"auto_investigate", "auto_repair"}:
            raise _error("SOURCE_SCHEDULER_SOURCE_FENCED", 403)
        return SourceAuthorityFacts(
            required=True,
            current=True,
            identity_current=True,
            receive_allowed=True,
            read_allowed=True,
            execution_allowed=True,
            origin_current=True,
            expires_at=min(loaded.expires, loaded.definition.valid_until),
        )

    def _fixture(self, run: Row) -> None:
        if run["execution_mode"] == "fixture":
            return
        if run["execution_mode"] == "live" and run["graph_version"] == "source-coding-v1":
            # The sole new live path rechecks the exact registered owner trial.
            # Ordinary legacy/live Runs do not acquire scheduler authority.
            self.engine.source_coding._mandate(self.engine.connection, run["run_id"])
            return
        raise _error("SOURCE_SCHEDULER_LIVE_BRIDGE_REQUIRED", 403)

    def _mode_allowed(self, run: Row) -> bool:
        self._fixture(run)
        return True

    def enqueue(self, cookie: str, request: SchedulerEnqueueRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            return self._enqueue_authorized(conn, user, request)

    def _enqueue_authorized(
        self, conn: Connection, user: UUID, request: SchedulerEnqueueRequest
    ) -> Row:
        """Trusted original-connection primitive; the public Owner check is retained."""
        self._conn(conn)
        config = self._configuration(conn, active=True)
        self._budgets(conn)
        fingerprint = digest(request.model_dump(mode="json"))
        previous = conn.execute(
            "SELECT * FROM app.sa_scheduler_queue WHERE project_id=%s AND request_id=%s",
            (self.project, request.request_id),
        ).fetchone()
        if previous:
            if previous["request_sha256"] != fingerprint:
                raise _error("SOURCE_SCHEDULER_IDEMPOTENCY_CONFLICT")
            return self._view(previous)
        run = self._run(conn, request.run_id)
        if (
            run["status"] != "queued"
            or run["ticket_status"] != "open"
            or run["execution_epoch"] != request.expected_epoch
            or self._target_hash(conn, run) != request.target_sha256
        ):
            raise _error("SOURCE_SCHEDULER_TARGET_STALE")
        pending = conn.execute(
            "SELECT count(*) AS n FROM app.sa_scheduler_queue WHERE project_id=%s AND "
            "state='queued'",
            (self.project,),
        ).fetchone()
        assert pending is not None
        if pending["n"] >= config.configuration.limits.queue:
            raise _error("SOURCE_SCHEDULER_QUEUE_CAPACITY", 429)
        source = request.source_input
        if source is not None and source.project_id != self.project:
            raise _error("SOURCE_SCHEDULER_SOURCE_SCOPE_DENIED", 403)
        origin: Row = {
            "input_id": source.input_id if source else None,
            "source_instance_id": source.source_instance_id if source else None,
            "version_fingerprint": source.version_fingerprint if source else None,
            "source_state_sha256": source.state_sha256 if source else None,
        }
        self._source(conn, origin, run)
        if source:
            loaded = self.engine.source_correlations._input(conn, source.input_id)
            classification = (
                "human" if loaded.item.event.sender.actor_kind == "human" else "automation"
            )
            fairness = _hash([self.project, source.source_instance_id])
        else:
            mandate = conn.execute(
                "SELECT actor FROM app.mandates WHERE mandate_id=%s",
                (run["mandate_id"],),
            ).fetchone()
            assert mandate is not None
            classification = "human"
            fairness = _hash([self.project, "owner", mandate["actor"]])
        sequence = conn.execute(
            "SELECT coalesce(max(generation),0)+1 AS generation FROM "
            "app.sa_scheduler_queue WHERE run_id=%s",
            (run["run_id"],),
        ).fetchone()
        assert sequence is not None
        queue_id, generation = uuid4(), sequence["generation"]
        key = queue_quota_key(queue_id, generation)
        self.engine.project_budgets.quota(
            conn, "queue", key, source_id=origin["source_instance_id"]
        )
        row = conn.execute(
            "INSERT INTO app.sa_scheduler_queue(queue_id,project_id,request_id,request_sha256,"
            "generation,thread_id,ticket_id,run_id,mandate_id,expected_epoch,target_sha256,"
            "cpu_millis,memory_mib,source_instance_id,input_id,version_fingerprint,"
            "source_state_sha256,"
            "classification,fairness_key,queue_quota_key,requested_by,review_sha256) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING *",
            (
                queue_id,
                self.project,
                request.request_id,
                fingerprint,
                generation,
                run["thread_id"],
                run["ticket_id"],
                run["run_id"],
                run["mandate_id"],
                request.expected_epoch,
                request.target_sha256,
                request.resources.cpu_millis,
                request.resources.memory_mib,
                origin["source_instance_id"],
                origin["input_id"],
                origin["version_fingerprint"],
                origin["source_state_sha256"],
                classification,
                fairness,
                key,
                user,
                digest(request.review_ref),
            ),
        ).fetchone()
        assert row is not None
        self._event(
            conn,
            "QUEUED",
            row,
            classification=classification,
            fairness_key=fairness,
            queue_generation=generation,
        )
        return self._view(row)

    @staticmethod
    def _view(row: Row) -> Row:
        return {
            key: value for key, value in row.items() if key not in {"token_sha256", "review_sha256"}
        }

    def queue(self, cookie: str, limit: int = 100) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            rows = conn.execute(
                "SELECT * FROM app.sa_scheduler_queue WHERE project_id=%s ORDER BY "
                "enqueued_at,queue_id LIMIT %s",
                (self.project, self._limit(limit)),
            ).fetchall()
            return {"items": [self._view(row) for row in rows]}

    @staticmethod
    def _limit(limit: int) -> int:
        if type(limit) is not int or not 1 <= limit <= 256:
            raise _error("SOURCE_SCHEDULER_PAGE_INVALID", 422)
        return limit

    def cancel_queue(self, cookie: str, request: SchedulerQueueCancelRequest) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            row = self._queue_row(conn, request.queue_id)
            if row["revision"] != request.expected_revision or row["state"] != "queued":
                raise _error("SOURCE_SCHEDULER_REVISION_CONFLICT")
            closed = conn.execute(
                "UPDATE app.sa_scheduler_queue SET state='closed',revision=revision+1,"
                "closed_at=clock_timestamp(),reason_code='OWNER_CANCELLED' WHERE queue_id=%s "
                "AND revision=%s RETURNING *",
                (request.queue_id, request.expected_revision),
            ).fetchone()
            assert closed is not None
            self.engine.project_budgets.release_quota(conn, "queue", row["queue_quota_key"])
            self._event(conn, "QUEUE_CANCELLED", closed, state="closed")
            return self._view(closed)

    def _session(self, conn: Connection, session_id: UUID, bearer: str | None = None) -> Row:
        row = conn.execute(
            "SELECT * FROM app.sa_scheduler_sessions WHERE session_id=%s AND project_id=%s "
            "FOR UPDATE",
            (session_id, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_SCHEDULER_SESSION_NOT_FOUND", 404)
        if bearer is not None:
            if (
                not isinstance(bearer, str)
                or not 32 <= len(bearer) <= 256
                or not hmac.compare_digest(
                    row["token_sha256"], _hash([self.project, session_id, bearer])
                )
            ):
                raise _error("SOURCE_SCHEDULER_SESSION_DENIED", 403)
            if (
                row["state"] != "registered"
                or not row["registered_at"] <= self._now(conn) < row["expires_at"]
            ):
                raise _error("SOURCE_SCHEDULER_SESSION_FENCED", 403)
        return row

    @staticmethod
    def _session_facts(row: Row) -> SessionFacts:
        return SessionFacts(
            **{
                name: row[name]
                for name in (
                    "session_id",
                    "project_id",
                    "revision",
                    "profile_sha256",
                    "state",
                    "registered_at",
                    "expires_at",
                )
            }
        )

    def register_session(self, cookie: str, request: SchedulerSessionRegisterRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            config = self._configuration(conn)
            previous = conn.execute(
                "SELECT * FROM app.sa_scheduler_sessions WHERE project_id=%s AND request_id=%s",
                (self.project, request.request_id),
            ).fetchone()
            fingerprint = digest(request.model_dump(mode="json"))
            if previous:
                if previous["request_sha256"] != fingerprint:
                    raise _error("SOURCE_SCHEDULER_IDEMPOTENCY_CONFLICT")
                return {
                    **self._view(previous),
                    "bearer": None,
                    "bearer_returned_once": True,
                }
            if self.driver is None or self.driver.profile_sha256 != request.profile_sha256:
                raise _error("SOURCE_SCHEDULER_DRIVER_REQUIRED", 403)
            at = self._now(conn)
            ttl = min(request.ttl_seconds, config.configuration.limits.session_ttl_seconds)
            session_id, bearer = uuid4(), secrets.token_urlsafe(48)
            row = conn.execute(
                "INSERT INTO app.sa_scheduler_sessions(session_id,project_id,request_id,"
                "request_sha256,"
                "token_sha256,profile_sha256,registered_by,review_sha256,registered_at,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    session_id,
                    self.project,
                    request.request_id,
                    fingerprint,
                    _hash([self.project, session_id, bearer]),
                    request.profile_sha256,
                    user,
                    digest(request.review_ref),
                    at,
                    at + timedelta(seconds=ttl),
                ),
            ).fetchone()
            assert row is not None
            self._event(conn, "SESSION_REGISTERED", row, profile_sha256=request.profile_sha256)
            return {**self._view(row), "bearer": bearer, "bearer_returned_once": True}

    def revoke_session(self, cookie: str, request: SchedulerSessionRevokeRequest) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            row = self._session(conn, request.session_id)
            if row["revision"] != request.expected_revision or row["state"] != "registered":
                raise _error("SOURCE_SCHEDULER_REVISION_CONFLICT")
            changed = conn.execute(
                "UPDATE app.sa_scheduler_sessions SET state='revoked',"
                "revoked_at=clock_timestamp(),revision=revision+1 WHERE session_id=%s AND "
                "revision=%s RETURNING *",
                (request.session_id, request.expected_revision),
            ).fetchone()
            assert changed is not None
            for assignment in self._occupied(conn):
                if assignment["session_id"] == request.session_id:
                    self._drain(conn, assignment, "SESSION_REVOKED")
            self._event(conn, "SESSION_REVOKED", changed, state="revoked")
            return self._view(changed)

    def _occupied(self, conn: Connection) -> list[Row]:
        return conn.execute(
            "SELECT * FROM app.sa_scheduler_assignments WHERE project_id=%s "
            "AND state<>'released' ORDER BY created_at FOR UPDATE",
            (self.project,),
        ).fetchall()

    def _assignment(self, conn: Connection, assignment_id: UUID) -> Row:
        row = conn.execute(
            "SELECT * FROM app.sa_scheduler_assignments WHERE assignment_id=%s AND "
            "project_id=%s FOR UPDATE",
            (assignment_id, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_SCHEDULER_ASSIGNMENT_NOT_FOUND", 404)
        return row

    @staticmethod
    def _assignment_facts(row: Row) -> AssignmentFacts:
        return AssignmentFacts(
            assignment_id=row["assignment_id"],
            queue_id=row["queue_id"],
            run_id=row["run_id"],
            ticket_id=row["ticket_id"],
            session_id=row["session_id"],
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            epoch=row["execution_epoch"],
            config_revision=row["config_revision"],
            revision=row["revision"],
            state=row["state"],
            lease_until=row["lease_until"],
            prepare_until=row["prepare_until"],
            fenced_at=row["fenced_at"],
            resources=ResourceRequest(cpu_millis=row["cpu_millis"], memory_mib=row["memory_mib"]),
        )

    def _usage(self, conn: Connection) -> ResourceUsage:
        rows = conn.execute(
            "SELECT a.*,q.fairness_key FROM app.sa_scheduler_assignments a "
            "JOIN app.sa_scheduler_queue q ON q.queue_id=a.queue_id WHERE a.project_id=%s AND "
            "a.state<>'released'",
            (self.project,),
        ).fetchall()
        sources = Counter(row["fairness_key"] for row in rows)
        return ResourceUsage(
            slots=len(rows),
            preparing=sum(row["state"] in {"preparing", "draining"} for row in rows),
            cpu_millis=sum(row["cpu_millis"] for row in rows),
            memory_mib=sum(row["memory_mib"] for row in rows),
            sources=tuple(
                SourceOccupancy(project_id=self.project, fairness_key=key, slots=value)
                for key, value in sources.items()
            ),
        )

    def capacity(self, cookie: str) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            configuration: Row | None
            try:
                configuration = self._configuration(conn).model_dump(mode="json")
            except DomainError as error:
                if error.code != "SOURCE_SCHEDULER_CONFIG_REQUIRED":
                    raise
                configuration = None
            return {
                "configuration": configuration,
                "status": "NOT_CONFIGURED" if configuration is None else "CONFIGURED",
                "usage": self._usage(conn).model_dump(mode="json"),
                "driver_configured": self.driver is not None,
            }

    def assignments(self, cookie: str, limit: int = 100) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            rows = conn.execute(
                "SELECT * FROM app.sa_scheduler_assignments WHERE project_id=%s ORDER BY "
                "created_at LIMIT %s",
                (self.project, self._limit(limit)),
            ).fetchall()
            return {"items": [self._view(row) for row in rows]}

    def get_assignment(self, cookie: str, assignment_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return self._view(self._assignment(conn, assignment_id))

    def _managed(self, conn: Connection, run_id: UUID) -> bool:
        return (
            self._ready(conn)
            and conn.execute(
                "SELECT 1 FROM app.sa_scheduler_queue WHERE run_id=%s AND project_id=%s LIMIT 1",
                (run_id, self.project),
            ).fetchone()
            is not None
        )

    def managed(self, run_id: UUID) -> bool:
        with self.engine.transaction() as conn:
            return self._managed(conn, run_id)

    def _authority(
        self,
        conn: Connection,
        row: Row,
        request: SchedulerAssignmentRequest,
        *,
        preparing: bool = False,
    ) -> Row:
        config = self._configuration(conn, active=True)
        session = self._session(conn, row["session_id"])
        queue = self._queue_row(conn, row["queue_id"])
        run = self._run(conn, row["run_id"])
        self._fixture(run)
        same = self._target_hash(conn, run) == row["target_sha256"]
        facts = RunFacts(
            run_id=run["run_id"],
            ticket_id=run["ticket_id"],
            project_id=self.project,
            epoch=run["execution_epoch"],
            status=run["status"],
            ticket_open=run["ticket_status"] == "open",
            target_current=same,
            mandate_current=same,
        )
        source = self._source(conn, queue, run)
        assignment = self._assignment_facts(row)
        validate_lease(
            config,
            self._session_facts(session),
            assignment,
            facts,
            source,
            request,
            at=self._now(conn),
            allow_preparing=preparing,
        )
        return {
            "assignment": assignment,
            "configuration": config,
            "session": self._session_facts(session),
            "run": facts,
            "source": source,
            "now": self._now(conn),
        }

    @staticmethod
    def _request(row: Row) -> SchedulerAssignmentRequest:
        return SchedulerAssignmentRequest(
            assignment_id=row["assignment_id"],
            session_id=row["session_id"],
            epoch=row["execution_epoch"],
            expected_revision=row["revision"],
        )

    def _permit(self, conn: Connection, run: Row) -> _Permit | None:
        permit = _PERMIT.get()
        if (
            permit is None
            or permit.service is not self
            or permit.connection is not conn
            or self._permits.get(permit.nonce) is not permit
            or permit.run_id != run["run_id"]
        ):
            return None
        self._conn(conn)
        return permit

    @contextmanager
    def _scope(self, permit: _Permit) -> Iterator[None]:
        self._permits[permit.nonce] = permit
        token = _PERMIT.set(permit)
        try:
            yield
        finally:
            _PERMIT.reset(token)
            self._permits.pop(permit.nonce, None)

    def check_run(self, conn: Connection, run: Row) -> None:
        self._conn(conn)
        if not self._managed(conn, run["run_id"]):
            return
        permit = self._permit(conn, run)
        if (
            permit is not None
            and permit.kind == "claim"
            and run["execution_epoch"] in {permit.old_epoch, permit.epoch}
        ):
            queue = self._queue_row(conn, permit.queue_id)
            self._session(conn, permit.session_id)
            if (
                queue["state"] == "queued"
                and queue["run_id"] == run["run_id"]
                and queue["expected_epoch"] == permit.old_epoch
            ):
                return
        if permit is None or permit.kind != "worker":
            raise _error("SOURCE_SCHEDULER_WORKER_CONTEXT_REQUIRED", 403)
        row = self._assignment(conn, permit.assignment_id)
        if (
            row["session_id"] != permit.session_id
            or row["queue_id"] != permit.queue_id
            or run["execution_epoch"] != permit.epoch
        ):
            raise _error("SOURCE_SCHEDULER_EPOCH_FENCED", 403)
        self._authority(conn, row, self._request(row))

    def before_begin(self, conn: Connection, run: Row) -> Row | None:
        self._conn(conn)
        if not self._managed(conn, run["run_id"]):
            return None
        permit = self._permit(conn, run)
        if (
            permit is not None
            and permit.kind == "claim"
            and run["status"] == "queued"
            and run["execution_epoch"] == permit.old_epoch
        ):
            self.check_run(conn, run)
            return None
        self.check_run(conn, run)
        if permit is None or permit.kind != "worker" or run["status"] != "running":
            raise _error("SOURCE_SCHEDULER_BEGIN_FENCED", 403)
        return run

    @contextmanager
    def worker_context(self, bearer: str, request: SchedulerAssignmentRequest) -> Iterator[None]:
        with self.engine.transaction() as conn:
            self._session(conn, request.session_id, bearer)
            row = self._assignment(conn, request.assignment_id)
            self._authority(conn, row, request)
            permit = _Permit(
                self,
                conn,
                secrets.token_hex(32),
                "worker",
                row["run_id"],
                row["queue_id"],
                row["session_id"],
                row["assignment_id"],
                row["execution_epoch"],
                row["execution_epoch"],
            )
        with self._scope(permit):
            yield

    def _history(self, conn: Connection) -> ServiceHistory:
        rows = conn.execute(
            "SELECT a.assignment_id,q.classification,q.fairness_key,a.created_at "
            "FROM app.sa_scheduler_assignments a JOIN app.sa_scheduler_queue q USING(queue_id) "
            "WHERE a.project_id=%s ORDER BY a.created_at DESC,a.assignment_id DESC LIMIT 256",
            (self.project,),
        ).fetchall()
        keys: dict[str, int] = {}
        for index, row in enumerate(reversed(rows), 1):
            keys[row["fairness_key"]] = index
        return ServiceHistory(
            last_class=rows[0]["classification"] if rows else None,
            buckets=tuple(
                ServiceBucket(
                    project_id=self.project,
                    fairness_key=key,
                    project_sequence=len(rows),
                    source_sequence=value,
                )
                for key, value in keys.items()
            ),
        )

    def claim(self, bearer: str, request: SchedulerClaimRequest) -> Row:
        with self.engine.transaction() as conn:
            self._conn(conn)
            if not self._ready(conn):
                raise _error("SOURCE_SCHEDULER_NOT_MIGRATED")
            session = self._session(conn, request.session_id, bearer)
            config = self._configuration(conn, active=True)
            if config.revision != request.expected_config_revision:
                raise _error("SOURCE_SCHEDULER_CONFIG_STALE")
            if self.driver is None or self.driver.profile_sha256 != session["profile_sha256"]:
                raise _error("SOURCE_SCHEDULER_DRIVER_REQUIRED", 403)
            fingerprint = digest(request.model_dump(mode="json"))
            previous = conn.execute(
                "SELECT * FROM app.sa_scheduler_assignments WHERE project_id=%s AND request_id=%s",
                (self.project, request.request_id),
            ).fetchone()
            if previous:
                if (
                    previous["request_sha256"] != fingerprint
                    or previous["session_id"] != request.session_id
                ):
                    raise _error("SOURCE_SCHEDULER_IDEMPOTENCY_CONFLICT")
                self._authority(conn, previous, self._request(previous), preparing=True)
                return self._claim_view(conn, previous)
            self._budgets(conn)
            queue_rows = conn.execute(
                "SELECT * FROM app.sa_scheduler_queue WHERE project_id=%s AND state='queued' "
                "ORDER BY enqueued_at,queue_id LIMIT 256 FOR UPDATE",
                (self.project,),
            ).fetchall()
            candidates = []
            for row in queue_rows:
                eligible = False
                try:
                    run = self._run(conn, row["run_id"])
                    eligible = (
                        run["status"] == "queued"
                        and run["ticket_status"] == "open"
                        and self._mode_allowed(run)
                        and run["execution_epoch"] == row["expected_epoch"]
                        and self._target_hash(conn, run) == row["target_sha256"]
                    )
                    if eligible:
                        self._source(conn, row, run)
                except DomainError:
                    eligible = False
                candidates.append(
                    QueueCandidate(
                        queue_id=row["queue_id"],
                        run_id=row["run_id"],
                        project_id=self.project,
                        fairness_key=row["fairness_key"],
                        classification=row["classification"],
                        enqueued_at=row["enqueued_at"],
                        generation=row["generation"],
                        eligible=eligible,
                        resources=ResourceRequest(
                            cpu_millis=row["cpu_millis"], memory_mib=row["memory_mib"]
                        ),
                    )
                )
            picked = choose_candidate(
                tuple(candidates),
                self._history(conn),
                project_id=self.project,
                limits=config.configuration.limits,
                usage=self._usage(conn),
                at=self._now(conn),
            )
            if picked is None:
                return {
                    "assignment": None,
                    "reason_code": "SOURCE_SCHEDULER_NO_CAPACITY_OR_WORK",
                }
            queue = self._queue_row(conn, picked.queue_id)
            run = self._run(conn, queue["run_id"])
            self._fixture(run)
            assignment_id, workspace_id = uuid4(), uuid4()
            slot = slot_quota_key(assignment_id)
            self.engine.project_budgets.quota(
                conn, "slot", slot, source_id=queue["source_instance_id"]
            )
            workspace_hash = self.driver.workspace_identity(
                self.project, assignment_id, workspace_id
            )
            self._sha(workspace_hash)
            permit = _Permit(
                self,
                conn,
                secrets.token_hex(32),
                "claim",
                run["run_id"],
                queue["queue_id"],
                request.session_id,
                assignment_id,
                run["execution_epoch"],
                run["execution_epoch"] + 1,
            )
            with self._scope(permit):
                started = self.engine.begin_run(run["run_id"])
            at = self._now(conn)
            limits = config.configuration.limits
            inserted = conn.execute(
                "INSERT INTO app.sa_scheduler_assignments(assignment_id,project_id,request_id,"
                "request_sha256,"
                "queue_id,session_id,thread_id,ticket_id,run_id,mandate_id,target_sha256,"
                "execution_epoch,"
                "config_revision,workspace_id,workspace_sha256,cpu_millis,memory_mib,"
                "lease_until,prepare_until,"
                "slot_quota_key,created_at,updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING *",
                (
                    assignment_id,
                    self.project,
                    request.request_id,
                    fingerprint,
                    queue["queue_id"],
                    request.session_id,
                    run["thread_id"],
                    run["ticket_id"],
                    run["run_id"],
                    run["mandate_id"],
                    queue["target_sha256"],
                    started["execution_epoch"],
                    config.revision,
                    workspace_id,
                    workspace_hash,
                    queue["cpu_millis"],
                    queue["memory_mib"],
                    min(
                        at + timedelta(seconds=limits.lease_seconds),
                        session["expires_at"],
                        config.configuration.valid_until,
                    ),
                    at + timedelta(seconds=limits.preparation_seconds),
                    slot,
                    at,
                    at,
                ),
            ).fetchone()
            assert inserted is not None
            row = inserted
            self._authority(conn, row, self._request(row), preparing=True)
            conn.execute(
                "UPDATE app.sa_scheduler_queue SET state='claimed',assignment_id=%s,"
                "claimed_at=clock_timestamp(),revision=revision+1 WHERE queue_id=%s AND "
                "revision=%s",
                (assignment_id, queue["queue_id"], queue["revision"]),
            )
            self.engine.project_budgets.release_quota(conn, "queue", queue["queue_quota_key"])
            self._event(
                conn,
                "ASSIGNMENT_CLAIMED",
                row,
                state="preparing",
                epoch=started["execution_epoch"],
            )
            context = self._new_operation(
                conn, row, "prepare", _hash([assignment_id, "prepare", workspace_hash])
            )
        self._perform(context)
        with self.engine.transaction() as conn:
            return self._claim_view(conn, self._assignment(conn, assignment_id))

    def _claim_view(self, conn: Connection, row: Row) -> Row:
        operation = conn.execute(
            "SELECT * FROM app.sa_scheduler_operations WHERE assignment_id=%s AND kind='prepare' "
            "ORDER BY created_at LIMIT 1",
            (row["assignment_id"],),
        ).fetchone()
        return {
            **self._view(row),
            "assignment": self._assignment_facts(row).model_dump(mode="json"),
            "prepare_operation": self._view(operation) if operation else None,
        }

    @staticmethod
    def _sha(value: str) -> str:
        if (
            type(value) is not str
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise _error("SOURCE_SCHEDULER_HASH_INVALID", 422)
        return value

    def _operation(self, conn: Connection, operation_id: UUID) -> Row:
        row = conn.execute(
            "SELECT * FROM app.sa_scheduler_operations WHERE operation_id=%s AND "
            "project_id=%s FOR UPDATE",
            (operation_id, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_SCHEDULER_OPERATION_NOT_FOUND", 404)
        return row

    def _context(
        self,
        conn: Connection,
        assignment: Row,
        operation: Row,
        *,
        cleanup: bool = False,
    ) -> ResourceContext:
        session = self._session(conn, assignment["session_id"])
        operations = conn.execute(
            "SELECT * FROM app.sa_scheduler_operations WHERE assignment_id=%s "
            "AND (state<>'known_terminal' OR kind IN ('prepare','start','job')) ORDER BY "
            "created_at,operation_id LIMIT 66",
            (assignment["assignment_id"],),
        ).fetchall()
        if len(operations) > 65:
            raise _error("SOURCE_SCHEDULER_OPERATION_CAPACITY", 429)
        context = ResourceContext(
            project_id=self.project,
            assignment_id=assignment["assignment_id"],
            queue_id=assignment["queue_id"],
            run_id=assignment["run_id"],
            session_id=assignment["session_id"],
            workspace_id=assignment["workspace_id"],
            epoch=assignment["execution_epoch"],
            operation_id=operation["operation_id"],
            kind=operation["kind"],
            request_sha256=operation["request_sha256"],
            workspace_sha256=assignment["workspace_sha256"],
            profile_sha256=session["profile_sha256"],
            target_sha256=assignment["target_sha256"],
            resources=ResourceRequest(
                cpu_millis=assignment["cpu_millis"], memory_mib=assignment["memory_mib"]
            ),
            nonce=secrets.token_hex(32),
            provider_operation_id=operation["provider_operation_id"],
            operations=tuple(
                ResourceOperation(
                    operation_id=row["operation_id"],
                    kind=row["kind"],
                    request_sha256=row["request_sha256"],
                    state=row["state"],
                    provider_operation_id=row["provider_operation_id"],
                    result_sha256=row["result_sha256"],
                    observed_at=row["observed_at"],
                )
                for row in operations
            ),
            cleanup_only=cleanup,
        )
        if len(self._contexts) >= 256:
            completed = conn.execute(
                "SELECT operation_id FROM app.sa_scheduler_operations "
                "WHERE operation_id=ANY(%s) AND state='known_terminal' ORDER BY created_at "
                "LIMIT 128",
                (list(self._contexts),),
            ).fetchall()
            for previous in completed:
                self._contexts.pop(previous["operation_id"], None)
        if len(self._contexts) >= 256 and context.operation_id not in self._contexts:
            raise _error("SOURCE_SCHEDULER_OPERATION_CAPACITY", 429)
        self._contexts[context.operation_id] = context
        return context

    def _issued(self, context: ResourceContext) -> None:
        original = self._contexts.get(context.operation_id)
        if (
            original is not context
            or original.nonce != context.nonce
            or context.project_id != self.project
        ):
            raise _error("SOURCE_SCHEDULER_RESOURCE_CONTEXT_DENIED", 403)
        if self.driver is None or self.driver.profile_sha256 != context.profile_sha256:
            raise _error("SOURCE_SCHEDULER_DRIVER_REQUIRED", 403)

    def _new_operation(
        self,
        conn: Connection,
        assignment: Row,
        kind: OperationKind,
        request_sha256: str,
        *,
        cleanup: bool = False,
    ) -> ResourceContext:
        self._sha(request_sha256)
        if cleanup:
            pending_cleanup = conn.execute(
                "SELECT * FROM app.sa_scheduler_operations "
                "WHERE assignment_id=%s AND kind IN ('stop','inspect') AND state<>'known_terminal' "
                "ORDER BY created_at LIMIT 1",
                (assignment["assignment_id"],),
            ).fetchone()
            if pending_cleanup:
                return self._context(conn, assignment, pending_cleanup, cleanup=True)
        previous = conn.execute(
            "SELECT * FROM app.sa_scheduler_operations WHERE assignment_id=%s AND kind=%s AND "
            "request_sha256=%s",
            (assignment["assignment_id"], kind, request_sha256),
        ).fetchone()
        if previous:
            return self._context(conn, assignment, previous, cleanup=cleanup)
        pending = conn.execute(
            "SELECT count(*) AS n FROM app.sa_scheduler_operations WHERE assignment_id=%s AND "
            "state<>'known_terminal'",
            (assignment["assignment_id"],),
        ).fetchone()
        assert pending is not None
        if pending["n"] >= 64 and kind not in {"stop", "inspect"}:
            raise _error("SOURCE_SCHEDULER_OPERATION_CAPACITY", 429)
        if kind in {"prepare", "start", "job"}:
            total = conn.execute(
                "SELECT count(*) AS n FROM app.sa_scheduler_operations "
                "WHERE assignment_id=%s AND kind IN ('prepare','start','job')",
                (assignment["assignment_id"],),
            ).fetchone()
            assert total is not None
            if total["n"] >= 64:
                raise _error("SOURCE_SCHEDULER_OPERATION_CAPACITY", 429)
        operation_id = uuid4()
        row = conn.execute(
            "INSERT INTO app.sa_scheduler_operations(operation_id,project_id,assignment_id,"
            "session_id,run_id,"
            "execution_epoch,kind,request_sha256,provider_operation_id) VALUES(%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s) RETURNING *",
            (
                operation_id,
                self.project,
                assignment["assignment_id"],
                assignment["session_id"],
                assignment["run_id"],
                assignment["execution_epoch"],
                kind,
                request_sha256,
                f"mgar-sa12-{operation_id.hex}",
            ),
        ).fetchone()
        assert row is not None
        self._event(
            conn,
            "RESOURCE_INTENT",
            row,
            state="requested",
            request_sha256=request_sha256,
        )
        return self._context(conn, assignment, row, cleanup=cleanup)

    def begin_operation(
        self,
        bearer: str,
        request: SchedulerAssignmentRequest,
        kind: Literal["start", "job"],
        request_sha256: str,
    ) -> ResourceContext:
        if kind not in {"start", "job"}:
            raise _error("SOURCE_SCHEDULER_OPERATION_KIND_DENIED", 403)
        with self.engine.transaction() as conn:
            self._session(conn, request.session_id, bearer)
            row = self._assignment(conn, request.assignment_id)
            self._authority(conn, row, request)
            return self._new_operation(conn, row, kind, request_sha256)

    def capability_context(self, context: ResourceContext) -> Row:
        self._issued(context)
        with self.engine.transaction() as conn:
            row = self._assignment(conn, context.assignment_id)
            operation = self._operation(conn, context.operation_id)
            self._binding(context, row, operation)
            if context.cleanup_only:
                config = self._configuration(conn)
                session = self._session(conn, row["session_id"])
                run = self._run(conn, row["run_id"])
                facts: Row = {
                    "assignment": self._assignment_facts(row),
                    "configuration": config,
                    "session": self._session_facts(session),
                    "now": self._now(conn),
                    "source": SourceAuthorityFacts(required=False),
                    "run": RunFacts(
                        run_id=run["run_id"],
                        ticket_id=run["ticket_id"],
                        project_id=self.project,
                        epoch=run["execution_epoch"],
                        status=run["status"],
                    ),
                }
            else:
                facts = self._authority(
                    conn, row, self._request(row), preparing=context.kind == "prepare"
                )
                run = self._run(conn, row["run_id"])
            mandate = conn.execute(
                "SELECT actor FROM app.mandates WHERE mandate_id=%s AND project_id=%s",
                (row["mandate_id"], self.project),
            ).fetchone()
            assert mandate is not None
            read_only = True
            coding = getattr(self.engine, "source_coding", None)
            if (
                not context.cleanup_only
                and coding is not None
                and coding.managed(conn, row["run_id"])
            ):
                read_only = coding.read_only(conn, row)
            return {
                **facts,
                "actor": mandate["actor"],
                "repository": run["repository"],
                "execution_mode": run["execution_mode"],
                "lease_until": row["lease_until"],
                "expires_at": min(
                    row["lease_until"],
                    facts["session"].expires_at,
                    facts["configuration"].configuration.valid_until,
                ),
                "execution_authorized": not context.cleanup_only,
                "read_only": read_only,
                "target_sha256": row["target_sha256"],
                "workspace_sha256": row["workspace_sha256"],
                "operation_state": operation["state"],
            }

    @staticmethod
    def _binding(context: ResourceContext, assignment: Row, operation: Row) -> None:
        if any(
            (
                context.assignment_id != assignment["assignment_id"],
                context.run_id != assignment["run_id"],
                context.session_id != assignment["session_id"],
                context.workspace_id != assignment["workspace_id"],
                context.epoch != assignment["execution_epoch"],
                context.queue_id != assignment["queue_id"],
                context.workspace_sha256 != assignment["workspace_sha256"],
                context.target_sha256 != assignment["target_sha256"],
                operation["assignment_id"] != context.assignment_id,
                operation["request_sha256"] != context.request_sha256,
                operation["kind"] != context.kind,
                operation["provider_operation_id"] != context.provider_operation_id,
            )
        ):
            raise _error("SOURCE_SCHEDULER_RESOURCE_BINDING_DENIED", 403)

    @contextmanager
    def dispatch_guard(self, context: ResourceContext) -> Iterator[None]:
        self._issued(context)
        if context.cleanup_only:
            raise _error("SOURCE_SCHEDULER_CLEANUP_CANNOT_EXECUTE", 403)
        with self.engine._mutex:
            with self.engine.transaction() as conn:
                row = self._assignment(conn, context.assignment_id)
                operation = self._operation(conn, context.operation_id)
                self._binding(context, row, operation)
                self._authority(conn, row, self._request(row), preparing=context.kind == "prepare")
                if operation["state"] != "requested":
                    raise _error("SOURCE_SCHEDULER_RESOURCE_ALREADY_DISPATCHED", 403)
                changed = conn.execute(
                    "UPDATE app.sa_scheduler_operations SET state='running',"
                    "revision=revision+1,updated_at=clock_timestamp() WHERE operation_id=%s "
                    "AND revision=%s RETURNING *",
                    (operation["operation_id"], operation["revision"]),
                ).fetchone()
                assert changed is not None
                self._event(conn, "RESOURCE_DISPATCHED", changed, state="running")
            if self.engine.connection.info.transaction_status != TransactionStatus.IDLE:
                raise _error("SOURCE_SCHEDULER_RESOURCE_IO_INSIDE_TRANSACTION", 403)
            yield

    def _receipt(self, context: ResourceContext, receipt: ResourceReceipt) -> None:
        if not isinstance(receipt, ResourceReceipt) or any(
            (
                receipt.project_id != context.project_id,
                receipt.assignment_id != context.assignment_id,
                receipt.run_id != context.run_id,
                receipt.session_id != context.session_id,
                receipt.workspace_id != context.workspace_id,
                receipt.epoch != context.epoch,
                receipt.operation_id != context.operation_id,
                receipt.request_sha256 != context.request_sha256,
                receipt.provider_operation_id != context.provider_operation_id,
            )
        ):
            raise _error("SOURCE_SCHEDULER_RECEIPT_MISMATCH", 403)
        if receipt.observed_at.utcoffset() is None or receipt.state not in {
            "running",
            "unknown",
            "known_terminal",
        }:
            raise _error("SOURCE_SCHEDULER_RECEIPT_INVALID", 403)
        if receipt.tree_sha256 is not None:
            self._sha(receipt.tree_sha256)
        if receipt.state == "known_terminal":
            if receipt.result_code not in {
                "succeeded",
                "failed",
                "cancelled",
                "absent",
            }:
                raise _error("SOURCE_SCHEDULER_RECEIPT_INVALID", 403)
        elif (
            receipt.result_code is not None
            or receipt.exit_code is not None
            or receipt.processes_stopped
            or receipt.workspace_fenced
        ):
            raise _error("SOURCE_SCHEDULER_RECEIPT_INVALID", 403)
        if len(receipt.related) > 64:
            raise _error("SOURCE_SCHEDULER_RECEIPT_INVALID", 403)

    def _record_receipt(
        self, conn: Connection, context: ResourceContext, receipt: ResourceReceipt
    ) -> Row:
        self._issued(context)
        self._receipt(context, receipt)
        assignment = self._assignment(conn, context.assignment_id)
        row = self._operation(conn, context.operation_id)
        self._binding(context, assignment, row)
        now = self._now(conn)
        if receipt.observed_at > now or receipt.observed_at < row["created_at"]:
            raise _error("SOURCE_SCHEDULER_RECEIPT_TIME_INVALID", 403)
        if row["state"] == "known_terminal":
            if row["result_sha256"] != receipt.result_sha256:
                raise _error("SOURCE_SCHEDULER_RECEIPT_CONFLICT")
            return self._view(row)
        for previous in receipt.related:
            if (
                previous.related
                or previous.state != "known_terminal"
                or previous.operation_id == context.operation_id
            ):
                raise _error("SOURCE_SCHEDULER_RECEIPT_INVALID", 403)
            old = self._operation(conn, previous.operation_id)
            if old["assignment_id"] != assignment["assignment_id"]:
                raise _error("SOURCE_SCHEDULER_RECEIPT_MISMATCH", 403)
            related = self._context(conn, assignment, old, cleanup=True)
            self._record_receipt(conn, related, previous)
        quiescent = receipt.processes_stopped and receipt.workspace_fenced
        if quiescent and context.kind not in {"stop", "inspect"}:
            quiescent = False
        state = receipt.state
        if state == "running":
            try:
                self._authority(
                    conn,
                    assignment,
                    self._request(assignment),
                    preparing=context.kind == "prepare",
                )
            except DomainError:
                # A live process observation is not renewed execution authority.
                state = "unknown"
        changed = conn.execute(
            "UPDATE app.sa_scheduler_operations SET state=%s,revision=revision+1,"
            "updated_at=clock_timestamp(),"
            "result_code=%s,result_sha256=%s,exit_code=%s,quiescent=%s,observed_at=%s "
            "WHERE operation_id=%s AND revision=%s RETURNING *",
            (
                state,
                receipt.result_code,
                receipt.result_sha256 if receipt.state == "known_terminal" else None,
                receipt.exit_code,
                quiescent,
                receipt.observed_at,
                row["operation_id"],
                row["revision"],
            ),
        ).fetchone()
        assert changed is not None
        self._event(
            conn,
            "RESOURCE_OBSERVED",
            changed,
            state=state,
            result_sha256=changed["result_sha256"],
            quiescent=quiescent,
        )
        if state == "unknown" or (
            context.kind == "prepare"
            and receipt.state == "known_terminal"
            and receipt.result_code != "succeeded"
        ):
            self._drain(conn, assignment, "AUTHORITY_CHANGED")
        return self._view(changed)

    def record_resource_receipt(self, context: ResourceContext, receipt: ResourceReceipt) -> Row:
        with self.engine.transaction() as conn:
            return self._record_receipt(conn, context, receipt)

    def _unknown(self, context: ResourceContext) -> None:
        with self.engine.transaction() as conn:
            operation = self._operation(conn, context.operation_id)
            if operation["state"] not in {"known_terminal", "unknown"}:
                changed = conn.execute(
                    "UPDATE app.sa_scheduler_operations SET state='unknown',revision=revision+1,"
                    "updated_at=clock_timestamp() WHERE operation_id=%s AND revision=%s "
                    "RETURNING *",
                    (context.operation_id, operation["revision"]),
                ).fetchone()
                assert changed is not None
                self._event(conn, "RESOURCE_UNKNOWN", changed, state="unknown")
            row = self._assignment(conn, context.assignment_id)
            self._drain(conn, row, "AUTHORITY_CHANGED")

    def _perform(self, context: ResourceContext) -> Row:
        self._issued(context)
        assert self.driver is not None
        try:
            if context.cleanup_only:
                with self.engine._mutex:
                    with self.engine.transaction() as conn:
                        row = self._assignment(conn, context.assignment_id)
                        operation = self._operation(conn, context.operation_id)
                        self._binding(context, row, operation)
                        if operation["state"] == "known_terminal":
                            return self._view(operation)
                        if row["state"] != "draining" or operation["state"] not in {
                            "requested",
                            "running",
                            "unknown",
                        }:
                            raise _error("SOURCE_SCHEDULER_DRAIN_REQUIRED")
                        first_dispatch = operation["state"] == "requested"
                        if first_dispatch:
                            conn.execute(
                                "UPDATE app.sa_scheduler_operations SET state='running',"
                                "revision=revision+1,"
                                "updated_at=clock_timestamp() WHERE operation_id=%s AND "
                                "revision=%s",
                                (operation["operation_id"], operation["revision"]),
                            )
                    if self.engine.connection.info.transaction_status != TransactionStatus.IDLE:
                        raise _error("SOURCE_SCHEDULER_RESOURCE_IO_INSIDE_TRANSACTION", 403)
                    receipt = (
                        self.driver.stop(context)
                        if first_dispatch and context.kind == "stop"
                        else self.driver.inspect(context)
                    )
                    return self.record_resource_receipt(context, receipt)
            with self.dispatch_guard(context):
                receipt = self.driver.prepare(context)
                return self.record_resource_receipt(context, receipt)
        except Exception:
            self._unknown(context)
            raise

    def heartbeat(self, bearer: str, request: SchedulerHeartbeatRequest) -> Row:
        with self.engine.transaction() as conn:
            self._session(conn, request.session_id, bearer)
            row = self._assignment(conn, request.assignment_id)
            facts = self._authority(conn, row, request, preparing=True)
            at = self._now(conn)
            until = min(
                at + timedelta(seconds=facts["configuration"].configuration.limits.lease_seconds),
                facts["session"].expires_at,
                facts["configuration"].configuration.valid_until,
            )
            changed = conn.execute(
                "UPDATE app.sa_scheduler_assignments SET lease_until=%s,last_heartbeat_at=%s,"
                "updated_at=%s,revision=revision+1 WHERE assignment_id=%s AND revision=%s "
                "RETURNING *",
                (until, at, at, row["assignment_id"], request.expected_revision),
            ).fetchone()
            assert changed is not None
            self._event(conn, "LEASE_HEARTBEAT", changed, lease_until=until)
            return self._view(changed)

    def ready(self, bearer: str, request: SchedulerReadyRequest) -> Row:
        with self.engine._mutex:
            with self.engine.transaction() as conn:
                self._session(conn, request.session_id, bearer)
                row = self._assignment(conn, request.assignment_id)
                self._authority(conn, row, request, preparing=True)
                operation = self._operation(conn, request.operation_id)
                if (
                    operation["assignment_id"] != row["assignment_id"]
                    or operation["kind"] != "prepare"
                    or operation["request_sha256"] != request.request_sha256
                    or operation["state"] != "known_terminal"
                    or operation["result_code"] != "succeeded"
                ):
                    raise _error("SOURCE_SCHEDULER_PREPARATION_REQUIRED", 403)
                context = self._context(conn, row, operation, cleanup=True)
            self._issued(context)
            assert self.driver is not None
            if self.engine.connection.info.transaction_status != TransactionStatus.IDLE:
                raise _error("SOURCE_SCHEDULER_RESOURCE_IO_INSIDE_TRANSACTION", 403)
            receipt = self.driver.inspect(context)
            self._receipt(context, receipt)
            if receipt.tree_sha256 is None or receipt.result_sha256 != operation["result_sha256"]:
                raise _error("SOURCE_SCHEDULER_WORKSPACE_STALE", 403)
            with self.engine.transaction() as conn:
                row = self._assignment(conn, request.assignment_id)
                self._authority(conn, row, request, preparing=True)
                if row["state"] != "preparing":
                    raise _error("SOURCE_SCHEDULER_REVISION_CONFLICT")
                changed = conn.execute(
                    "UPDATE app.sa_scheduler_assignments SET state='active',revision=revision+1,"
                    "updated_at=clock_timestamp() WHERE assignment_id=%s AND revision=%s "
                    "RETURNING *",
                    (request.assignment_id, request.expected_revision),
                ).fetchone()
                assert changed is not None
                self._event(conn, "ASSIGNMENT_READY", changed, state="active")
                return self._view(changed)

    def _fence_original_run(self, conn: Connection, row: Row, reason: str) -> None:
        self._conn(conn)
        run = self._run(conn, row["run_id"])
        if run["execution_epoch"] != row["execution_epoch"] or run["status"] in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            return
        if run["status"] != "paused":
            try:
                self.engine.pause(run["run_id"])
                return
            except DomainError as error:
                if error.code != "CONTROL_UNSUPPORTED":
                    raise
        # Original R1 has no public pause API. Preserve the original pause facts
        # on the held connection with an exact old-epoch CAS, never a new Run.
        invalidated = self.engine._invalidate_proposals(conn, run["run_id"])
        updated = conn.execute(
            "UPDATE app.runs SET status='paused',execution_epoch=execution_epoch+1,"
            "proposal_generation=proposal_generation+%s,reason_code=%s "
            "WHERE run_id=%s AND execution_epoch=%s "
            "AND status NOT IN ('succeeded','failed','cancelled') RETURNING execution_epoch",
            (
                int(invalidated > 0),
                "SOURCE_SCHEDULER_" + reason,
                run["run_id"],
                row["execution_epoch"],
            ),
        ).fetchone()
        if updated is not None:
            conn.execute(
                "UPDATE app.read_jobs SET status='queued',lease_token=NULL "
                "WHERE run_id=%s AND status='running'",
                (run["run_id"],),
            )
            self.engine._event(conn, run["run_id"], "run_paused")

    def _drain(self, conn: Connection, row: Row, reason: str) -> Row:
        if row["state"] == "released":
            return row
        self._fence_original_run(conn, row, reason)
        if row["state"] == "draining":
            return row
        changed = conn.execute(
            "UPDATE app.sa_scheduler_assignments SET state='draining',reason_code=%s,"
            "fenced_at=clock_timestamp(),updated_at=clock_timestamp(),revision=revision+1 "
            "WHERE assignment_id=%s AND revision=%s RETURNING *",
            (reason, row["assignment_id"], row["revision"]),
        ).fetchone()
        assert changed is not None
        self._event(conn, "ASSIGNMENT_DRAINING", changed, state="draining")
        return changed

    def drain(self, cookie: str, request: SchedulerOwnerDrainRequest) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            row = self._assignment(conn, request.assignment_id)
            if row["revision"] != request.expected_revision:
                raise _error("SOURCE_SCHEDULER_REVISION_CONFLICT")
            return self._view(self._drain(conn, row, request.reason_code))

    def report(self, bearer: str, request: SchedulerReportRequest) -> Row:
        with self.engine.transaction() as conn:
            self._session(conn, request.session_id, bearer)
            row = self._assignment(conn, request.assignment_id)
            self._authority(conn, row, request)
            operation = self._operation(conn, request.operation_id)
            if (
                operation["assignment_id"] != row["assignment_id"]
                or operation["request_sha256"] != request.request_sha256
                or operation["state"] != "known_terminal"
            ):
                raise _error("SOURCE_SCHEDULER_RESOURCE_RECEIPT_REQUIRED", 403)
            reason = "WAITING_INPUT" if request.status == "waiting_user" else "RUN_STOPPED"
            return {
                **self._view(self._drain(conn, row, reason)),
                "business_outcome_changed": False,
            }

    def _pending(self, conn: Connection, assignment: Row) -> PendingWork:
        rows = conn.execute(
            "SELECT state,count(*) AS n FROM app.sa_scheduler_operations WHERE "
            "assignment_id=%s GROUP BY state",
            (assignment["assignment_id"],),
        ).fetchall()
        operations = {row["state"]: row["n"] for row in rows}
        jobs = conn.execute(
            "SELECT status,count(*) AS n FROM app.read_jobs WHERE run_id=%s AND status IN "
            "('queued','running') GROUP BY status",
            (assignment["run_id"],),
        ).fetchall()
        counts = {row["status"]: row["n"] for row in jobs}
        return PendingWork(
            requested_operations=operations.get("requested", 0),
            running_operations=operations.get("running", 0),
            unknown_operations=operations.get("unknown", 0),
            queued_read_jobs=counts.get("queued", 0),
            running_read_jobs=counts.get("running", 0),
        )

    def _release(self, conn: Connection, row: Row, receipt: ResourceReceipt) -> Row:
        evidence = QuiescenceFacts(
            assignment_id=receipt.assignment_id,
            session_id=receipt.session_id,
            run_id=receipt.run_id,
            workspace_id=receipt.workspace_id,
            epoch=receipt.epoch,
            provider_receipt_sha256=receipt.result_sha256,
            observed_at=receipt.observed_at,
            trusted=True,
            processes_stopped=receipt.processes_stopped,
            workspace_fenced=receipt.workspace_fenced,
        )
        validate_release(
            self._assignment_facts(row),
            evidence,
            self._pending(conn, row),
            at=self._now(conn),
        )
        if row["state"] == "released":
            return row
        changed = conn.execute(
            "UPDATE app.sa_scheduler_assignments SET state='released',revision=revision+1,"
            "updated_at=clock_timestamp(),released_at=clock_timestamp(),quiescence_operation_id=%s,"
            "quiescence_sha256=%s,quiescent_at=%s WHERE assignment_id=%s AND revision=%s "
            "RETURNING *",
            (
                receipt.operation_id,
                receipt.result_sha256,
                receipt.observed_at,
                row["assignment_id"],
                row["revision"],
            ),
        ).fetchone()
        assert changed is not None
        self.engine.project_budgets.release_quota(conn, "slot", row["slot_quota_key"])
        queue = self._queue_row(conn, row["queue_id"])
        if queue["state"] != "closed":
            conn.execute(
                "UPDATE app.sa_scheduler_queue SET state='closed',closed_at=clock_timestamp(),"
                "revision=revision+1 WHERE queue_id=%s AND revision=%s",
                (queue["queue_id"], queue["revision"]),
            )
        self._event(conn, "ASSIGNMENT_RELEASED", changed, state="released", quiescent=True)
        return changed

    def reconcile(self, cookie: str, request: SchedulerReconcileRequest) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            row = self._assignment(conn, request.assignment_id)
            if row["revision"] != request.expected_revision or row["state"] != "draining":
                raise _error("SOURCE_SCHEDULER_REVISION_CONFLICT")
            operation = self._operation(conn, request.operation_id)
            if (
                operation["assignment_id"] != row["assignment_id"]
                or operation["request_sha256"] != request.request_sha256
            ):
                raise _error("SOURCE_SCHEDULER_RESOURCE_BINDING_DENIED", 403)
            context = self._new_operation(
                conn,
                row,
                "stop",
                _hash(
                    [
                        row["assignment_id"],
                        row["fenced_at"],
                        request.operation_id,
                        request.request_sha256,
                        digest(request.review_ref),
                    ]
                ),
                cleanup=True,
            )
        self._perform(context)
        with self.engine.transaction() as conn:
            row = self._assignment(conn, request.assignment_id)
            stopped = self._operation(conn, context.operation_id)
            if stopped["state"] != "known_terminal" or not stopped["quiescent"]:
                return {
                    **self._view(row),
                    "reason_code": "SOURCE_SCHEDULER_QUIESCENCE_REQUIRED",
                }
        self._issued(context)
        assert self.driver is not None
        with self.engine._mutex:
            receipt = self.driver.inspect(context)
            self._receipt(context, receipt)
            if receipt.result_sha256 != stopped["result_sha256"]:
                raise _error("SOURCE_SCHEDULER_RECEIPT_CONFLICT")
            with self.engine.transaction() as conn:
                return self._view(
                    self._release(conn, self._assignment(conn, request.assignment_id), receipt)
                )

    def release(self, bearer: str, request: SchedulerReleaseRequest) -> Row:
        with self.engine._mutex:
            with self.engine.transaction() as conn:
                self._session(conn, request.session_id, bearer)
                row = self._assignment(conn, request.assignment_id)
                if (
                    row["session_id"] != request.session_id
                    or row["execution_epoch"] != request.epoch
                    or row["revision"] != request.expected_revision
                ):
                    raise _error("SOURCE_SCHEDULER_REVISION_CONFLICT")
                operation = self._operation(conn, request.operation_id)
                if (
                    operation["assignment_id"] != row["assignment_id"]
                    or operation["request_sha256"] != request.request_sha256
                    or operation["state"] != "known_terminal"
                    or not operation["quiescent"]
                ):
                    raise _error("SOURCE_SCHEDULER_QUIESCENCE_REQUIRED", 403)
                context = self._context(conn, row, operation, cleanup=True)
            self._issued(context)
            assert self.driver is not None
            receipt = self.driver.inspect(context)
            self._receipt(context, receipt)
            if receipt.result_sha256 != operation["result_sha256"]:
                raise _error("SOURCE_SCHEDULER_RECEIPT_CONFLICT")
            with self.engine.transaction() as conn:
                return self._view(self._release(conn, row, receipt))

    def recover_conn(self, conn: Connection) -> None:
        self._conn(conn)
        if not self._ready(conn):
            return
        for row in self._occupied(conn):
            self._drain(conn, row, "PROCESS_RESTARTED")

    def cleanup_context(self, assignment_id: UUID, operation_id: UUID) -> ResourceContext:
        """Trusted recovery port: fresh cleanup-only proof, never new execution."""
        with self.engine.transaction() as conn:
            self._conn(conn)
            row = self._assignment(conn, assignment_id)
            operation = self._operation(conn, operation_id)
            if operation["assignment_id"] != assignment_id:
                raise _error("SOURCE_SCHEDULER_RESOURCE_BINDING_DENIED", 403)
            return self._context(conn, row, operation, cleanup=True)

    def pending_resource_operations(self) -> tuple[tuple[UUID, UUID], ...]:
        """Trusted recovery metadata only; no process or execution authority."""
        with self.engine.transaction() as conn:
            self._conn(conn)
            if not self._ready(conn):
                return ()
            rows = conn.execute(
                "SELECT o.assignment_id,o.operation_id FROM app.sa_scheduler_operations o "
                "JOIN app.sa_scheduler_assignments a ON a.assignment_id=o.assignment_id "
                "AND a.project_id=o.project_id WHERE a.project_id=%s AND a.state<>'released' "
                "AND o.kind IN ('start','job') AND o.state IN ('requested','running','unknown') "
                "ORDER BY a.created_at,o.created_at,o.operation_id LIMIT 129",
                (self.project,),
            ).fetchall()
            usage = Counter(row["assignment_id"] for row in rows)
            if len(rows) > 128 or any(count > 64 for count in usage.values()):
                raise _error("SOURCE_SCHEDULER_OPERATION_CAPACITY", 429)
            return tuple(
                (UUID(str(row["assignment_id"])), UUID(str(row["operation_id"]))) for row in rows
            )

    def request_stop(
        self,
        context: ResourceContext,
        reason: Literal["COMMAND_TIMEOUT", "RUNTIME_SHUTDOWN"] = "COMMAND_TIMEOUT",
    ) -> Row:
        """Trusted Runtime stop request; caller then ticks maintain for actual IO."""
        self._issued(context)
        if reason not in {"COMMAND_TIMEOUT", "RUNTIME_SHUTDOWN"}:
            raise _error("SOURCE_SCHEDULER_STOP_REASON_INVALID", 422)
        with self.engine.transaction() as conn:
            row = self._assignment(conn, context.assignment_id)
            operation = self._operation(conn, context.operation_id)
            self._binding(context, row, operation)
            return self._view(self._drain(conn, row, reason))

    def maintain(self) -> dict[str, int]:
        """Explicit trusted Runner tick; construction never starts this loop.

        Expiry first fences and commits cleanup intent, then stops/inspects the
        exact installed driver's resources outside the transaction. Unknown work
        retains occupancy and is inspected on later ticks instead of resent.
        """
        counts = dict.fromkeys(
            (
                "scanned",
                "healthy",
                "drained",
                "released",
                "unknown",
                "blocked",
                "driver_unavailable",
            ),
            0,
        )
        contexts = []
        with self.engine.transaction() as conn:
            self._conn(conn)
            if not self._ready(conn):
                return counts
            rows = self._occupied(conn)
            counts["scanned"] = len(rows)
            for row in rows:
                if row["state"] in {"preparing", "active"}:
                    try:
                        self._authority(conn, row, self._request(row), preparing=True)
                    except DomainError as error:
                        if "SESSION" in error.code:
                            reason = "SESSION_REVOKED"
                        elif "EXPIRED" in error.code or "LEASE" in error.code:
                            reason = "LEASE_EXPIRED"
                        elif "RUN_FENCED" in error.code or "EPOCH" in error.code:
                            reason = "RUN_STOPPED"
                        else:
                            reason = "AUTHORITY_CHANGED"
                        row = self._drain(conn, row, reason)
                        counts["drained"] += 1
                    else:
                        counts["healthy"] += 1
                        continue
                if self.driver is None:
                    counts["driver_unavailable"] += 1
                    continue
                context = self._new_operation(
                    conn,
                    row,
                    "stop",
                    _hash([row["assignment_id"], row["fenced_at"], "trusted-maintenance"]),
                    cleanup=True,
                )
                contexts.append(context)
        for context in contexts:
            try:
                self._perform(context)
                with self.engine._mutex:
                    with self.engine.transaction() as conn:
                        row = self._assignment(conn, context.assignment_id)
                        operation = self._operation(conn, context.operation_id)
                        if operation["state"] != "known_terminal":
                            counts["unknown"] += 1
                            continue
                        if not operation["quiescent"]:
                            counts["blocked"] += 1
                            continue
                        current = self._context(conn, row, operation, cleanup=True)
                    self._issued(current)
                    assert self.driver is not None
                    if self.engine.connection.info.transaction_status != TransactionStatus.IDLE:
                        raise _error("SOURCE_SCHEDULER_RESOURCE_IO_INSIDE_TRANSACTION", 403)
                    receipt = self.driver.inspect(current)
                    self._receipt(current, receipt)
                    if receipt.result_sha256 != operation["result_sha256"]:
                        raise _error("SOURCE_SCHEDULER_RECEIPT_CONFLICT")
                    with self.engine.transaction() as conn:
                        row = self._assignment(conn, context.assignment_id)
                        released = row["state"] == "released"
                        self._release(conn, row, receipt)
                        counts["released"] += int(not released)
            except DomainError as error:
                if error.code == "SOURCE_SCHEDULER_WORK_PENDING":
                    counts["blocked"] += 1
                else:
                    counts["unknown"] += 1
            except Exception:
                counts["unknown"] += 1
        return counts
