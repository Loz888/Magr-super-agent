"""Owner-scoped operations facts on the original Engine's held connection.

No service credential is a business grant. A reserved tick survives loss of its
HTTP receipt or process; only that execution can record a known result.
"""

from __future__ import annotations

import hmac
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.super_agent.source_operations_client import ServiceView, TickReceipt
from mgar_v3.super_agent.source_operations_domain import (
    ComponentGrant,
    ComponentHeartbeat,
    ComponentOpen,
    ComponentTick,
    Counter,
    IncidentDecision,
    OperationsDefinition,
    OperationsSave,
    OperationsSnapshot,
    Role,
    SignalSubmission,
    signals,
    valid_definition,
)

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine

Row = dict[str, Any]


def denied(code: str, status: int = 409) -> DomainError:
    return DomainError("SOURCE_OPERATIONS_" + code, "Operations request was not authorized", status)


class TickOutcome(Model):
    processed: Counter = 0
    pending: Counter = 0
    blocked: Counter = 0
    unknown: Counter = 0
    waiting_runner: bool = False
    model_calls: Counter = 0
    external_writes: Counter = 0


class OperationsPort(ABC):
    """Trusted server composition only, never selected by a model or HTTP body.

    Each bounded invocation uses original business services and permissions.
    The guard must precede each dispatch; no IO may occur in a SQL transaction.
    """

    role: Role

    @abstractmethod
    def invoke(self, guard: Callable[[], None]) -> TickOutcome: ...


class SourceOperations:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.identities = engine.source_correlations.identities
        self._ports: dict[str, OperationsPort] = {}
        self._recovered = False

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    def bind(self, port: OperationsPort) -> None:
        if not isinstance(port, OperationsPort) or port.role not in {
            "worker",
            "scheduler",
            "dispatcher",
        }:
            raise denied("TRUSTED_PORT_REQUIRED", 422)
        self._ports[port.role] = port

    def now(self, conn: Connection) -> datetime:
        row = conn.execute("SELECT clock_timestamp() AS at").fetchone()
        assert row is not None
        return row["at"]

    def _console(self, conn: Connection, cookie: str) -> UUID:
        return self.identities._console(conn, cookie)

    def _current(self, conn: Connection) -> tuple[Row | None, OperationsDefinition | None]:
        row = conn.execute(
            "SELECT v.* FROM app.sa_operations_controls c JOIN app.sa_operations_versions v "
            "USING(project_id,revision) WHERE c.project_id=%s",
            (self.project,),
        ).fetchone()
        if row is None:
            return None, None
        definition = OperationsDefinition.model_validate(row["definition"])
        if digest(definition.model_dump(mode="json")) != row["definition_sha256"].strip():
            raise denied("CONFIGURATION_CORRUPT", 503)
        return row, definition

    def read(self, cookie: str) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            row, definition = self._current(conn)
            return {
                "revision": row["revision"] if row else 0,
                "definition": definition.model_dump(mode="json") if definition else None,
                "default": "disabled",
                "real_gates": "NOT_RUN",
                "unknown_retry": False,
            }

    def save(self, cookie: str, body: OperationsSave) -> Row:
        with self.engine.transaction() as conn:
            owner = self._console(conn, cookie)
            old, _ = self._current(conn)
            if (old["revision"] if old else 0) != body.expected_revision:
                raise denied("REVISION_CONFLICT")
            if not valid_definition(body.definition, self.now(conn)):
                raise denied("EXPIRY_INVALID", 422)
            for grant in body.definition.components:
                previous = self._component(conn, grant.component_id)
                if previous and previous["role"] != grant.role:
                    raise denied("ROLE_IMMUTABLE")
                for source_id in grant.source_ids:
                    self.identities.registry._instance(conn, source_id)
            revision = body.expected_revision + 1
            conn.execute(
                "INSERT INTO app.sa_operations_versions(project_id,revision,definition,"
                "definition_sha256,signed_by,review_ref,valid_until) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    self.project,
                    revision,
                    Jsonb(body.definition.model_dump(mode="json")),
                    digest(body.definition.model_dump(mode="json")),
                    owner,
                    body.review_ref,
                    body.definition.valid_until,
                ),
            )
            conn.execute(
                "INSERT INTO app.sa_operations_controls(project_id,revision) VALUES(%s,%s) "
                "ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision",
                (self.project, revision),
            )
            return {"revision": revision, "definition": body.definition.model_dump(mode="json")}

    def _component(self, conn: Connection, component_id: UUID) -> Row | None:
        return conn.execute(
            "SELECT * FROM app.sa_operations_components WHERE project_id=%s AND component_id=%s",
            (self.project, component_id),
        ).fetchone()

    def _grant(
        self,
        conn: Connection,
        component_id: UUID,
        *,
        role: Role | None = None,
    ) -> tuple[Row, OperationsDefinition, ComponentGrant]:
        row, definition = self._current(conn)
        if (
            row is None
            or definition is None
            or not definition.enabled
            or (definition.valid_until <= self.now(conn))
        ):
            raise denied("INACTIVE", 403)
        grant = next((g for g in definition.components if g.component_id == component_id), None)
        owner = conn.execute(
            "SELECT 1 FROM app.web_accounts WHERE user_id=%s AND singleton",
            (row["signed_by"],),
        ).fetchone()
        if grant is None or not grant.enabled or owner is None or (role and grant.role != role):
            raise denied("SCOPE_DENIED", 403)
        return row, definition, grant

    def _lease(
        self,
        conn: Connection,
        component_id: UUID,
        generation: int,
        boot_id: UUID,
    ) -> Row:
        version, _, _ = self._grant(conn, component_id)
        row = self._component(conn, component_id)
        if (
            row is None
            or (row["generation"], row["boot_id"], row["config_revision"])
            != (generation, boot_id, version["revision"])
            or row["expires_at"] <= self.now(conn)
            or row["state"] == "stopping"
        ):
            raise denied("FENCED")
        return row

    def open(self, component_id: UUID, body: ComponentOpen) -> Row:
        with self.engine.transaction() as conn:
            version, definition, grant = self._grant(conn, component_id)
            old, now = self._component(conn, component_id), self.now(conn)
            if body.config_revision != version["revision"]:
                raise denied("REVISION_CONFLICT")
            if old and old["boot_id"] == body.boot_id and old["open_request_id"] == body.request_id:
                if old["config_revision"] != version["revision"]:
                    raise denied("FENCED")
                return {"generation": old["generation"], "created": False}
            if (old["generation"] if old else 0) != body.expected_generation:
                raise denied("GENERATION_CONFLICT")
            if old and old["tick_state"] in {"reserved", "unknown"}:
                raise denied("AWAITING_RECONCILIATION")
            expires = min(now + timedelta(seconds=definition.lease_seconds), definition.valid_until)
            row = conn.execute(
                "INSERT INTO app.sa_operations_components(project_id,component_id,role,"
                "config_revision,generation,boot_id,open_request_id,state,opened_at,heartbeat_at,"
                "expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'starting',%s,%s,%s) "
                "ON CONFLICT(project_id,component_id) DO UPDATE SET "
                "config_revision=excluded.config_revision,generation=excluded.generation,"
                "boot_id=excluded.boot_id,open_request_id=excluded.open_request_id,state='starting',"
                "sequence=0,opened_at=excluded.opened_at,heartbeat_at=excluded.heartbeat_at,"
                "expires_at=excluded.expires_at RETURNING generation",
                (
                    self.project,
                    component_id,
                    grant.role,
                    version["revision"],
                    body.expected_generation + 1,
                    body.boot_id,
                    body.request_id,
                    now,
                    now,
                    expires,
                ),
            ).fetchone()
            assert row is not None
            return {"generation": row["generation"], "created": True}

    def heartbeat(self, component_id: UUID, body: ComponentHeartbeat) -> Row:
        with self.engine.transaction() as conn:
            row = self._lease(conn, component_id, body.generation, body.boot_id)
            if body.sequence <= row["sequence"]:
                if body.sequence == row["sequence"] and body.state == row["state"]:
                    return {"sequence": row["sequence"], "extended": False}
                raise denied("SEQUENCE_CONFLICT")
            if body.sequence != row["sequence"] + 1:
                raise denied("SEQUENCE_CONFLICT")
            _, definition, _ = self._grant(conn, component_id)
            now = self.now(conn)
            conn.execute(
                "UPDATE app.sa_operations_components SET sequence=%s,state=%s,heartbeat_at=%s,"
                "expires_at=%s WHERE project_id=%s AND component_id=%s",
                (
                    body.sequence,
                    body.state,
                    now,
                    min(now + timedelta(seconds=definition.lease_seconds), definition.valid_until),
                    self.project,
                    component_id,
                ),
            )
            return {"sequence": body.sequence, "extended": True}

    def snapshot(self, cookie: str) -> OperationsSnapshot:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return self._snapshot(conn)

    def _snapshot(self, conn: Connection) -> OperationsSnapshot:
        from mgar_v3.super_agent.source_operations_projection import snapshot

        return snapshot(self, conn)

    def service_view(self, component_id: UUID) -> ServiceView:
        # Credential authentication is independent of activation, to permit
        # read-only reconciliation even after an Owner disables the component.
        with self.engine.transaction() as conn:
            _, definition = self._current(conn)
            snapshot = self._snapshot(conn)
            row = self._component(conn, component_id)
            tick = (
                TickReceipt(
                    state=row["tick_state"],
                    sequence=row["tick_sequence"],
                    request_id=row["tick_request_id"],
                    processed=row["tick_result"].get("processed", 0),
                )
                if row
                else TickReceipt()
            )
            return ServiceView(
                definition=definition,
                snapshot=snapshot,
                snapshot_sha256=self.basis(snapshot),
                tick=tick,
                driver_available="worker" in self._ports,
            )

    @staticmethod
    def basis(snapshot: OperationsSnapshot) -> str:
        # Sampling time is not a progress/incident generation. Server recomputes
        # the condition as well; crossing a deadline cannot be forged by a client.
        return digest(snapshot.model_dump(mode="json", exclude={"observed_at"}))

    def check_tick(self, component_id: UUID, body: ComponentTick) -> None:
        with self.engine.transaction() as conn:
            row = self._lease(conn, component_id, body.generation, body.boot_id)
            if (row["tick_request_id"], row["tick_sequence"], row["tick_state"]) != (
                body.request_id,
                body.sequence,
                "reserved",
            ):
                raise denied("FENCED")

    def tick(self, component_id: UUID, body: ComponentTick) -> Row:
        with self.engine.transaction() as conn:
            row = self._lease(conn, component_id, body.generation, body.boot_id)
            if row["role"] not in {"scheduler", "worker", "dispatcher"}:
                raise denied("ROLE_DENIED", 403)
            if (row["tick_request_id"], row["tick_sequence"]) == (body.request_id, body.sequence):
                return {"state": row["tick_state"], **row["tick_result"], "reused": True}
            if row["tick_state"] in {"reserved", "unknown"}:
                raise denied("AWAITING_RECONCILIATION")
            if (
                body.sequence != row["tick_sequence"] + 1
                or body.request_id == row["tick_request_id"]
            ):
                raise denied("SEQUENCE_CONFLICT")
            role = row["role"]
            conn.execute(
                "UPDATE app.sa_operations_components SET tick_request_id=%s,tick_sequence=%s,"
                "tick_state='reserved',tick_result='{}' WHERE project_id=%s AND component_id=%s",
                (body.request_id, body.sequence, self.project, component_id),
            )
        state, result = "unknown", TickOutcome(unknown=1)
        # Distinguish a definitive pre-dispatch fence from an exception after a
        # port may already have performed part of its bounded work.
        with self.engine._mutex:
            try:
                self.check_tick(component_id, body)
            except DomainError:
                result, state = TickOutcome(blocked=1), "known"
            else:
                try:
                    port = self._ports.get(role)
                    if port is None:
                        result = TickOutcome(
                            waiting_runner=role == "worker", blocked=role != "worker"
                        )
                    else:
                        result = port.invoke(lambda: self.check_tick(component_id, body))
                        if not isinstance(result, TickOutcome):
                            raise ValueError("A bounded receipt is required")
                    state = "known"
                except Exception:
                    # A partial invocation is unconfirmed, including a late
                    # DomainError. Never infer that its effects did not happen.
                    pass
        with self.engine.transaction() as conn:
            changed = conn.execute(
                "UPDATE app.sa_operations_components SET tick_state=%s,tick_result=%s "
                "WHERE project_id=%s AND component_id=%s AND tick_request_id=%s "
                "AND tick_sequence=%s AND tick_state='reserved' RETURNING tick_state",
                (
                    state,
                    Jsonb(result.model_dump(mode="json")),
                    self.project,
                    component_id,
                    body.request_id,
                    body.sequence,
                ),
            ).fetchone()
            if changed is None:
                raise denied("AWAITING_RECONCILIATION")
        return {"state": state, **result.model_dump(mode="json"), "reused": False}

    def recover(self) -> int:
        if self._recovered:
            return 0
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "UPDATE app.sa_operations_components SET tick_state='unknown',"
                "tick_result=%s WHERE project_id=%s AND tick_state='reserved' RETURNING component_id",
                (Jsonb(TickOutcome(unknown=1).model_dump(mode="json")), self.project),
            ).fetchall()
        self._recovered = True
        return len(rows)

    def observe(self, component_id: UUID, body: SignalSubmission) -> Row:
        with self.engine.transaction() as conn:
            _, definition, _ = self._grant(conn, component_id, role="watchdog")
            lease = self._component(conn, component_id)
            if lease is None or lease["expires_at"] <= self.now(conn):
                raise denied("FENCED")
            snapshot = self._snapshot(conn)
            if not hmac.compare_digest(body.snapshot_sha256, self.basis(snapshot)):
                raise denied("STALE_OBSERVATION")
            fact = next(
                (
                    f
                    for f in signals(snapshot, definition)
                    if (
                        f.kind == body.kind
                        and f.subject_id == body.subject_id
                        and f.key(self.project) == body.observation_key
                    )
                ),
                None,
            )
            if fact is None:
                raise denied("STALE_OBSERVATION")
            conn.execute(
                "INSERT INTO app.sa_operations_incidents(incident_key,project_id,config_revision,"
                "kind,subject_id,basis_sha256,cursor_sha256,severity) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(incident_key) DO NOTHING",
                (
                    body.observation_key,
                    self.project,
                    snapshot.config_revision,
                    fact.kind,
                    fact.subject_id,
                    body.snapshot_sha256,
                    fact.cursor,
                    fact.severity,
                ),
            )
            return {"incident_key": body.observation_key, "saved": True, "business_action": False}

    def incidents(self, cookie: str) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            rows = conn.execute(
                "SELECT * FROM app.sa_operations_incidents WHERE project_id=%s "
                "ORDER BY first_seen_at DESC,incident_key LIMIT 101",
                (self.project,),
            ).fetchall()
            _, definition = self._current(conn)
            snapshot = self._snapshot(conn)
            current = (
                {f.key(self.project) for f in signals(snapshot, definition)}
                if definition
                else set()
            )
            return {
                "items": [
                    {**r, "current": r["incident_key"].strip() in current} for r in rows[:100]
                ],
                "partial": len(rows) > 100,
            }

    def decide(self, cookie: str, key: str, body: IncidentDecision) -> Row:
        with self.engine.transaction() as conn:
            owner = self._console(conn, cookie)
            row = conn.execute(
                "SELECT * FROM app.sa_operations_incidents WHERE project_id=%s AND incident_key=%s",
                (self.project, key),
            ).fetchone()
            if row is None:
                raise denied("NOT_FOUND", 404)
            if row["revision"] != body.expected_revision or row["state"] != "open":
                raise denied("REVISION_CONFLICT")
            # wait/escalate is an explicit audit decision, never a task control.
            conn.execute(
                "UPDATE app.sa_operations_incidents SET state=%s,revision=revision+1,"
                "decided_by=%s,decided_at=clock_timestamp(),review_ref=%s WHERE incident_key=%s",
                (
                    "waiting" if body.decision == "wait" else "escalated",
                    owner,
                    body.review_ref,
                    key,
                ),
            )
            return {
                "state": "waiting" if body.decision == "wait" else "escalated",
                "revision": body.expected_revision + 1,
                "business_action": False,
            }
