"""Engine-owned Source evaluation facts; no task, execution or notification writer.

Only an explicit runtime may inject a live provider and its original RouterPermit.
Source text exists in temporary ModelContext objects; durable snapshots and results
contain typed references, hashes and bounded enums. All PostgreSQL writes use the
original Engine connection, including original RouterBudget reservations/settlement.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.routing.budget import RouterBudget
from mgar_v3.sources.ingress_domain import ItemMetadata
from mgar_v3.super_agent.fast_path import FastPath
from mgar_v3.super_agent.fast_path_rules import extract_text, parse_intent
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicyFacts
from mgar_v3.super_agent.source_eval_domain import (
    CanonicalInputRef,
    ModelContext,
    ModelContextGroup,
    SourceAction,
    SourceDecision,
    SourceEvalConfigure,
    SourceEvalSettings,
    SourceEvaluateRequest,
    SourceProposals,
    SourceSnapshot,
    SourceSnapshotGroup,
    SourceTargetRef,
)
from mgar_v3.super_agent.source_eval_model import (
    PROMPT_SHA256,
    PROMPT_VERSION,
    SourceEvalModel,
    SourceModelRequest,
    guard_source_send,
    render_request,
)
from mgar_v3.super_agent.source_eval_rules import snapshot_hash
from mgar_v3.super_agent.source_eval_rules import wrap as wrap_proposals

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.live import LivePermit

Row = dict[str, Any]


def _error(code: str, status: int = 409) -> DomainError:
    return DomainError(code, "Source evaluation requires current bounded authority", status)


def _code(code: str) -> str:
    return code if re.fullmatch(r"[A-Z0-9_]{1,100}", code) else "SOURCE_EVAL_FAILED"


class SourceEval:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.fast = FastPath(engine)
        self.identities = self.fast.identities

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    @staticmethod
    def now(conn: Connection) -> datetime:
        return FastPath.now(conn)

    @staticmethod
    def _ready(conn: Connection) -> bool:
        row = conn.execute(
            "SELECT to_regclass('app.sa_source_eval_controls') IS NOT NULL AS ready"
        ).fetchone()
        return row is not None and bool(row["ready"])

    def _source(self, conn: Connection, source_id: UUID) -> Any:
        if not self._ready(conn):
            raise _error("SOURCE_EVAL_NOT_MIGRATED")
        return self.identities.registry._instance(conn, source_id)

    def _control(self, conn: Connection, source_id: UUID) -> tuple[Row | None, SourceEvalSettings]:
        row = conn.execute(
            "SELECT * FROM app.sa_source_eval_controls WHERE source_instance_id=%s "
            "AND project_id=%s", (source_id, self.project),
        ).fetchone()
        settings = (SourceEvalSettings.model_validate_json(json.dumps(row["settings"]))
                    if row else SourceEvalSettings())
        return row, settings

    def configure(self, cookie: str, source_id: UUID, request: SourceEvalConfigure) -> Row:
        with self.engine.transaction() as conn:
            user = self.identities._console(conn, cookie)
            self._source(conn, source_id)
            row, _ = self._control(conn, source_id)
            revision = row["revision"] if row else 0
            if revision != request.expected_revision:
                raise _error("SOURCE_EVAL_REVISION")
            profile = request.settings.profile
            if profile is not None and (
                profile.prompt_version != PROMPT_VERSION or profile.prompt_sha256 != PROMPT_SHA256
            ):
                raise _error("SOURCE_EVAL_PROMPT_MISMATCH", 422)
            if request.settings.enabled:
                fast, settings = self.fast._control(conn, source_id)
                if fast is None or not settings.enabled:
                    raise _error("SOURCE_EVAL_FAST_PATH_DISABLED")
            settings_json = request.settings.model_dump(mode="json")
            fingerprint = digest(settings_json)
            conn.execute(
                "INSERT INTO app.sa_source_eval_controls(source_instance_id,project_id,revision,"
                "settings,settings_sha256,updated_by) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(source_instance_id) DO UPDATE SET revision=EXCLUDED.revision,"
                "settings=EXCLUDED.settings,settings_sha256=EXCLUDED.settings_sha256,"
                "updated_by=EXCLUDED.updated_by,updated_at=clock_timestamp()",
                (source_id, self.project, revision+1, Jsonb(settings_json), fingerprint, user),
            )
            conn.execute(
                "INSERT INTO app.sa_source_eval_versions(source_instance_id,project_id,revision,"
                "settings,settings_sha256,signed_by,review_ref) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (source_id, self.project, revision+1, Jsonb(settings_json), fingerprint,
                 user, request.review_ref),
            )
            return {"revision": revision+1, "settings": settings_json,
                    "settings_sha256": fingerprint, "executes_tasks": False}

    def read(self, cookie: str, source_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            self._source(conn, source_id)
            row, settings = self._control(conn, source_id)
            return {"revision": row["revision"] if row else 0,
                    "settings": settings.model_dump(mode="json"), "executes_tasks": False}

    def _budget_hash(self, conn: Connection, settings: SourceEvalSettings) -> str:
        configs = self.engine.project_budgets._configs(conn)
        if not configs or settings.profile is None:
            raise _error("SOURCE_EVAL_BUDGET_REQUIRED", 403)
        for _, definition in configs:
            self.engine.project_budgets._active(definition, self.now(conn))
            if settings.profile.price not in definition.prices:
                raise _error("SOURCE_EVAL_PRICE_DENIED", 403)
        return digest([
            [row["scope_type"], row["scope_id"], row["revision"], row["definition_sha256"]]
            for row, _ in configs
        ])

    def _policy_budget(self, conn: Connection, definition: PolicyDefinition,
                       additional: int = 0) -> None:
        """Conservative project lifetime upper bound, additional to original SA07 caps.

        The Source policy has no independent billing window. Original known costs and
        all reserved/unknown amounts count, including unrelated original project calls.
        No old reservation is refunded and no accounting expression is replaced.
        """
        total = router = 0
        for call in self.engine.project_budgets._calls(conn, [self.project]):
            amount = (call["settled_micros"] if call["status"] == "known"
                      else call["reserved_micros"])
            if type(amount) is not int or amount < 0:
                raise _error("SOURCE_EVAL_ACCOUNTING_UNAVAILABLE", 403)
            total += amount
            if call["ledger_kind"] == "router":
                router += amount
        limits = definition.budget
        if (limits.total_limit_micros <= 0 or limits.router_limit_micros <= 0
                or total+additional > limits.total_limit_micros
                or router+additional > limits.router_limit_micros):
            raise _error("SOURCE_EVAL_POLICY_BUDGET", 403)

    def _body(self, conn: Connection, row: Row) -> tuple[str, datetime]:
        body = conn.execute(
            "SELECT ciphertext,expires_at FROM app.source_ingress_bodies WHERE input_id=%s "
            "AND expires_at>clock_timestamp()", (row["input_id"],),
        ).fetchone()
        cipher = self.engine.source_ingress.cipher
        if body is None or cipher is None:
            raise _error("SOURCE_EVAL_BODY_UNAVAILABLE", 403)
        clear = cipher.decrypt(bytes(body["ciphertext"]), str(row["input_id"]))
        if hashlib.sha256(clear).hexdigest() != row["body_sha256"]:
            raise _error("SOURCE_EVAL_BODY_HASH", 403)
        return extract_text(clear), body["expires_at"]

    def _targets(self, conn: Connection, items: list[ItemMetadata],
                 repository: str, definition: PolicyDefinition) -> tuple[SourceTargetRef, ...]:
        common: set[UUID] | None = None
        for item in items:
            event = item.event
            rows = conn.execute(
                "SELECT c.ticket_id FROM app.source_task_controllers c JOIN "
                "app.source_role_bindings b USING(binding_id,project_id) JOIN "
                "app.source_identities i USING(identity_id,project_id) WHERE i.identity_key=%s "
                "AND i.project_id=%s AND i.revoked_at IS NULL AND b.active AND c.active "
                "AND b.conversation_key=%s AND b.roles ? 'task_controller'",
                (event.sender.key, self.project, event.source_thread.conversation_key),
            ).fetchall() if event.sender.actor_kind == "human" else []
            allowed = {row["ticket_id"] for row in rows}
            common = allowed if common is None else common & allowed
        result = []
        for ticket_id in sorted(common or (), key=str):
            admitted = True
            for item in items:
                event = item.event
                roles, controlled = self.engine.source_policies._roles(
                    conn, event.sender, event.source_thread, ticket_id
                )
                decision = self.engine.source_policies._evaluate(
                    definition, "admission", event.sender, event.source_thread,
                    PolicyFacts(event_kind=event.event_kind, request_kind="follow_up",
                                ticket_id=ticket_id),
                    roles, self.now(conn), controlled,
                )
                if not (decision.allowed or decision.would_allow):
                    admitted = False
                    break
            if not admitted:
                continue
            target = conn.execute(
                "SELECT t.ticket_id,h.thread_id,h.revision,h.project_id,h.repository,"
                "m.mandate_id,to_jsonb(m) AS mandate,r.run_id,r.execution_epoch "
                "FROM app.tickets t JOIN app.threads h USING(thread_id) "
                "JOIN app.mandates m USING(mandate_id) JOIN LATERAL(SELECT * FROM app.runs x "
                "WHERE x.ticket_id=t.ticket_id ORDER BY attempt_no DESC LIMIT 1) r ON true "
                "WHERE t.ticket_id=%s AND h.project_id=%s AND m.project_id=%s "
                "AND lower(h.repository)=%s AND t.status='open'",
                (ticket_id, self.project, self.project, repository),
            ).fetchone()
            if target is not None:
                result.append(SourceTargetRef(
                    ticket_id=target["ticket_id"], run_id=target["run_id"],
                    thread_id=target["thread_id"], thread_revision=target["revision"],
                    execution_epoch=target["execution_epoch"], mandate_id=target["mandate_id"],
                    mandate_sha256=digest(target["mandate"]), project_id=self.project,
                    repository=repository, allowed=True,
                ))
            if len(result) == 3:
                break
        return tuple(result)

    def _build(self, conn: Connection, source_id: UUID, fast_id: UUID, batch_id: UUID,
               expires: datetime | None = None) -> tuple[SourceSnapshot, ModelContext]:
        instance = self._source(conn, source_id)
        control, settings = self._control(conn, source_id)
        if control is None or not settings.enabled or settings.profile is None:
            raise _error("SOURCE_EVAL_DISABLED")
        profile = settings.profile
        if profile.prompt_version != PROMPT_VERSION or profile.prompt_sha256 != PROMPT_SHA256:
            raise _error("SOURCE_EVAL_PROMPT_MISMATCH", 403)
        budget_hash = self._budget_hash(conn, settings)
        fast_control, fast_settings = self.fast._control(conn, source_id)
        ingress, _ = self.engine.source_ingress._control(conn, source_id)
        fast = conn.execute(
            "SELECT * FROM app.sa_fast_path_batches WHERE batch_id=%s AND "
            "source_instance_id=%s AND project_id=%s", (fast_id, source_id, self.project),
        ).fetchone()
        if (fast is None or fast_control is None or not fast_settings.enabled
                or fast["priority"] != 0 or fast["source_revision"] != instance.revision
                or fast["generation"] != ingress["generation"]
                or fast["config_revision"] != fast_control["revision"]):
            raise _error("SOURCE_EVAL_FAST_BATCH_STALE", 403)
        rows = conn.execute(
            "SELECT i.* FROM app.sa_fast_path_decisions d JOIN app.source_ingress_inputs i "
            "USING(input_id) WHERE d.batch_id=%s ORDER BY d.observed_at,d.input_id", (fast_id,),
        ).fetchall()
        if not 1 <= len(rows) <= 16:
            raise _error("SOURCE_EVAL_MEMBERS_INVALID")
        inputs, items, text, actors = [], [], [], {}
        definition: PolicyDefinition | None = None
        policy_id = policy_revision = None
        repository = None
        deadline = self.now(conn)+timedelta(seconds=settings.decision_ttl_seconds)
        for row in rows:
            assessment = self.fast._assess(conn, row, fast_settings)
            if assessment["disposition"] != "candidate":
                raise _error("SOURCE_EVAL_INPUT_NOT_CANDIDATE", 403)
            item, grant = self.engine.source_ingress._readable(conn, row)
            if not grant["model_input"]:
                raise _error("SOURCE_EVAL_MODEL_INPUT_DENIED", 403)
            current = self.engine.source_policies._current(conn, grant["policy_id"])
            value = PolicyDefinition.model_validate(current["definition"])
            if policy_id is not None and (
                policy_id != grant["policy_id"] or policy_revision != grant["policy_revision"]
            ):
                raise _error("SOURCE_EVAL_POLICY_MISMATCH", 403)
            policy_id, policy_revision = grant["policy_id"], grant["policy_revision"]
            definition = value
            body, body_expiry = self._body(conn, row)
            if not body.strip():
                raise _error("SOURCE_EVAL_CONTEXT_REQUIRED")
            event = item.event
            # A verified platform hint can select an existing signed repository boundary.
            # With no hint, only one explicit signed boundary is unambiguous. Never infer
            # a repository/base/path from message text or from model output.
            boundaries = [b for b in value.execution.repositories if (
                event.repository_hint is None
                or b.repository.lower() == event.repository_hint.full_name.lower()
            )]
            if len(boundaries) != 1:
                raise _error("SOURCE_EVAL_REPOSITORY_REQUIRED")
            boundary = boundaries[0]
            actual_repository = boundary.repository.lower()
            if repository is not None and repository != actual_repository:
                raise _error("SOURCE_EVAL_REPOSITORY_MISMATCH", 403)
            repository = actual_repository
            intent = parse_intent(item, body)
            kind = intent.request_kind
            if kind is None and "team_rule" in value.admission.request_kinds:
                kind = "team_rule"
            roles, controlled = self.engine.source_policies._roles(
                conn, event.sender, event.source_thread, None
            )
            allowed = self.engine.source_policies._evaluate(
                value, "execution", event.sender, event.source_thread,
                PolicyFacts(event_kind=event.event_kind, request_kind=kind,
                            capability="model_call", repository=repository,
                            base_sha=boundary.base_sha, path=boundary.paths[0]),
                roles, self.now(conn), controlled, shared_budget=True,
            )
            if not (allowed.allowed or allowed.would_allow):
                raise _error("SOURCE_EVAL_MODEL_POLICY_DENIED", 403)
            self._policy_budget(conn, value)
            actors[event.sender.key] = event.sender
            items.append(item)
            inputs.append(CanonicalInputRef(
                input_id=row["input_id"], version_fingerprint=row["version_fingerprint"],
                identity_id=grant["identity_id"], identity_revision=grant["identity_revision"],
                actor_key=event.sender.key,
            ))
            text.append(body)
            _, ingress_settings = self.engine.source_ingress._control(conn, source_id)
            deadline = min(deadline, body_expiry, row["metadata_expires_at"], value.valid_until,
                           event.occurred_at+timedelta(seconds=ingress_settings.live_age_seconds))
        assert definition is not None and repository is not None
        assert policy_id is not None and policy_revision is not None
        targets = self._targets(conn, items, repository, definition)
        actions: list[SourceAction] = ["ignore", "observe", "create", "ask"]
        if targets:
            actions.append("attach")
        expiry = expires or deadline
        if expiry > deadline or expiry <= self.now(conn):
            raise _error("SOURCE_EVAL_SNAPSHOT_EXPIRED")
        group = SourceSnapshotGroup(
            group_id=fast_id, fast_batch_id=fast_id, fast_batch_revision=fast["revision"],
            topic_key=fast["topic_key"], actors=tuple(actors.values()), inputs=tuple(inputs),
            targets=targets, repository=repository, context_complete=True,
            allowed_actions=tuple(actions),
        )
        snapshot = SourceSnapshot(
            batch_id=batch_id, project_id=self.project, scope=instance.scope,
            source_revision=instance.revision, generation=ingress["generation"],
            fast_config_revision=fast_control["revision"], eval_config_revision=control["revision"],
            policy_id=policy_id, policy_revision=policy_revision,
            rule_ref=definition.admission.rule_ref, rule_sha256=definition.admission.rule_sha256,
            prompt_version=profile.prompt_version, prompt_sha256=profile.prompt_sha256,
            profile_sha256=digest(profile.model_dump(mode="json")),
            price_sha256=digest(profile.price.model_dump(mode="json")),
            budget_snapshot_sha256=budget_hash, expires_at=expiry, groups=(group,),
        )
        combined = "\n\n".join(text)
        # Enforce the byte bound before a model validator can include private
        # source text in its ValidationError. Only a fixed pending reason escapes.
        if len(combined.encode()) > settings.max_context_bytes:
            raise _error("SOURCE_EVAL_CONTEXT_BOUND")
        context = ModelContext(groups=(ModelContextGroup(group_id=fast_id, text=combined),))
        return snapshot, context

    def _batch(self, conn: Connection, batch_id: UUID) -> Row:
        row = conn.execute(
            "SELECT b.*,s.source_instance_id,s.snapshot AS source_snapshot,s.snapshot_sha256 "
            "FROM app.router_batches b JOIN app.sa_source_eval_snapshots s USING(batch_id) "
            "WHERE b.batch_id=%s AND b.project_id=%s AND s.project_id=%s",
            (batch_id, self.project, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_EVAL_BATCH_NOT_FOUND", 404)
        if (row["snapshot"].get("schema_version") != "source-router-snapshot-v1"
                or row["snapshot"].get("threads") != []):
            raise _error("SOURCE_EVAL_PIPELINE_MISMATCH")
        return row

    def _checked(self, conn: Connection, batch: Row) -> tuple[SourceSnapshot, ModelContext]:
        stored = self._batch(conn, batch["batch_id"])
        snapshot = SourceSnapshot.model_validate_json(json.dumps(stored["source_snapshot"]))
        if (snapshot_hash(snapshot) != stored["context_hash"]
                or stored["snapshot_sha256"] != stored["context_hash"]
                or {k: v for k, v in stored["snapshot"].items() if k != "threads"}
                != stored["source_snapshot"]
                or stored["policy_version"] != self.engine.settings.policy_version
                or stored["expires_at"] != snapshot.expires_at
                or batch["context_hash"] != stored["context_hash"]):
            raise _error("SOURCE_EVAL_SNAPSHOT_INVALID")
        current, context = self._build(
            conn, stored["source_instance_id"], snapshot.groups[0].fast_batch_id,
            snapshot.batch_id, snapshot.expires_at,
        )
        if snapshot_hash(current) != snapshot_hash(snapshot):
            raise _error("SOURCE_EVAL_SNAPSHOT_STALE")
        return snapshot, context

    def _attempted(self, conn: Connection, input_ids: tuple[UUID, ...],
                   excluding: UUID | None = None) -> bool:
        # Canonical input identity, rather than an invented job/config ID, fences retries.
        return conn.execute(
            "SELECT 1 FROM app.sa_source_eval_inputs i JOIN app.router_batches b USING(batch_id) "
            "LEFT JOIN app.router_calls c USING(batch_id) LEFT JOIN app.sa_source_eval_results r "
            "USING(batch_id) WHERE i.project_id=%s AND i.input_id=ANY(%s) "
            "AND (%s::uuid IS NULL OR i.batch_id<>%s) AND (c.call_id IS NOT NULL "
            "OR r.batch_id IS NOT NULL) LIMIT 1",
            (self.project, list(input_ids), excluding, excluding),
        ).fetchone() is not None

    def enqueue(self, cookie: str, source_id: UUID, request: SourceEvaluateRequest,
                *, mode: str = "fixture") -> Row:
        if mode not in {"fixture", "live"}:
            raise _error("SOURCE_EVAL_MODE_INVALID", 422)
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            self._source(conn, source_id)
            control, settings = self._control(conn, source_id)
            if control is None or not settings.enabled:
                raise _error("SOURCE_EVAL_DISABLED")
            results = []
            for fast_id in request.fast_batch_ids:
                try:
                    # Rejected enqueue attempts do not partially create an evaluation.
                    with conn.transaction():
                        snapshot, _ = self._build(conn, source_id, fast_id, uuid4())
                        previous = conn.execute(
                            "SELECT b.* FROM app.sa_source_eval_snapshots s JOIN "
                            "app.router_batches b USING(batch_id) WHERE s.source_instance_id=%s "
                            "AND s.fast_batch_id=%s AND s.fast_batch_revision=%s "
                            "AND s.eval_revision=%s",
                            (source_id, fast_id, snapshot.groups[0].fast_batch_revision,
                             control["revision"]),
                        ).fetchone()
                        if previous:
                            if previous["mode"] != mode:
                                raise _error("SOURCE_EVAL_MODE_CONFLICT")
                            results.append({"fast_batch_id": fast_id,
                                            "batch_id": previous["batch_id"],
                                            "status": previous["status"], "duplicate": True})
                            continue
                        members = tuple(i.input_id for i in snapshot.groups[0].inputs)
                        if self._attempted(conn, members):
                            raise _error("SOURCE_EVAL_INPUT_ALREADY_ATTEMPTED")
                        count = conn.execute(
                            "SELECT count(*) AS n FROM app.sa_source_eval_snapshots s JOIN "
                            "app.router_batches b USING(batch_id) WHERE s.source_instance_id=%s "
                            "AND b.status IN ('prepared','model_running','ready') "
                            "AND (b.expires_at>clock_timestamp() OR b.status='model_running')",
                            (source_id,),
                        ).fetchone()
                        assert count is not None
                        if count["n"] >= settings.max_pending:
                            raise _error("SOURCE_EVAL_PENDING_CAPACITY")
                        if not self.engine.project_budgets.model_available(conn):
                            raise _error("SOURCE_EVAL_BUDGET_UNAVAILABLE", 403)
                        value = snapshot.model_dump(mode="json")
                        fingerprint = snapshot_hash(snapshot)
                        conn.execute(
                            "INSERT INTO app.router_batches(batch_id,project_id,policy_version,"
                            "context_hash,snapshot,mode,status,expires_at) "
                            "VALUES(%s,%s,%s,%s,%s,%s,'prepared',%s)",
                            (snapshot.batch_id, self.project, self.engine.settings.policy_version,
                             fingerprint, Jsonb({**value, "threads": []}), mode,
                             snapshot.expires_at),
                        )
                        conn.execute(
                            "INSERT INTO app.sa_source_eval_snapshots(batch_id,source_instance_id,"
                            "project_id,fast_batch_id,fast_batch_revision,eval_revision,"
                            "snapshot_sha256,snapshot) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                            (snapshot.batch_id, source_id, self.project, fast_id,
                             snapshot.groups[0].fast_batch_revision, control["revision"],
                             fingerprint, Jsonb(value)),
                        )
                        for member in snapshot.groups[0].inputs:
                            conn.execute(
                                "INSERT INTO app.sa_source_eval_inputs(batch_id,input_id,"
                                "source_instance_id,project_id,version_fingerprint) "
                                "VALUES(%s,%s,%s,%s,%s)",
                                (snapshot.batch_id, member.input_id, source_id, self.project,
                                 member.version_fingerprint),
                            )
                        results.append({"fast_batch_id": fast_id, "batch_id": snapshot.batch_id,
                                        "status": "prepared", "duplicate": False})
                except DomainError as error:
                    results.append({"fast_batch_id": fast_id, "status": "pending",
                                    "reason_code": _code(error.code)})
            return {"evaluations": results, "semantic_evaluation": "NOT_RUN",
                    "executes_tasks": False}

    def prepare(self, mode: str = "fixture") -> Row | None:
        if mode not in {"fixture", "live"}:
            raise _error("SOURCE_EVAL_MODE_INVALID", 422)
        with self.engine.transaction() as conn:
            if not self._ready(conn):
                return None
            rows = conn.execute(
                "SELECT b.batch_id FROM app.router_batches b JOIN app.sa_source_eval_snapshots s "
                "USING(batch_id) WHERE b.project_id=%s AND b.mode=%s "
                "AND b.status IN ('prepared','ready') ORDER BY b.created_at,b.batch_id LIMIT 100",
                (self.project, mode),
            ).fetchall()
            for row in rows:
                batch = self._batch(conn, row["batch_id"])
                try:
                    self._checked(conn, batch)
                    return batch
                except DomainError as error:
                    self._failure(conn, batch["batch_id"], error.code)
            return None

    def wrap(self, batch: Row, proposals: SourceProposals) -> list[SourceDecision]:
        with self.engine.transaction() as conn:
            snapshot, _ = self._checked(conn, batch)
            if not isinstance(proposals, SourceProposals):
                raise _error("SOURCE_EVAL_PROPOSALS_INVALID", 422)
            return list(wrap_proposals(snapshot, proposals, at=self.now(conn)))

    @staticmethod
    def _proposals(decisions: list[SourceDecision]) -> SourceProposals:
        return SourceProposals.model_validate_json(json.dumps({
            "schema_version": "source-router-proposals-v1", "proposals": [{
                "group_id": str(d.group_id), "action": d.intended_action.removeprefix("would_"),
                "target_id": str(d.target.ticket_id) if d.target else None,
                "reason_code": d.reason_code,
            } for d in decisions],
        }))

    def _validate_decisions(self, conn: Connection, batch: Row,
                            decisions: list[SourceDecision]) -> SourceProposals:
        snapshot, _ = self._checked(conn, batch)
        proposals = self._proposals(decisions)
        expected = list(wrap_proposals(snapshot, proposals, at=self.now(conn)))
        if [d.model_dump() for d in expected] != [d.model_dump() for d in decisions]:
            raise _error("SOURCE_EVAL_DECISION_INVALID")
        return proposals

    def save(self, batch_id: UUID, decisions: list[SourceDecision]) -> None:
        with self.engine.transaction() as conn:
            batch = self._batch(conn, batch_id)
            if batch["status"] not in {"prepared", "model_running"}:
                raise _error("SOURCE_EVAL_BATCH_STATE")
            proposals = self._validate_decisions(conn, batch, decisions)
            result = conn.execute(
                "SELECT * FROM app.sa_source_eval_results WHERE batch_id=%s", (batch_id,),
            ).fetchone()
            if result is None:
                if batch["mode"] != "fixture":
                    raise _error("SOURCE_EVAL_KNOWN_RESULT_REQUIRED")
                conn.execute(
                    "INSERT INTO app.sa_source_eval_results(batch_id,outcome_code,proposals,"
                    "usage_known) VALUES(%s,'SOURCE_EVAL_FIXTURE_EVALUATED',%s,false)",
                    (batch_id, Jsonb(proposals.model_dump(mode="json"))),
                )
            elif result["proposals"] != proposals.model_dump(mode="json"):
                raise _error("SOURCE_EVAL_RESULT_CONFLICT")
            conn.execute(
                "UPDATE app.router_batches SET status='ready',decisions=%s WHERE batch_id=%s",
                (Jsonb([d.model_dump(mode="json") for d in decisions]), batch_id),
            )

    def commit(self, batch_id: UUID, decisions: list[SourceDecision],
               *, permit: LivePermit | None = None) -> list[Row]:
        if permit is not None:
            raise _error("SOURCE_EVAL_EXECUTION_PERMIT_DENIED", 403)
        with self.engine.transaction() as conn:
            batch = self._batch(conn, batch_id)
            if batch["status"] not in {"ready", "applied"}:
                raise _error("SOURCE_EVAL_BATCH_STATE")
            proposals = self._validate_decisions(conn, batch, decisions)
            result = conn.execute(
                "SELECT * FROM app.sa_source_eval_results WHERE batch_id=%s", (batch_id,),
            ).fetchone()
            if result is None or result["proposals"] != proposals.model_dump(mode="json"):
                raise _error("SOURCE_EVAL_RESULT_CONFLICT")
            if batch["status"] == "applied":
                saved = conn.execute(
                    "SELECT payload FROM app.sa_source_eval_decisions WHERE batch_id=%s "
                    "ORDER BY group_id", (batch_id,),
                ).fetchall()
                if {digest(r["payload"]) for r in saved} != {
                    digest(d.model_dump(mode="json")) for d in decisions
                }:
                    raise _error("SOURCE_EVAL_DECISION_REUSED")
                return [r["payload"] for r in saved]
            for decision in decisions:
                value = decision.model_dump(mode="json")
                conn.execute(
                    "INSERT INTO app.sa_source_eval_decisions(decision_key,batch_id,group_id,"
                    "payload_sha256,payload) VALUES(%s,%s,%s,%s,%s)",
                    (decision.decision_key, batch_id, decision.group_id,
                     digest(value), Jsonb(value)),
                )
            conn.execute(
                "UPDATE app.router_batches SET status='applied' WHERE batch_id=%s", (batch_id,)
            )
            return [d.model_dump(mode="json") for d in decisions]

    def _failure(self, conn: Connection, batch_id: UUID, reason: str) -> None:
        self._batch(conn, batch_id)
        conn.execute(
            "UPDATE app.router_batches SET status=CASE WHEN status='usage_unknown' THEN status "
            "ELSE 'failed' END,reason_code=%s WHERE batch_id=%s AND status<>'applied'",
            (_code(reason), batch_id),
        )

    def failure(self, batch_id: UUID, reason: str) -> None:
        with self.engine.transaction() as conn:
            self._failure(conn, batch_id, reason)

    @staticmethod
    def _limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise _error("SOURCE_EVAL_LIMIT", 422)

    def preview(self, cookie: str, source_id: UUID, limit: int = 100) -> Row:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            self._source(conn, source_id)
            rows = conn.execute(
                "SELECT b.batch_id FROM app.sa_source_eval_snapshots s JOIN app.router_batches b "
                "USING(batch_id) WHERE s.source_instance_id=%s AND s.project_id=%s "
                "ORDER BY b.created_at DESC,b.batch_id LIMIT %s", (source_id, self.project, limit),
            ).fetchall()
            values = []
            for row in rows:
                batch = self._batch(conn, row["batch_id"])
                current, reason = True, None
                try:
                    self._checked(conn, batch)
                except DomainError as error:
                    current, reason = False, _code(error.code)
                values.append({"batch_id": batch["batch_id"], "status": batch["status"],
                               "snapshot_sha256": batch["context_hash"], "current": current,
                               "reason_code": reason, "eligible_for_execution": False})
            return {"evaluations": values, "executes_tasks": False}

    def decisions(self, cookie: str, source_id: UUID, limit: int = 100) -> list[Row]:
        self._limit(limit)
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            self._source(conn, source_id)
            return conn.execute(
                "SELECT d.* FROM app.sa_source_eval_decisions d JOIN "
                "app.sa_source_eval_snapshots s "
                "USING(batch_id) WHERE s.source_instance_id=%s AND s.project_id=%s "
                "ORDER BY d.created_at DESC,d.decision_key LIMIT %s",
                (source_id, self.project, limit),
            ).fetchall()

    def metrics(self, cookie: str, source_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            self._source(conn, source_id)
            batches = conn.execute(
                "SELECT b.status,count(*) AS n FROM app.sa_source_eval_snapshots s JOIN "
                "app.router_batches b USING(batch_id) WHERE s.source_instance_id=%s "
                "AND s.project_id=%s GROUP BY b.status", (source_id, self.project),
            ).fetchall()
            calls = conn.execute(
                "SELECT c.status,count(*) AS n,sum(c.settled_micros) AS settled,"
                "sum(c.reserved_micros) AS reserved FROM app.router_calls c JOIN "
                "app.sa_source_eval_snapshots s USING(batch_id) WHERE s.source_instance_id=%s "
                "AND s.project_id=%s GROUP BY c.status", (source_id, self.project),
            ).fetchall()
            return {"batches": {r["status"]: r["n"] for r in batches},
                    "recorded_model_attempts": sum(r["n"] for r in calls),
                    "known_billed_calls": sum(r["n"] for r in calls if r["status"] == "known"),
                    "unconfirmed_model_attempts": sum(r["n"] for r in calls
                                                      if r["status"] != "known"),
                    "settled_cost_micros": sum(r["settled"] or 0 for r in calls),
                    "held_micros": sum((r["reserved"] or 0) for r in calls
                                       if r["status"] != "known"),
                    "calls_by_status": {r["status"]: r["n"] for r in calls},
                    "executes_tasks": False, "external_business_writes": 0}


class SourceEvalProvider:
    """Explicit live composition. The model owns transport; original PG owns money."""

    mode = "live"

    def __init__(self, service: SourceEval, model: SourceEvalModel):
        self.service, self.model = service, model

    def propose(self, batch: Row) -> SourceProposals:
        service = self.service
        # A caller's outer transaction would turn our reservation into only a
        # savepoint, then allow a POST whose reservation can still be rolled back.
        with service.engine._mutex:
            if service.engine.connection.info.transaction_status != TransactionStatus.IDLE:
                raise _error("SOURCE_EVAL_TRANSACTION")
        budget = RouterBudget(service.engine)
        with service.engine.transaction() as conn:
            snapshot, context = service._checked(conn, batch)
            _, signed = service._control(conn, snapshot.scope.source_instance_id)
            for field in ("max_context_bytes", "max_output_bytes"):
                actual = getattr(self.model, field, None)
                if type(actual) is not int or not 1 <= actual <= getattr(signed, field):
                    raise _error("SOURCE_EVAL_MODEL_BOUND_MISMATCH", 403)
            result = conn.execute(
                "SELECT * FROM app.sa_source_eval_results WHERE batch_id=%s", (batch["batch_id"],),
            ).fetchone()
            if result is not None:
                if result["proposals"] is None:
                    raise _error("SOURCE_EVAL_KNOWN_RESULT_UNAVAILABLE")
                return SourceProposals.model_validate_json(json.dumps(result["proposals"]))
            if conn.execute(
                "SELECT 1 FROM app.router_calls WHERE batch_id=%s", (batch["batch_id"],),
            ).fetchone():
                raise _error("SOURCE_EVAL_CALL_ALREADY_RECORDED")
            if service._attempted(
                conn, tuple(i.input_id for g in snapshot.groups for i in g.inputs),
                snapshot.batch_id,
            ):
                raise _error("SOURCE_EVAL_INPUT_ALREADY_ATTEMPTED")
            request = render_request(snapshot, context)
            body = self.model.request_body(request)
            definition = PolicyDefinition.model_validate(
                service.engine.source_policies._current(conn, snapshot.policy_id)["definition"]
            )
            service._policy_budget(conn, definition, self.model.permit.profile.reservation_micros)
            call_id = budget.reserve(batch, self.model.permit, digest(body))

        @contextmanager
        def dispatch_guard(actual: SourceModelRequest, actual_body: Row) -> Iterator[None]:
            # Revocation uses this same Engine mutex. Keep it from the final current
            # authority check through HTTP dispatch, while committing PG before IO.
            with service.engine._mutex:
                if service.engine.connection.info.transaction_status != TransactionStatus.IDLE:
                    raise _error("SOURCE_EVAL_TRANSACTION")
                with service.engine.transaction() as conn:
                    current, context = service._checked(conn, batch)
                    fresh = render_request(current, context)
                    if (fresh.request_sha256 != actual.request_sha256
                            or actual.request_sha256 != request.request_sha256
                            or digest(actual_body) != digest(body)):
                        raise _error("SOURCE_EVAL_SNAPSHOT_STALE")
                    _, signed = service._control(conn, current.scope.source_instance_id)
                    for field in ("max_context_bytes", "max_output_bytes"):
                        bound = getattr(self.model, field, None)
                        if type(bound) is not int or not 1 <= bound <= getattr(signed, field):
                            raise _error("SOURCE_EVAL_MODEL_BOUND_MISMATCH", 403)
                    call = conn.execute(
                        "SELECT status FROM app.router_calls WHERE call_id=%s AND batch_id=%s "
                        "FOR UPDATE", (call_id, current.batch_id),
                    ).fetchone()
                    if call is None or call["status"] != "reserved":
                        raise _error("SOURCE_EVAL_CALL_ALREADY_RECORDED")
                    self.model.permit.validate(actual.snapshot_sha256)
                yield

        # The sole physical request is outside all PostgreSQL transactions. The model
        # enters this fence after final request construction, immediately before IO.
        try:
            with guard_source_send(dispatch_guard):
                outcome = self.model.propose(request)
        except DomainError as error:
            budget.settle(call_id, None)
            raise _error(_code(error.code)) from None
        except Exception:
            budget.settle(call_id, None)
            raise _error("SOURCE_EVAL_MODEL_UNKNOWN") from None
        with service.engine.transaction() as conn:
            known = outcome.known_usage and type(outcome.cost_micros) is int
            usage = {"prompt_tokens": outcome.input_tokens,
                     "completion_tokens": outcome.output_tokens} if known else None
            budget.settle(call_id, outcome.cost_micros if known else None, usage)
            if known:
                proposals = outcome.proposals if outcome.status == "ok" else None
                conn.execute(
                    "INSERT INTO app.sa_source_eval_results(batch_id,call_id,outcome_code,"
                    "proposals,usage_known,response_sha256) VALUES(%s,%s,%s,%s,true,%s)",
                    (snapshot.batch_id, call_id, _code(outcome.reason_code),
                     Jsonb(proposals.model_dump(mode="json")) if proposals is not None else None,
                     outcome.response_sha256),
                )
        # No authority error may roll back actual settlement. The worker's wrap/save
        # transactions revalidate the current snapshot before accepting these safe enums.
        if not known or outcome.status != "ok" or outcome.proposals is None:
            raise _error(_code(outcome.reason_code))
        return outcome.proposals
