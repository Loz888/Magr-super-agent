"""Versioned source authorization on the sole Engine connection, never a task engine."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.domain import ActorRef, SourceThreadRef
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.identity_domain import IdentityCredential
from mgar_v3.super_agent.policy_domain import (
    Layer,
    LayerDecision,
    NotificationRequest,
    PolicyDefinition,
    PolicyFacts,
    PolicyPreview,
    PolicyRevoke,
    PolicySave,
)

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine, Row

T = TypeVar("T")
LAYERS: tuple[Layer, ...] = ("receive", "admission", "execution", "notification")
LEGACY_READ_PATH = "fixtures/orders/service.py"
READ_CAPABILITIES = {
    "read_repository",
    "read_knowledge",
    "search_knowledge",
    "recall_memory",
    "describe_tables",
    "query_readonly",
}


class SourcePolicies:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.identities = SourceIdentities(engine)

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    @staticmethod
    def _now(conn: Connection) -> datetime:
        row = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        assert row is not None
        value = row["now"]
        assert isinstance(value, datetime)
        return value

    def _audit(
        self,
        conn: Connection,
        policy_id: UUID,
        event: str,
        user: UUID | None = None,
        revision: int | None = None,
        **details: Any,
    ) -> None:
        conn.execute(
            "INSERT INTO app.source_policy_audit(project_id,policy_id,revision,"
            "actor_user_id,event,details) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (self.project, policy_id, revision, user, event, Jsonb(details)),
        )

    def _manage(
        self, cookie: str, policy_id: UUID, event: str, command: Callable[[Connection, UUID], T]
    ) -> T:
        try:
            with self.engine.transaction() as conn:
                user = self.identities._console(conn, cookie)
                result = command(conn, user)
                self._audit(conn, policy_id, event, user)
                return result
        except DomainError as exc:
            with self.engine.transaction() as conn:
                self._audit(conn, policy_id, event + "_denied", reason_code=exc.code)
            raise

    def _current(self, conn: Connection, policy_id: UUID) -> Row:
        row = conn.execute(
            "SELECT p.*,v.definition,v.definition_sha256,v.signed_by,v.signed_at,"
            "v.review_ref,v.revoked "
            "FROM app.source_policies p JOIN app.source_policy_versions v "
            "ON v.policy_id=p.policy_id AND v.revision=p.current_revision "
            "WHERE p.policy_id=%s AND p.project_id=%s FOR UPDATE OF p",
            (policy_id, self.project),
        ).fetchone()
        if row is None:
            raise DomainError("SOURCE_POLICY_NOT_FOUND", "Policy not found in this project", 404)
        if digest(row["definition"]) != row["definition_sha256"]:
            raise DomainError(
                "SOURCE_POLICY_HASH_INVALID", "Policy definition hash is invalid", 403
            )
        return row

    def _validate_source(self, conn: Connection, definition: PolicyDefinition) -> None:
        instance = self.identities.registry._instance(
            conn, definition.selector.scope.source_instance_id
        )
        if (
            instance.scope != definition.selector.scope
            or instance.revision != definition.selector.source_revision
        ):
            raise DomainError(
                "SOURCE_POLICY_SCOPE_STALE", "Exact current installation revision required", 409
            )
        if instance.scope.installation_id == "legacy-trigger-input-v1":
            raise DomainError(
                "SOURCE_POLICY_LEGACY_DENIED", "Legacy inputs cannot become source grants", 403
            )

    @staticmethod
    def _cas(actual: int, expected: int) -> None:
        if actual != expected:
            raise DomainError(
                "SOURCE_POLICY_REVISION_CONFLICT", "Policy changed; reload and preview again", 409
            )

    def save(self, cookie: str, policy_id: UUID, request: PolicySave) -> Row:
        def command(conn: Connection, user: UUID) -> Row:
            self._validate_source(conn, request.definition)
            exists = conn.execute(
                "SELECT 1 FROM app.source_policies WHERE policy_id=%s", (policy_id,)
            ).fetchone()
            if exists:
                old = self._current(conn, policy_id)
                self._cas(old["current_revision"], request.expected_revision)
                if old["revoked"]:
                    raise DomainError(
                        "SOURCE_POLICY_REVOKED",
                        "Revoked policy cannot be revived; sign a new policy",
                        409,
                    )
                if (
                    old["source_instance_id"]
                    != request.definition.selector.scope.source_instance_id
                ):
                    raise DomainError(
                        "SOURCE_POLICY_INSTALLATION_IMMUTABLE",
                        "Use a new policy for another installation",
                        409,
                    )
                revision = old["current_revision"] + 1
                conn.execute(
                    "UPDATE app.source_policies SET current_revision=%s WHERE policy_id=%s",
                    (revision, policy_id),
                )
            else:
                self._cas(0, request.expected_revision)
                revision = 1
                conn.execute(
                    "INSERT INTO app.source_policies(policy_id,project_id,"
                    "source_instance_id,current_revision) VALUES (%s,%s,%s,1)",
                    (policy_id, self.project, request.definition.selector.scope.source_instance_id),
                )
            definition = request.definition.model_dump(mode="json")
            conn.execute(
                "INSERT INTO app.source_policy_versions(policy_id,revision,definition,"
                "definition_sha256,signed_by,review_ref) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    policy_id,
                    revision,
                    Jsonb(definition),
                    digest(definition),
                    user,
                    request.review_ref,
                ),
            )
            self._audit(
                conn, policy_id, "policy_signed", user, revision, review_ref=request.review_ref
            )
            return self._current(conn, policy_id)

        return self._manage(cookie, policy_id, "policy_save", command)

    def revoke(self, cookie: str, policy_id: UUID, request: PolicyRevoke) -> Row:
        def command(conn: Connection, user: UUID) -> Row:
            old = self._current(conn, policy_id)
            self._cas(old["current_revision"], request.expected_revision)
            if old["revoked"]:
                raise DomainError("SOURCE_POLICY_REVOKED", "Policy is already revoked", 409)
            revision = old["current_revision"] + 1
            conn.execute(
                "UPDATE app.source_policies SET current_revision=%s WHERE policy_id=%s",
                (revision, policy_id),
            )
            conn.execute(
                "INSERT INTO app.source_policy_versions(policy_id,revision,definition,"
                "definition_sha256,signed_by,review_ref,revoked) "
                "VALUES (%s,%s,%s,%s,%s,%s,true)",
                (
                    policy_id,
                    revision,
                    Jsonb(old["definition"]),
                    old["definition_sha256"],
                    user,
                    request.review_ref,
                ),
            )
            self._audit(
                conn, policy_id, "policy_revoked", user, revision, review_ref=request.review_ref
            )
            return self._current(conn, policy_id)

        return self._manage(cookie, policy_id, "policy_revoke", command)

    def read(self, cookie: str, policy_id: UUID) -> Row:
        return self._manage(
            cookie, policy_id, "policy_read", lambda conn, user: self._current(conn, policy_id)
        )

    def history(self, cookie: str, policy_id: UUID) -> list[Row]:
        def query(conn: Connection, user: UUID) -> list[Row]:
            self._current(conn, policy_id)
            return conn.execute(
                "SELECT * FROM app.source_policy_versions WHERE policy_id=%s ORDER BY revision",
                (policy_id,),
            ).fetchall()

        return self._manage(cookie, policy_id, "policy_history_read", query)

    @staticmethod
    def _scope_matches(
        definition: PolicyDefinition, actor: ActorRef, thread: SourceThreadRef, facts: PolicyFacts
    ) -> bool:
        selector = definition.selector
        return (
            actor.scope == selector.scope == thread.scope
            and actor in selector.actors
            and thread.conversation_id in selector.conversations
            and (not selector.native_threads or thread.native_thread_id in selector.native_threads)
            and facts.event_kind in selector.event_kinds
        )

    @staticmethod
    def _decision(layer: Layer, allowed: bool, reason: str, **extra: Any) -> LayerDecision:
        return LayerDecision(layer=layer, allowed=allowed, reason_code=reason, **extra)

    def _evaluate(
        self,
        definition: PolicyDefinition,
        layer: Layer,
        actor: ActorRef,
        thread: SourceThreadRef,
        facts: PolicyFacts,
        roles: set[str],
        at: datetime,
        controlled: bool = False,
    ) -> LayerDecision:
        def deny(code: str) -> LayerDecision:
            return self._decision(layer, False, code)

        if not definition.valid_from <= at < definition.valid_until:
            return deny("SOURCE_POLICY_TIME")
        if not self._scope_matches(definition, actor, thread, facts):
            return deny("SOURCE_POLICY_SCOPE_DENIED")
        receive = definition.receive
        received = (
            receive.enabled
            and "context_provider" in roles
            and (not facts.historical or receive.allow_history_context)
        )
        if layer == "receive":
            return self._decision(
                layer,
                received,
                "RECEIVE_ALLOWED" if received else "RECEIVE_DENIED",
                retain_body=received and receive.retain_body,
                model_input=received and receive.model_input,
            )
        if layer == "notification":
            if definition.mode in {"disabled", "shadow"}:
                return deny("SOURCE_POLICY_" + definition.mode.upper())
            eligible = definition.notification.enabled and not any(
                q.contains(at) for q in definition.notification.quiet_hours
            )
            return self._decision(
                layer, False, "NOTIFICATION_PAYLOAD_REQUIRED", would_allow=eligible
            )
        if not received:
            return deny("RECEIVE_DENIED")
        admission = definition.admission
        admitted = (
            admission.enabled
            and facts.request_kind in admission.request_kinds
            and not facts.historical
        )
        if facts.request_kind == "explicit_request":
            admitted = admitted and "requester" in roles and actor.actor_kind == "human"
        elif facts.request_kind == "follow_up":
            admitted = (
                admitted
                and "task_controller" in roles
                and controlled
                and actor.actor_kind == "human"
            )
        # team_rule is authority from the exact owner-signed rule, not a context provider's words.
        if not admitted:
            return deny("ADMISSION_DENIED")
        if definition.mode == "disabled":
            return deny("SOURCE_POLICY_DISABLED")
        if layer == "admission":
            return self._decision(
                layer,
                definition.mode != "shadow",
                "SOURCE_POLICY_SHADOW" if definition.mode == "shadow" else "ADMISSION_ALLOWED",
                would_allow=True,
            )
        execution = definition.execution
        if not execution.enabled or facts.capability not in execution.capabilities:
            return deny("EXECUTION_CAPABILITY_DENIED")
        boundary = next(
            (
                r
                for r in execution.repositories
                if r.repository.lower() == (facts.repository or "").lower()
                and r.base_sha == facts.base_sha
            ),
            None,
        )
        if (
            boundary is None
            or facts.path is None
            or not any(facts.path == p or facts.path.startswith(p + "/") for p in boundary.paths)
        ):
            return deny("EXECUTION_SCOPE_DENIED")
        if (
            facts.capability == "network_access"
            and facts.network_host not in execution.network_hosts
        ):
            return deny("EXECUTION_NETWORK_DENIED")
        if facts.capability == "model_call":
            return deny(
                "SOURCE_SHARED_BUDGET_NOT_AVAILABLE"
            )  # SA07 must provide actual shared accounting.
        if definition.mode == "auto_investigate" and facts.capability in {
            "edit_files",
            "run_tests",
        }:
            return deny("EXECUTION_READ_ONLY")
        return self._decision(
            layer,
            definition.mode != "shadow",
            "SOURCE_POLICY_SHADOW" if definition.mode == "shadow" else "EXECUTION_ALLOWED",
            would_allow=True,
        )

    def _roles(
        self, conn: Connection, actor: ActorRef, thread: SourceThreadRef, ticket_id: UUID | None
    ) -> tuple[set[str], bool]:
        row = conn.execute(
            "SELECT b.binding_id,b.roles FROM app.source_role_bindings b JOIN "
            "app.source_identities i USING(identity_id,project_id) "
            "WHERE i.identity_key=%s AND i.project_id=%s AND i.revoked_at IS NULL AND "
            "b.active AND b.conversation_key=%s",
            (actor.key, self.project, thread.conversation_key),
        ).fetchone()
        if row is None:
            return set(), False
        controlled = (
            ticket_id is not None
            and conn.execute(
                "SELECT 1 FROM app.source_task_controllers WHERE binding_id=%s AND "
                "ticket_id=%s AND project_id=%s AND active",
                (row["binding_id"], ticket_id, self.project),
            ).fetchone()
            is not None
        )
        return set(row["roles"]), controlled

    def preview(self, cookie: str, policy_id: UUID, request: PolicyPreview) -> Row:
        # Read-only even for audit: preview never signs, saves, fences, or reserves.
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            self._validate_source(conn, request.definition)
            exists = conn.execute(
                "SELECT 1 FROM app.source_policies WHERE policy_id=%s", (policy_id,)
            ).fetchone()
            revision = self._current(conn, policy_id)["current_revision"] if exists else 0
            self._cas(revision, request.expected_revision)
            roles, controlled = self._roles(
                conn, request.actor, request.source_thread, request.facts.ticket_id
            )
            at = self._now(conn)
            counts = conn.execute(
                "SELECT count(*) AS associated_runs,count(*) FILTER (WHERE invalidated_at "
                "IS NULL) AS would_fence "
                "FROM app.source_run_policies WHERE policy_id=%s AND project_id=%s",
                (policy_id, self.project),
            ).fetchone()
            pending = conn.execute(
                "SELECT count(*) FILTER (WHERE a.status IN ('proposed','approved',"
                "'ready')) AS pending_actions,"
                "count(*) FILTER (WHERE a.status IN ('dispatching','outcome_unknown')) AS "
                "in_flight_or_unknown "
                "FROM app.actions a JOIN app.source_run_policies b USING(run_id) WHERE "
                "b.policy_id=%s AND b.project_id=%s",
                (policy_id, self.project),
            ).fetchone()
            tools = conn.execute(
                "SELECT count(*) FILTER (WHERE j.status='queued') AS queued_tools,"
                "count(*) FILTER (WHERE j.status='running') AS in_flight_tools "
                "FROM app.read_jobs j JOIN app.source_run_policies b USING(run_id) "
                "WHERE b.policy_id=%s AND b.project_id=%s",
                (policy_id, self.project),
            ).fetchone()
            leases = conn.execute(
                "SELECT count(*) AS unexpired_worker_leases FROM app.worker_heartbeats w "
                "JOIN app.source_run_policies b USING(run_id) WHERE b.policy_id=%s "
                "AND b.project_id=%s AND w.expires_at>clock_timestamp()",
                (policy_id, self.project),
            ).fetchone()
            return {
                "preview_only": True,
                "grants_authority": False,
                "expected_revision": revision,
                "definition_sha256": digest(request.definition.model_dump(mode="json")),
                "impact": {**(counts or {}), **(pending or {}), **(tools or {}), **(leases or {})},
                "decisions": {
                    layer: self._evaluate(
                        request.definition,
                        layer,
                        request.actor,
                        request.source_thread,
                        request.facts,
                        roles,
                        at,
                        controlled,
                    ).model_dump(mode="json")
                    for layer in LAYERS
                },
            }

    def evaluate(
        self,
        credential: IdentityCredential,
        policy_id: UUID,
        expected_revision: int,
        layer: Layer,
        facts: PolicyFacts,
    ) -> LayerDecision:
        with self.identities.authorized(credential, "provide_context"):
            with self.engine.transaction() as conn:
                row = self._current(conn, policy_id)
                self._cas(row["current_revision"], expected_revision)
                if row["revoked"]:
                    return self._decision(layer, False, "SOURCE_POLICY_REVOKED")
                definition = PolicyDefinition.model_validate(row["definition"])
                self._validate_source(conn, definition)
                proof, identity = self.identities._evidence(conn, credential.evidence_id)
                actor = ActorRef.model_validate(identity["actor_ref"])
                thread = SourceThreadRef.model_validate(proof["source_thread"])
                roles, controlled = self._roles(conn, actor, thread, facts.ticket_id)
                return self._evaluate(
                    definition, layer, actor, thread, facts, roles, self._now(conn), controlled
                )

    def bind_run(
        self,
        credential: IdentityCredential,
        policy_id: UUID,
        expected_revision: int,
        run_id: UUID,
        facts: PolicyFacts,
    ) -> None:
        """Trusted bridge only: restrict an existing fixture investigation; never create/upgrade it.

        SA04/SA13 must supply verified candidate metadata and the real worker bridge.
        No HTTP endpoint accepts this method's bearer or caller-proposed binding.
        """
        with self.identities.authorized(credential, "provide_context"):
            with self.engine.transaction() as conn:
                for layer in ("admission", "execution"):
                    decision = self.evaluate(credential, policy_id, expected_revision, layer, facts)
                    if not decision.allowed:
                        raise DomainError(
                            decision.reason_code,
                            "Current source policy does not authorize this binding",
                            403,
                        )
                run = self.engine._run(conn, run_id)
                if (
                    run["status"] != "queued"
                    or run["execution_mode"] != "fixture"
                    or run["graph_version"]
                    not in {"r1-quotes-v1", "r2-mailbox-v1", "r4-experts-v1"}
                ):
                    raise DomainError(
                        "SOURCE_POLICY_BRIDGE_UNAVAILABLE",
                        "This card only restricts existing queued fixture investigations",
                        403,
                    )
                if (
                    facts.repository is None
                    or facts.repository.lower() != run["repository"]
                    or facts.capability != "read_repository"
                    or facts.path != LEGACY_READ_PATH
                ):
                    raise DomainError(
                        "SOURCE_POLICY_RUN_SCOPE",
                        "Existing fixture source must match its exact repository/path",
                        403,
                    )
                row = self._current(conn, policy_id)
                mandate = conn.execute(
                    "SELECT scope FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
                ).fetchone()
                if (
                    mandate
                    and mandate["scope"].get("read_only")
                    and row["definition"]["mode"] == "auto_repair"
                ):
                    raise DomainError(
                        "SOURCE_POLICY_LEGACY_READ_ONLY",
                        "A new policy cannot upgrade an old read-only mandate",
                        403,
                    )
                proof, identity = self.identities._evidence(conn, credential.evidence_id)
                binding = conn.execute(
                    "SELECT binding_id FROM app.source_role_bindings WHERE identity_id=%s "
                    "AND conversation_key=%s AND active",
                    (identity["identity_id"], proof["conversation_key"]),
                ).fetchone()
                assert binding is not None
                if conn.execute(
                    "SELECT 1 FROM app.source_run_policies WHERE run_id=%s", (run_id,)
                ).fetchone():
                    raise DomainError(
                        "SOURCE_POLICY_RUN_IMMUTABLE",
                        "A Run cannot replace its frozen source authority",
                        409,
                    )
                conn.execute(
                    "INSERT INTO app.source_run_policies(run_id,ticket_id,thread_id,"
                    "policy_id,policy_revision,"
                    "project_id,identity_id,identity_revision,binding_id,evidence_id,facts) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        run_id,
                        run["ticket_id"],
                        run["thread_id"],
                        policy_id,
                        expected_revision,
                        self.project,
                        identity["identity_id"],
                        identity["revision"],
                        binding["binding_id"],
                        credential.evidence_id,
                        Jsonb(facts.model_dump(mode="json")),
                    ),
                )
                self._audit(
                    conn,
                    policy_id,
                    "source_run_bound",
                    revision=expected_revision,
                    run_id=str(run_id),
                )

    def _bound(self, conn: Connection, run_id: UUID) -> Row | None:
        return conn.execute(
            "SELECT * FROM app.source_run_policies WHERE run_id=%s AND project_id=%s",
            (run_id, self.project),
        ).fetchone()

    def _binding_definition(self, conn: Connection, binding: Row) -> PolicyDefinition:
        if binding["invalidated_at"] is not None:
            raise DomainError(
                "SOURCE_POLICY_FENCED", "Frozen source authority has been invalidated", 403
            )
        row = self._current(conn, binding["policy_id"])
        if row["revoked"] or row["current_revision"] != binding["policy_revision"]:
            raise DomainError(
                "SOURCE_POLICY_FENCED", "Current source policy differs from this Run", 403
            )
        definition = PolicyDefinition.model_validate(row["definition"])
        self._validate_source(conn, definition)
        identity = self.identities._identity(conn, binding["identity_id"])
        if (
            identity["revoked_at"] is not None
            or identity["revision"] != binding["identity_revision"]
        ):
            raise DomainError(
                "SOURCE_POLICY_IDENTITY_FENCED", "Source identity authority changed", 403
            )
        at = self._now(conn)
        if not definition.valid_from <= at < definition.valid_until:
            raise DomainError("SOURCE_POLICY_TIME", "Source policy is not currently effective", 403)
        return definition

    def check_run(
        self, conn: Connection, run: Row, capability: str | None = None, *, resume: bool = False
    ) -> None:
        binding = self._bound(conn, run["run_id"])
        if binding is None:
            return  # Byte-compatible old authorization semantics, never upgrade an old grant.
        definition = self._binding_definition(conn, binding)
        if resume:
            raise DomainError(
                "SOURCE_POLICY_REBIND_REQUIRED",
                "Source Run restart requires a new reviewed assignment",
                403,
            )
        facts = PolicyFacts.model_validate(binding["facts"])
        if capability is not None:
            if capability not in definition.execution.capabilities:
                raise DomainError(
                    "SOURCE_POLICY_TOOL_DENIED",
                    "This tool capability is outside the current grant",
                    403,
                )
            facts = PolicyFacts.model_validate(
                {**facts.model_dump(mode="json"), "capability": capability}
            )
        proof = conn.execute(
            "SELECT source_thread FROM app.source_identity_evidence WHERE evidence_id=%s",
            (binding["evidence_id"],),
        ).fetchone()
        identity = self.identities._identity(conn, binding["identity_id"])
        assert proof is not None
        actor = ActorRef.model_validate(identity["actor_ref"])
        thread = SourceThreadRef.model_validate(proof["source_thread"])
        roles, controlled = self._roles(conn, actor, thread, facts.ticket_id)
        decision = self._evaluate(
            definition, "execution", actor, thread, facts, roles, self._now(conn), controlled
        )
        if not decision.allowed:
            raise DomainError(
                decision.reason_code, "Current source execution policy denied the operation", 403
            )

    def finish_inflight_read(
        self, conn: Connection, job: Row, content_hash: str, byte_count: int, uri: str, version: str
    ) -> Row | None:
        binding = self._bound(conn, job["run_id"])
        if binding is None:
            return None
        try:
            self._binding_definition(conn, binding)
            return None
        except DomainError:
            # Receipt-only path: no Mailbox delivery, new permission, or execution revival.
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) ON "
                "CONFLICT DO NOTHING",
                (content_hash, byte_count),
            )
            result = conn.execute(
                "UPDATE app.read_jobs SET status='succeeded',lease_token=NULL,"
                "content_hash=%s,source_uri=%s,source_version=%s WHERE job_id=%s RETURNING *",
                (content_hash, uri, version, job["job_id"]),
            ).fetchone()
            self.engine._event(
                conn,
                job["run_id"],
                "source_late_tool_result",
                job_id=str(job["job_id"]),
                content_hash=content_hash,
            )
            return result

    def _notification(self, conn: Connection, run: Row, request: NotificationRequest) -> Row:
        binding = self._bound(conn, run["run_id"])
        if binding is None:
            raise DomainError(
                "SOURCE_POLICY_NOTIFICATION_UNBOUND",
                "Notification needs a frozen source policy",
                403,
            )
        definition = self._binding_definition(conn, binding)
        grant = definition.notification
        if definition.mode in {"disabled", "shadow"} or not grant.enabled:
            raise DomainError(
                "SOURCE_POLICY_NOTIFICATION_DENIED",
                "Mode and independent notification grant must allow sending",
                403,
            )
        if any(q.contains(self._now(conn)) for q in grant.quiet_hours):
            raise DomainError(
                "SOURCE_POLICY_QUIET_HOURS", "Notification is currently inside quiet hours", 403
            )
        action = conn.execute(
            "SELECT * FROM app.actions WHERE action_id=%s AND run_id=%s",
            (request.action_id, run["run_id"]),
        ).fetchone()
        if action is None or action["action_type"] != "issue_comment":
            raise DomainError(
                "SOURCE_POLICY_PUBLICATION_DENIED",
                "Notification does not grant Draft PR publication",
                403,
            )
        body = action["payload"].get("body")
        if (
            not isinstance(body, str)
            or hashlib.sha256(body.encode()).hexdigest() != request.body_sha256
        ):
            raise DomainError(
                "SOURCE_POLICY_CONTENT_HASH", "Notification must bind the exact immutable body", 403
            )
        if (
            request.destination not in grant.destinations
            or request.destination.target != action["target"]
            or request.content_class not in grant.content_classes
            or request.data_class not in grant.data_classes
            or len(body.encode()) > grant.max_bytes
            or (
                request.destination.visibility == "public"
                and request.data_class != "public_summary"
            )
        ):
            raise DomainError(
                "SOURCE_POLICY_DISCLOSURE_DENIED",
                "Destination or content exceeds notification boundary",
                403,
            )
        return action

    def authorize_notification(self, run_id: UUID, request: NotificationRequest) -> None:
        """Policy fact only; existing exact Action approval/Outbox is still required."""
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id)
            action = self._notification(conn, run, request)
            existing = conn.execute(
                "SELECT * FROM app.source_notification_authorizations WHERE action_id=%s",
                (request.action_id,),
            ).fetchone()
            expected = request.model_dump(mode="json", exclude={"action_id"})
            if existing is not None:
                actual = {key: existing[key] for key in expected}
                if actual != expected or existing["payload_hash"] != action["payload_hash"]:
                    raise DomainError(
                        "SOURCE_POLICY_NOTIFICATION_CONFLICT",
                        "Action already has another notification boundary",
                        409,
                    )
                return
            binding = self._bound(conn, run_id)
            assert binding is not None
            definition = self._binding_definition(conn, binding)
            count = conn.execute(
                "SELECT count(*) AS n FROM app.source_notification_authorizations n JOIN "
                "app.source_run_policies b USING(run_id) "
                "WHERE b.policy_id=%s AND n.authorized_at>clock_timestamp()-interval '1 hour'",
                (binding["policy_id"],),
            ).fetchone()
            assert count is not None
            if count["n"] >= definition.notification.max_per_hour:
                raise DomainError(
                    "SOURCE_POLICY_NOTIFICATION_QUOTA",
                    "Shared policy notification allowance is exhausted",
                    403,
                )
            conn.execute(
                "INSERT INTO app.source_notification_authorizations(action_id,run_id,"
                "payload_hash,body_sha256,destination,content_class,data_class) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    request.action_id,
                    run_id,
                    action["payload_hash"],
                    request.body_sha256,
                    Jsonb(request.destination.model_dump(mode="json")),
                    request.content_class,
                    request.data_class,
                ),
            )
            self._audit(
                conn,
                binding["policy_id"],
                "notification_authorized",
                revision=binding["policy_revision"],
                action_id=str(request.action_id),
            )

    def check_action(self, conn: Connection, action: Row, run: Row) -> None:
        if self._bound(conn, run["run_id"]) is None:
            return
        saved = conn.execute(
            "SELECT * FROM app.source_notification_authorizations WHERE action_id=%s",
            (action["action_id"],),
        ).fetchone()
        if saved is None or saved["payload_hash"] != action["payload_hash"]:
            raise DomainError(
                "SOURCE_POLICY_NOTIFICATION_REQUIRED",
                "Independent exact notification authorization required",
                403,
            )
        request = NotificationRequest.model_validate(
            {
                "action_id": action["action_id"],
                **{
                    key: saved[key]
                    for key in ("destination", "content_class", "data_class", "body_sha256")
                },
            }
        )
        self._notification(conn, run, request)
        binding = self._bound(conn, run["run_id"])
        assert binding is not None
        definition = self._binding_definition(conn, binding)
        started = conn.execute(
            "SELECT count(*) AS n FROM app.source_notification_authorizations n "
            "JOIN app.source_run_policies b USING(run_id) WHERE b.policy_id=%s "
            "AND n.action_id<>%s AND EXISTS (SELECT 1 FROM app.run_events e "
            "WHERE e.run_id=n.run_id AND e.kind='action_dispatching' "
            "AND e.safe_payload->>'action_id'=n.action_id::text "
            "AND e.created_at>clock_timestamp()-interval '1 hour')",
            (binding["policy_id"], action["action_id"]),
        ).fetchone()
        assert started is not None
        if started["n"] >= definition.notification.max_per_hour:
            raise DomainError(
                "SOURCE_POLICY_NOTIFICATION_QUOTA", "Current dispatch window is exhausted", 403
            )

    def retain_inflight_outcome(self, conn: Connection, run: Row, status: str) -> bool:
        binding = self._bound(conn, run["run_id"])
        if binding is None:
            return False
        try:
            self._binding_definition(conn, binding)
            return False
        except DomainError:
            self.engine._event(conn, run["run_id"], "source_late_effect_outcome", status=status)
            return True  # Remote receipt is true; it is not permission to complete/revive the Run.

    def expire_authorities(self) -> int:
        """Trusted local maintenance; effective-time checks already deny every new operation."""
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT b.run_id FROM app.source_run_policies b JOIN app.source_policy_versions v "
                "ON b.policy_id=v.policy_id AND b.policy_revision=v.revision WHERE b.project_id=%s "
                "AND b.invalidated_at IS NULL AND "
                "(v.definition->>'valid_until')::timestamptz<=clock_timestamp()",
                (self.project,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "SELECT app.fence_source_run(%s,'SOURCE_POLICY_EXPIRED')", (row["run_id"],)
                )
            return len(rows)
