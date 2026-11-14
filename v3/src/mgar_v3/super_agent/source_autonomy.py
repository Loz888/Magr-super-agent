"""SA16 deterministic coordination of original tasks under reviewed team rules.

Construction is inert. A cycle consumes committed suggestions, not HTTP facts or
model permissions. All business writes use the original held Engine transaction.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.super_agent.policy_domain import PolicyFacts
from mgar_v3.super_agent.source_autonomy_domain import (
    AUTONOMY_VERSION,
    AutonomyAdmissionFacts,
    AutonomyAdmissionPlan,
    AutonomyCycleRequest,
    AutonomyReopenRequest,
    AutonomyRevisionRequest,
    AutonomyRuleSave,
    AutonomySignal,
)
from mgar_v3.super_agent.source_autonomy_factory import create_original
from mgar_v3.super_agent.source_autonomy_profiles import AutonomyProfile
from mgar_v3.super_agent.source_autonomy_rules import plan_admission
from mgar_v3.super_agent.source_autonomy_runtime import maintain, pending, stop, view
from mgar_v3.super_agent.source_autonomy_schedule import latest_due
from mgar_v3.super_agent.source_autonomy_store import AutonomyStore, Row, decode_rule, error
from mgar_v3.super_agent.source_eval_domain import SourceDecision

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.super_agent.source_correlation import _Input


class SourceAutonomy(AutonomyStore):
    def __init__(self, engine: Engine):
        self.engine = engine
        self._profiles = {}

    def capabilities(self, cookie: str) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return {
                "version": AUTONOMY_VERSION,
                "engineering": "controlled_fixture_only",
                "profiles": sorted(self._profiles),
                "default": "disabled",
                "model_calls_per_cycle": 0,
                "notifications": False,
                "publication": False,
                "periodic_paid_work": False,
                "runtime_from_http": False,
                "live_coding": "NOT_RUN",
                "H12_live_agent": "FAIL",
                "H12_mvp": "FAIL",
                "max_cycle": 64,
                "bounded_daily_catchup": 1,
                "migration": "0048",
            }

    @staticmethod
    def _limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 64:
            raise error("LIMIT_INVALID", 422)

    @staticmethod
    def _rule_view(row: Row) -> Row:
        return {k: v for k, v in row.items() if k not in {"signed_by", "review_sha256"}}

    def rules(self, cookie: str, limit: int = 64) -> Row:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            rows = conn.execute(
                "SELECT v.*,h.last_local_date FROM app.sa_autonomy_rules h JOIN "
                "app.sa_autonomy_rule_versions v "
                "USING(rule_id,project_id,source_instance_id,revision) "
                "WHERE h.project_id=%s ORDER BY h.created_at,h.rule_id LIMIT %s",
                (self.project, limit + 1),
            ).fetchall()
            return {
                "rules": [self._rule_view(r) for r in rows[:limit]],
                "partial": len(rows) > limit,
            }

    def get_rule(self, cookie: str, rule_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return self._rule_view(self._rule(conn, rule_id))

    def save_rule(self, cookie: str, rule_id: UUID, request: AutonomyRuleSave) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            saved = self._save(conn, user, rule_id, request)
            # Propagate committed changes in the same original transaction. SQL
            # also checks rule revision on every final dispatch before maintenance.
            propagation = maintain(self, conn, limit=64, rule_id=rule_id)
            return {**self._rule_view(saved), "propagation": propagation}

    def disable_rule(self, cookie: str, rule_id: UUID, request: AutonomyRevisionRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            old = self._rule(conn, rule_id)
            rule = decode_rule(old).model_copy(update={"mode": "disabled"})
            saved = self._save(
                conn,
                user,
                rule_id,
                AutonomyRuleSave(
                    expected_revision=request.expected_revision,
                    definition=rule,
                    review_ref=request.review_ref,
                ),
                disabled=True,
            )
            propagation = maintain(self, conn, limit=64, rule_id=rule_id)
            return {**self._rule_view(saved), "propagation": propagation}

    def _decision_signal(self, conn: Connection, row: Row, key: str) -> AutonomySignal:
        saved = conn.execute(
            "SELECT d.*,s.source_instance_id,s.project_id FROM app.sa_source_eval_decisions d "
            "JOIN app.sa_source_eval_snapshots s USING(batch_id) WHERE d.decision_key=%s "
            "AND s.project_id=%s",
            (key, self.project),
        ).fetchone()
        if saved is None:
            raise error("DECISION_NOT_FOUND", 404)
        decision = SourceDecision.model_validate_json(json.dumps(saved["payload"]))
        if digest(decision.model_dump(mode="json")) != saved["payload_sha256"]:
            raise error("DECISION_INVALID")
        batch = self.engine.source_eval._batch(conn, decision.batch_id)
        if batch["status"] != "applied":
            raise error("DECISION_NOT_COMMITTED")
        if batch["mode"] != "fixture":
            raise error("REAL_QUALITY_GATE_CLOSED")
        snapshot, _ = self.engine.source_eval._checked(conn, batch)
        group = next((g for g in snapshot.groups if g.group_id == decision.group_id), None)
        rule = decode_rule(row)
        if (
            group is None
            or decision.snapshot_sha256 != batch["context_hash"]
            or snapshot.scope.source_instance_id != rule.source_instance_id
            or snapshot.policy_id != rule.policy_id
            or snapshot.policy_revision != rule.policy_revision
            or snapshot.rule_ref != rule.rule_ref
            or snapshot.rule_sha256 != rule.rule_sha256
            or group.repository is None
            or group.repository.lower() != rule.repository
            or tuple(i.input_id for i in group.inputs) != decision.input_ids
            or tuple(i.version_fingerprint for i in group.inputs)
            != decision.input_version_fingerprints
        ):
            raise error("DECISION_SCOPE_CHANGED")
        refs = tuple(
            self.engine.source_correlations._input(conn, i).ref for i in decision.input_ids
        )
        return AutonomySignal(
            key=key,
            subject_key=digest(
                [self.project, rule.repository, str(rule.source_instance_id), group.topic_key]
            ),
            rule_id=row["rule_id"],
            rule_revision=row["revision"],
            source_instance_id=rule.source_instance_id,
            policy_id=rule.policy_id,
            policy_revision=rule.policy_revision,
            policy_sha256=rule.policy_sha256,
            repository=rule.repository,
            intended_action=decision.intended_action,
            inputs=refs,
            expires_at=decision.expires_at,
        )

    def _unknown_project(self, conn: Connection) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM app.usage_calls u JOIN app.runs r USING(run_id) "
                "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
                "WHERE h.project_id=%s AND u.status IN ('reserved','unknown') "
                "UNION ALL SELECT 1 FROM app.router_calls c JOIN "
                "app.router_batches b USING(batch_id) "
                "WHERE b.project_id=%s AND c.status IN ('reserved','unknown') LIMIT 1",
                (self.project, self.project),
            ).fetchone()
            is not None
        )

    def _plan(
        self,
        conn: Connection,
        row: Row,
        signal: AutonomySignal,
        *,
        reviewed_reopen: bool = False,
        exclude_run: UUID | None = None,
    ) -> tuple[AutonomyAdmissionPlan, _Input | None, AutonomyProfile | None]:
        rule = decode_rule(row)

        def held(reason: str) -> tuple[AutonomyAdmissionPlan, None, None]:
            return (
                AutonomyAdmissionPlan(disposition="held", reason_code="AUTONOMY_" + reason),
                None,
                None,
            )

        if signal.intended_action != "would_create":
            return (
                AutonomyAdmissionPlan(disposition="ignored", reason_code="AUTONOMY_NO_NEW_TASK"),
                None,
                None,
            )
        if rule.mode == "disabled":
            return (
                AutonomyAdmissionPlan(disposition="ignored", reason_code="AUTONOMY_DISABLED"),
                None,
                None,
            )
        if len(signal.inputs) != 1:
            return held("SINGLE_ORIGIN_REQUIRED")
        try:
            self._rule_policy(conn, rule)
            source = self.engine.source_correlations._input(conn, signal.inputs[0].input_id)
            if source.ref != signal.inputs[0] or (
                source.row["context_only"] and rule.mode != "shadow"
            ):
                return held("SOURCE_CHANGED")
            if source.native:
                return held("RESOURCE_WINNER_FACTORY_REQUIRED")
            profile = self._profile(rule)
            roles, controlled = self.engine.source_policies._roles(
                conn, source.item.event.sender, source.item.event.source_thread, None
            )
            capabilities = (
                ("read_repository",)
                if profile.mode == "readonly"
                else ("read_repository", "edit_files", "run_tests")
            )
            for cap in capabilities:
                path = (
                    profile.read_path
                    if profile.coding is None
                    else profile.coding.grant.read_paths[0]
                )
                facts = PolicyFacts(
                    event_kind=source.item.event.event_kind,
                    request_kind="team_rule",
                    capability=cap,
                    repository=rule.repository,
                    base_sha=profile.base_sha,
                    path=path,
                )
                decision = self.engine.source_policies._evaluate(
                    source.definition,
                    "execution",
                    source.item.event.sender,
                    source.item.event.source_thread,
                    facts,
                    roles,
                    self.now(conn),
                    controlled,
                    shared_budget=self.engine.project_budgets.model_available(conn),
                )
                if not (decision.allowed or decision.would_allow):
                    return held("EXECUTION_DENIED")
            if profile.mode == "coding" and (
                source.item.event.sender.actor_kind != "human" or "requester" not in roles
            ):
                return held("CODING_REQUESTER_REQUIRED")
            correlation, _ = self.engine.source_correlations._build(conn, (source.ref.input_id,))
            if any(h.link_id is not None for h in correlation.heads):
                return held("EXISTING_CORRELATION")
        except DomainError as e:
            return held(
                "EXECUTOR_NOT_CONFIGURED"
                if e.code == "AUTONOMY_EXECUTOR_NOT_CONFIGURED"
                else "AUTHORITY_CHANGED"
            )
        history = conn.execute(
            "SELECT count(*) AS n,max(a.created_at) AS last_at,"
            "bool_or(a.control_state='stopped' OR r.status='cancelled') AS stopped "
            "FROM app.sa_autonomy_attempts a JOIN app.sa_autonomy_claims c USING(claim_id) "
            "JOIN app.runs r USING(run_id) WHERE c.project_id=%s AND c.subject_key=%s",
            (self.project, signal.subject_key),
        ).fetchone()
        active = conn.execute(
            "SELECT count(*) AS n FROM app.sa_autonomy_attempts a JOIN app.runs r USING(run_id) "
            "WHERE a.rule_id=%s AND a.project_id=%s AND a.control_state='active' "
            "AND r.status NOT IN ('succeeded','failed','cancelled') "
            "AND (%s::uuid IS NULL OR a.run_id<>%s)",
            (row["rule_id"], self.project, exclude_run, exclude_run),
        ).fetchone()
        assert history is not None and active is not None
        budget = self.engine.project_budgets.model_available(conn)
        if profile.coding is not None:
            configs = self.engine.project_budgets._configs(conn)
            prices = [
                p
                for _, d in configs
                for p in d.prices
                if p.price_version == profile.coding.budget.price_version
                and digest(p.model_dump(mode="json")) == profile.coding.budget.price_sha256
            ]
            budget = (
                budget
                and bool(prices)
                and self.engine.source_coding.budget_ref(
                    conn, prices[0], profile.budget_profile_sha256
                )
                == profile.coding.budget
            )
        f = AutonomyAdmissionFacts(
            rule_id=row["rule_id"],
            rule_revision=row["revision"],
            definition_sha256=rule.definition_sha256,
            source_current=True,
            identity_current=True,
            policy_current=True,
            receive_allowed=True,
            model_input_allowed=True,
            admission_allowed=True,
            execution_allowed=True,
            rule_basis_current=True,
            canonical_current=True,
            resource_claim_current=True,
            profile_current=True,
            profile_mode=profile.mode,
            actual_model_mode="fixture",
            budget_current=budget,
            usage_unknown=self._unknown_project(conn),
            previously_stopped=bool(history["stopped"]) and not reviewed_reopen,
            attempts=history["n"],
            last_admitted_at=history["last_at"],
            active_count=active["n"],
            factory_configured=hasattr(self.engine.source_scheduler, "_enqueue_authorized")
            and hasattr(self.engine.source_coding, "_admit_authorized"),
        )
        return plan_admission(rule, signal, f, at=self.now(conn)), source, profile

    def _accept(
        self,
        conn: Connection,
        row: Row,
        signal: AutonomySignal,
        *,
        decision_key: str | None = None,
        tick_id: UUID | None = None,
        previous: Row | None = None,
        review_sha256: str | None = None,
    ) -> Row:
        old = conn.execute(
            "SELECT * FROM app.sa_autonomy_claims WHERE project_id=%s AND signal_key=%s FOR UPDATE",
            (self.project, signal.key),
        ).fetchone()
        if old is not None:
            if old["rule_revision"] != row["revision"]:
                return {
                    "claim_id": old["claim_id"],
                    "disposition": "held",
                    "created": False,
                    "reason_code": "AUTONOMY_CLAIM_RULE_CHANGED",
                }
            attempt = conn.execute(
                "SELECT attempt_id FROM app.sa_autonomy_attempts WHERE claim_id=%s",
                (old["claim_id"],),
            ).fetchone()
            if (
                attempt is not None
                or old["disposition"] not in {"held", "admit"}
                or old["rule_id"] != row["rule_id"]
            ):
                return {
                    "claim_id": old["claim_id"],
                    "disposition": "duplicate",
                    "created": False,
                    "reason_code": "AUTONOMY_ALREADY_CLAIMED",
                }
        plan, source, profile = self._plan(
            conn,
            row,
            signal,
            reviewed_reopen=previous is not None,
            exclude_run=previous["run_id"] if previous else None,
        )
        if old is None:
            old = conn.execute(
                "INSERT INTO app.sa_autonomy_claims(claim_id,rule_id,rule_revision,project_id,"
                "source_instance_id,signal_key,subject_key,decision_key,tick_id,signal,signal_sha256,"
                "disposition,reason_code,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING *",
                (
                    uuid4(),
                    row["rule_id"],
                    row["revision"],
                    self.project,
                    signal.source_instance_id,
                    signal.key,
                    signal.subject_key,
                    decision_key,
                    tick_id,
                    Jsonb(signal.model_dump(mode="json")),
                    digest(signal.model_dump(mode="json")),
                    plan.disposition,
                    plan.reason_code,
                    signal.expires_at,
                ),
            ).fetchone()
            assert old is not None
            self._event(
                conn, row, "claim", plan.reason_code, claim_id=old["claim_id"], tick_id=tick_id
            )
        if plan.disposition != "admit":
            return {"claim_id": old["claim_id"], **plan.model_dump(mode="json"), "created": False}
        assert source is not None and profile is not None
        try:
            with conn.transaction():
                if previous is not None:
                    stop(self, conn, previous, "AUTONOMY_OWNER_REOPENED")
                attempt = create_original(
                    self,
                    conn,
                    row,
                    old,
                    signal,
                    source,
                    profile,
                    previous=previous,
                    review_sha256=review_sha256,
                )
            return {
                "claim_id": old["claim_id"],
                "attempt_id": attempt["attempt_id"],
                "run_id": attempt["run_id"],
                "created": True,
                "disposition": "admit",
                "reason_code": plan.reason_code,
            }
        except DomainError as e:
            reason = "AUTONOMY_ORIGINAL_ADMISSION_WAIT"
            self._event(conn, row, "held", reason, claim_id=old["claim_id"])
            return {
                "claim_id": old["claim_id"],
                "disposition": "held",
                "created": False,
                "reason_code": reason,
                "original_reason_code": e.code,
            }

    def _cycle(self, conn: Connection, limit: int) -> Row:
        outcomes: list[Row] = []
        rules = conn.execute(
            "SELECT rule_id FROM app.sa_autonomy_rules WHERE project_id=%s "
            "ORDER BY created_at,rule_id LIMIT 65",
            (self.project,),
        ).fetchall()
        for h in rules[:64]:
            row = self._rule(conn, h["rule_id"])
            if decode_rule(row).mode == "disabled":
                continue
            keys = conn.execute(
                "SELECT d.decision_key FROM app.sa_source_eval_decisions d JOIN "
                "app.sa_source_eval_snapshots s "
                "USING(batch_id) LEFT JOIN app.sa_autonomy_claims c ON c.project_id=s.project_id "
                "AND c.signal_key=d.decision_key WHERE s.project_id=%s AND s.source_instance_id=%s "
                "AND s.snapshot->>'policy_id'=%s AND s.snapshot->>'policy_revision'=%s "
                "AND (d.payload->>'expires_at')::timestamptz>clock_timestamp() "
                "AND (c.claim_id IS NULL OR (c.disposition IN ('held','admit') AND c.rule_id=%s "
                "AND NOT EXISTS(SELECT 1 FROM app.sa_autonomy_attempts a WHERE "
                "a.claim_id=c.claim_id))) "
                "ORDER BY d.created_at,d.decision_key LIMIT %s",
                (
                    self.project,
                    row["source_instance_id"],
                    str(row["policy_id"]),
                    str(row["policy_revision"]),
                    row["rule_id"],
                    limit - len(outcomes) + 1,
                ),
            ).fetchall()
            for k in keys[: limit - len(outcomes)]:
                try:
                    with conn.transaction():
                        signal = self._decision_signal(conn, row, k["decision_key"])
                        outcomes.append(
                            self._accept(conn, row, signal, decision_key=k["decision_key"])
                        )
                except DomainError as e:
                    outcomes.append(
                        {
                            "decision_key": k["decision_key"],
                            "created": False,
                            "disposition": "held",
                            "reason_code": "AUTONOMY_CANDIDATE_STALE",
                            "original_reason_code": e.code,
                        }
                    )
            if len(outcomes) >= limit:
                break
        return {
            "outcomes": outcomes,
            "bounded": True,
            "partial": len(outcomes) >= limit or len(rules) > 64,
            "model_calls": 0,
            "notification_sent": False,
            "published": False,
        }

    def cycle(self, cookie: str, request: AutonomyCycleRequest) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            maintained = maintain(self, conn, limit=request.limit)
            return {**self._cycle(conn, request.limit), "maintenance": maintained}

    def tick(self, cookie: str, request: AutonomyCycleRequest) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            rules = conn.execute(
                "SELECT rule_id FROM app.sa_autonomy_rules WHERE project_id=%s "
                "ORDER BY created_at,rule_id LIMIT %s",
                (self.project, request.limit + 1),
            ).fetchall()
            outcomes = []
            for head in rules[: request.limit]:
                row = self._rule(conn, head["rule_id"])
                rule, now = decode_rule(row), self.now(conn)
                due = latest_due(
                    rule,
                    rule_id=str(row["rule_id"]),
                    signed_at=row["signed_at"],
                    now=now,
                    last_local_date=row["last_local_date"],
                )
                if due is None:
                    continue
                assert rule.schedule is not None
                tick_id = uuid4()
                conn.execute(
                    "INSERT INTO app.sa_autonomy_ticks(tick_id,rule_id,rule_revision,project_id,"
                    "source_instance_id,timezone,local_date,scheduled_at,signal_sha256,"
                    "missed_coalesced,observed_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        tick_id,
                        row["rule_id"],
                        row["revision"],
                        self.project,
                        rule.source_instance_id,
                        rule.schedule.timezone,
                        due.local_date,
                        due.scheduled_at,
                        due.signal_sha256,
                        due.missed_coalesced,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE app.sa_autonomy_rules SET "
                    "last_local_date=%s,updated_at=clock_timestamp() "
                    "WHERE rule_id=%s",
                    (due.local_date, row["rule_id"]),
                )
                self._event(conn, row, "tick", "AUTONOMY_TIMER_SIGNAL", tick_id=tick_id)
                outcome: Row = {
                    "tick_id": tick_id,
                    "created": False,
                    "reason_code": "AUTONOMY_TIMER_WORK_NOT_AUTHORIZED",
                }
                if rule.schedule.allow_scheduled_work:
                    ref = rule.schedule.seed_input
                    assert ref is not None
                    signal = AutonomySignal(
                        key=due.signal_sha256,
                        subject_key=digest(
                            [
                                self.project,
                                rule.repository,
                                str(rule.source_instance_id),
                                "schedule",
                                rule.rule_ref,
                            ]
                        ),
                        rule_id=row["rule_id"],
                        rule_revision=row["revision"],
                        source_instance_id=rule.source_instance_id,
                        policy_id=rule.policy_id,
                        policy_revision=rule.policy_revision,
                        policy_sha256=rule.policy_sha256,
                        repository=rule.repository,
                        intended_action="would_create",
                        origin="timer",
                        inputs=(ref,),
                        expires_at=rule.valid_until,
                    )
                    outcome = self._accept(conn, row, signal, tick_id=tick_id)
                outcomes.append(outcome)
            return {
                "outcomes": outcomes,
                "partial": len(rules) > request.limit,
                "model_calls": 0,
                "notification_sent": False,
            }

    def claims(self, cookie: str, limit: int = 64) -> Row:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            rows = conn.execute(
                "SELECT c.*,a.attempt_id,a.run_id,e.kind AS latest_kind,"
                "e.reason_code AS latest_reason "
                "FROM app.sa_autonomy_claims c "
                "LEFT JOIN app.sa_autonomy_attempts a USING(claim_id) "
                "LEFT JOIN LATERAL(SELECT kind,reason_code FROM app.sa_autonomy_events "
                "WHERE claim_id=c.claim_id ORDER BY event_id DESC LIMIT 1) e ON true "
                "WHERE c.project_id=%s "
                "ORDER BY c.created_at DESC,c.claim_id LIMIT %s",
                (self.project, limit + 1),
            ).fetchall()
            return {
                "claims": [
                    {
                        **{k: v for k, v in r.items() if k != "signal"},
                        "initial_disposition": r["disposition"],
                        "disposition": "held"
                        if r["attempt_id"] is None and r["latest_kind"] == "held"
                        else r["disposition"],
                        "reason_code": r["latest_reason"] or r["reason_code"],
                    }
                    for r in rows[:limit]
                ],
                "partial": len(rows) > limit,
            }

    def _attempt(self, conn: Connection, identifier: UUID) -> Row:
        row = conn.execute(
            "SELECT * FROM app.sa_autonomy_attempts WHERE attempt_id=%s AND project_id=%s "
            "FOR UPDATE",
            (identifier, self.project),
        ).fetchone()
        if row is None:
            raise error("ATTEMPT_NOT_FOUND", 404)
        return row

    def attempts(self, cookie: str, limit: int = 64) -> Row:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            rows = conn.execute(
                "SELECT * FROM app.sa_autonomy_attempts WHERE project_id=%s "
                "ORDER BY created_at DESC,attempt_id LIMIT %s",
                (self.project, limit + 1),
            ).fetchall()
            return {
                "attempts": [view(self, conn, r) for r in rows[:limit]],
                "partial": len(rows) > limit,
            }

    def get_attempt(self, cookie: str, identifier: UUID) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return view(self, conn, self._attempt(conn, identifier))

    def stop(self, cookie: str, identifier: UUID, request: AutonomyRevisionRequest) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            row = self._attempt(conn, identifier)
            if row["revision"] != request.expected_revision:
                raise error("REVISION_CONFLICT", 409)
            return view(self, conn, stop(self, conn, row, "AUTONOMY_OWNER_STOPPED"))

    def reopen(self, cookie: str, identifier: UUID, request: AutonomyReopenRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            previous = self._attempt(conn, identifier)
            original = self.engine._run(conn, previous["run_id"])
            row = self._rule(conn, previous["rule_id"])
            if (
                previous["revision"] != request.expected_revision
                or original["execution_epoch"] != request.expected_epoch
                or row["signed_by"] != user
                or original["status"]
                not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "paused",
                    "waiting_input",
                    "waiting_approval",
                }
                or any(pending(self, conn, previous["run_id"]))
            ):
                raise error("REOPEN_STALE_OR_UNCONFIRMED", 409)
            if conn.execute(
                "SELECT 1 FROM app.sa_scheduler_assignments WHERE run_id=%s AND state<>'released'",
                (previous["run_id"],),
            ).fetchone():
                raise error("REOPEN_RESOURCE_PENDING", 409)
            signal = self._decision_signal(conn, row, request.decision_key)
            prior = conn.execute(
                "SELECT subject_key FROM app.sa_autonomy_claims WHERE claim_id=%s",
                (previous["claim_id"],),
            ).fetchone()
            if (
                prior is None
                or signal.subject_key != prior["subject_key"]
                or signal.inputs != (request.source_input,)
                or request.source_input.input_id == previous["input_id"]
            ):
                raise error("REOPEN_NEW_REVIEWED_SOURCE_REQUIRED")
            # Reopen has its own exact Owner review in the frozen new mandate.
            self.engine._event(
                conn,
                original["run_id"],
                "source_autonomy_reopen_reviewed",
                review_sha256=digest(request.review_ref),
                signed_by=str(user),
                decision_key=request.decision_key,
                expected_epoch=request.expected_epoch,
            )
            return self._accept(
                conn,
                row,
                signal,
                decision_key=request.decision_key,
                previous=previous,
                review_sha256=digest(request.review_ref),
            )

    def maintain(self, cookie: str, request: AutonomyCycleRequest) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return maintain(self, conn, limit=request.limit)

    def recover_conn(self, conn: Connection) -> None:
        self._conn(conn)
        if self._ready(conn):
            maintain(self, conn, limit=64)
