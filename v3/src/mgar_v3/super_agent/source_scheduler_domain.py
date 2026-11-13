"""Bounded scheduling metadata for original Runs; never a second Task ledger."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, ConfigDict, Field, model_validator

from mgar_v3.domain import Model
from mgar_v3.sources.domain import OpaqueId, ProjectId
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef

SCHEDULER_VERSION: Literal["source-scheduler-v1"] = "source-scheduler-v1"
MAX_COUNTER = 9223372036854775807
SHA256 = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
Counter = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNTER)]
Revision = Annotated[int, Field(strict=True, ge=1, le=MAX_COUNTER)]
SessionTTL = Annotated[int, Field(strict=True, ge=1, le=86400)]
AssignmentState = Literal["preparing", "active", "draining", "released"]
QueueClass = Literal["human", "automation"]
RunStatus = Literal[
    "queued", "running", "waiting_input", "waiting_approval", "paused",
    "succeeded", "failed", "cancelled",
]
DrainReason = Literal[
    "WAITING_INPUT", "WAITING_APPROVAL", "OWNER_DRAIN", "LEASE_EXPIRED",
    "SESSION_REVOKED", "AUTHORITY_CHANGED", "PROCESS_RESTARTED", "RUN_STOPPED",
]


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UTCTime = Annotated[AwareDatetime, AfterValidator(_utc)]


class SchedulerModel(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ResourceRequest(SchedulerModel):
    cpu_millis: Annotated[int, Field(strict=True, ge=100, le=2000)]
    memory_mib: Annotated[int, Field(strict=True, ge=64, le=2048)]


class SchedulerLimits(SchedulerModel):
    slots: Annotated[int, Field(strict=True, ge=0, le=2)] = 0
    preparing: Annotated[int, Field(strict=True, ge=0, le=2)] = 0
    cpu_millis: Annotated[int, Field(strict=True, ge=0, le=4000)] = 0
    memory_mib: Annotated[int, Field(strict=True, ge=0, le=4096)] = 0
    queue: Annotated[int, Field(strict=True, ge=0, le=256)] = 0
    source_slots: Annotated[int, Field(strict=True, ge=0, le=2)] = 0
    lease_seconds: Annotated[int, Field(strict=True, ge=30, le=120)] = 30
    preparation_seconds: Annotated[int, Field(strict=True, ge=1, le=600)] = 120
    session_ttl_seconds: SessionTTL = 3600

    @model_validator(mode="after")
    def subordinate_capacity(self) -> Self:
        if self.preparing > self.slots or self.source_slots > self.slots:
            raise ValueError("Preparation and source capacity must fit total slots")
        return self


class SchedulerConfiguration(SchedulerModel):
    enabled: bool = False
    limits: SchedulerLimits = Field(default_factory=SchedulerLimits)
    valid_from: UTCTime
    valid_until: UTCTime

    @model_validator(mode="after")
    def nonempty_validity(self) -> Self:
        if self.valid_until <= self.valid_from:
            raise ValueError("Configuration requires an explicit nonempty validity interval")
        return self


class SchedulerConfigureRequest(SchedulerModel):
    expected_revision: Counter
    configuration: SchedulerConfiguration
    review_ref: OpaqueId


class SchedulerEnqueueRequest(SchedulerModel):
    request_id: UUID
    run_id: UUID
    expected_epoch: Counter
    target_sha256: SHA256
    resources: ResourceRequest
    source_input: CorrelationInputRef | None = None
    review_ref: OpaqueId


class SchedulerSessionRegisterRequest(SchedulerModel):
    request_id: UUID
    profile_sha256: SHA256
    ttl_seconds: SessionTTL
    review_ref: OpaqueId


class SchedulerSessionRevokeRequest(SchedulerModel):
    session_id: UUID
    expected_revision: Revision
    review_ref: OpaqueId


class SchedulerQueueCancelRequest(SchedulerModel):
    queue_id: UUID
    expected_revision: Revision
    review_ref: OpaqueId


class SchedulerClaimRequest(SchedulerModel):
    request_id: UUID
    session_id: UUID
    expected_config_revision: Revision


class SchedulerAssignmentRequest(SchedulerModel):
    assignment_id: UUID
    session_id: UUID
    epoch: Revision
    expected_revision: Revision


class SchedulerOperationRequest(SchedulerAssignmentRequest):
    operation_id: UUID
    request_sha256: SHA256


class SchedulerHeartbeatRequest(SchedulerAssignmentRequest):
    pass


class SchedulerReadyRequest(SchedulerOperationRequest):
    pass


class SchedulerReleaseRequest(SchedulerOperationRequest):
    pass


class SchedulerReportRequest(SchedulerOperationRequest):
    """A resource suggestion cannot promote the original business outcome."""

    status: Literal["waiting_user", "finished", "failed"]


class SchedulerDrainRequest(SchedulerAssignmentRequest):
    reason_code: DrainReason


class SchedulerOwnerDrainRequest(SchedulerModel):
    assignment_id: UUID
    expected_revision: Revision
    reason_code: DrainReason
    review_ref: OpaqueId


class SchedulerReconcileRequest(SchedulerModel):
    assignment_id: UUID
    expected_revision: Revision
    operation_id: UUID
    request_sha256: SHA256
    review_ref: OpaqueId


class SchedulerConfigFacts(SchedulerModel):
    project_id: ProjectId
    revision: Revision
    configuration_sha256: SHA256
    configuration: SchedulerConfiguration


class SessionFacts(SchedulerModel):
    session_id: UUID
    project_id: ProjectId
    revision: Revision
    profile_sha256: SHA256
    state: Literal["registered", "revoked"]
    registered_at: UTCTime
    expires_at: UTCTime

    @model_validator(mode="after")
    def bounded_session(self) -> Self:
        seconds = (self.expires_at - self.registered_at).total_seconds()
        if not 0 < seconds <= 86400:
            raise ValueError("Session lifetime must be positive and at most one day")
        return self


class AssignmentFacts(SchedulerModel):
    assignment_id: UUID
    queue_id: UUID
    run_id: UUID
    ticket_id: UUID
    session_id: UUID
    workspace_id: UUID
    project_id: ProjectId
    epoch: Revision
    config_revision: Revision
    revision: Revision
    state: AssignmentState
    lease_until: UTCTime
    resources: ResourceRequest
    prepare_until: UTCTime | None = None
    fenced_at: UTCTime | None = None


class RunFacts(SchedulerModel):
    run_id: UUID
    ticket_id: UUID
    project_id: ProjectId
    epoch: Counter
    status: RunStatus
    ticket_open: bool = False
    target_current: bool = False
    mandate_current: bool = False


class SourceAuthorityFacts(SchedulerModel):
    """Only a trusted current Engine lookup constructs these facts."""

    required: bool = True
    current: bool = False
    identity_current: bool = False
    receive_allowed: bool = False
    read_allowed: bool = False
    execution_allowed: bool = False
    origin_current: bool = False
    expires_at: UTCTime | None = None


class QueueCandidate(SchedulerModel):
    """Trusted origin classification is intentionally absent from enqueue DTOs."""

    queue_id: UUID
    run_id: UUID
    project_id: ProjectId
    fairness_key: SHA256
    classification: QueueClass
    enqueued_at: UTCTime
    resources: ResourceRequest
    generation: Revision
    state: Literal["queued", "claimed", "closed"] = "queued"
    eligible: bool = False


class ServiceBucket(SchedulerModel):
    project_id: ProjectId
    fairness_key: SHA256
    project_sequence: Counter = 0
    source_sequence: Counter = 0


class ServiceHistory(SchedulerModel):
    last_class: QueueClass | None = None
    buckets: Annotated[tuple[ServiceBucket, ...], Field(max_length=256)] = ()

    @model_validator(mode="after")
    def unique_buckets(self) -> Self:
        keys = {(item.project_id, item.fairness_key) for item in self.buckets}
        if len(keys) != len(self.buckets):
            raise ValueError("Service history cannot contain duplicate project/source buckets")
        return self


class SourceOccupancy(SchedulerModel):
    project_id: ProjectId
    fairness_key: SHA256
    slots: Counter


class ResourceUsage(SchedulerModel):
    """All nonreleased assignments occupy resources.

    ``preparing`` includes every preparing or draining assignment until release;
    a lost preparation process cannot free preparation capacity by draining.
    """

    slots: Counter = 0
    preparing: Counter = 0
    cpu_millis: Counter = 0
    memory_mib: Counter = 0
    sources: Annotated[tuple[SourceOccupancy, ...], Field(max_length=256)] = ()

    @model_validator(mode="after")
    def coherent_occupancy(self) -> Self:
        keys = {(item.project_id, item.fairness_key) for item in self.sources}
        if self.preparing > self.slots or len(keys) != len(self.sources):
            raise ValueError("Preparation/source occupancy must be unambiguous")
        if sum(item.slots for item in self.sources) > self.slots:
            raise ValueError("Source occupancy exceeds total occupied slots")
        return self


class QuiescenceFacts(SchedulerModel):
    """Provider evidence, never accepted as a Worker request payload."""

    assignment_id: UUID
    session_id: UUID
    run_id: UUID
    workspace_id: UUID
    epoch: Revision
    provider_receipt_sha256: SHA256
    observed_at: UTCTime
    trusted: bool = False
    processes_stopped: bool = False
    workspace_fenced: bool = False


class PendingWork(SchedulerModel):
    requested_operations: Counter = 0
    running_operations: Counter = 0
    unknown_operations: Counter = 0
    queued_read_jobs: Counter = 0
    running_read_jobs: Counter = 0
