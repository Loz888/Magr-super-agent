"""Source Coding authority on the original Engine connection.

Registration is a trusted Python port, never request data. Construction performs
no IO and starts no worker. Fixture engineering cannot enable the failed H12 gate.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.budget_domain import PriceQuote
from mgar_v3.super_agent.source_coding_domain import (
    CODING_VERSION,
    CodingAdmitRequest,
    CodingBudgetRef,
    CodingCancelRequest,
    CodingCurrentFacts,
    CodingGrant,
    CodingHarnessGates,
    CodingInspectRequest,
    CodingMandate,
    CodingRepository,
    CodingRunRequest,
    within,
)
from mgar_v3.super_agent.source_coding_rules import validate_coding_current
from mgar_v3.super_agent.source_scheduler_domain import (
    SchedulerAssignmentRequest,
    SchedulerEnqueueRequest,
    SchedulerQueueCancelRequest,
    SourceAuthorityFacts,
)

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.super_agent.source_correlation import _Input

Row = dict[str, Any]
H11_COMMIT = "d3f3457b3d9dfab68bc73a40add299e4e633fbeb"
H12_COMMIT = "88c75b8dac75da7e7d0c283a451c0ff5a3a3c9b0"


def _error(code: str, status: int = 403) -> DomainError:
    return DomainError(code, "Source Coding requires current original authority", status)


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class CodingRuntime(Protocol):
    def execute(self, bearer: str, request: CodingRunRequest) -> Row: ...


@dataclass(frozen=True)
class CodingProfile:
    """Server-installed, immutable fixture boundary; no provider is activated."""

    profile_sha256: str
    repository: CodingRepository
    grant: CodingGrant
    budget: CodingBudgetRef
    harness: CodingHarnessGates
    acceptance_sha256: str
    limit_micros: int = 0
    max_model_calls: int = 32


class SourceCoding:
    def __init__(
        self,
        engine: Engine,
        *,
        runtime: CodingRuntime | None = None,
        cipher: BodyCipher | None = None,
        payload_retention_seconds: int = 1800,
    ):
        if type(payload_retention_seconds) is not int or not 1 <= payload_retention_seconds <= 1800:
            raise _error("SOURCE_CODING_PAYLOAD_RETENTION_INVALID", 422)
        self.engine = engine
        self.runtime = runtime
        self.cipher = cipher
        self.payload_retention_seconds = payload_retention_seconds
        self._profiles: dict[str, CodingProfile] = {}

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    def _conn(self, conn: Connection) -> None:
        if conn is not self.engine.connection:
            raise _error("SOURCE_CODING_ORIGINAL_CONNECTION_REQUIRED")
        if conn.info.transaction_status != TransactionStatus.INTRANS:
            raise _error("SOURCE_CODING_ORIGINAL_TRANSACTION_REQUIRED")

    def now(self, conn: Connection) -> datetime:
        self._conn(conn)
        return self.engine.source_ingress.now(conn)

    def _console(self, conn: Connection, cookie: str) -> UUID:
        self._conn(conn)
        user = self.engine.source_correlations.identities._console(conn, cookie)
        ready = conn.execute("SELECT to_regclass('app.sa_coding_bindings') AS ready").fetchone()
        if ready is None or not ready["ready"]:
            raise _error("SOURCE_CODING_NOT_MIGRATED", 409)
        return user

    def register_profile(self, profile: CodingProfile) -> None:
        if not isinstance(profile, CodingProfile):
            raise _error("SOURCE_CODING_TRUSTED_PROFILE_REQUIRED")
        h11, h12 = profile.harness.h11, profile.harness.h12
        if (
            h11.commit != H11_COMMIT
            or h12.commit != H12_COMMIT
            or h11.engineering != "PASS"
            or h11.recovery != "PASS"
            or h12.engineering != "PASS"
            or h12.live_agent != "FAIL"
            or h12.mvp != "FAIL"
            or profile.repository.private
            or profile.repository.source_kind != "fixture_git"
            or profile.repository.model_disclosure != "controlled_fixture"
            or profile.repository.repository != self.engine.settings.repository.lower()
            or type(profile.limit_micros) is not int
            or not 0 <= profile.limit_micros <= 10**12
            or type(profile.max_model_calls) is not int
            or not 1 <= profile.max_model_calls <= 100
        ):
            raise _error("SOURCE_CODING_FIXTURE_ENGINEERING_ONLY")
        previous = self._profiles.get(profile.profile_sha256)
        if previous is not None and previous != profile:
            raise _error("SOURCE_CODING_PROFILE_IMMUTABLE", 409)
        self._profiles[profile.profile_sha256] = profile

    def _profile(self, mandate: CodingMandate) -> CodingProfile:
        if mandate.context_refs != (mandate.origin.input,):
            raise _error("SOURCE_CODING_SINGLE_ORIGIN_CONTEXT_REQUIRED")
        profile = self._profiles.get(mandate.worker_profile_sha256)
        if profile is None or (
            profile.repository != mandate.repository
            or profile.grant != mandate.grant
            or profile.budget != mandate.budget
            or profile.harness != mandate.harness
            or profile.acceptance_sha256 != mandate.acceptance_sha256
        ):
            raise _error("SOURCE_CODING_PROFILE_REQUIRED")
        return profile

    def budget_ref(
        self, conn: Connection, price: PriceQuote, profile_sha256: str
    ) -> CodingBudgetRef:
        self._conn(conn)
        configs = self.engine.project_budgets._configs(conn)
        if len(configs) != 2:
            raise _error("SOURCE_CODING_SHARED_BUDGET_REQUIRED")
        for _, definition in configs:
            self.engine.project_budgets._active(definition, self.now(conn))
            if price not in definition.prices:
                raise _error("SOURCE_CODING_PRICE_CHANGED")
        organization, project = configs
        return CodingBudgetRef(
            organization_id=organization[0]["scope_id"],
            organization_revision=organization[0]["revision"],
            project_revision=project[0]["revision"],
            profile_sha256=profile_sha256,
            price_version=price.price_version,
            price_sha256=digest(price.model_dump(mode="json")),
            limits_sha256=digest(
                [definition.caps.model_dump(mode="json") for _, definition in configs]
            ),
        )

    def _budget_current(self, conn: Connection, mandate: CodingMandate) -> bool:
        configs = self.engine.project_budgets._configs(conn)
        if len(configs) != 2:
            return False
        price = next(
            (
                price
                for price in configs[1][1].prices
                if price.price_version == mandate.budget.price_version
                and digest(price.model_dump(mode="json")) == mandate.budget.price_sha256
            ),
            None,
        )
        return (
            price is not None
            and self.budget_ref(conn, price, mandate.budget.profile_sha256) == mandate.budget
        )

    def capabilities(self, cookie: str) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return {
                "schema_version": CODING_VERSION,
                "engineering": "fixture_only",
                "profiles": sorted(self._profiles),
                "runtime_configured": self.runtime is not None,
                "live_agent": "FAIL",
                "mvp": "FAIL",
                "live_coding": "NOT_RUN",
                "paid": False,
                "publication": False,
            }

    def binding(self, conn: Connection, coding_id: UUID) -> Row:
        self._conn(conn)
        row = conn.execute(
            "SELECT * FROM app.sa_coding_bindings WHERE coding_id=%s AND project_id=%s FOR UPDATE",
            (coding_id, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_CODING_NOT_FOUND", 404)
        return row

    def _mandate(self, conn: Connection, run_id: UUID) -> tuple[Row, CodingMandate]:
        self._conn(conn)
        run = self.engine._run(conn, run_id)
        scope = conn.execute(
            "SELECT scope,actor,policy_version,execution_mode FROM app.mandates "
            "WHERE mandate_id=%s AND project_id=%s",
            (run["mandate_id"], self.project),
        ).fetchone()
        if (
            scope is None
            or run["graph_version"] != "source-coding-v1"
            or scope["policy_version"] != CODING_VERSION
            or run["execution_mode"] != "fixture"
            or scope["execution_mode"] != "fixture"
        ):
            raise _error("SOURCE_CODING_NEW_MANDATE_REQUIRED")
        try:
            mandate = CodingMandate.model_validate_json(json.dumps(scope["scope"]))
        except (ValueError, TypeError):
            raise _error("SOURCE_CODING_NEW_MANDATE_REQUIRED") from None
        target = mandate.target
        if (
            target.project_id != self.project
            or target.run_id != run_id
            or target.ticket_id != run["ticket_id"]
            or target.thread_id != run["thread_id"]
            or target.mandate_id != run["mandate_id"]
            or scope["actor"] != mandate.origin.engine_actor
        ):
            raise _error("SOURCE_CODING_TARGET_STALE")
        self._profile(mandate)
        return run, mandate

    def mandate(self, conn: Connection, coding_id: UUID) -> CodingMandate:
        """Frozen metadata for truthful late settlement, never current authority."""
        binding = self.binding(conn, coding_id)
        row = conn.execute(
            "SELECT scope FROM app.mandates WHERE mandate_id=%s AND project_id=%s",
            (binding["mandate_id"], self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_CODING_MANDATE_MISSING")
        return CodingMandate.model_validate_json(json.dumps(row["scope"]))

    def _source(self, conn: Connection, mandate: CodingMandate) -> _Input:
        source = self.engine.source_correlations._input(conn, mandate.origin.input.input_id)
        if (
            source.ref != mandate.origin.input
            or source.expires != mandate.origin.expires_at
            or source.expires < mandate.valid_until
        ):
            raise _error("SOURCE_CODING_SOURCE_CHANGED")
        identity = conn.execute(
            "SELECT i.*,p.engine_actor,b.roles,b.active AS roles_active "
            "FROM app.source_identities i JOIN app.source_principals p "
            "USING(principal_id,project_id) JOIN app.source_role_bindings b "
            "USING(identity_id,project_id) WHERE i.identity_id=%s AND i.project_id=%s "
            "AND b.conversation_key=%s",
            (
                source.row["identity_id"],
                self.project,
                source.item.event.source_thread.conversation_key,
            ),
        ).fetchone()
        policy = self.engine.source_policies._current(conn, source.row["policy_id"])
        if (
            identity is None
            or identity["identity_id"] != mandate.origin.identity_id
            or identity["revision"] != mandate.origin.identity_revision
            or identity["revoked_at"] is not None
            or identity["principal_id"] != mandate.origin.principal_id
            or identity["engine_actor"] != mandate.origin.engine_actor
            or not identity["roles_active"]
            or not {"context_provider", "requester"}.issubset(identity["roles"])
            or source.item.event.sender.actor_kind != "human"
            or source.row["context_only"]
            or policy["revoked"]
            or policy["policy_id"] != mandate.origin.policy_id
            or policy["current_revision"] != mandate.origin.policy_revision
            or policy["definition_sha256"] != mandate.origin.policy_sha256
        ):
            raise _error("SOURCE_CODING_IDENTITY_POLICY_FENCED")
        definition = source.definition
        if (
            definition.mode != "auto_repair"
            or not definition.receive.enabled
            or not definition.receive.model_input
            or not definition.admission.enabled
            or "explicit_request" not in definition.admission.request_kinds
            or not definition.execution.enabled
            or not {"read_repository", "edit_files", "run_tests"}.issubset(
                definition.execution.capabilities
            )
        ):
            raise _error("SOURCE_CODING_AUTO_REPAIR_REQUIRED")
        paths = self._policy_paths(source, mandate)
        if any(
            not within(path, paths)
            for path in (*mandate.grant.read_paths, *mandate.grant.write_paths)
        ):
            raise _error("SOURCE_CODING_PATH_POLICY_DENIED")
        if not self._budget_current(conn, mandate):
            raise _error("SOURCE_CODING_BUDGET_CHANGED")
        return source

    @staticmethod
    def _policy_paths(source: _Input, mandate: CodingMandate) -> tuple[str, ...]:
        boundary = next(
            (
                item
                for item in source.definition.execution.repositories
                if item.repository.lower() == mandate.repository.repository
                and item.base_sha == mandate.repository.base_commit
            ),
            None,
        )
        if boundary is None:
            raise _error("SOURCE_CODING_REPOSITORY_POLICY_DENIED")
        return boundary.paths

    def fresh_context(self, conn: Connection, mandate: CodingMandate) -> str:
        source = self._source(conn, mandate)
        if self.context_sha256(source) != mandate.context_sha256:
            raise _error("SOURCE_CODING_CONTEXT_CHANGED")
        return source.text

    @staticmethod
    def context_sha256(source: _Input) -> str:
        return digest(
            {
                "inputs": [source.ref.model_dump(mode="json")],
                "content_sha256": digest(source.clear.hex()),
            }
        )

    def managed(self, conn: Connection, run_id: UUID) -> bool:
        self._conn(conn)
        ready = conn.execute("SELECT to_regclass('app.sa_coding_bindings') AS ready").fetchone()
        return bool(
            ready
            and ready["ready"]
            and conn.execute(
                "SELECT 1 FROM app.sa_coding_bindings WHERE run_id=%s AND project_id=%s",
                (run_id, self.project),
            ).fetchone()
        )

    def source_authority(self, conn: Connection, run: Row, queue: Row) -> SourceAuthorityFacts:
        asynchronous = getattr(self.engine, "source_async", None)
        if asynchronous is not None:
            binding = conn.execute(
                "SELECT coding_id FROM app.sa_coding_bindings WHERE run_id=%s AND project_id=%s",
                (run["run_id"], self.project),
            ).fetchone()
            if binding is not None:
                asynchronous.check_managed_access(conn, binding["coding_id"])
        _, mandate = self._mandate(conn, run["run_id"])
        binding = self.binding(conn, mandate.coding_id)
        source = self._source(conn, mandate)
        if (
            binding["state"] != "admitted"
            or binding["revision"] != mandate.revision
            or binding["mandate_sha256"] != mandate.mandate_sha256
            or queue["input_id"] != source.ref.input_id
            or queue["source_instance_id"] != source.ref.source_instance_id
            or queue["version_fingerprint"] != source.ref.version_fingerprint
            or queue["source_state_sha256"] != source.ref.state_sha256
            or run["ticket_status"] != "open"
            or not mandate.valid_from <= self.now(conn) < mandate.valid_until
        ):
            raise _error("SOURCE_CODING_AUTHORITY_FENCED")
        return SourceAuthorityFacts(
            required=True,
            current=True,
            identity_current=True,
            receive_allowed=True,
            read_allowed=True,
            execution_allowed=True,
            origin_current=True,
            expires_at=mandate.valid_until,
        )

    def read_only(self, conn: Connection, assignment: Row) -> bool:
        run, _ = self._mandate(conn, assignment["run_id"])
        queue = self.engine.source_scheduler._queue_row(conn, assignment["queue_id"])
        self.source_authority(conn, run, queue)
        if assignment["state"] not in {"preparing", "active"}:
            raise _error("SOURCE_CODING_ASSIGNMENT_FENCED")
        return False

    def _create_original(self, conn: Connection, mandate: CodingMandate) -> None:
        """Trusted factory primitive. Caller must bind and queue before commit."""
        self._conn(conn)
        profile = self._profile(mandate)
        t = mandate.target
        source = self._source(conn, mandate)
        if source.native:
            # Native Issue/PR ownership needs the original correlation winner
            # factory. This initial explicit fixture factory cannot duplicate it.
            raise _error("SOURCE_CODING_RESOURCE_WINNER_FACTORY_REQUIRED")
        if conn.execute(
            "SELECT 1 FROM app.sa_coding_bindings WHERE project_id=%s AND input_id=%s",
            (self.project, mandate.origin.input.input_id),
        ).fetchone():
            raise _error("SOURCE_CODING_DUPLICATE_TARGET", 409)
        if t.project_id != self.project:
            raise _error("SOURCE_CODING_PROJECT_DENIED")
        conn.execute(
            "INSERT INTO app.threads(thread_id,project_id,repository,external_key,revision) "
            "VALUES(%s,%s,%s,%s,1)",
            (
                t.thread_id,
                self.project,
                mandate.repository.repository,
                "source-coding:" + str(mandate.origin.input.input_id),
            ),
        )
        conn.execute(
            "INSERT INTO app.mandates(mandate_id,project_id,actor,scope,policy_version,"
            "execution_mode) VALUES(%s,%s,%s,%s,%s,'fixture')",
            (
                t.mandate_id,
                self.project,
                mandate.origin.engine_actor,
                Jsonb(mandate.model_dump(mode="json")),
                CODING_VERSION,
            ),
        )
        conn.execute(
            "INSERT INTO app.tickets(ticket_id,mandate_id,thread_id,status) "
            "VALUES(%s,%s,%s,'open')",
            (t.ticket_id, t.mandate_id, t.thread_id),
        )
        conn.execute(
            "INSERT INTO app.runs(run_id,ticket_id,attempt_no,graph_version,execution_mode,"
            "status,question,input_revision) VALUES(%s,%s,1,'source-coding-v1','fixture',"
            "'queued','Reviewed Source Coding target',1)",
            (t.run_id, t.ticket_id),
        )
        conn.execute(
            "INSERT INTO app.budgets(mandate_id,limit_micros) VALUES(%s,%s)",
            (t.mandate_id, profile.limit_micros),
        )
        original = conn.execute(
            "SELECT limit_micros FROM app.authorization_budgets WHERE profile_hash=%s",
            (mandate.budget.profile_sha256,),
        ).fetchone()
        if original is None:
            conn.execute(
                "INSERT INTO app.authorization_budgets(profile_hash,limit_micros,call_limit) "
                "VALUES(%s,%s,%s)",
                (
                    mandate.budget.profile_sha256,
                    max(1, profile.limit_micros),
                    profile.max_model_calls,
                ),
            )
        elif original["limit_micros"] != max(1, profile.limit_micros):
            raise _error("SOURCE_CODING_BUDGET_PROFILE_CONFLICT")

    def create_target(self, cookie: str, mandate: CodingMandate) -> Row:
        """Explicit trusted server port; never mounted as a client API."""
        if not isinstance(mandate, CodingMandate):
            raise _error("SOURCE_CODING_TYPED_TARGET_REQUIRED")
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            if mandate.signed_by != user:
                raise _error("SOURCE_CODING_SIGNER_DENIED")
            self._create_original(conn, mandate)
            run = self.engine._run(conn, mandate.target.run_id)
            return self.admit(
                cookie,
                CodingAdmitRequest(
                    request_id=uuid4(),
                    run_id=run["run_id"],
                    expected_epoch=0,
                    source_input=mandate.origin.input,
                    expected_policy_revision=mandate.origin.policy_revision,
                    expected_revision=0,
                    target_sha256=self.engine.source_scheduler._target_hash(conn, run),
                    profile_sha256=mandate.worker_profile_sha256,
                    review_ref="SA13-trusted-target",
                ),
            )

    def admit(self, cookie: str, request: CodingAdmitRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            previous = conn.execute(
                "SELECT * FROM app.sa_coding_bindings WHERE project_id=%s AND request_id=%s",
                (self.project, request.request_id),
            ).fetchone()
            request_hash = digest(request.model_dump(mode="json"))
            if previous:
                if previous["request_sha256"] != request_hash:
                    raise _error("SOURCE_CODING_IDEMPOTENCY_CONFLICT", 409)
                return self._view(previous)
            run, mandate = self._mandate(conn, request.run_id)
            self._source(conn, mandate)
            self.fresh_context(conn, mandate)
            if (
                mandate.signed_by != user
                or mandate.origin.input != request.source_input
                or mandate.origin.policy_revision != request.expected_policy_revision
                or request.expected_revision != 0
                or mandate.revision != 1
                or request.expected_epoch != 0
                or run["execution_epoch"] != 0
                or run["status"] != "queued"
                or run["ticket_status"] != "open"
                or request.profile_sha256 != mandate.worker_profile_sha256
                or request.target_sha256 != self.engine.source_scheduler._target_hash(conn, run)
                or not mandate.valid_from <= self.now(conn) < mandate.valid_until
            ):
                raise _error("SOURCE_CODING_ADMISSION_STALE", 409)
            if (
                conn.execute(
                    "SELECT 1 FROM app.usage_calls WHERE run_id=%s", (request.run_id,)
                ).fetchone()
                or conn.execute(
                    "SELECT 1 FROM app.sa_coding_bindings WHERE project_id=%s AND input_id=%s",
                    (self.project, mandate.origin.input.input_id),
                ).fetchone()
            ):
                raise _error("SOURCE_CODING_DUPLICATE_TARGET", 409)
            origin, target = mandate.origin, mandate.target
            values = {
                "coding_id": mandate.coding_id,
                "project_id": self.project,
                "thread_id": target.thread_id,
                "ticket_id": target.ticket_id,
                "run_id": target.run_id,
                "mandate_id": target.mandate_id,
                "mandate_revision": mandate.revision,
                "request_id": request.request_id,
                "request_sha256": request_hash,
                "execution_mode": "fixture",
                "source_instance_id": origin.input.source_instance_id,
                "input_id": origin.input.input_id,
                "version_fingerprint": origin.input.version_fingerprint,
                "source_state_sha256": origin.input.state_sha256,
                "identity_id": origin.identity_id,
                "identity_revision": origin.identity_revision,
                "principal_id": origin.principal_id,
                "engine_actor": origin.engine_actor,
                "policy_id": origin.policy_id,
                "policy_revision": origin.policy_revision,
                "policy_sha256": origin.policy_sha256,
                "mandate_sha256": mandate.mandate_sha256,
                "profile_sha256": mandate.worker_profile_sha256,
                "context_sha256": mandate.context_sha256,
                "acceptance_sha256": mandate.acceptance_sha256,
                "review_sha256": mandate.review_sha256,
                "signed_by": user,
                "valid_from": mandate.valid_from,
                "valid_until": mandate.valid_until,
            }
            columns = ",".join(values)
            placeholders = ",".join("%s" for _ in values)
            row = conn.execute(
                "INSERT INTO app.sa_coding_bindings("
                + columns
                + ") VALUES("
                + placeholders
                + ") RETURNING *",
                tuple(values.values()),
            ).fetchone()
            assert row is not None
            self.event(conn, row, "CODING_ADMITTED", state="admitted")
            self.engine.source_scheduler.enqueue(
                cookie,
                SchedulerEnqueueRequest(
                    request_id=request.request_id,
                    run_id=request.run_id,
                    expected_epoch=0,
                    target_sha256=request.target_sha256,
                    resources=mandate.grant.resources,
                    source_input=origin.input,
                    review_ref=request.review_ref,
                ),
            )
            return self._view(row)

    @staticmethod
    def _view(row: Row) -> Row:
        return {
            key: value
            for key, value in row.items()
            if key not in {"engine_actor", "review_sha256", "signed_by"}
        }

    def get(self, cookie: str, coding_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return self._view(self.binding(conn, coding_id))

    def current(
        self,
        conn: Connection,
        bearer: str,
        request: CodingRunRequest,
        *,
        tree_sha256: str | None = None,
    ) -> tuple[CodingMandate, CodingCurrentFacts]:
        binding = self.binding(conn, request.coding_id)
        run, mandate = self._mandate(conn, request.run_id)
        source = self._source(conn, mandate)
        self.fresh_context(conn, mandate)
        scheduler = self.engine.source_scheduler
        session = scheduler._session(conn, request.session_id, bearer)
        assignment = scheduler._assignment(conn, request.assignment_id)
        scheduler._authority(
            conn,
            assignment,
            SchedulerAssignmentRequest(
                assignment_id=request.assignment_id,
                session_id=request.session_id,
                epoch=request.epoch,
                expected_revision=assignment["revision"],
            ),
        )
        queue = scheduler._queue_row(conn, assignment["queue_id"])
        self.source_authority(conn, run, queue)
        config = scheduler._configuration(conn, active=True)
        if (
            binding["state"] != "admitted"
            or binding["revision"] != mandate.revision
            or binding["mandate_sha256"] != mandate.mandate_sha256
            or assignment["run_id"] != request.run_id
            or assignment["session_id"] != request.session_id
            or assignment["mandate_id"] != mandate.target.mandate_id
            or assignment["config_revision"] != config.revision
        ):
            raise _error("SOURCE_CODING_ASSIGNMENT_FENCED")
        facts = CodingCurrentFacts(
            target=mandate.target,
            origin=mandate.origin,
            repository=mandate.repository,
            budget=mandate.budget,
            mandate_sha256=mandate.mandate_sha256,
            coding_revision=binding["revision"],
            profile_sha256=session["profile_sha256"],
            context_sha256=mandate.context_sha256,
            harness=mandate.harness,
            assignment_id=assignment["assignment_id"],
            session_id=session["session_id"],
            workspace_id=assignment["workspace_id"],
            epoch=assignment["execution_epoch"],
            run_epoch=run["execution_epoch"],
            assignment_epoch=assignment["execution_epoch"],
            assignment_created_at=assignment["created_at"],
            lease_until=assignment["lease_until"],
            session_expires_at=session["expires_at"],
            configuration_expires_at=config.configuration.valid_until,
            current_tree_sha256=tree_sha256 or mandate.repository.materialized_tree_sha256,
            environment_sha256=mandate.grant.environment_sha256,
            mode="fixture",
            original_scope_kind=CODING_VERSION,
            original_read_only=False,
            source_mode="auto_repair",
            run_state=run["status"],
            assignment_state=assignment["state"],
            source_current=True,
            identity_current=True,
            policy_current=True,
            canonical_current=True,
            admission_allowed=True,
            model_input_allowed=True,
            execution_allowed=True,
            budget_current=True,
            snapshot_current=True,
            target_current=True,
            runtime_profile_current=True,
            assignment_current=True,
            session_current=True,
            policy_read_paths=self._policy_paths(source, mandate),
            policy_write_paths=self._policy_paths(source, mandate),
            policy_commands=mandate.grant.allowed_commands,
        )
        validate_coding_current(mandate, facts, request, at=self.now(conn))
        return mandate, facts

    @contextmanager
    def guard(
        self, bearer: str, request: CodingRunRequest, *, tree_sha256: str | None = None
    ) -> Iterator[tuple[CodingMandate, CodingCurrentFacts]]:
        with self.engine._mutex:
            with self.engine.transaction() as conn:
                values = self.current(conn, bearer, request, tree_sha256=tree_sha256)
            yield values
            with self.engine.transaction() as conn:
                self.current(conn, bearer, request, tree_sha256=tree_sha256)

    def attempt(self, conn: Connection, request: CodingRunRequest) -> Row:
        self._conn(conn)
        row = conn.execute(
            "SELECT * FROM app.sa_coding_attempts WHERE coding_id=%s AND "
            "assignment_id=%s AND project_id=%s FOR UPDATE",
            (request.coding_id, request.assignment_id, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_CODING_ATTEMPT_REQUIRED")
        return row

    def begin_attempt(self, conn: Connection, bearer: str, request: CodingRunRequest) -> Row:
        mandate, facts = self.current(conn, bearer, request)
        row = conn.execute(
            "SELECT * FROM app.sa_coding_attempts WHERE assignment_id=%s "
            "AND project_id=%s FOR UPDATE",
            (request.assignment_id, self.project),
        ).fetchone()
        fingerprint = digest(request.model_dump(mode="json"))
        if row is not None:
            if row["request_sha256"] != fingerprint or row["state"] != "running":
                raise _error("SOURCE_CODING_ATTEMPT_FENCED")
            return row
        assignment = self.engine.source_scheduler._assignment(conn, request.assignment_id)
        row = conn.execute(
            "INSERT INTO app.sa_coding_attempts(attempt_id,coding_id,project_id,"
            "run_id,assignment_id,session_id,execution_epoch,workspace_id,workspace_sha256,"
            "profile_sha256,initial_tree_sha256,environment_sha256,request_id,request_sha256) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                uuid4(),
                request.coding_id,
                self.project,
                request.run_id,
                request.assignment_id,
                request.session_id,
                request.epoch,
                facts.workspace_id,
                assignment["workspace_sha256"],
                mandate.worker_profile_sha256,
                mandate.repository.materialized_tree_sha256,
                mandate.grant.environment_sha256,
                request.request_id,
                fingerprint,
            ),
        ).fetchone()
        assert row is not None
        binding = self.binding(conn, request.coding_id)
        self.event(conn, binding, "ATTEMPT_READY", attempt_id=row["attempt_id"], state="ready")
        row = conn.execute(
            "UPDATE app.sa_coding_attempts SET state='running',revision=revision+1,"
            "updated_at=clock_timestamp() WHERE attempt_id=%s RETURNING *",
            (row["attempt_id"],),
        ).fetchone()
        assert row is not None
        self.event(conn, binding, "ATTEMPT_RUNNING", attempt_id=row["attempt_id"], state="running")
        return row

    def event(
        self,
        conn: Connection,
        binding: Row,
        event_type: str,
        *,
        attempt_id: UUID | None = None,
        operation_id: UUID | None = None,
        reason_code: str | None = None,
        **details: Any,
    ) -> None:
        self._conn(conn)
        conn.execute(
            "INSERT INTO app.sa_coding_events(coding_id,project_id,attempt_id,"
            "operation_id,revision,event_type,reason_code,details) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                binding["coding_id"],
                self.project,
                attempt_id,
                operation_id,
                binding["revision"],
                event_type,
                reason_code,
                Jsonb(_json(details)),
            ),
        )

    def run(self, bearer: str, request: CodingRunRequest) -> Row:
        with self.engine.transaction() as conn:
            self.current(conn, bearer, request)
            asynchronous = getattr(self.engine, "source_async", None)
            if asynchronous is not None and asynchronous.managed(conn, request.coding_id):
                raise _error("SOURCE_ASYNC_EXPLICIT_START_REQUIRED")
        if self.runtime is None:
            raise _error("SOURCE_CODING_RUNTIME_REQUIRED")
        return self.runtime.execute(bearer, request)

    def state(self, bearer: str, request: CodingInspectRequest) -> Row:
        with self.engine.transaction() as conn:
            binding = self.binding(conn, request.coding_id)
            if binding["run_id"] != request.run_id:
                raise _error("SOURCE_CODING_TARGET_STALE")
            row = conn.execute(
                "SELECT * FROM app.sa_scheduler_assignments WHERE run_id=%s "
                "AND project_id=%s ORDER BY created_at DESC LIMIT 1",
                (request.run_id, self.project),
            ).fetchone()
            if row is None:
                raise _error("SOURCE_CODING_ASSIGNMENT_REQUIRED")
            check = CodingRunRequest(
                request_id=uuid4(),
                coding_id=request.coding_id,
                run_id=request.run_id,
                assignment_id=row["assignment_id"],
                session_id=row["session_id"],
                epoch=row["execution_epoch"],
                expected_revision=1,
            )
            self.current(conn, bearer, check)
            attempts = conn.execute(
                "SELECT attempt_id,state,revision FROM app.sa_coding_attempts "
                "WHERE coding_id=%s ORDER BY created_at DESC LIMIT 10",
                (request.coding_id,),
            ).fetchall()
            return {
                "coding_id": request.coding_id,
                "run_id": request.run_id,
                "state": binding["state"],
                "revision": binding["revision"],
                "attempts": attempts,
            }

    def _fence(self, conn: Connection, binding: Row, reason: str) -> Row:
        attempts = conn.execute(
            "UPDATE app.sa_coding_attempts SET state='stopped',"
            "revision=revision+1,fenced_at=clock_timestamp(),reason_code=%s,"
            "updated_at=clock_timestamp() WHERE coding_id=%s AND state IN ('ready','running') "
            "RETURNING *",
            (reason, binding["coding_id"]),
        ).fetchall()
        for row in attempts:
            self.event(
                conn,
                binding,
                "ATTEMPT_STOPPED",
                attempt_id=row["attempt_id"],
                state="stopped",
                reason_code=reason,
            )
        operations = conn.execute(
            "UPDATE app.sa_coding_operations SET state='unknown',"
            "revision=revision+1,updated_at=clock_timestamp() WHERE coding_id=%s "
            "AND state IN ('queued','running') RETURNING *",
            (binding["coding_id"],),
        ).fetchall()
        for row in operations:
            self.event(
                conn,
                binding,
                "OPERATION_UNKNOWN",
                attempt_id=row["attempt_id"],
                operation_id=row["operation_id"],
                state="unknown",
                reason_code=reason,
            )
        payloads = conn.execute(
            "UPDATE app.sa_coding_payloads SET ciphertext=NULL,"
            "unavailable_at=clock_timestamp(),reason_code=%s,revision=revision+1,"
            "updated_at=clock_timestamp() WHERE coding_id=%s AND unavailable_at IS NULL "
            "RETURNING *",
            (reason, binding["coding_id"]),
        ).fetchall()
        for row in payloads:
            self.event(
                conn,
                binding,
                "PAYLOAD_ERASED",
                attempt_id=row["attempt_id"],
                operation_id=row["operation_id"],
                payload_kind=row["payload_kind"],
                reason_code=reason,
            )
        return {
            "attempts_stopped": len(attempts),
            "operations_unknown": len(operations),
            "payloads_erased": len(payloads),
        }

    def maintain(
        self,
        *,
        coding_id: UUID | None = None,
        limit: int = 128,
        after: UUID | None = None,
    ) -> Row:
        """Explicit server maintenance, without resource/model IO or settlement.

        Commit content erasure before a caller enters a transaction which may
        reject access. The cursor bounds each pass without starving later IDs.
        This Python port is deliberately absent from the public metadata API.
        """
        if (
            type(limit) is not int
            or not 1 <= limit <= 256
            or (coding_id is not None and not isinstance(coding_id, UUID))
            or (after is not None and not isinstance(after, UUID))
        ):
            raise _error("SOURCE_CODING_MAINTENANCE_SCOPE_INVALID", 422)
        totals: Row = {
            "bindings_checked": 0,
            "bindings_fenced": 0,
            "attempts_stopped": 0,
            "operations_unknown": 0,
            "payloads_erased": 0,
            "next_after": None,
        }
        with self.engine._mutex:
            if self.engine.connection.info.transaction_status != TransactionStatus.IDLE:
                raise _error("SOURCE_CODING_MAINTENANCE_OUTSIDE_TRANSACTION_REQUIRED", 409)
            with self.engine.transaction() as conn:
                ready = conn.execute(
                    "SELECT to_regclass('app.sa_coding_bindings') AS ready"
                ).fetchone()
                if not ready or not ready["ready"]:
                    return totals
                bindings = conn.execute(
                    "SELECT * FROM app.sa_coding_bindings WHERE project_id=%s "
                    "AND (%s::uuid IS NULL OR coding_id=%s::uuid) "
                    "AND (%s::uuid IS NULL OR coding_id>%s::uuid) "
                    "ORDER BY coding_id LIMIT %s FOR UPDATE",
                    (self.project, coding_id, coding_id, after, after, limit),
                ).fetchall()
                totals["bindings_checked"] = len(bindings)
                if len(bindings) == limit:
                    totals["next_after"] = bindings[-1]["coding_id"]
                for binding in bindings:
                    invalid = binding["state"] != "admitted"
                    if not invalid:
                        try:
                            run, _ = self._mandate(conn, binding["run_id"])
                            self.source_authority(conn, run, binding)
                            invalid = run["status"] not in {"queued", "running"}
                            stale = conn.execute(
                                "SELECT 1 FROM app.sa_coding_attempts a WHERE a.coding_id=%s "
                                "AND a.state IN ('ready','running') AND NOT app.sa_coding_current("
                                "a.coding_id,a.run_id,a.assignment_id,"
                                "a.session_id,a.execution_epoch) "
                                "LIMIT 1",
                                (binding["coding_id"],),
                            ).fetchone()
                            invalid = invalid or stale is not None
                        except (DomainError, ValueError, TypeError):
                            invalid = True
                    if invalid:
                        changed = self._fence(conn, binding, "CURRENT_AUTHORITY_LOST")
                        totals["bindings_fenced"] += int(any(changed.values()))
                        for name, value in changed.items():
                            totals[name] += value
                        continue
                    payloads = conn.execute(
                        "UPDATE app.sa_coding_payloads SET ciphertext=NULL,"
                        "unavailable_at=clock_timestamp(),reason_code='TTL_EXPIRED',"
                        "revision=revision+1,updated_at=clock_timestamp() WHERE coding_id=%s "
                        "AND unavailable_at IS NULL AND expires_at<=clock_timestamp() RETURNING *",
                        (binding["coding_id"],),
                    ).fetchall()
                    totals["payloads_erased"] += len(payloads)
                    for payload in payloads:
                        self.event(
                            conn,
                            binding,
                            "PAYLOAD_ERASED",
                            attempt_id=payload["attempt_id"],
                            operation_id=payload["operation_id"],
                            payload_kind=payload["payload_kind"],
                            reason_code="TTL_EXPIRED",
                        )
        return totals

    def cancel(self, cookie: str, request: CodingCancelRequest) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            row = self.binding(conn, request.coding_id)
            run = self.engine._run(conn, request.run_id)
            if (
                row["run_id"] != request.run_id
                or row["revision"] != request.expected_revision
                or row["state"] != "admitted"
                or run["execution_epoch"] != request.expected_epoch
            ):
                raise _error("SOURCE_CODING_REVISION_CONFLICT", 409)
            changed = conn.execute(
                "UPDATE app.sa_coding_bindings SET state='revoked',"
                "revision=revision+1,invalidated_at=clock_timestamp(),reason_code='OWNER_CANCELLED',"
                "updated_at=clock_timestamp() WHERE coding_id=%s RETURNING *",
                (request.coding_id,),
            ).fetchone()
            assert changed is not None
            self._fence(conn, changed, "OWNER_CANCELLED")
            queues = conn.execute(
                "SELECT queue_id,revision FROM app.sa_scheduler_queue "
                "WHERE run_id=%s AND state='queued'",
                (request.run_id,),
            ).fetchall()
            for queue in queues:
                self.engine.source_scheduler.cancel_queue(
                    cookie,
                    SchedulerQueueCancelRequest(
                        queue_id=queue["queue_id"],
                        expected_revision=queue["revision"],
                        review_ref=request.review_ref,
                    ),
                )
            for assignment in self.engine.source_scheduler._occupied(conn):
                if assignment["run_id"] == request.run_id:
                    self.engine.source_scheduler._drain(conn, assignment, "SOURCE_CODING_REVOKED")
            self.event(
                conn, changed, "CODING_REVOKED", state="revoked", reason_code="OWNER_CANCELLED"
            )
            return self._view(changed)

    def recover_conn(self, conn: Connection) -> None:
        """Restart fences intent; no unknown operation is dispatched or refunded."""
        self._conn(conn)
        ready = conn.execute("SELECT to_regclass('app.sa_coding_bindings') AS ready").fetchone()
        if not ready or not ready["ready"]:
            return
        for binding in conn.execute(
            "SELECT * FROM app.sa_coding_bindings WHERE project_id=%s", (self.project,)
        ).fetchall():
            self._fence(conn, binding, "CONTROLLER_RECOVERY")
