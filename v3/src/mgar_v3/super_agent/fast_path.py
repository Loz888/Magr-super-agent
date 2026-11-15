"""Engine-owned deterministic observation. This module has no execution/model/send port."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.github_domain import GitHubResource
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.ingress_domain import ItemMetadata
from mgar_v3.super_agent.fast_path_domain import (
    FastPathConfigure,
    FastPathSettings,
    ObserveRequest,
)
from mgar_v3.super_agent.fast_path_rules import extract_text, group_key, parse_intent
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicyFacts

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine

Row = dict[str, Any]


class FastPath:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.identities = SourceIdentities(engine)

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    @staticmethod
    def now(conn: Connection) -> datetime:
        row = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        assert row is not None and isinstance(row["now"], datetime)
        return row["now"]

    @staticmethod
    def _ready(conn: Connection) -> bool:
        row = conn.execute("SELECT to_regclass('app.sa_fast_path_controls') IS NOT NULL AS ready")
        value = row.fetchone()
        assert value is not None
        return bool(value["ready"])

    def _source(self, conn: Connection, source_id: UUID) -> Any:
        if not self._ready(conn):
            raise DomainError("FAST_PATH_NOT_MIGRATED", "Fast path storage is not installed", 409)
        return self.identities.registry._instance(conn, source_id)

    def _control(self, conn: Connection, source_id: UUID) -> tuple[Row | None, FastPathSettings]:
        row = conn.execute(
            "SELECT * FROM app.sa_fast_path_controls WHERE source_instance_id=%s AND project_id=%s",
            (source_id, self.project),
        ).fetchone()
        return row, FastPathSettings.model_validate(row["settings"]) if row else FastPathSettings()

    def configure(self, cookie: str, source_id: UUID, request: FastPathConfigure) -> Row:
        with self.engine.transaction() as conn:
            user = self.identities._console(conn, cookie)
            instance = self._source(conn, source_id)
            if any(actor.scope != instance.scope for actor in request.settings.own_actors):
                raise DomainError("FAST_PATH_ACTOR_SCOPE", "Own actors must match this source", 422)
            old, _ = self._control(conn, source_id)
            revision = old["revision"] if old else 0
            if revision != request.expected_revision:
                raise DomainError("FAST_PATH_REVISION", "Fast path configuration changed", 409)
            if request.settings.enabled:
                _, ingress = self.engine.source_ingress._control(conn, source_id)
                if ingress.owner != "gateway":
                    raise DomainError("FAST_PATH_OWNER", "Gateway ownership is required", 409)
            value = request.settings.model_dump(mode="json")
            fingerprint = digest(value)
            conn.execute(
                "INSERT INTO app.sa_fast_path_controls(source_instance_id,project_id,revision,"
                "settings,settings_sha256,updated_by) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(source_instance_id) DO UPDATE SET revision=EXCLUDED.revision,"
                "settings=EXCLUDED.settings,settings_sha256=EXCLUDED.settings_sha256,"
                "updated_by=EXCLUDED.updated_by,updated_at=clock_timestamp()",
                (source_id, self.project, revision + 1, Jsonb(value), fingerprint, user),
            )
            conn.execute(
                "INSERT INTO app.sa_fast_path_versions(source_instance_id,project_id,revision,"
                "settings,settings_sha256,signed_by,review_ref) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (source_id, self.project, revision + 1, Jsonb(value), fingerprint, user,
                 request.review_ref),
            )
            return {"source_instance_id": source_id, "revision": revision + 1,
                    "settings": value, "settings_sha256": fingerprint,
                    "semantic_evaluation": "NOT_RUN"}

    def read(self, cookie: str, source_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            self._source(conn, source_id)
            row, settings = self._control(conn, source_id)
            return {"revision": row["revision"] if row else 0,
                    "settings": settings.model_dump(mode="json"),
                    "semantic_evaluation": "NOT_RUN"}

    def ingested(
        self, conn: Connection, input_id: UUID, *, historical: bool = False,
        buffered: bool = False, gap: bool = False, old: bool = False,
    ) -> None:
        """Trusted ingress composition only; origin flags never come from an HTTP DTO.

        Capture at most one bodyless origin per existing canonical input, in its
        original outer transaction. Observation capacity does not roll back ingress.
        """
        if (
            conn is not self.engine.connection
            or conn.info.transaction_status != TransactionStatus.INTRANS
        ):
            raise DomainError(
                "FAST_PATH_TRANSACTION", "Use the original Engine transaction", 409
            )
        if not self._ready(conn):
            return
        row = conn.execute(
            "SELECT * FROM app.source_ingress_inputs WHERE input_id=%s AND project_id=%s",
            (input_id, self.project),
        ).fetchone()
        assert row is not None
        inserted = conn.execute(
            "INSERT INTO app.sa_fast_path_origins(input_id,source_instance_id,project_id,"
            "historical,buffered,gap,old) VALUES(%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(input_id) DO NOTHING RETURNING input_id",
            (input_id, row["source_instance_id"], self.project, historical, buffered, gap, old),
        ).fetchone()
        if inserted is None:
            return  # An alias cannot automatically activate an older pending input.
        control, settings = self._control(conn, row["source_instance_id"])
        if control and settings.enabled:
            self._observe(conn, row, control, settings)

    def _input(self, conn: Connection, source_id: UUID, input_id: UUID) -> Row:
        row = conn.execute(
            "SELECT * FROM app.source_ingress_inputs WHERE input_id=%s AND source_instance_id=%s "
            "AND project_id=%s", (input_id, source_id, self.project),
        ).fetchone()
        if row is None:
            raise DomainError("FAST_PATH_INPUT", "Canonical input is outside this source", 404)
        return row

    def _own_result(self, conn: Connection, item: ItemMetadata, clear: bytes) -> bool:
        if self.engine.source_notifications.own_result(conn, item):
            return True
        # Only a current, verified GitHub resource and an actual known receipt count.
        # Body markers, fixture:// links, hints alone and unknown writes never count.
        if item.event.scope.source_kind != "github":
            return False
        try:
            content = json.loads(clear)
            if (
                not isinstance(content, dict)
                or content.get("schema_version") != "github-content-v1"
            ):
                return False
            resource = GitHubResource.model_validate(content.get("resource"))
        except (ValueError, TypeError, ValidationError):
            return False
        repository = resource.repository.full_name.lower()
        numbers = {str(number) for number in resource.pull_requests}
        if resource.kind == "pull_request" and resource.number:
            numbers.add(str(resource.number))
        if not numbers:
            return False
        external_ids = sorted(numbers | {
            "https://github.com/" + repository + "/pull/" + number for number in numbers
        })
        known = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM app.actions a JOIN app.runs r USING(run_id) "
            "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
            "WHERE h.project_id=%s AND a.status='succeeded' AND a.action_type='draft_pr' "
            "AND r.execution_mode='live' AND lower(a.payload->>'repository')=%s "
            "AND lower(a.external_id)=ANY(%s)) OR EXISTS("
            "SELECT 1 FROM app.workbench_publications p JOIN app.workbench_tasks w "
            "USING(task_id) JOIN app.threads h ON h.thread_id=w.thread_id "
            "WHERE h.project_id=%s AND p.status='succeeded' AND p.execution_mode='live' "
            "AND lower(p.repository)=%s AND lower(p.external_id)=ANY(%s)) AS known",
            (self.project, repository, external_ids, self.project, repository, external_ids),
        ).fetchone()
        assert known is not None
        return bool(known["known"])

    def _assess(self, conn: Connection, row: Row, settings: FastPathSettings) -> Row:
        result: Row = {"disposition": "ignored", "reason_code": "FAST_PATH_DISABLED",
                       "priority": 0, "intended_action": "would_ignore", "mode": "disabled",
                       "topic_key": None, "purpose_key": None}

        def context(code: str) -> Row:
            return {**result, "disposition": "context", "reason_code": code,
                    "intended_action": "would_observe"}

        if not settings.enabled:
            return result
        origin = conn.execute(
            "SELECT * FROM app.sa_fast_path_origins WHERE input_id=%s", (row["input_id"],),
        ).fetchone()
        if origin is None:
            return {**result, "reason_code": "FAST_PATH_ORIGIN_UNKNOWN"}
        try:
            item, grant = self.engine.source_ingress._readable(conn, row)
        except DomainError as error:
            return {**result, "reason_code": error.code}
        current = self.engine.source_policies._current(conn, grant["policy_id"])
        definition = PolicyDefinition.model_validate(current["definition"])
        result.update(mode=definition.mode, topic_key=group_key(item), item=item, grant=grant)
        event = item.event
        for flag, reason in [("historical", "FAST_PATH_HISTORY"), ("buffered", "FAST_PATH_BUFFER"),
                             ("gap", "FAST_PATH_GAP"), ("old", "FAST_PATH_AGE")]:
            if origin[flag] or (flag == "historical" and event.historical_backfill):
                return context(reason)
        _, ingress = self.engine.source_ingress._control(conn, row["source_instance_id"])
        at = self.now(conn)
        if not at - timedelta(seconds=ingress.live_age_seconds) <= event.occurred_at <= (
            at + timedelta(seconds=30)
        ):
            return context("FAST_PATH_AGE")
        if conn.execute(
            "SELECT 1 FROM app.source_ingress_gaps WHERE source_instance_id=%s "
            "AND resolved_at IS NULL LIMIT 1", (row["source_instance_id"],),
        ).fetchone():
            return context("FAST_PATH_GAP")
        if definition.mode == "disabled":
            return {**result, "reason_code": "FAST_PATH_POLICY_DISABLED"}
        if event.sender in settings.own_actors:
            return {**result, "reason_code": "FAST_PATH_OWN_ACTOR"}
        if not grant["retain_body"] or self.engine.source_ingress.cipher is None:
            return context("FAST_PATH_BODY_UNAVAILABLE")
        body = conn.execute(
            "SELECT ciphertext FROM app.source_ingress_bodies WHERE input_id=%s "
            "AND expires_at>clock_timestamp()", (row["input_id"],),
        ).fetchone()
        if body is None:
            return context("FAST_PATH_BODY_UNAVAILABLE")
        try:
            clear = self.engine.source_ingress.cipher.decrypt(
                bytes(body["ciphertext"]), str(row["input_id"])
            )
            if hashlib.sha256(clear).hexdigest() != row["body_sha256"]:
                return context("INGRESS_BODY_HASH")
            text = extract_text(clear)
            intent = parse_intent(item, text)
        except DomainError as error:
            return context(error.code)
        roles, controlled = self.engine.source_policies._roles(
            conn, event.sender, event.source_thread, intent.ticket_id
        )
        if intent.control_action:
            if (
                event.sender.actor_kind != "human" or not controlled
                or "task_controller" not in roles
            ):
                return {**result, "reason_code": "FAST_PATH_CONTROL_DENIED"}
            facts = PolicyFacts(event_kind=event.event_kind, request_kind="follow_up",
                                ticket_id=intent.ticket_id)
            decision = self.engine.source_policies._evaluate(
                definition, "admission", event.sender, event.source_thread, facts,
                roles, self.now(conn), controlled,
            )
            if not (decision.allowed or decision.would_allow):
                return {**result, "reason_code": decision.reason_code}
            result.update(disposition="control", reason_code="FAST_PATH_CONTROL_SHADOW",
                          intended_action="would_" + intent.control_action, priority=100,
                          purpose_key=digest([result["topic_key"], intent.control_action,
                                              str(intent.ticket_id)]))
            return result
        source_subscriptions = getattr(self.engine, "source_subscriptions", None)
        if (source_subscriptions is not None and source_subscriptions._ready(conn)
                and source_subscriptions.handled(conn, row)):
            return context("FAST_PATH_SUBSCRIPTION_ROUTED")
        if self._own_result(conn, item, clear):
            return {**result, "reason_code": "FAST_PATH_OWN_RESULT"}
        if not grant["model_input"]:
            return context("FAST_PATH_MODEL_INPUT_DENIED")
        kind = intent.request_kind
        if kind is None and "team_rule" in definition.admission.request_kinds:
            kind = "team_rule"  # Signed administrator rule; never inferred from prose.
        if kind is None:
            return {**result, "reason_code": "FAST_PATH_UNADDRESSED"}
        decision = self.engine.source_policies._evaluate(
            definition, "admission", event.sender, event.source_thread,
            PolicyFacts(event_kind=event.event_kind, request_kind=kind), roles, self.now(conn),
        )
        if not (decision.allowed or decision.would_allow):
            return {**result, "reason_code": decision.reason_code}
        action = ("would_observe" if event.sender.actor_kind != "human" else
                  "would_ask" if intent.command == "ask" else "would_evaluate")
        result.update(disposition="candidate", reason_code="FAST_PATH_SHADOW_CANDIDATE",
                      intended_action=action, purpose_key=digest([result["topic_key"], action]))
        return result

    def _batch(self, conn: Connection, row: Row, control: Row,
               settings: FastPathSettings, assessment: Row) -> tuple[UUID | None, str | None]:
        instance = self.identities.registry._instance(conn, row["source_instance_id"])
        ingress, _ = self.engine.source_ingress._control(conn, row["source_instance_id"])
        now = self.now(conn)
        latest = conn.execute(
            "SELECT b.*,(SELECT count(*) FROM app.sa_fast_path_decisions d "
            "WHERE d.batch_id=b.batch_id) AS members FROM app.sa_fast_path_batches b "
            "WHERE b.source_instance_id=%s AND b.purpose_key=%s AND b.config_revision=%s "
            "AND b.policy_id=%s AND b.policy_revision=%s AND b.source_revision=%s "
            "AND b.generation=%s ORDER BY b.created_at DESC,b.batch_id LIMIT 1",
            (row["source_instance_id"], assessment["purpose_key"], control["revision"],
             row["policy_id"], row["policy_revision"], instance.revision, ingress["generation"]),
        ).fetchone()
        if (
            latest and latest["created_at"] <= now < latest["merge_until"]
            and latest["members"] < settings.max_batch_items
        ):
            # Avoid merging into a batch whose earlier authority/content is already stale.
            members = conn.execute(
                "SELECT i.*,d.config_revision AS decision_config FROM app.sa_fast_path_decisions d "
                "JOIN app.source_ingress_inputs i USING(input_id) WHERE d.batch_id=%s",
                (latest["batch_id"],),
            ).fetchall()
            if all(self._assess(conn, member, settings)["disposition"] in {"candidate", "control"}
                   for member in members):
                conn.execute(
                    "UPDATE app.sa_fast_path_batches SET revision=revision+1 WHERE batch_id=%s",
                    (latest["batch_id"],),
                )
                return latest["batch_id"], None
        if assessment["priority"] == 0 and latest and now < (
            latest["created_at"] + timedelta(seconds=settings.cooldown_seconds)
        ):
            return None, "FAST_PATH_COOLDOWN"
        count = conn.execute(
            "SELECT count(*) AS n FROM app.sa_fast_path_batches WHERE source_instance_id=%s "
            "AND priority=%s", (row["source_instance_id"], assessment["priority"]),
        ).fetchone()
        assert count is not None
        if count["n"] >= settings.max_batches:
            return None, "FAST_PATH_BATCH_CAPACITY"
        if assessment["priority"] == 0:
            if not self.engine.project_budgets.configured(conn):
                return None, "FAST_PATH_BUDGET_NOT_CONFIGURED"
            try:
                with conn.transaction():
                    self.engine.project_budgets.quota(
                        conn, "candidate", "sa08:" + str(row["input_id"]),
                        source_id=row["source_instance_id"],
                    )
            except DomainError as error:
                return None, error.code
        identifier = uuid4()
        conn.execute(
            "INSERT INTO app.sa_fast_path_batches(batch_id,source_instance_id,project_id,"
            "source_revision,generation,config_revision,policy_id,policy_revision,topic_key,"
            "purpose_key,priority,intended_action,created_at,merge_until) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (identifier, row["source_instance_id"], self.project, instance.revision,
             ingress["generation"], control["revision"], row["policy_id"], row["policy_revision"],
             assessment["topic_key"], assessment["purpose_key"], assessment["priority"],
             assessment["intended_action"], now,
             now + timedelta(seconds=settings.merge_window_seconds)),
        )
        return identifier, None

    def _observe(self, conn: Connection, row: Row, control: Row, settings: FastPathSettings) -> Row:
        previous = conn.execute(
            "SELECT * FROM app.sa_fast_path_decisions WHERE input_id=%s", (row["input_id"],),
        ).fetchone()
        if previous:
            return previous
        assessment = self._assess(conn, row, settings)
        count = conn.execute(
            "SELECT count(*) AS n FROM app.sa_fast_path_decisions WHERE source_instance_id=%s "
            "AND priority=%s", (row["source_instance_id"], assessment["priority"]),
        ).fetchone()
        assert count is not None
        if count["n"] >= settings.max_decisions:
            return {"input_id": row["input_id"], "disposition": "pending",
                    "reason_code": "FAST_PATH_DECISION_CAPACITY", "semantic_evaluation": "NOT_RUN"}
        batch_id = None
        if assessment["disposition"] in {"candidate", "control"}:
            batch_id, reason = self._batch(conn, row, control, settings, assessment)
            if reason:
                assessment.update(disposition="context", reason_code=reason, priority=0,
                                  intended_action="would_observe")
                count = conn.execute(
                    "SELECT count(*) AS n FROM app.sa_fast_path_decisions "
                    "WHERE source_instance_id=%s AND priority=0", (row["source_instance_id"],),
                ).fetchone()
                assert count is not None
                if count["n"] >= settings.max_decisions:
                    return {"input_id": row["input_id"], "disposition": "pending",
                            "reason_code": "FAST_PATH_DECISION_CAPACITY",
                            "semantic_evaluation": "NOT_RUN"}
        instance = self.identities.registry._instance(conn, row["source_instance_id"])
        ingress, _ = self.engine.source_ingress._control(conn, row["source_instance_id"])
        saved = conn.execute(
            "INSERT INTO app.sa_fast_path_decisions(input_id,source_instance_id,project_id,"
            "config_revision,source_revision,generation,policy_id,policy_revision,identity_revision,"
            "disposition,reason_code,mode,priority,intended_action,topic_key,batch_id,observed_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (row["input_id"], row["source_instance_id"], self.project, control["revision"],
             instance.revision, ingress["generation"], row["policy_id"], row["policy_revision"],
             row["identity_revision"], assessment["disposition"], assessment["reason_code"],
             assessment["mode"], assessment["priority"], assessment["intended_action"],
             assessment["topic_key"], batch_id, self.now(conn)),
        ).fetchone()
        assert saved is not None
        conn.execute(
            "UPDATE app.source_ingress_inputs SET consumed_at=coalesce(consumed_at,%s) "
            "WHERE input_id=%s",
            (self.now(conn), row["input_id"]),
        )
        return saved

    def observe(self, cookie: str, source_id: UUID, request: ObserveRequest) -> Row:
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            self._source(conn, source_id)
            control, settings = self._control(conn, source_id)
            if control is None:
                raise DomainError("FAST_PATH_NOT_CONFIGURED", "Save an explicit configuration", 409)
            rows = [self._input(conn, source_id, identifier) for identifier in request.input_ids]
            decisions = [self._observe(conn, row, control, settings) for row in rows]
            return {"decisions": decisions, "semantic_evaluation": "NOT_RUN"}

    @staticmethod
    def _limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError("FAST_PATH_LIMIT", "A limit between 1 and 100 is required", 422)

    def decisions(self, cookie: str, source_id: UUID, limit: int = 100) -> list[Row]:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            self._source(conn, source_id)
            return conn.execute(
                "SELECT * FROM app.sa_fast_path_decisions WHERE source_instance_id=%s "
                "AND project_id=%s ORDER BY observed_at DESC,input_id LIMIT %s",
                (source_id, self.project, limit),
            ).fetchall()

    def preview(self, cookie: str, source_id: UUID, limit: int = 100) -> Row:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            instance = self._source(conn, source_id)
            control, settings = self._control(conn, source_id)
            ingress, _ = self.engine.source_ingress._control(conn, source_id)
            batches = conn.execute(
                "SELECT * FROM app.sa_fast_path_batches WHERE source_instance_id=%s "
                "AND project_id=%s ORDER BY priority DESC,created_at DESC,batch_id LIMIT %s",
                (source_id, self.project, limit),
            ).fetchall()
            result = []
            for batch in batches:
                members = conn.execute(
                    "SELECT i.*,d.config_revision AS decision_config "
                    "FROM app.sa_fast_path_decisions d "
                    "JOIN app.source_ingress_inputs i USING(input_id) WHERE d.batch_id=%s "
                    "ORDER BY d.observed_at,d.input_id", (batch["batch_id"],),
                ).fetchall()
                active, reasons = [], []
                for member in members:
                    assessment = self._assess(conn, member, settings)
                    fresh = (
                        control is not None and member["decision_config"] == control["revision"]
                        and batch["source_revision"] == instance.revision
                        and batch["generation"] == ingress["generation"]
                        and assessment["disposition"] in {"candidate", "control"}
                        and assessment["intended_action"] == batch["intended_action"]
                    )
                    if fresh:
                        active.append(member["input_id"])
                    else:
                        reasons.append(assessment["reason_code"] if assessment["disposition"] not in
                                       {"candidate", "control"} else "FAST_PATH_SNAPSHOT_STALE")
                value = {**batch, "input_ids": [m["input_id"] for m in members],
                         "active_input_ids": active,
                         "current": bool(active) and len(active) == len(members),
                         "eligible_for_model": False,
                         "stale_reasons": sorted(set(reasons))}
                value["snapshot_sha256"] = digest([
                    str(batch["batch_id"]), batch["revision"],
                    control["revision"] if control else 0,
                    [(str(m["input_id"]), m["version_fingerprint"], m["input_id"] in active)
                     for m in members], value["stale_reasons"],
                ])
                result.append(value)
            return {"batches": result, "semantic_evaluation": "NOT_RUN", "actual_model_calls": 0}

    def metrics(self, cookie: str, source_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            self._source(conn, source_id)
            rows = conn.execute(
                "SELECT reason_code,disposition,mode,priority,count(*) AS n "
                "FROM app.sa_fast_path_decisions WHERE source_instance_id=%s AND project_id=%s "
                "GROUP BY reason_code,disposition,mode,priority", (source_id, self.project),
            ).fetchall()
            groups: dict[str, dict[Any, int]] = {key: {} for key in
                                               ["reason_code", "disposition", "mode", "priority"]}
            for row in rows:
                for key, group in groups.items():
                    group[row[key]] = group.get(row[key], 0) + row["n"]
            pending = conn.execute(
                "SELECT count(*) AS n FROM app.source_ingress_inputs i WHERE source_instance_id=%s "
                "AND project_id=%s AND unavailable_at IS NULL AND NOT EXISTS(SELECT 1 FROM "
                "app.sa_fast_path_decisions d WHERE d.input_id=i.input_id)",
                (source_id, self.project),
            ).fetchone()
            assert pending is not None
            return {"decisions": sum(row["n"] for row in rows), "pending": pending["n"],
                    "by_reason": [{"reason_code": k, "count": v}
                                  for k, v in sorted(groups["reason_code"].items())],
                    "by_disposition": groups["disposition"], "by_mode": groups["mode"],
                    "by_priority": groups["priority"], "semantic_evaluation": "NOT_RUN",
                    "actual_model_calls": 0, "actual_cost_micros": 0,
                    "candidate_budget_configured": self.engine.project_budgets.configured(conn)}
