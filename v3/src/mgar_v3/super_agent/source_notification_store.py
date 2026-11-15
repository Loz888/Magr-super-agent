"""SQL-only SA17 authority projections on the original held Engine connection."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.pq import TransactionStatus

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.domain import SourceInstance
from mgar_v3.super_agent.policy_domain import ContentClass, NotificationGrant, PolicyDefinition
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef
from mgar_v3.super_agent.source_notification_domain import (
    ACTION_TYPE,
    NotificationFacts,
    NotificationSettings,
)
from mgar_v3.super_agent.source_notification_ports import NotificationProfile
from mgar_v3.super_agent.source_notification_rules import quiet_until

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.super_agent.source_correlation import _Input

Row = dict[str, Any]


def denied(code: str, status: int = 403) -> DomainError:
    return DomainError(
        "NOTIFICATION_" + code, "Current exact notification authority required", status
    )


class NotificationStore:
    engine: Engine
    _profiles: dict[str, NotificationProfile]

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    def _conn(self, conn: Connection) -> None:
        if (
            conn is not self.engine.connection
            or conn.info.transaction_status != TransactionStatus.INTRANS
        ):
            raise denied("ORIGINAL_TRANSACTION_REQUIRED", 409)

    def _ready(self, conn: Connection) -> bool:
        self._conn(conn)
        row = conn.execute("SELECT to_regclass('app.sa_notification_controls') AS t").fetchone()
        return bool(row and row["t"])

    def _console(self, conn: Connection, cookie: str) -> UUID:
        self._conn(conn)
        user = self.engine.source_correlations.identities._console(conn, cookie)
        if not self._ready(conn):
            raise denied("NOT_MIGRATED", 409)
        return user

    def now(self, conn: Connection) -> datetime:
        self._conn(conn)
        return self.engine.source_ingress.now(conn)

    @staticmethod
    def handles(action: Row) -> bool:
        return bool(action["action_type"] == ACTION_TYPE)

    @staticmethod
    def _limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 64:
            raise denied("LIMIT_INVALID", 422)

    def _config(self, conn: Connection, source_id: UUID) -> tuple[Row, NotificationSettings]:
        self._conn(conn)
        row = conn.execute(
            "SELECT v.* FROM app.sa_notification_controls c JOIN app.sa_notification_versions v "
            "USING(source_instance_id,project_id,revision) WHERE c.source_instance_id=%s "
            "AND c.project_id=%s FOR UPDATE OF c",
            (source_id, self.project),
        ).fetchone()
        if row is None:
            raise denied("NOT_CONFIGURED", 409)
        settings = NotificationSettings.model_validate_json(json.dumps(row["settings"]))
        if settings.settings_sha256 != row["settings_sha256"]:
            raise denied("CONFIGURATION_INVALID")
        return row, settings

    def _intent(self, conn: Connection, action_id: UUID) -> Row:
        self._conn(conn)
        row = conn.execute(
            "SELECT * FROM app.sa_notification_intents WHERE action_id=%s AND project_id=%s",
            (action_id, self.project),
        ).fetchone()
        if row is None:
            raise denied("PLAN_NOT_FOUND", 404)
        return row

    def _event(
        self,
        conn: Connection,
        source_id: UUID,
        kind: str,
        reason: str,
        action_id: UUID | None = None,
    ) -> None:
        self._conn(conn)
        conn.execute(
            "INSERT INTO app.sa_notification_events(project_id,source_instance_id,"
            "action_id,kind,reason_code) VALUES(%s,%s,%s,%s,%s)",
            (self.project, source_id, action_id, kind, "NOTIFICATION_" + reason),
        )

    def _policy(
        self, conn: Connection, settings: NotificationSettings, source_id: UUID, signer: UUID
    ) -> tuple[SourceInstance, PolicyDefinition, NotificationProfile]:
        instance = self.engine.source_correlations.identities.registry._instance(conn, source_id)
        row = self.engine.source_policies._current(conn, settings.policy_id)
        definition = PolicyDefinition.model_validate(row["definition"])
        now = self.now(conn)
        profile = self._profiles.get(settings.profile_sha256)
        grant = definition.notification
        if (
            not settings.enabled
            or not settings.valid_from <= now < settings.valid_until
            or row["source_instance_id"] != source_id
            or row["revoked"]
            or row["current_revision"] != settings.policy_revision
            or row["definition_sha256"] != settings.policy_sha256
            or row["signed_by"] != signer
            or not definition.valid_from
            <= settings.valid_from
            < settings.valid_until
            <= definition.valid_until
            or definition.mode not in {"auto_investigate", "auto_repair"}
            or not definition.receive.enabled
            or not grant.enabled
            or settings.destination not in grant.destinations
            or settings.destination.thread.scope != instance.scope
            or not set(settings.content_classes).issubset(grant.content_classes)
            or "public_summary" not in grant.data_classes
            or profile is None
            or profile.scope != instance.scope
            or profile.instance_revision != instance.revision
            or profile.capabilities != instance.channel_capabilities
        ):
            raise denied("POLICY_OR_CHANNEL_CHANGED")
        profile.validate()
        return instance, definition, profile

    def _source(
        self, conn: Connection, run: Row, ref: CorrelationInputRef, settings: NotificationSettings
    ) -> _Input:
        source = self.engine.source_correlations._input(conn, ref.input_id)
        if (
            source.ref != ref
            or source.row["policy_id"] != settings.policy_id
            or source.row["policy_revision"] != settings.policy_revision
            or source.item.event.source_thread != settings.destination.thread
        ):
            raise denied("SOURCE_OR_DESTINATION_CHANGED")
        # Original immutable provenance only. A shared title/topic/hash is not a task link.
        origin = conn.execute(
            "SELECT input_id,source_state_sha256 FROM app.sa_autonomy_attempts "
            "WHERE run_id=%s AND project_id=%s UNION ALL "
            "SELECT input_id,source_state_sha256 FROM app.sa_coding_bindings "
            "WHERE run_id=%s AND project_id=%s",
            (run["run_id"], self.project, run["run_id"], self.project),
        ).fetchall()
        bound = any(
            r["input_id"] == ref.input_id and r["source_state_sha256"] == ref.state_sha256
            for r in origin
        )
        if not bound:
            bound = (
                conn.execute(
                    "SELECT 1 FROM app.sa_thread_link_inputs l JOIN app.sa_task_correlations c "
                    "USING(correlation_id,revision,project_id) JOIN "
                    "app.sa_task_correlation_versions v "
                    "USING(correlation_id,revision,project_id) WHERE v.run_id=%s AND l.input_id=%s "
                    "AND l.project_id=%s AND v.payload->>'state'='bound' "
                    "AND l.version_fingerprint=%s LIMIT 1",
                    (run["run_id"], ref.input_id, self.project, ref.version_fingerprint),
                ).fetchone()
                is not None
            )
        if not bound:
            raise denied("ORIGINAL_TASK_LINK_REQUIRED")
        if run["execution_mode"] != "fixture" or run["status"] == "cancelled":
            raise denied("CONTROLLED_ORIGINAL_RUN_REQUIRED")
        return source

    def _facts(self, conn: Connection, run: Row, kind: ContentClass) -> NotificationFacts:
        verdict = conn.execute(
            "SELECT verdict_id,report_hash,passed FROM app.verdicts "
            "WHERE run_id=%s AND report_hash=%s AND passed ORDER BY created_at DESC LIMIT 1",
            (run["run_id"], run["report_hash"]),
        ).fetchone()
        verification = conn.execute(
            "SELECT v.verification_id,v.tree_sha256,v.result,a.state AS attempt_state "
            "FROM app.sa_coding_verifications v "
            "JOIN app.sa_coding_attempts a USING(attempt_id,coding_id,project_id) "
            "WHERE a.run_id=%s AND a.execution_epoch=%s AND v.project_id=%s "
            "AND a.attempt_id=(SELECT latest.attempt_id FROM app.sa_coding_attempts latest "
            "WHERE latest.run_id=a.run_id AND latest.execution_epoch=a.execution_epoch "
            "AND latest.project_id=a.project_id ORDER BY latest.created_at DESC,latest.attempt_id "
            "DESC LIMIT 1) ORDER BY v.observed_at DESC,v.verification_id DESC LIMIT 1",
            (run["run_id"], run["execution_epoch"], self.project),
        ).fetchone()
        final_coding = bool(
            verification
            and verification["result"] == "passed"
            and verification["attempt_state"] == "result_recorded"
        )
        state = run["status"]
        critical = state == "failed" or (
            state == "paused"
            and ("BUDGET" in (run["reason_code"] or "") or run["reason_code"] == "OUTCOME_UNKNOWN")
        )
        cause = digest(
            [
                str(run["run_id"]),
                run["execution_epoch"],
                run["input_revision"],
                state,
                run["reason_code"],
                {k: str(v) for k, v in verdict.items()} if verdict else None,
                {k: str(v) for k, v in verification.items()} if verification else None,
            ]
        )
        return NotificationFacts(
            run_id=run["run_id"],
            ticket_id=run["ticket_id"],
            execution_epoch=run["execution_epoch"],
            input_revision=run["input_revision"],
            thread_revision=run["thread_revision"],
            state=state,
            content_class=kind,
            cause_sha256=cause,
            verified=(verdict is not None and state in {"waiting_approval", "succeeded"})
            or (final_coding and state in {"running", "paused", "waiting_approval", "succeeded"}),
            reviewable=final_coding and state in {"running", "paused", "waiting_approval"},
            needs_input=state == "waiting_input",
            critical=critical,
        )

    def _wait_until(
        self,
        conn: Connection,
        intent: Row,
        settings: NotificationSettings,
        grant: NotificationGrant,
    ) -> datetime | None:
        now = self.now(conn)
        quiet = quiet_until(grant, now)
        other = conn.execute(
            "SELECT max(e.created_at) AS last,count(*) FILTER(WHERE e.created_at>%s) AS hour "
            "FROM app.sa_notification_events e JOIN app.sa_notification_intents n USING(action_id) "
            "WHERE e.project_id=%s AND n.source_instance_id=%s AND e.kind='send_authorized' "
            "AND e.action_id<>%s AND n.destination=%s::jsonb",
            (
                now - timedelta(hours=1),
                self.project,
                intent["source_instance_id"],
                intent["action_id"],
                json.dumps(intent["destination"]),
            ),
        ).fetchone()
        assert other is not None
        until = intent["created_at"] + timedelta(seconds=settings.aggregation_seconds)
        if quiet is not None:
            until = max(until, quiet)
        if other["last"] is not None:
            until = max(until, other["last"] + timedelta(seconds=settings.cooldown_seconds))
            if other["hour"] >= grant.max_per_hour:
                until = max(until, other["last"] + timedelta(hours=1))
        return until if until > now else None
