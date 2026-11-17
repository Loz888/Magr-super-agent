"""Owner-installed per-case profiles over the original trial/queue/Run authority."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.contracts import (
    BudgetLimits,
    CapabilityGrant,
    ExecutionRequest,
    VerificationRequirement,
)
from mgar_v3.harness.workspace import WorkspaceRegistry
from mgar_v3.super_agent.budget_domain import PriceQuote
from mgar_v3.super_agent.source_coding import H11_COMMIT, CodingProfile
from mgar_v3.super_agent.source_coding_domain import (
    CodingGrant,
    CodingHarnessGate,
    CodingHarnessGates,
    CodingRepository,
)
from mgar_v3.super_agent.source_coding_experiment import SourceCodingExperiment
from mgar_v3.super_agent.source_coding_runtime import (
    ExperimentalCodingModel,
    SourceCodingFixtureProfile,
    SourceCodingRuntime,
)
from mgar_v3.super_agent.source_coding_trial import (
    R05_FINGERPRINT,
    R05_SOURCE,
    CodingTrial,
    authorize_trial,
)
from mgar_v3.super_agent.source_scheduler_domain import ResourceRequest
from mgar_v3.super_agent.source_scheduler_runtime import (
    H03SchedulerResourceDriver,
    SchedulerRuntimeProfile,
)
from mgar_v3.super_agent.source_trial_limits import validate_owner_trial_limit

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine


def build_case_experiment(
    engine: Engine,
    cookie: str,
    *,
    source_id: UUID,
    actor_id: str,
    source_root: Path,
    root: Path,
    upstream_commit: str,
    upstream_tree: str,
    image_id: str,
    price: PriceQuote,
    model: ExperimentalCodingModel,
    expires: datetime,
    manifest_sha256: str,
    h12_evidence_sha256: str,
    objective: str,
    baseline_command: tuple[str, ...],
    read_paths: tuple[str, ...],
    write_paths: tuple[str, ...],
    working_directory: str = "v3",
    repository_id: int = 1438269299,
    maximum_project_micros: int = 2000000,
) -> SourceCodingExperiment:
    """Install one explicit Owner trial without replacing scheduler configuration.

    Caller supplies a frozen public spec. This does not submit, claim or run work,
    and never reads independent graders. No service API or model calls this port.
    """
    limit = validate_owner_trial_limit(maximum_project_micros)
    core, scheduler = engine.source_coding, engine.source_scheduler
    with engine.transaction() as conn:
        owner = core._console(conn, cookie)
        source = engine.source_correlations.identities.registry._instance(conn, source_id)
        scheduler._configuration(conn, active=True)
        identity = conn.execute(
            "SELECT p.engine_actor FROM app.source_identities i JOIN app.source_principals p "
            "USING(principal_id,project_id) WHERE i.source_instance_id=%s AND i.actor_id=%s "
            "AND i.actor_kind='human' AND i.revoked_at IS NULL",
            (source_id, actor_id),
        ).fetchone()
        policy = conn.execute(
            "SELECT * FROM app.source_policies WHERE source_instance_id=%s",
            (source_id,),
        ).fetchone()
        if identity is None or policy is None:
            raise DomainError("SOURCE_CASE_IDENTITY_REQUIRED", "Current owner required", 403)
        actor = identity["engine_actor"]
    registry = WorkspaceRegistry(root / "registry")
    registration = registry.register_local(source_root, actor=engine.settings.local_actor)
    grant = CapabilityGrant(
        actor=actor,
        workspace_id=registration.workspace_id,
        repository=engine.settings.repository,
        read_paths=read_paths,
        write_paths=write_paths,
        execute=True,
        network=False,
        allowed_commands=(baseline_command[0],),
        max_seconds=180,
    )
    lock = root / "image-lock.json"
    lock.write_text(json.dumps({"image_id": image_id}) + "\n")
    lock.chmod(0o600)
    driver = H03SchedulerResourceDriver(
        scheduler=scheduler,
        registry_root=registry.root,
        registration=registration,
        source_root=source_root,
        profile=SchedulerRuntimeProfile(
            actor=actor,
            repository=engine.settings.repository,
            grant=grant,
            request=ExecutionRequest(
                operation_id="sa22-case-trial", argv=baseline_command, timeout_seconds=60
            ),
            image_id=image_id,
            review_ref="SA22-frozen-public-case",
            provider_generation="source-owner-trial-v1",
            execution_mode="live",
        ),
        receipt_root=root / "resource-receipts",
        image_lock=lock,
    )
    lease = issue_lease(
        mode="investigate",
        actor=actor,
        workspace_id=registration.workspace_id,
        repository=engine.settings.repository,
        execution_epoch=1,
    )
    environment = IsolatedExecutionBroker(source_root, lease, image_id=image_id).environment_hash(
        lease
    )
    snapshot = registry.create_task_workspace(
        registration,
        "sa22-case-initial-snapshot",
        source_root=source_root,
    ).snapshot()
    runtime = SourceCodingFixtureProfile(
        driver=driver,
        model=model,
        experimental=True,
        objective=objective,
        requirements=(
            VerificationRequirement(
                requirement_id="frozen-public-case-check",
                command_contains=baseline_command,
                min_tests=1,
            ),
        ),
        baseline_command=baseline_command,
        price=price,
        limits=BudgetLimits(
            max_model_calls=8, max_tool_calls=24, max_cost_micros=300000, max_seconds=180
        ),
        working_directory=working_directory,
        reservation_micros=(
            32768 * price.input_micros_per_million + 8192 * price.output_micros_per_million + 999999
        )
        // 1000000,
        max_output_tokens=8192,
        reserved_tokens=40960,
    )
    trial_id = uuid4()
    repository = CodingRepository(
        repository=engine.settings.repository,
        repository_id=repository_id,
        source_kind="local_git_snapshot",
        base_commit=upstream_commit,
        upstream_tree=upstream_tree,
        materialized_commit=registration.base_commit,
        materialized_tree_sha256=snapshot.tree_hash,
        registration_sha256=registration.registration_hash,
        snapshot_sha256=digest(
            {"registration": registration.registration_hash, "tree": snapshot.tree_hash}
        ),
        model_disclosure="approved_live",
    )
    with engine.transaction() as conn:
        budget = core.budget_ref(conn, price, digest([manifest_sha256, "case-shared-budget"]))
        now = core.now(conn)
        profile = CodingProfile(
            profile_sha256=driver.profile_sha256,
            repository=repository,
            grant=CodingGrant(
                read_paths=read_paths,
                write_paths=write_paths,
                allowed_commands=(baseline_command[0],),
                resources=ResourceRequest(cpu_millis=1000, memory_mib=512),
                environment_sha256=environment,
                max_seconds=180,
            ),
            budget=budget,
            harness=CodingHarnessGates(
                h11=CodingHarnessGate(
                    card="HV4-H11",
                    commit=H11_COMMIT,
                    source_sha256="a0cc8fac7467f7a99866df01c8ebea9f611f9844c0001c2d4130190fd9676cf2",
                    evidence_sha256="786481b3ceac91b3f3503154ee002d57fabc6df917985d88aaafd8eea0d511ac",
                    engineering="PASS",
                    recovery="PASS",
                ),
                h12=CodingHarnessGate(
                    card="HV4-H12",
                    commit=R05_SOURCE,
                    source_sha256=R05_FINGERPRINT,
                    evidence_sha256=h12_evidence_sha256,
                    engineering="PASS",
                    live_agent="FAIL",
                    mvp="FAIL",
                ),
            ),
            acceptance_sha256=runtime.acceptance_sha256,
            limit_micros=limit,
            max_model_calls=100,
            trial_id=trial_id,
        )
        authorize_trial(
            engine,
            cookie,
            CodingTrial(
                trial_id=trial_id,
                project_id=engine.settings.project_id,
                source_instance_id=source_id,
                source_revision=source.revision,
                policy_id=policy["policy_id"],
                policy_revision=policy["current_revision"],
                profile_sha256=profile.profile_sha256,
                repository=repository.repository,
                base_commit=upstream_commit,
                registration_sha256=registration.registration_hash,
                h12_commit=R05_SOURCE,
                h12_source_sha256=R05_FINGERPRINT,
                h12_evidence_sha256=h12_evidence_sha256,
                manifest_sha256=manifest_sha256,
                signed_by=owner,
                review_sha256=digest(["SA22-case", manifest_sha256]),
                maximum_project_micros=limit,
                valid_from=now,
                valid_until=expires,
            ),
        )
        core.register_profile(profile)
    return SourceCodingExperiment(engine, cookie, profile, runtime, registry, root / "artifacts")


def canonical_request(text: str) -> str:
    return " ".join(re.sub(r"@[_A-Za-z0-9-]+", "", text).split())


class SourceCaseExperiments:
    """Exact frozen input selection; only one execution swaps the trusted driver."""

    def __init__(
        self,
        engine: Engine,
        cases: dict[str, tuple[str, SourceCodingExperiment]],
        default: SourceCodingExperiment,
    ):
        if not cases or len(cases) > 5:
            raise ValueError("One to five frozen cases required")
        self.engine, self.cases, self.default = engine, cases, default
        self._lock = threading.Lock()
        messages = [canonical_request(message) for message, _ in cases.values()]
        hashes = [experiment.profile.profile_sha256 for _, experiment in cases.values()]
        if len(set(messages)) != len(messages) or len(set(hashes)) != len(hashes):
            raise ValueError("Frozen case inputs and profiles must be unambiguous")
        if any(experiment.engine is not engine for _, experiment in cases.values()):
            raise ValueError("Original Engine required for every case")

    def case_for(self, input_id: UUID) -> tuple[str, SourceCodingExperiment] | None:
        with self.engine.transaction() as conn:
            source = self.engine.source_correlations._input(conn, input_id)
            text = canonical_request(source.text)
        return next(
            (
                (case_id, experiment)
                for case_id, (message, experiment) in self.cases.items()
                if canonical_request(message) == text
            ),
            None,
        )

    def admit(self, input_id: UUID) -> dict[str, Any] | None:
        selected = self.case_for(input_id)
        return selected[1].submit(input_id) if selected else None

    def __call__(self, input_id: UUID) -> str:
        with self._lock:
            selected = self.case_for(input_id)
            experiment = selected[1] if selected else self.default
            # Admissions never replace these pointers. The execution lock and
            # existing one-worker capacity keep each live attempt on its own profile.
            self.engine.source_scheduler.driver = experiment.runtime_profile.driver
            self.engine.source_coding.runtime = SourceCodingRuntime(
                self.engine.source_coding, experiment.runtime_profile
            )
            return experiment(input_id)
