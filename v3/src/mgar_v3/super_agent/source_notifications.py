"""SA17 notification plans use original Action/Approval/Outbox, never task outcomes."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.domain import ActorRef
from mgar_v3.sources.ingress_domain import ItemMetadata
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef
from mgar_v3.super_agent.source_notification_domain import (
    ACTION_TYPE,
    NOTIFICATION_VERSION,
    NotificationConfigure,
    NotificationPlanRequest,
)
from mgar_v3.super_agent.source_notification_ports import NotificationProfile
from mgar_v3.super_agent.source_notification_rules import delivery_key, render
from mgar_v3.super_agent.source_notification_store import NotificationStore, Row, denied

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine


class SourceNotifications(NotificationStore):
    def __init__(self, engine: Engine):
        self.engine = engine
        self._profiles = {}

    def register_profile(self, profile: NotificationProfile) -> None:
        if not isinstance(profile, NotificationProfile):
            raise denied("TRUSTED_CHANNEL_REQUIRED", 422)
        profile.validate()
        old = self._profiles.get(profile.profile_sha256)
        if old is not None and old != profile:
            raise denied("PROFILE_IMMUTABLE", 409)
        self._profiles[profile.profile_sha256] = profile

    def capabilities(self, cookie: str) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return {
                "version": NOTIFICATION_VERSION,
                "default": "disabled",
                "profiles": sorted(self._profiles),
                "mode": "fixture_only",
                "channels": ["github", "feishu"],
                "free_text": False,
                "cross_channel_disclosure": False,
                "model_calls": 0,
                "unknown_retry": False,
                "real_platform": "NOT_RUN",
                "publication": "NOT_RUN",
            }

    def read(self, cookie: str, source_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            self.engine.source_correlations.identities.registry._instance(conn, source_id)
            head = conn.execute(
                "SELECT revision FROM app.sa_notification_controls "
                "WHERE source_instance_id=%s AND project_id=%s",
                (source_id, self.project),
            ).fetchone()
            if head is None:
                return {"revision": 0, "settings": None, "default": "disabled"}
            row, _ = self._config(conn, source_id)
            return {
                k: row[k]
                for k in (
                    "source_instance_id",
                    "revision",
                    "settings",
                    "settings_sha256",
                    "signed_at",
                )
            }

    def configure(self, cookie: str, source_id: UUID, request: NotificationConfigure) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            self.engine.source_correlations.identities.registry._instance(conn, source_id)
            old = conn.execute(
                "SELECT * FROM app.sa_notification_controls WHERE "
                "source_instance_id=%s AND project_id=%s FOR UPDATE",
                (source_id, self.project),
            ).fetchone()
            if (old["revision"] if old else 0) != request.expected_revision:
                raise denied("REVISION_CONFLICT", 409)
            settings = request.settings
            if settings.enabled:
                self._policy(conn, settings, source_id, user)
            revision = request.expected_revision + 1
            if old is None:
                conn.execute(
                    "INSERT INTO app.sa_notification_controls(source_instance_id,"
                    "project_id,revision) VALUES(%s,%s,%s)",
                    (source_id, self.project, revision),
                )
            conn.execute(
                "INSERT INTO app.sa_notification_versions(source_instance_id,project_id,"
                "revision,settings,settings_sha256,policy_id,policy_revision,profile_sha256,"
                "signed_by,review_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    source_id,
                    self.project,
                    revision,
                    Jsonb(settings.model_dump(mode="json")),
                    settings.settings_sha256,
                    settings.policy_id,
                    settings.policy_revision,
                    settings.profile_sha256,
                    user,
                    digest(request.review_ref),
                ),
            )
            if old is not None:
                conn.execute(
                    "UPDATE app.sa_notification_controls SET revision=%s WHERE "
                    "source_instance_id=%s",
                    (revision, source_id),
                )
            self._event(conn, source_id, "configured", "CONFIGURATION_SIGNED")
            return {
                "source_instance_id": source_id,
                "revision": revision,
                "settings": settings.model_dump(mode="json"),
                "settings_sha256": settings.settings_sha256,
            }

    def plan(self, cookie: str, request: NotificationPlanRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            ref = request.source_input
            if ref.project_id != self.project:
                raise denied("PROJECT_DENIED")
            config, settings = self._config(conn, ref.source_instance_id)
            _, definition, profile = self._policy(conn, settings, ref.source_instance_id, user)
            run = self.engine._run(conn, request.run_id)
            source = self._source(conn, run, ref, settings)
            if request.content_class not in settings.content_classes:
                raise denied("CONTENT_CLASS_DENIED")
            facts = self._facts(conn, run, request.content_class)
            template = render(facts)
            if len(template.body.encode()) > definition.notification.max_bytes:
                raise denied("CONTENT_LIMIT")
            destination = settings.destination.model_dump(mode="json")
            key = delivery_key(
                self.project,
                str(run["run_id"]),
                facts.cause_sha256,
                destination,
                request.content_class,
            )
            existing = conn.execute(
                "SELECT action_id FROM app.sa_notification_intents "
                "WHERE project_id=%s AND idempotency_key=%s",
                (self.project, key),
            ).fetchone()
            if existing is not None:
                self._event(
                    conn,
                    ref.source_instance_id,
                    "duplicate",
                    "ALREADY_PLANNED",
                    existing["action_id"],
                )
                return {
                    "action_id": existing["action_id"],
                    "created": False,
                    "reason_code": "NOTIFICATION_DUPLICATE",
                }
            now = self.now(conn)
            expires = min(
                source.expires, settings.valid_until, now + timedelta(seconds=settings.ttl_seconds)
            )
            if expires <= now + timedelta(seconds=1):
                raise denied("EXPIRED")
            aggregation = digest(
                [self.project, str(run["run_id"]), destination, request.content_class]
            )
            superseded = conn.execute(
                "UPDATE app.actions a SET status='cancelled',"
                "reason_code='NOTIFICATION_AGGREGATED' FROM app.sa_notification_intents n "
                "WHERE a.action_id=n.action_id AND n.project_id=%s AND n.aggregation_key=%s "
                "AND n.created_at>%s AND a.status='ready' RETURNING a.action_id",
                (self.project, aggregation, now - timedelta(seconds=settings.aggregation_seconds)),
            ).fetchall()
            for old in superseded:
                conn.execute(
                    "UPDATE app.outbox SET status='cancelled' WHERE action_id=%s "
                    "AND status='ready'",
                    (old["action_id"],),
                )
            payload = {
                "notification_version": NOTIFICATION_VERSION,
                "body": template.body,
                "template": template.template_version,
                "content_class": request.content_class,
                "body_sha256": template.body_sha256,
                "cause_sha256": facts.cause_sha256,
                "destination_sha256": digest(destination),
                "idempotency_key": key,
            }
            action_id = uuid4()
            actual_hash = digest(
                {
                    "type": ACTION_TYPE,
                    "target": settings.destination.target,
                    "payload": payload,
                    "base_revision": run["thread_revision"],
                }
            )
            conn.execute(
                "INSERT INTO app.actions(action_id,run_id,action_type,target,payload,"
                "payload_hash,base_revision,execution_epoch,fencing_token,dedupe_key,"
                "status,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,%s,'ready',%s)",
                (
                    action_id,
                    run["run_id"],
                    ACTION_TYPE,
                    settings.destination.target,
                    Jsonb(payload),
                    actual_hash,
                    run["thread_revision"],
                    run["execution_epoch"],
                    "source-notification:" + key,
                    expires,
                ),
            )
            values = {
                "action_id": action_id,
                "project_id": self.project,
                "source_instance_id": ref.source_instance_id,
                "control_revision": config["revision"],
                "run_id": run["run_id"],
                "ticket_id": run["ticket_id"],
                "input_id": ref.input_id,
                "source_ref": Jsonb(ref.model_dump(mode="json")),
                "policy_id": settings.policy_id,
                "policy_revision": settings.policy_revision,
                "profile_sha256": settings.profile_sha256,
                "destination": Jsonb(destination),
                "sender": Jsonb(profile.sender.model_dump(mode="json")),
                "content_class": request.content_class,
                "cause_sha256": facts.cause_sha256,
                "facts_sha256": template.facts_sha256,
                "body_sha256": template.body_sha256,
                "payload_sha256": actual_hash,
                "idempotency_key": key,
                "aggregation_key": aggregation,
                "expires_at": expires,
            }
            conn.execute(
                "INSERT INTO app.sa_notification_intents("
                + ",".join(values)
                + ") VALUES("
                + ",".join("%s" for _ in values)
                + ")",
                tuple(values.values()),
            )
            conn.execute(
                "INSERT INTO app.approvals(approval_id,action_id,actor,payload_hash) "
                "VALUES(%s,%s,%s,%s)",
                (uuid4(), action_id, "notification-policy:" + str(user), actual_hash),
            )
            conn.execute(
                "INSERT INTO app.outbox(outbox_id,action_id,status,next_retry_at) "
                "VALUES(%s,%s,'ready',%s)",
                (uuid4(), action_id, now + timedelta(seconds=settings.aggregation_seconds)),
            )
            self._event(
                conn,
                ref.source_instance_id,
                "aggregated" if superseded else "planned",
                "EXACT_TEMPLATE_APPROVED",
                action_id,
            )
            return {
                "action_id": action_id,
                "created": True,
                "payload_sha256": actual_hash,
                "body_sha256": template.body_sha256,
                "expires_at": expires,
                "superseded": len(superseded),
                "sent": False,
            }

    def validate_action(self, conn: Connection, action: Row, run: Row) -> None:
        intent = self._intent(conn, action["action_id"])
        config, settings = self._config(conn, intent["source_instance_id"])
        _, definition, _ = self._policy(
            conn, settings, intent["source_instance_id"], config["signed_by"]
        )
        self._source(
            conn,
            run,
            CorrelationInputRef.model_validate_json(json.dumps(intent["source_ref"])),
            settings,
        )
        facts = self._facts(conn, run, intent["content_class"])
        template = render(facts)
        payload = action["payload"]
        approval = conn.execute(
            "SELECT actor,payload_hash FROM app.approvals WHERE action_id=%s",
            (action["action_id"],),
        ).fetchone()
        if (
            config["revision"] != intent["control_revision"]
            or not action["unexpired"]
            or intent["expires_at"] <= self.now(conn)
            or action["target"] != settings.destination.target
            or intent["destination"] != settings.destination.model_dump(mode="json")
            or intent["profile_sha256"] != settings.profile_sha256
            or intent["content_class"] not in settings.content_classes
            or action["execution_epoch"] != run["execution_epoch"]
            or action["base_revision"] != run["thread_revision"]
            or facts.cause_sha256 != intent["cause_sha256"]
            or template.facts_sha256 != intent["facts_sha256"]
            or template.body != payload.get("body")
            or template.body_sha256 != intent["body_sha256"]
            or len(template.body.encode()) > definition.notification.max_bytes
            or payload
            != {
                "notification_version": NOTIFICATION_VERSION,
                "body": template.body,
                "template": template.template_version,
                "content_class": intent["content_class"],
                "body_sha256": template.body_sha256,
                "cause_sha256": facts.cause_sha256,
                "destination_sha256": digest(intent["destination"]),
                "idempotency_key": intent["idempotency_key"],
            }
            or intent["payload_sha256"] != action["payload_hash"]
            or action["payload_hash"]
            != digest(
                {
                    "type": ACTION_TYPE,
                    "target": action["target"],
                    "payload": payload,
                    "base_revision": action["base_revision"],
                }
            )
            or approval is None
            or approval["payload_hash"] != action["payload_hash"]
            or approval["actor"] != "notification-policy:" + str(config["signed_by"])
        ):
            raise denied("STALE_OR_UNAPPROVED")
        if self._wait_until(conn, intent, settings, definition.notification) is not None:
            raise denied("DEFERRED", 429)

    def defer(self, conn: Connection, action: Row, exc: DomainError) -> bool:
        if not self.handles(action) or exc.code != "NOTIFICATION_DEFERRED":
            return False
        intent = self._intent(conn, action["action_id"])
        config, settings = self._config(conn, intent["source_instance_id"])
        _, definition, _ = self._policy(
            conn, settings, intent["source_instance_id"], config["signed_by"]
        )
        until = self._wait_until(conn, intent, settings, definition.notification)
        if until is None:
            until = self.now(conn) + timedelta(seconds=1)
        conn.execute(
            "UPDATE app.outbox SET next_retry_at=%s WHERE action_id=%s AND status='ready'",
            (until, action["action_id"]),
        )
        self._event(conn, intent["source_instance_id"], "deferred", "DEFERRED", action["action_id"])
        return True

    def record_effect(
        self, conn: Connection, action: Row, status: str, external_id: str | None
    ) -> bool:
        if not self.handles(action):
            return False
        intent = self._intent(conn, action["action_id"])
        phase = "query" if action["status"] == "outcome_unknown" else "dispatch"
        outcome = {"succeeded": "accepted", "failed": "rejected", "outcome_unknown": "unknown"}[
            status
        ]
        if not conn.execute(
            "SELECT 1 FROM app.sa_notification_receipts WHERE action_id=%s "
            "AND phase=%s AND outcome=%s AND remote_message_id IS NOT DISTINCT FROM %s",
            (action["action_id"], phase, outcome, external_id),
        ).fetchone():
            self._receipt(conn, intent, phase, outcome, external_id)
        if (
            status == "failed"
            and conn.execute(
                "SELECT 1 FROM app.sa_notification_events WHERE action_id=%s AND kind='blocked'",
                (action["action_id"],),
            ).fetchone()
        ):
            conn.execute(
                "UPDATE app.actions SET status='cancelled',"
                "reason_code='NOTIFICATION_FINAL_CHECK_BLOCKED' "
                "WHERE action_id=%s",
                (action["action_id"],),
            )
            conn.execute(
                "UPDATE app.outbox SET status='cancelled' WHERE action_id=%s",
                (action["action_id"],),
            )
        self._event(
            conn, intent["source_instance_id"], "settled", "DELIVERY_RECORDED", action["action_id"]
        )
        return True  # A Delivery receipt NEVER completes, revives or publishes the business task.

    def _receipt(
        self, conn: Connection, intent: Row, phase: str, outcome: str, remote: str | None = None
    ) -> None:
        self._conn(conn)
        conn.execute(
            "INSERT INTO app.sa_notification_receipts(receipt_id,action_id,project_id,"
            "phase,outcome,remote_message_id,profile_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                uuid4(),
                intent["action_id"],
                self.project,
                phase,
                outcome,
                remote,
                intent["profile_sha256"],
            ),
        )

    def own_result(self, conn: Connection, item: ItemMetadata) -> bool:
        if not self._ready(conn) or item.event.sender.actor_kind not in {"bot", "service"}:
            return False
        rows = conn.execute(
            "SELECT n.sender,n.destination FROM app.sa_notification_intents n "
            "JOIN app.actions a USING(action_id) WHERE n.project_id=%s AND n.source_instance_id=%s "
            "AND a.status='succeeded' AND a.external_id=%s LIMIT 64",
            (self.project, item.event.scope.source_instance_id, item.event.entity_id),
        ).fetchall()
        return any(
            ActorRef.model_validate(r["sender"]) == item.event.sender
            and r["destination"]["thread"] == item.event.source_thread.model_dump(mode="json")
            for r in rows
        )

    def _maintain(self, conn: Connection, limit: int) -> Row:
        self._conn(conn)
        cancelled, deferred = 0, 0
        rows = conn.execute(
            "SELECT n.action_id FROM app.sa_notification_intents n JOIN app.actions a "
            "USING(action_id) WHERE n.project_id=%s AND a.status='ready' "
            "ORDER BY n.created_at,n.action_id LIMIT %s",
            (self.project, limit),
        ).fetchall()
        for row in rows:
            action, run = self.engine._action(conn, row["action_id"])
            try:
                self.validate_action(conn, action, run)
            except DomainError as exc:
                if self.defer(conn, action, exc):
                    deferred += 1
                    continue
                conn.execute(
                    "UPDATE app.actions SET status='cancelled',reason_code=%s WHERE action_id=%s",
                    ("NOTIFICATION_AUTHORITY_EXPIRED", row["action_id"]),
                )
                conn.execute(
                    "UPDATE app.outbox SET status='cancelled' WHERE action_id=%s",
                    (row["action_id"],),
                )
                intent = self._intent(conn, row["action_id"])
                self._event(
                    conn,
                    intent["source_instance_id"],
                    "cancelled",
                    "AUTHORITY_EXPIRED",
                    row["action_id"],
                )
                cancelled += 1
        return {
            "checked": len(rows),
            "cancelled": cancelled,
            "deferred": deferred,
            "model_calls": 0,
        }

    def recover_conn(self, conn: Connection) -> None:
        if not self._ready(conn):
            return
        rows = conn.execute(
            "SELECT n.* FROM app.sa_notification_intents n JOIN app.actions a "
            "USING(action_id) WHERE n.project_id=%s AND a.status='outcome_unknown' "
            "AND NOT EXISTS(SELECT 1 FROM app.sa_notification_receipts d WHERE "
            "d.action_id=n.action_id "
            "AND d.phase IN ('dispatch','recovery')) ORDER BY n.created_at LIMIT 64",
            (self.project,),
        ).fetchall()
        for row in rows:
            self._receipt(conn, row, "recovery", "unknown")
            self._event(
                conn, row["source_instance_id"], "recovered", "UNKNOWN_NO_RESEND", row["action_id"]
            )
        self._maintain(conn, 64)

    def maintain(self, cookie: str, limit: int = 16) -> Row:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return self._maintain(conn, limit)

    def plans(self, cookie: str, limit: int = 64) -> Row:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            rows = conn.execute(
                "SELECT n.action_id,n.run_id,n.content_class,n.body_sha256,"
                "n.payload_sha256,n.expires_at,a.status,a.external_id FROM "
                "app.sa_notification_intents n "
                "JOIN app.actions a USING(action_id) WHERE n.project_id=%s "
                "ORDER BY n.created_at DESC,n.action_id LIMIT %s",
                (self.project, limit + 1),
            ).fetchall()
            return {"plans": rows[:limit], "partial": len(rows) > limit}

    def get(self, cookie: str, action_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            intent = self._intent(conn, action_id)
            action, _ = self.engine._action(conn, action_id)
            receipts = conn.execute(
                "SELECT phase,outcome,remote_message_id,observed_at "
                "FROM app.sa_notification_receipts WHERE action_id=%s ORDER BY "
                "observed_at LIMIT 64",
                (action_id,),
            ).fetchall()
            return {
                "action_id": action_id,
                "run_id": intent["run_id"],
                "status": action["status"],
                "payload_sha256": intent["payload_sha256"],
                "body_sha256": intent["body_sha256"],
                "reason_code": action["reason_code"],
                "receipts": receipts,
                "task_outcome_from_delivery": False,
            }

    def cancel(self, cookie: str, action_id: UUID, payload_sha256: str) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            intent = self._intent(conn, action_id)
            action, _ = self.engine._action(conn, action_id)
            if action["payload_hash"] != payload_sha256:
                raise denied("PAYLOAD_CHANGED", 409)
            if action["status"] not in {"ready", "proposed", "approved"}:
                raise denied("INFLIGHT_OR_SETTLED", 409)
            conn.execute(
                "UPDATE app.actions SET status='cancelled',"
                "reason_code='NOTIFICATION_OWNER_CANCELLED' "
                "WHERE action_id=%s",
                (action_id,),
            )
            conn.execute(
                "UPDATE app.outbox SET status='cancelled' WHERE action_id=%s", (action_id,)
            )
            self._event(
                conn, intent["source_instance_id"], "cancelled", "OWNER_CANCELLED", action_id
            )
            return {"action_id": action_id, "status": "cancelled"}

    def events(self, cookie: str, limit: int = 64) -> Row:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            rows = conn.execute(
                "SELECT event_id,action_id,source_instance_id,kind,reason_code,created_at "
                "FROM app.sa_notification_events WHERE project_id=%s ORDER BY event_id "
                "DESC LIMIT %s",
                (self.project, limit + 1),
            ).fetchall()
            return {"events": rows[:limit], "partial": len(rows) > limit}

    def tick(self, cookie: str, limit: int = 16) -> Row:
        from mgar_v3.effects.dispatcher import Dispatcher
        from mgar_v3.super_agent.source_notification_transport import NotificationTransport

        self._limit(limit)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
        dispatcher = Dispatcher(self.engine, NotificationTransport(self))
        sent = []
        for _ in range(limit):
            action_id = dispatcher.tick()
            if action_id is None:
                break
            sent.append(action_id)
        return {"dispatched": sent, "model_calls": 0, "mode": "fixture"}

    def reconcile(self, cookie: str, action_id: UUID) -> Row:
        from mgar_v3.effects.dispatcher import Dispatcher
        from mgar_v3.super_agent.source_notification_transport import NotificationTransport

        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            self._intent(conn, action_id)
            action, _ = self.engine._action(conn, action_id)
            if action["status"] != "outcome_unknown":
                raise denied("NOT_UNKNOWN", 409)
        resolved = Dispatcher(self.engine, NotificationTransport(self)).reconcile(action_id)
        return {"action_id": action_id, "resolved": resolved, "resent": False}
