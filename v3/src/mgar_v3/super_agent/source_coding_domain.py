"""Strict private Coding metadata; only the original Engine supplies authority facts."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, ConfigDict, Field, model_validator

from mgar_v3.domain import Model, digest
from mgar_v3.sources.domain import OpaqueId, ProjectId
from mgar_v3.super_agent.policy_domain import safe_path
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef
from mgar_v3.super_agent.source_scheduler_domain import (
    SHA256,
    Counter,
    ResourceRequest,
    Revision,
    UTCTime,
)

CODING_VERSION: Literal["source-coding-mandate-v1"] = "source-coding-mandate-v1"
Commit = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{40}$")]
EngineActor = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_-]{1,100}$")]
CommandName = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_+.-]{0,63}$")]
PathRoot = Annotated[
    str,
    Field(strict=True, max_length=256),
    AfterValidator(
        lambda value: safe_path(value) if value else value,
    ),
]
GateStatus = Literal["PASS", "FAIL", "NOT_RUN"]


class CodingModel(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def within(path: str, roots: tuple[str, ...]) -> bool:
    return any(root == "" or path == root or path.startswith(root + "/") for root in roots)


class CodingTarget(CodingModel):
    project_id: ProjectId
    thread_id: UUID
    ticket_id: UUID
    run_id: UUID
    mandate_id: UUID


class CodingOrigin(CodingModel):
    input: CorrelationInputRef
    identity_id: UUID
    identity_revision: Revision
    principal_id: UUID
    engine_actor: EngineActor
    policy_id: UUID
    policy_revision: Revision
    policy_sha256: SHA256
    expires_at: UTCTime


class CodingRepository(CodingModel):
    repository: Annotated[str, Field(pattern=r"^[a-z0-9_.-]+/[a-z0-9_.-]+$", max_length=200)]
    repository_id: Revision
    source_kind: Literal["fixture_git", "github_snapshot", "local_git_snapshot"]
    base_commit: Commit
    upstream_tree: Commit
    materialized_commit: Commit
    materialized_tree_sha256: SHA256
    registration_sha256: SHA256
    snapshot_sha256: SHA256
    private: bool = False
    model_disclosure: Literal["denied", "controlled_fixture", "approved_live"] = "denied"


class CodingGrant(CodingModel):
    read_paths: Annotated[tuple[PathRoot, ...], Field(min_length=1, max_length=64)]
    write_paths: Annotated[tuple[PathRoot, ...], Field(min_length=1, max_length=32)]
    allowed_commands: Annotated[tuple[CommandName, ...], Field(min_length=1, max_length=32)]
    protected_paths: Annotated[tuple[PathRoot, ...], Field(min_length=3, max_length=64)] = (
        ".git",
        ".mgar",
        ".env",
    )
    network: bool = False
    resources: ResourceRequest
    environment_sha256: SHA256
    max_seconds: Annotated[int, Field(strict=True, ge=1, le=600)] = 120

    @model_validator(mode="after")
    def bounded_intersection(self) -> Self:
        if self.network:
            raise ValueError("This Coding protocol does not grant networking")
        for roots in (self.read_paths, self.write_paths, self.protected_paths):
            if len(set(roots)) != len(roots):
                raise ValueError("Duplicate path root")
        if len(set(self.allowed_commands)) != len(self.allowed_commands):
            raise ValueError("Duplicate command")
        if any(not path or not within(path, self.read_paths) for path in self.write_paths):
            raise ValueError("Writes need a bounded readable subpath")
        if any(
            within(path, (protected,)) or within(protected, (path,))
            for path in self.write_paths
            for protected in self.protected_paths
        ):
            raise ValueError("Write roots overlap protected paths")
        if not {".git", ".mgar", ".env"}.issubset(self.protected_paths):
            raise ValueError("Reserved workspace metadata must stay protected")
        return self


class CodingBudgetRef(CodingModel):
    organization_id: OpaqueId
    organization_revision: Revision
    project_revision: Revision
    profile_sha256: SHA256
    price_version: OpaqueId
    price_sha256: SHA256
    limits_sha256: SHA256


class CodingHarnessGate(CodingModel):
    card: Literal["HV4-H11", "HV4-H12"]
    commit: Commit
    source_sha256: SHA256
    evidence_sha256: SHA256
    engineering: GateStatus = "NOT_RUN"
    recovery: GateStatus = "NOT_RUN"
    live_agent: GateStatus = "NOT_RUN"
    mvp: GateStatus = "NOT_RUN"


class CodingHarnessGates(CodingModel):
    h11: CodingHarnessGate
    h12: CodingHarnessGate

    @model_validator(mode="after")
    def exact_cards(self) -> Self:
        if self.h11.card != "HV4-H11" or self.h12.card != "HV4-H12":
            raise ValueError("H11 and H12 gates must be independently identified")
        return self


class CodingMandate(CodingModel):
    schema_version: Literal["source-coding-mandate-v1"] = CODING_VERSION
    scope_kind: Literal["source-coding-mandate-v1"] = CODING_VERSION
    coding_id: UUID
    revision: Revision
    target: CodingTarget
    origin: CodingOrigin
    repository: CodingRepository
    grant: CodingGrant
    budget: CodingBudgetRef
    harness: CodingHarnessGates
    context_refs: Annotated[tuple[CorrelationInputRef, ...], Field(min_length=1, max_length=32)]
    context_sha256: SHA256
    acceptance_sha256: SHA256
    worker_profile_sha256: SHA256
    review_sha256: SHA256
    signed_by: UUID
    valid_from: UTCTime
    valid_until: UTCTime

    @model_validator(mode="after")
    def original_scope(self) -> Self:
        if self.valid_until <= self.valid_from or self.valid_until > self.origin.expires_at:
            raise ValueError("Coding validity must fit its Source origin")
        refs = self.context_refs
        if any(ref.project_id != self.target.project_id for ref in (*refs, self.origin.input)):
            raise ValueError("Canonical references must stay in the original project")
        if len({ref.input_id for ref in refs}) != len(refs) or self.origin.input not in refs:
            raise ValueError("Context references need one exact unique admitted origin")
        return self

    @property
    def mandate_sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


class CodingAdmitRequest(CodingModel):
    request_id: UUID
    run_id: UUID
    expected_epoch: Counter
    source_input: CorrelationInputRef
    expected_policy_revision: Revision
    expected_revision: Counter
    target_sha256: SHA256
    profile_sha256: SHA256
    review_ref: OpaqueId


class CodingRunRequest(CodingModel):
    request_id: UUID
    coding_id: UUID
    run_id: UUID
    assignment_id: UUID
    session_id: UUID
    epoch: Revision
    expected_revision: Revision


class CodingInspectRequest(CodingModel):
    coding_id: UUID
    run_id: UUID


class CodingCancelRequest(CodingModel):
    request_id: UUID
    coding_id: UUID
    run_id: UUID
    expected_epoch: Counter
    expected_revision: Revision
    review_ref: OpaqueId


class CodingCurrentFacts(CodingModel):
    """Trusted current PG projection, never an HTTP or model request."""

    target: CodingTarget
    origin: CodingOrigin
    repository: CodingRepository
    budget: CodingBudgetRef
    mandate_sha256: SHA256
    coding_revision: Revision
    profile_sha256: SHA256
    context_sha256: SHA256
    harness: CodingHarnessGates
    assignment_id: UUID
    session_id: UUID
    workspace_id: UUID
    epoch: Revision
    run_epoch: Revision
    assignment_epoch: Revision
    assignment_created_at: UTCTime
    lease_until: UTCTime
    session_expires_at: UTCTime
    configuration_expires_at: UTCTime
    current_tree_sha256: SHA256
    environment_sha256: SHA256
    mode: Literal["fixture", "live"] = "fixture"
    original_scope_kind: OpaqueId = "legacy"
    original_read_only: bool = True
    source_mode: Literal["disabled", "shadow", "auto_investigate", "auto_repair"] = "disabled"
    run_state: Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"] = "queued"
    assignment_state: Literal["preparing", "active", "draining", "released"] = "preparing"
    source_current: bool = False
    identity_current: bool = False
    policy_current: bool = False
    canonical_current: bool = False
    admission_allowed: bool = False
    model_input_allowed: bool = False
    execution_allowed: bool = False
    budget_current: bool = False
    snapshot_current: bool = False
    target_current: bool = False
    runtime_profile_current: bool = False
    assignment_current: bool = False
    session_current: bool = False
    live_source_authorized: bool = False
    live_model_authorized: bool = False
    live_execution_authorized: bool = False
    owner_trial_current: bool = False
    policy_read_paths: tuple[PathRoot, ...] = ()
    policy_write_paths: tuple[PathRoot, ...] = ()
    policy_commands: tuple[CommandName, ...] = ()


class CodingReceiptFacts(CodingModel):
    """Receipt binding supplied only by the trusted journal/Broker/Verifier adapter."""

    coding_id: UUID
    coding_revision: Revision
    mandate_sha256: SHA256
    target: CodingTarget
    assignment_id: UUID
    session_id: UUID
    workspace_id: UUID
    epoch: Revision
    operation_id: UUID
    operation_sha256: SHA256
    receipt_sha256: SHA256
    tree_sha256: SHA256
    environment_sha256: SHA256
    profile_sha256: SHA256
    acceptance_sha256: SHA256
    observed_at: UTCTime
    provenance: Literal["untrusted", "broker_verifier"] = "untrusted"
    journaled: bool = False
    result: Literal["passed", "failed", "missing"] = "missing"
    tests_run: Counter = 0
