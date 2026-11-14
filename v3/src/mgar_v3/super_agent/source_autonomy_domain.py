"""SA16 owner-reviewed rule and planning contracts; none is an execution bearer."""

from datetime import timedelta
from typing import Annotated, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from mgar_v3.domain import Model, digest
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef

AUTONOMY_VERSION: Literal["source-autonomy-v1"] = "source-autonomy-v1"
SHA256 = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=1)]
Mode = Literal["disabled", "shadow", "auto_investigate", "auto_repair"]


class AutonomyModel(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DailySignal(AutonomyModel):
    timezone: Annotated[str, Field(min_length=1, max_length=100)]
    at: Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
    weekdays: Annotated[tuple[int, ...], Field(min_length=1, max_length=7)] = (0, 1, 2, 3, 4, 5, 6)
    allow_scheduled_work: bool = False
    seed_input: CorrelationInputRef | None = None

    @model_validator(mode="after")
    def bounded_schedule(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("An exact IANA timezone is required") from None
        if len(set(self.weekdays)) != len(self.weekdays) or any(
            type(d) is not int or not 0 <= d <= 6 for d in self.weekdays
        ):
            raise ValueError("Weekdays must be distinct strict integers0..6")
        if self.allow_scheduled_work and self.seed_input is None:
            raise ValueError("Scheduled work needs an explicitly reviewed current Source reference")
        return self


class AutonomyRule(AutonomyModel):
    source_instance_id: UUID
    policy_id: UUID
    policy_revision: Revision
    policy_sha256: SHA256
    rule_ref: Annotated[str, Field(min_length=1, max_length=200)]
    rule_sha256: SHA256
    repository: Annotated[str, Field(pattern=r"^[a-z0-9_.-]+/[a-z0-9_.-]+$")]
    mode: Mode = "disabled"
    profile_sha256: SHA256
    objective_sha256: SHA256
    cooldown_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = 300
    task_ttl_seconds: Annotated[int, Field(strict=True, ge=30, le=86400)] = 1800
    idle_ttl_seconds: Annotated[int, Field(strict=True, ge=5, le=3600)] = 300
    max_active: Annotated[int, Field(strict=True, ge=1, le=16)] = 1
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=3)] = 1
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    schedule: DailySignal | None = None

    @model_validator(mode="after")
    def bounded_rule(self) -> Self:
        if not timedelta(0) < self.valid_until - self.valid_from <= timedelta(days=7):
            raise ValueError("Rule validity must be positive and at most7days")
        if self.idle_ttl_seconds > self.task_ttl_seconds:
            raise ValueError("Idle TTL must fit the task TTL")
        if (
            self.schedule is not None
            and self.schedule.seed_input is not None
            and self.schedule.seed_input.source_instance_id != self.source_instance_id
        ):
            raise ValueError("Timer context must belong to the same installation")
        return self

    @property
    def definition_sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


class AutonomyRuleSave(AutonomyModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    definition: AutonomyRule
    review_ref: Annotated[str, Field(min_length=1, max_length=200)]


class AutonomyRevisionRequest(AutonomyModel):
    expected_revision: Revision
    review_ref: Annotated[str, Field(min_length=1, max_length=200)]


class AutonomyReopenRequest(AutonomyRevisionRequest):
    decision_key: SHA256
    source_input: CorrelationInputRef
    expected_epoch: Annotated[int, Field(strict=True, ge=0)]


class AutonomyCycleRequest(AutonomyModel):
    limit: Annotated[int, Field(strict=True, ge=1, le=64)] = 16


class AutonomySignal(AutonomyModel):
    """Loaded and verified by the original Engine, never from arbitrary HTTP JSON."""

    key: SHA256
    subject_key: SHA256
    rule_id: UUID
    rule_revision: Revision
    source_instance_id: UUID
    policy_id: UUID
    policy_revision: Revision
    policy_sha256: SHA256
    repository: Annotated[str, Field(pattern=r"^[a-z0-9_.-]+/[a-z0-9_.-]+$")]
    intended_action: Literal[
        "would_ignore", "would_observe", "would_attach", "would_create", "would_ask"
    ]
    origin: Literal["decision", "timer"] = "decision"
    inputs: Annotated[tuple[CorrelationInputRef, ...], Field(min_length=1, max_length=16)]
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def exact_input_scope(self) -> Self:
        if (
            len({r.input_id for r in self.inputs}) != len(self.inputs)
            or any(r.source_instance_id != self.source_instance_id for r in self.inputs)
            or len({r.project_id for r in self.inputs}) != 1
        ):
            raise ValueError("Signal inputs must be distinct and in one exact project/installation")
        return self


class AutonomyAdmissionFacts(AutonomyModel):
    """Server-only current projection. A planner output still grants no operations."""

    rule_id: UUID
    rule_revision: Revision
    definition_sha256: SHA256
    source_current: bool
    identity_current: bool
    policy_current: bool
    receive_allowed: bool
    model_input_allowed: bool
    admission_allowed: bool
    execution_allowed: bool
    rule_basis_current: bool
    canonical_current: bool
    resource_claim_current: bool
    profile_current: bool
    profile_mode: Literal["readonly", "coding"]
    actual_model_mode: Literal["fixture", "live"]
    real_quality_authorized: bool = False
    budget_current: bool
    usage_unknown: bool = False
    already_claimed: bool = False
    previously_stopped: bool = False
    active_count: Annotated[int, Field(strict=True, ge=0)] = 0
    attempts: Annotated[int, Field(strict=True, ge=0)] = 0
    last_admitted_at: AwareDatetime | None = None
    factory_configured: bool = False


class AutonomyAdmissionPlan(AutonomyModel):
    disposition: Literal["ignored", "shadow", "duplicate", "held", "admit"]
    reason_code: Annotated[str, Field(pattern=r"^AUTONOMY_[A-Z0-9_]{1,80}$")]
    creates_authority: Literal[False] = False
    sends_notification: Literal[False] = False


class AutonomyRuntimeFacts(AutonomyModel):
    original_run_state: Literal[
        "queued",
        "running",
        "waiting_input",
        "waiting_approval",
        "paused",
        "succeeded",
        "failed",
        "cancelled",
    ]
    authority_current: bool
    expires_at: AwareDatetime
    created_at: AwareDatetime
    semantic_progress_at: AwareDatetime | None = None
    unknown_usage: bool = False
    resource_unconfirmed: bool = False
    original_result_known: bool = False
    original_result_passed: bool = False


class AutonomyMaintenancePlan(AutonomyModel):
    control: Literal["none", "hold", "release", "stop"]
    reason_code: Annotated[str, Field(pattern=r"^AUTONOMY_[A-Z0-9_]{1,80}$")]
    terminal_success: bool = False
    discard_unknown_or_refund: Literal[False] = False
    may_release_resource: bool = False
