"""Bounded source-to-isolated-Coding experiment on the original SA13/SA12 services."""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from cryptography.fernet import Fernet

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.contracts import (
    BudgetLimits,
    CapabilityGrant,
    ExecutionRequest,
    VerificationRequirement,
)
from mgar_v3.harness.workspace import WorkspaceRegistry
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.budget_domain import PriceQuote
from mgar_v3.super_agent.source_coding import H11_COMMIT, CodingProfile
from mgar_v3.super_agent.source_coding_domain import (
    CodingGrant,
    CodingHarnessGate,
    CodingHarnessGates,
    CodingMandate,
    CodingOrigin,
    CodingRepository,
    CodingRunRequest,
    CodingTarget,
)
from mgar_v3.super_agent.source_coding_live_model import ExperimentalDeepSeekModel
from mgar_v3.super_agent.source_coding_runtime import (
    SourceCodingFixtureProfile,
    SourceCodingRuntime,
)
from mgar_v3.super_agent.source_coding_trial import (
    R05_FINGERPRINT,
    R05_SOURCE,
    CodingTrial,
    authorize_trial,
)
from mgar_v3.super_agent.source_scheduler_domain import (
    ResourceRequest,
    SchedulerClaimRequest,
    SchedulerConfiguration,
    SchedulerConfigureRequest,
    SchedulerHeartbeatRequest,
    SchedulerLimits,
    SchedulerOwnerDrainRequest,
    SchedulerReadyRequest,
    SchedulerSessionRegisterRequest,
)
from mgar_v3.super_agent.source_scheduler_runtime import (
    H03SchedulerResourceDriver,
    SchedulerRuntimeProfile,
)
from mgar_v3.super_agent.source_trial_limits import validate_owner_trial_limit

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine


def normalize_coding_mention(text: str) -> str:
    text = re.sub(r"@[_A-Za-z0-9-]+", "", text).strip()
    # Native rich text can leave one separate leading @ beside the verified
    # mention. Only normalize that token; objective and constraints stay exact.
    return re.sub(r"\A@\s+", "", text)


def explicit_coding_request(text: str) -> bool:
    text = normalize_coding_mention(text)
    return (
        re.match(
            r"(?i)^(?:/code\b|/编码|(?:请(?:你)?(?:帮我)?|帮我)(?:修复|修改|实现|新增|编写|重构|添加)|fix\b|implement\b)",
            text,
        )
        is not None
    )


class SourceCodingExperiment:
    def __init__(
        self,
        engine: Engine,
        cookie: str,
        profile: CodingProfile,
        runtime_profile: SourceCodingFixtureProfile,
        registry: WorkspaceRegistry,
        artifacts: Path,
    ):
        self.engine, self.cookie, self.profile = engine, cookie, profile
        self.runtime_profile, self.registry, self.artifacts = runtime_profile, registry, artifacts
        if not profile.trial_id or not runtime_profile.experimental:
            raise ValueError("An explicit experimental profile is required")

    def submit(self, input_id: UUID) -> dict[str, Any]:
        core = self.engine.source_coding
        with self.engine.transaction() as conn:
            owner = core._console(conn, self.cookie)
            source = self.engine.source_correlations._input(conn, input_id)
            if not explicit_coding_request(source.text):
                raise DomainError(
                    "SOURCE_CODING_EXPLICIT_REQUEST_REQUIRED",
                    "Explicit coding request required",
                    403,
                )
            old = conn.execute(
                "SELECT coding_id,run_id FROM app.sa_coding_bindings "
                "WHERE input_id=%s AND project_id=%s",
                (input_id, self.engine.settings.project_id),
            ).fetchone()
            if old:
                return old
            identity = conn.execute(
                "SELECT i.*,p.engine_actor FROM app.source_identities i "
                "JOIN app.source_principals p "
                "USING(principal_id,project_id) WHERE identity_id=%s",
                (source.row["identity_id"],),
            ).fetchone()
            if identity is None:
                raise DomainError(
                    "SOURCE_CODING_IDENTITY_REQUIRED", "Current source identity required", 403
                )
            trial = conn.execute(
                "SELECT * FROM app.sa_coding_trial_authorizations WHERE trial_id=%s",
                (self.profile.trial_id,),
            ).fetchone()
            if trial is None:
                raise DomainError(
                    "SOURCE_CODING_TRIAL_REQUIRED", "Current experiment required", 403
                )
            now = core.now(conn)
            target = CodingTarget(
                project_id=self.engine.settings.project_id,
                thread_id=uuid4(),
                ticket_id=uuid4(),
                run_id=uuid4(),
                mandate_id=uuid4(),
            )
            mandate = CodingMandate(
                coding_id=uuid4(),
                revision=1,
                target=target,
                origin=CodingOrigin(
                    input=source.ref,
                    identity_id=identity["identity_id"],
                    identity_revision=identity["revision"],
                    principal_id=identity["principal_id"],
                    engine_actor=identity["engine_actor"],
                    policy_id=source.row["policy_id"],
                    policy_revision=source.row["policy_revision"],
                    policy_sha256=digest(source.definition.model_dump(mode="json")),
                    expires_at=source.expires,
                ),
                repository=self.profile.repository,
                grant=self.profile.grant,
                budget=self.profile.budget,
                harness=self.profile.harness,
                context_refs=(source.ref,),
                context_sha256=core.context_sha256(source),
                acceptance_sha256=self.profile.acceptance_sha256,
                worker_profile_sha256=self.profile.profile_sha256,
                review_sha256=digest(
                    ["owner-authorized-SA22-trial", str(self.profile.trial_id), str(input_id)]
                ),
                signed_by=owner,
                valid_from=now,
                valid_until=min(source.expires, trial["valid_until"], now + timedelta(minutes=10)),
            )
            return core.create_target(self.cookie, mandate)

    def __call__(self, input_id: UUID) -> str:
        if not explicit_coding_request(self._text(input_id)):
            return "若要实际编码，请使用“请修改／请修复……”并写明目标；本次未运行编码任务。"
        try:
            target = self.submit(input_id)
            result = self.run_one()
        except DomainError as error:
            return "隔离编码实验未完成（" + error.code + "）；没有宣称修复成功。"
        if result is None:
            return "编码请求已进入原任务队列，等待空闲执行器。任务：" + str(target["run_id"])
        changed = result.get("changed", False)
        return (
            ("已在隔离工作区生成代码差异。" if changed else "本次隔离运行未生成代码差异。")
            + "任务："
            + str(result["run_id"])
            + "。固定检查结果及补丁已保留；完整回归与独立验收未完成，原分支未修改。"
        )

    def _text(self, input_id: UUID) -> str:
        with self.engine.transaction() as conn:
            return self.engine.source_correlations._input(conn, input_id).text

    def run_one(self) -> dict[str, Any] | None:
        scheduler, core = self.engine.source_scheduler, self.engine.source_coding
        session = scheduler.register_session(
            self.cookie,
            SchedulerSessionRegisterRequest(
                request_id=uuid4(),
                profile_sha256=self.profile.profile_sha256,
                ttl_seconds=1200,
                review_ref="SA22-owner-experimental-worker",
            ),
        )
        with self.engine.transaction() as conn:
            revision = scheduler._configuration(conn, active=True).revision
        claimed = scheduler.claim(
            session["bearer"],
            SchedulerClaimRequest(
                request_id=uuid4(),
                session_id=session["session_id"],
                expected_config_revision=revision,
            ),
        )
        if claimed.get("assignment") is None and "assignment_id" not in claimed:
            return None
        assignment_id = claimed["assignment_id"]
        stop = threading.Event()
        keeper = None
        try:
            assignment = scheduler.get_assignment(self.cookie, assignment_id)
            prepared = claimed["prepare_operation"]
            active = scheduler.ready(
                session["bearer"],
                SchedulerReadyRequest(
                    assignment_id=assignment_id,
                    session_id=session["session_id"],
                    epoch=assignment["execution_epoch"],
                    expected_revision=assignment["revision"],
                    operation_id=prepared["operation_id"],
                    request_sha256=prepared["request_sha256"],
                ),
            )
            with self.engine.transaction() as conn:
                binding = conn.execute(
                    "SELECT coding_id,revision FROM app.sa_coding_bindings WHERE run_id=%s",
                    (assignment["run_id"],),
                ).fetchone()
            if binding is None:
                raise DomainError(
                    "SOURCE_CODING_BINDING_REQUIRED", "Original binding required", 403
                )
            request = CodingRunRequest(
                request_id=uuid4(),
                coding_id=binding["coding_id"],
                run_id=assignment["run_id"],
                assignment_id=assignment_id,
                session_id=session["session_id"],
                epoch=active["execution_epoch"],
                expected_revision=binding["revision"],
            )

            def heartbeat() -> None:
                while not stop.wait(20):
                    try:
                        row = scheduler.get_assignment(self.cookie, assignment_id)
                        scheduler.heartbeat(
                            session["bearer"],
                            SchedulerHeartbeatRequest(
                                assignment_id=assignment_id,
                                session_id=session["session_id"],
                                epoch=request.epoch,
                                expected_revision=row["revision"],
                            ),
                        )
                    except DomainError:
                        stop.set()

            keeper = threading.Thread(target=heartbeat, name="sa22-coding-lease", daemon=True)
            keeper.start()
            result = core.run(session["bearer"], request)
            with core.guard(session["bearer"], request):
                raw = self.registry.open_task_workspace(
                    self.runtime_profile.driver.registration, str(assignment_id)
                )
                patch = raw.diff()
                artifact = self.artifacts / str(request.run_id)
                artifact.mkdir(parents=True, mode=0o700, exist_ok=True)
                (artifact / "patch.diff").write_text(patch)
                (artifact / "patch.diff").chmod(0o600)
                (artifact / "result.json").write_text(
                    json.dumps(result, default=str, indent=2) + "\n"
                )
                (artifact / "result.json").chmod(0o600)
                with self.engine.transaction() as conn:
                    self.engine._event(
                        conn,
                        request.run_id,
                        "source_trial_artifact_available",
                        patch_sha256=sha256(patch.encode()).hexdigest(),
                        changed=bool(patch),
                        independent_verification="NOT_RUN",
                        original_branch_unchanged=True,
                    )
                return {**result, "changed": bool(patch), "artifact_directory": str(artifact)}
        finally:
            stop.set()
            if keeper is not None:
                keeper.join(timeout=3)
            assignment = scheduler.get_assignment(self.cookie, assignment_id)
            if assignment["state"] != "released":
                scheduler.drain(
                    self.cookie,
                    SchedulerOwnerDrainRequest(
                        assignment_id=assignment_id,
                        expected_revision=assignment["revision"],
                        reason_code="OWNER_DRAIN",
                        review_ref="SA22-isolated-experiment-cleanup",
                    ),
                )
            scheduler.maintain()


def install_coding_trial(
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
    model: ExperimentalDeepSeekModel,
    expires: datetime,
    manifest_sha256: str,
    h12_evidence_sha256: str,
    repository_id: int = 1438269299,
    baseline_command: tuple[str, ...] = ("python3", "-m", "pytest", "tests/test_domain.py", "-q"),
    working_directory: str = "v3",
    read_paths: tuple[str, ...] = ("v3", "README.md"),
    write_paths: tuple[str, ...] = ("v3/src",),
    maximum_project_micros: int = 2000000,
) -> SourceCodingExperiment:
    maximum_project_micros = validate_owner_trial_limit(maximum_project_micros)
    core, scheduler = engine.source_coding, engine.source_scheduler
    with engine.transaction() as conn:
        owner = core._console(conn, cookie)
        source = engine.source_correlations.identities.registry._instance(conn, source_id)
        identity = conn.execute(
            "SELECT p.engine_actor FROM app.source_identities i JOIN app.source_principals p "
            "USING(principal_id,project_id) WHERE i.source_instance_id=%s AND i.actor_id=%s "
            "AND i.actor_kind='human' AND i.revoked_at IS NULL",
            (source_id, actor_id),
        ).fetchone()
        policy = conn.execute(
            "SELECT * FROM app.source_policies WHERE source_instance_id=%s", (source_id,)
        ).fetchone()
        if identity is None or policy is None:
            raise DomainError("SOURCE_CODING_TRIAL_IDENTITY", "Bound owner source required", 403)
        actor = identity["engine_actor"]
    registry = WorkspaceRegistry(root / "registry")
    registration = registry.register_local(source_root, actor=engine.settings.local_actor)
    lock = root / "image-lock.json"
    lock.write_text(json.dumps({"image_id": image_id}) + "\n")
    lock.chmod(0o600)
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
    scheduler_profile = SchedulerRuntimeProfile(
        actor=actor,
        repository=engine.settings.repository,
        grant=grant,
        request=ExecutionRequest(
            operation_id="sa22-owner-trial", argv=baseline_command, timeout_seconds=60
        ),
        image_id=image_id,
        review_ref="SA22-user-explicit-quality-exception",
        provider_generation="source-owner-trial-v1",
        execution_mode="live",
    )
    driver = H03SchedulerResourceDriver(
        scheduler=scheduler,
        registry_root=registry.root,
        registration=registration,
        source_root=source_root,
        profile=scheduler_profile,
        receipt_root=root / "resource-receipts",
        image_lock=lock,
    )
    scheduler.driver = driver
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
    materialized = registry.create_task_workspace(
        registration, "sa22-initial-snapshot", source_root=source_root
    )
    snapshot = materialized.snapshot()
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
    coding_grant = CodingGrant(
        read_paths=read_paths,
        write_paths=write_paths,
        allowed_commands=(baseline_command[0],),
        resources=ResourceRequest(cpu_millis=1000, memory_mib=512),
        environment_sha256=environment,
        max_seconds=180,
    )
    reserved_tokens, output_tokens = 40960, 8192
    reservation = (
        32768 * price.input_micros_per_million
        + output_tokens * price.output_micros_per_million
        + 999999
    ) // 1000000
    runtime_profile = SourceCodingFixtureProfile(
        driver=driver,
        model=model,
        experimental=True,
        objective="完成当前来源中本人明确提出的编码请求。只修改授权现有代码文件，保留固定测试；产物待人工审查。",
        requirements=(
            VerificationRequirement(
                requirement_id="fixed-smoke-check",
                command_contains=(baseline_command[0],),
                min_tests=1,
            ),
        ),
        baseline_command=baseline_command,
        price=price,
        limits=BudgetLimits(
            max_model_calls=8, max_tool_calls=24, max_cost_micros=300000, max_seconds=180
        ),
        working_directory=working_directory,
        reservation_micros=reservation,
        max_output_tokens=output_tokens,
        reserved_tokens=reserved_tokens,
    )
    trial_id = uuid4()
    with engine.transaction() as conn:
        budget = core.budget_ref(conn, price, digest([manifest_sha256, "coding-original-budget"]))
        profile = CodingProfile(
            profile_sha256=driver.profile_sha256,
            repository=repository,
            grant=coding_grant,
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
            acceptance_sha256=runtime_profile.acceptance_sha256,
            limit_micros=maximum_project_micros,
            max_model_calls=100,
            trial_id=trial_id,
        )
        now = core.now(conn)
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
                profile_sha256=driver.profile_sha256,
                repository=repository.repository,
                base_commit=upstream_commit,
                registration_sha256=registration.registration_hash,
                h12_commit=R05_SOURCE,
                h12_source_sha256=R05_FINGERPRINT,
                h12_evidence_sha256=h12_evidence_sha256,
                manifest_sha256=manifest_sha256,
                signed_by=owner,
                review_sha256=digest("user-SA22-quality-exception"),
                maximum_project_micros=maximum_project_micros,
                valid_from=now,
                valid_until=expires,
            ),
        )
        core.register_profile(profile)
    if core.cipher is None:
        core.cipher = BodyCipher(Fernet.generate_key())
    core.runtime = SourceCodingRuntime(core, runtime_profile)
    scheduler.configure(
        cookie,
        SchedulerConfigureRequest(
            expected_revision=0,
            configuration=SchedulerConfiguration(
                enabled=True,
                valid_from=datetime.now(UTC) - timedelta(seconds=1),
                valid_until=expires,
                limits=SchedulerLimits(
                    slots=1,
                    preparing=1,
                    cpu_millis=1000,
                    memory_mib=512,
                    queue=16,
                    source_slots=1,
                    lease_seconds=120,
                    preparation_seconds=120,
                ),
            ),
            review_ref="SA22-owner-single-isolated-worker",
        ),
    )
    return SourceCodingExperiment(
        engine, cookie, profile, runtime_profile, registry, root / "artifacts"
    )
