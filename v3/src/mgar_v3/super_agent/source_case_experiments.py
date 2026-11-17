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
    existing_trial: dict[str, Any] | None = None,
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
    if existing_trial is None:
        registration = registry.register_local(source_root, actor=engine.settings.local_actor)
    else:
        if (
            existing_trial["manifest_sha256"] != manifest_sha256
            or existing_trial["source_instance_id"] != source_id
            or existing_trial["maximum_project_micros"] != limit
            or existing_trial["valid_until"] != expires
        ):
            raise DomainError("SOURCE_CASE_RESTORE_FENCED", "Exact signed case required", 403)
        matches = [
            registry.load(path.parent.name)
            for path in registry.root.glob("*/registration.json")
            if registry.load(path.parent.name).registration_hash
            == existing_trial["registration_sha256"]
        ]
        if len(matches) != 1:
            raise DomainError("SOURCE_CASE_RESTORE_FENCED", "Exact registration required", 403)
        registration = matches[0]
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
    if existing_trial is None:
        lock.write_text(json.dumps({"image_id": image_id}) + "\n")
        lock.chmod(0o600)
    elif json.loads(lock.read_text()) != {"image_id": image_id}:
        raise DomainError("SOURCE_CASE_RESTORE_FENCED", "Original image lock required", 403)
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
    snapshot = (
        registry.create_task_workspace(
            registration, "sa22-case-initial-snapshot", source_root=source_root
        )
        if existing_trial is None
        else registry.open_task_workspace(registration, "sa22-case-initial-snapshot")
    ).snapshot()
    input_bound = getattr(model, "context_tokens", 32768)
    if type(input_bound) is not int or not 4096 <= input_bound <= 65536:
        raise DomainError("SOURCE_CASE_MODEL_INPUT_BOUND", "Bounded model context required", 403)
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
            input_bound * price.input_micros_per_million
            + 8192 * price.output_micros_per_million
            + 999999
        )
        // 1000000,
        max_output_tokens=8192,
        reserved_tokens=input_bound + 8192,
    )
    trial_id = existing_trial["trial_id"] if existing_trial is not None else uuid4()
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
        if existing_trial is None:
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
    text = " ".join(re.sub(r"@[_A-Za-z0-9-]+", "", text).split())
    # Native rich text can insert a space when a Chinese word crosses spans.
    # Preserve ASCII/token boundaries, punctuation, paths and all other text.
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    return text.removesuffix("。")


class SourceCaseExperiments:
    """Exact frozen input selection; only one execution swaps the trusted driver."""

    def __init__(
        self,
        engine: Engine,
        cases: dict[str, tuple[str, SourceCodingExperiment]],
        default: SourceCodingExperiment,
        *,
        reviewed_failed_bindings: dict[str, UUID | tuple[UUID, ...]] | None = None,
    ):
        if not cases or len(cases) > 5:
            raise ValueError("One to five frozen cases required")
        self.engine, self.cases, self.default = engine, cases, default
        self.reviewed_failed_bindings = {
            key: (value,) if isinstance(value, UUID) else value
            for key, value in (reviewed_failed_bindings or {}).items()
        }
        if not set(self.reviewed_failed_bindings) <= set(cases) or any(
            not isinstance(value, tuple)
            or not 1 <= len(value) <= 16
            or len(set(value)) != len(value)
            or any(not isinstance(item, UUID) for item in value)
            for value in self.reviewed_failed_bindings.values()
        ):
            raise ValueError("Only exact owner-reviewed prior case bindings may be retried")
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
        if selected is None:
            return None
        with self.engine.transaction() as conn:
            duplicates = conn.execute(
                "SELECT * FROM app.sa_coding_bindings WHERE project_id=%s "
                "AND profile_sha256=%s AND input_id<>%s",
                (self.engine.settings.project_id, selected[1].profile.profile_sha256, input_id),
            ).fetchall()
            reviewed = self.reviewed_failed_bindings.get(selected[0], ())
            for prior in duplicates:
                safe_retry = (
                    prior["coding_id"] in reviewed
                    and prior["state"] == "revoked"
                    and prior["reason_code"] == "OWNER_CANCELLED"
                    and prior["signed_by"]
                    == self.engine.source_coding._console(conn, selected[1].cookie)
                    and not conn.execute(
                        "SELECT 1 FROM app.usage_calls WHERE run_id=%s AND status<>'known' LIMIT 1",
                        (prior["run_id"],),
                    ).fetchone()
                    and not conn.execute(
                        "SELECT 1 FROM app.actions WHERE run_id=%s LIMIT 1",
                        (prior["run_id"],),
                    ).fetchone()
                    and not conn.execute(
                        "SELECT 1 FROM app.sa_coding_operations WHERE coding_id=%s "
                        "AND (state<>'known' OR capability='write') LIMIT 1",
                        (prior["coding_id"],),
                    ).fetchone()
                    and not conn.execute(
                        "SELECT 1 FROM app.sa_scheduler_assignments WHERE run_id=%s "
                        "AND state<>'released' LIMIT 1",
                        (prior["run_id"],),
                    ).fetchone()
                    and (
                        conn.execute(
                            "SELECT 1 FROM app.run_events WHERE run_id=%s "
                            "AND kind='source_trial_artifact_available' "
                            "AND safe_payload->'changed'='false'::jsonb LIMIT 1",
                            (prior["run_id"],),
                        ).fetchone()
                        is not None
                        or (
                            not conn.execute(
                                "SELECT 1 FROM app.sa_coding_attempts WHERE coding_id=%s LIMIT 1",
                                (prior["coding_id"],),
                            ).fetchone()
                            and not conn.execute(
                                "SELECT 1 FROM app.usage_calls WHERE run_id=%s LIMIT 1",
                                (prior["run_id"],),
                            ).fetchone()
                            and not conn.execute(
                                "SELECT 1 FROM app.sa_coding_operations WHERE coding_id=%s LIMIT 1",
                                (prior["coding_id"],),
                            ).fetchone()
                        )
                    )
                )
                if safe_retry:
                    continue
                raise DomainError(
                    "SOURCE_CASE_ALREADY_REGISTERED", "Frozen case already has an input", 409
                )
            return selected[1].submit(input_id)

    def __call__(self, input_id: UUID) -> str:
        with self._lock:
            selected = self.case_for(input_id)
            experiment = selected[1] if selected else self.default
            if selected:
                try:
                    self.admit(input_id)
                except DomainError as error:
                    if error.code != "SOURCE_CASE_ALREADY_REGISTERED":
                        raise
                    return "本项实验已有登记输入，本次未重复运行编码。请查看原任务。"
            # Admissions never replace these pointers. The execution lock and
            # existing one-worker capacity keep each live attempt on its own profile.
            self.engine.source_scheduler.driver = experiment.runtime_profile.driver
            self.engine.source_coding.runtime = SourceCodingRuntime(
                self.engine.source_coding, experiment.runtime_profile
            )
            return experiment(input_id)
