"""SA14 metadata contracts; construction and caller assertions grant no authority."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, ConfigDict, Field, model_validator

from mgar_v3.domain import Model, digest
from mgar_v3.sources.domain import OpaqueId
from mgar_v3.super_agent.policy_domain import safe_path
from mgar_v3.super_agent.source_coding_domain import CodingTarget, CommandName
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef
from mgar_v3.super_agent.source_scheduler_domain import SHA256, Counter, Revision, UTCTime

ASYNC_VERSION: Literal["source-async-v1"] = "source-async-v1"
InputKind = Literal["context", "restriction", "invalidation", "job_progress"]
InputState = Literal["received", "applied", "blocked"]
JobState = Literal["prepared", "running", "stop_requested", "unknown", "known"]
SafePointKind = Literal[
    "before_model", "after_model", "before_tool", "before_command", "command_poll", "before_result"
]
Path = Annotated[str, Field(strict=True, max_length=256), AfterValidator(safe_path)]


class AsyncModel(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AsyncScope(AsyncModel):
    coding_id: UUID
    coding_revision: Revision
    target: CodingTarget
    assignment_id: UUID
    session_id: UUID
    workspace_id: UUID
    epoch: Revision
    mandate_sha256: SHA256
    profile_sha256: SHA256
    initial_context_sha256: SHA256
    control_profile_sha256: SHA256 | None = None

    @property
    def scope_sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


class AsyncRestriction(AsyncModel):
    """Only deny sets grow; no field can grant paths, commands, network or payment."""

    deny_write_paths: Annotated[tuple[Path, ...], Field(max_length=64)] = ()
    deny_commands: Annotated[tuple[CommandName, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def canonical_sets(self) -> Self:
        for values in (self.deny_write_paths, self.deny_commands):
            if values != tuple(sorted(set(values))):
                raise ValueError("Restriction sets must be sorted and unique")
        return self

    @property
    def restriction_sha256(self) -> str:
        return digest(self.model_dump(mode="json"))

    def narrow(self, extra: "AsyncRestriction") -> "AsyncRestriction":
        return AsyncRestriction(
            deny_write_paths=tuple(
                sorted(set(self.deny_write_paths) | set(extra.deny_write_paths))
            ),
            deny_commands=tuple(sorted(set(self.deny_commands) | set(extra.deny_commands))),
        )


class AsyncAdmitRequest(AsyncModel):
    request_id: UUID
    coding_id: UUID
    run_id: UUID
    expected_coding_revision: Revision
    expected_epoch: Revision
    expected_revision: Annotated[int, Field(strict=True, ge=0, le=0)] = 0
    review_ref: OpaqueId


class AsyncRestrictRequest(AsyncModel):
    request_id: UUID
    coding_id: UUID
    run_id: UUID
    expected_epoch: Revision
    expected_revision: Revision
    restriction: AsyncRestriction
    review_ref: OpaqueId

    @model_validator(mode="after")
    def real_restriction(self) -> Self:
        if not (self.restriction.deny_write_paths or self.restriction.deny_commands):
            raise ValueError("A control request requires a concrete restriction")
        return self


class AsyncWorkerRequest(AsyncModel):
    request_id: UUID
    coding_id: UUID
    run_id: UUID
    assignment_id: UUID
    session_id: UUID
    epoch: Revision
    expected_revision: Revision


class AsyncJobCancelRequest(AsyncModel):
    request_id: UUID
    coding_id: UUID
    run_id: UUID
    job_id: UUID
    expected_revision: Revision
    review_ref: OpaqueId


class AsyncWorkerJobCancelRequest(AsyncWorkerRequest):
    job_id: UUID


class AsyncInputFacts(AsyncModel):
    """Actual Engine facts for pure classification; never a public grant DTO."""

    kind: InputKind
    source: Literal["owner", "source", "system", "job"]
    source_input: CorrelationInputRef | None = None
    expires_at: UTCTime
    receive_current: bool = False
    identity_current: bool = False
    policy_current: bool = False
    canonical_current: bool = False
    exact_target: bool = False
    read_allowed: bool = False
    context_only: bool = True
    human: bool = False
    task_controller: bool = False
    ticket_membership: bool = False
    owner_session_current: bool = False
    trusted_observation: bool = False
    restriction: AsyncRestriction | None = None


class AsyncInputReceipt(AsyncModel):
    input_id: UUID
    async_id: UUID
    scope_sha256: SHA256
    sequence: Revision
    kind: InputKind
    state: InputState
    input_revision: Counter
    request_sha256: SHA256
    source_input: CorrelationInputRef | None = None
    restriction: AsyncRestriction | None = None
    reason_code: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,100}$")] | None = None
    applied_checkpoint_id: UUID | None = None

    @model_validator(mode="after")
    def disposition_is_explicit(self) -> Self:
        if (self.state == "applied") != (self.applied_checkpoint_id is not None):
            raise ValueError("Only a committed applied fact names its safe point")
        if (self.state == "blocked") != (self.reason_code is not None):
            raise ValueError("Blocked input must retain a reason and never pretend applied")
        if self.kind == "restriction" and self.restriction is None:
            raise ValueError("A restriction receipt must freeze its typed deny set")
        return self


class AsyncCheckpoint(AsyncModel):
    checkpoint_id: UUID
    async_id: UUID
    scope: AsyncScope
    stage: SafePointKind
    input_revision: Counter
    through_sequence: Counter
    restriction: AsyncRestriction
    source_refs: Annotated[tuple[CorrelationInputRef, ...], Field(max_length=64)] = ()
    context_sha256: SHA256
    tree_sha256: SHA256
    environment_sha256: SHA256
    created_at: UTCTime
    expires_at: UTCTime

    @model_validator(mode="after")
    def bounded_view(self) -> Self:
        if not 0 < (self.expires_at - self.created_at).total_seconds() <= 1800:
            raise ValueError("A view must expire within thirty minutes and every source dependency")
        identities = [item.input_id for item in self.source_refs]
        if len(set(identities)) != len(identities):
            raise ValueError("A view cannot repeat a Source input under another alias")
        if any(item.project_id != self.scope.target.project_id for item in self.source_refs):
            raise ValueError("A view cannot cross its original project")
        return self

    @property
    def checkpoint_sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


class AsyncCurrentFacts(AsyncModel):
    async_id: UUID
    scope: AsyncScope
    revision: Revision
    input_revision: Counter
    restriction: AsyncRestriction
    tree_sha256: SHA256
    environment_sha256: SHA256
    expires_at: UTCTime
    original_coding_current: bool = False
    assignment_current: bool = False
    session_current: bool = False
    sources_current: bool = False
    budget_current: bool = False
    active: bool = False
    pending_invalidation: bool = True


class AsyncJob(AsyncModel):
    job_id: UUID
    async_id: UUID
    scope: AsyncScope
    checkpoint_id: UUID
    coding_operation_id: UUID
    resource_operation_id: UUID
    request_sha256: SHA256
    tree_sha256: SHA256
    environment_sha256: SHA256
    input_revision: Counter
    revision: Revision
    state: JobState
    result_code: Literal["succeeded", "failed", "cancelled", "absent"] | None = None
    receipt_sha256: SHA256 | None = None
    observed_at: UTCTime | None = None

    @model_validator(mode="after")
    def known_is_observed(self) -> Self:
        filled = (
            self.result_code is not None,
            self.receipt_sha256 is not None,
            self.observed_at is not None,
        )
        if (self.state == "known" and not all(filled)) or (self.state != "known" and any(filled)):
            raise ValueError("Known Job state requires a real bound original-resource receipt")
        return self
