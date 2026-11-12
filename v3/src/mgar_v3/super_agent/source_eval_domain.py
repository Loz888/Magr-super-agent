"""Bodyless Source Router snapshots and suggestions; none grants execution authority."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from mgar_v3.domain import Model
from mgar_v3.sources.domain import ActorRef, OpaqueId, ProjectId, SourceScope
from mgar_v3.super_agent.budget_domain import PriceQuote

SOURCE_PROPOSALS_VERSION: Literal["source-router-proposals-v1"] = "source-router-proposals-v1"
SOURCE_SNAPSHOT_VERSION: Literal["source-router-snapshot-v1"] = "source-router-snapshot-v1"
SOURCE_DECISION_VERSION: Literal["source-router-decision-v1"] = "source-router-decision-v1"
MAX_EVAL_BYTES = 65536
SHA256 = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]
Repository = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
SourceAction = Literal["ignore", "observe", "attach", "create", "ask"]
SourceIntendedAction = Literal[
    "would_ignore", "would_observe", "would_attach", "would_create", "would_ask",
]
SourceReason = Literal["IRRELEVANT", "CONTEXT", "EXISTING_TASK", "ACTIONABLE", "NEEDS_CONTEXT"]
REASONS: dict[SourceAction, SourceReason] = {
    "ignore": "IRRELEVANT", "observe": "CONTEXT", "attach": "EXISTING_TASK",
    "create": "ACTIONABLE", "ask": "NEEDS_CONTEXT",
}


class EvalModel(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceProposal(EvalModel):
    group_id: UUID
    action: SourceAction
    target_id: UUID | None = None
    reason_code: SourceReason

    @model_validator(mode="after")
    def coherent_reason(self) -> Self:
        if self.reason_code != REASONS[self.action]:
            raise ValueError("Reason must match the bounded action")
        return self


class SourceProposals(EvalModel):
    schema_version: Literal["source-router-proposals-v1"]
    proposals: Annotated[tuple[SourceProposal, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def unique_groups(self) -> Self:
        if len({p.group_id for p in self.proposals}) != len(self.proposals):
            raise ValueError("One proposal per group is required")
        return self


class CanonicalInputRef(EvalModel):
    input_id: UUID
    version_fingerprint: SHA256
    identity_id: UUID
    identity_revision: Revision
    actor_key: SHA256


class SourceTargetRef(EvalModel):
    ticket_id: UUID
    run_id: UUID
    thread_id: UUID
    thread_revision: Revision
    execution_epoch: Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]
    mandate_id: UUID
    mandate_sha256: SHA256
    project_id: ProjectId
    repository: Repository
    allowed: bool


class SourceSnapshotGroup(EvalModel):
    group_id: UUID
    fast_batch_id: UUID
    fast_batch_revision: Revision
    topic_key: SHA256
    actors: Annotated[tuple[ActorRef, ...], Field(min_length=1, max_length=16)]
    inputs: Annotated[tuple[CanonicalInputRef, ...], Field(min_length=1, max_length=16)]
    targets: Annotated[tuple[SourceTargetRef, ...], Field(max_length=3)] = ()
    repository: Repository | None = None
    context_complete: bool = False
    allowed_actions: Annotated[tuple[SourceAction, ...], Field(min_length=1, max_length=5)]

    @model_validator(mode="after")
    def coherent_membership(self) -> Self:
        actor_keys = {actor.key for actor in self.actors}
        if len(actor_keys) != len(self.actors) or actor_keys != {i.actor_key for i in self.inputs}:
            raise ValueError("Input authors must exactly match the supplied actors")
        if (
            len({i.input_id for i in self.inputs}) != len(self.inputs)
            or len({t.ticket_id for t in self.targets}) != len(self.targets)
            or len(set(self.allowed_actions)) != len(self.allowed_actions)
        ):
            raise ValueError("Group references and actions must be distinct")
        if any(t.repository != self.repository for t in self.targets):
            raise ValueError("Targets must match the group repository")
        return self


class SourceSnapshot(EvalModel):
    schema_version: Literal["source-router-snapshot-v1"] = SOURCE_SNAPSHOT_VERSION
    batch_id: UUID
    project_id: ProjectId
    scope: SourceScope
    source_revision: Revision
    generation: Revision
    fast_config_revision: Revision
    eval_config_revision: Revision
    policy_id: UUID
    policy_revision: Revision
    rule_ref: OpaqueId | None = None
    rule_sha256: SHA256 | None = None
    prompt_version: OpaqueId
    prompt_sha256: SHA256
    profile_sha256: SHA256
    price_sha256: SHA256
    budget_snapshot_sha256: SHA256
    expires_at: AwareDatetime
    groups: Annotated[tuple[SourceSnapshotGroup, ...], Field(min_length=1, max_length=16)]

    @field_validator("expires_at", mode="before")
    @classmethod
    def explicit_time(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise ValueError("An explicit timezone-aware ISO timestamp is required") from None
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("An explicit timezone-aware timestamp is required")
        return value

    @model_validator(mode="after")
    def coherent_scope(self) -> Self:
        if (self.rule_ref is None) != (self.rule_sha256 is None):
            raise ValueError("A rule reference and hash must be supplied together")
        if any(actor.scope != self.scope for g in self.groups for actor in g.actors):
            raise ValueError("Actors must match the exact source scope")
        if any(t.project_id != self.project_id for g in self.groups for t in g.targets):
            raise ValueError("Targets must match the exact project")
        for values in [
            [g.group_id for g in self.groups], [g.fast_batch_id for g in self.groups],
            [i.input_id for g in self.groups for i in g.inputs],
        ]:
            if len(values) != len(set(values)):
                raise ValueError("Snapshot groups, batches and input references must be distinct")
        return self


class ModelContextGroup(EvalModel):
    group_id: UUID
    text: Annotated[str, Field(strict=True, max_length=MAX_EVAL_BYTES)]


class ModelContext(EvalModel):
    """Ephemeral authorized text, deliberately separate from the durable snapshot."""

    groups: Annotated[tuple[ModelContextGroup, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def bounded_text(self) -> Self:
        try:
            size = sum(len(group.text.encode("utf-8")) for group in self.groups)
        except UnicodeError:
            raise ValueError("Context must contain valid UTF-8 text") from None
        if size > MAX_EVAL_BYTES or len({g.group_id for g in self.groups}) != len(self.groups):
            raise ValueError("Context must have distinct groups within its UTF-8 byte bound")
        return self


class SourceDecision(EvalModel):
    schema_version: Literal["source-router-decision-v1"] = SOURCE_DECISION_VERSION
    decision_key: SHA256
    batch_id: UUID
    group_id: UUID
    snapshot_sha256: SHA256
    fast_batch_id: UUID
    fast_batch_revision: Revision
    input_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=16)]
    input_version_fingerprints: Annotated[tuple[SHA256, ...], Field(min_length=1, max_length=16)]
    intended_action: SourceIntendedAction
    reason_code: SourceReason
    target: SourceTargetRef | None = None
    expires_at: AwareDatetime
    eligible_for_execution: Literal[False] = False

    @field_validator("eligible_for_execution", mode="before")
    @classmethod
    def never_executable(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Source suggestions cannot grant execution authority")
        return value

    @model_validator(mode="after")
    def coherent_inputs(self) -> Self:
        if len(self.input_ids) != len(self.input_version_fingerprints) or (
            len(set(self.input_ids)) != len(self.input_ids)
        ):
            raise ValueError("Decision inputs require distinct IDs and corresponding versions")
        return self


class SourceEvalProfile(EvalModel):
    provider_id: OpaqueId
    profile_ref: OpaqueId
    price: PriceQuote
    prompt_version: OpaqueId
    prompt_sha256: SHA256


class SourceEvalSettings(EvalModel):
    enabled: bool = False
    profile: SourceEvalProfile | None = None
    decision_ttl_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 60
    max_context_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_EVAL_BYTES)] = MAX_EVAL_BYTES
    max_output_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_EVAL_BYTES)] = MAX_EVAL_BYTES
    max_pending: Annotated[int, Field(strict=True, ge=1, le=10000)] = 256

    @model_validator(mode="after")
    def explicit_profile(self) -> Self:
        if self.enabled and self.profile is None:
            raise ValueError("Enabled evaluation requires an explicit signed profile")
        return self


class SourceEvalConfigure(EvalModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]
    settings: SourceEvalSettings
    review_ref: OpaqueId


class SourceEvaluateRequest(EvalModel):
    fast_batch_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def distinct_batches(self) -> Self:
        if len(set(self.fast_batch_ids)) != len(self.fast_batch_ids):
            raise ValueError("Duplicate Fast Path batch reference")
        return self
