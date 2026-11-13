"""Reference-only Source subscription and intake contracts, never execution grants."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from mgar_v3.domain import Model, digest
from mgar_v3.sources.domain import OpaqueId, ProjectId
from mgar_v3.super_agent.source_correlation_domain import (
    CorrelationInputRef,
    ResourceRef,
    TargetRef,
)

SUBSCRIPTION_VERSION: Literal["source-subscription-v1"] = "source-subscription-v1"
INTAKE_VERSION: Literal["source-intake-v1"] = "source-intake-v1"
MAX_TTL_SECONDS = 604800
MAX_PAGE_ITEMS = 16
MAX_PAGE_BYTES = 65536
MAX_HANDBACK_CYCLES = 3
SHA256 = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
Counter = Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]
Revision = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]
TTL = Annotated[int, Field(strict=True, ge=1, le=MAX_TTL_SECONDS)]
SubscriptionRole = Literal["primary", "context_only", "observer"]
SelectorKind = Literal["resource", "native_topic", "reply_chain"]
SubscriptionState = Literal["active", "unsubscribed", "handed_back", "expired", "fenced"]
InputIDs = Annotated[tuple[UUID, ...], Field(min_length=1, max_length=MAX_PAGE_ITEMS)]
ReasonCode = Annotated[str, Field(strict=True, pattern=r"^[A-Z0-9_]{1,100}$")]


class SubscriptionModel(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RefOnlyOutcome(SubscriptionModel):
    eligible_for_execution: Literal[False] = False
    calls_model: Literal[False] = False
    external_writes: Literal[0] = 0

    @field_validator("eligible_for_execution", "calls_model", mode="before")
    @classmethod
    def no_execution(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Source input facts never grant execution")
        return value

    @field_validator("external_writes", mode="before")
    @classmethod
    def no_external_write(cls, value: object) -> object:
        if type(value) is not int or value != 0:
            raise ValueError("Source input facts never grant external writes")
        return value


class SubscriptionSaveRequest(SubscriptionModel):
    correlation_id: UUID
    expected_correlation_revision: Revision
    seed_input_id: UUID
    resource_key: SHA256
    target: TargetRef
    role: SubscriptionRole
    selector_kind: SelectorKind
    ttl_seconds: TTL = 3600
    snapshot_sha256: SHA256
    expected_revision: Counter = 0
    review_ref: OpaqueId


class SubscriptionRenewRequest(SubscriptionModel):
    expected_revision: Revision
    snapshot_sha256: SHA256
    seed_input_id: UUID
    ttl_seconds: TTL = 3600
    review_ref: OpaqueId


class SubscriptionRebindRequest(SubscriptionRenewRequest):
    target: TargetRef


class SubscriptionCloseRequest(SubscriptionModel):
    expected_revision: Revision
    snapshot_sha256: SHA256
    review_ref: OpaqueId


class SubscriptionHandbackRequest(SubscriptionCloseRequest):
    input_ids: InputIDs
    reason_code: Literal["NEEDS_ROUTING", "OUT_OF_SCOPE", "TARGET_CLOSED"] = "NEEDS_ROUTING"

    @field_validator("input_ids")
    @classmethod
    def unique_inputs(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Canonical inputs must be distinct")
        return value


class SubscriptionDispatchRequest(SubscriptionModel):
    input_ids: InputIDs

    @field_validator("input_ids")
    @classmethod
    def unique_inputs(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Canonical inputs must be distinct")
        return value


class SubscriptionPrepareRequest(SubscriptionModel):
    epoch: Counter
    expected_cursor: Counter
    lane: SubscriptionRole
    limit: Annotated[int, Field(strict=True, ge=1, le=MAX_PAGE_ITEMS)] = MAX_PAGE_ITEMS
    max_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_PAGE_BYTES)] = MAX_PAGE_BYTES


class SubscriptionAckRequest(SubscriptionModel):
    epoch: Counter
    expected_cursor: Counter
    lane: SubscriptionRole
    snapshot_id: UUID
    snapshot_sha256: SHA256


class SubscriptionAnchor(SubscriptionModel):
    """A new owner-reviewed routing selector, not an everlasting readable seed."""

    source_instance_id: UUID
    scope_sha256: SHA256
    conversation_sha256: SHA256
    topic_sha256: SHA256 | None = None
    resource_key: SHA256
    resource: ResourceRef
    correlation_id: UUID
    correlation_revision: Revision
    selector_kind: SelectorKind
    seed_input_id: UUID
    seed_version_fingerprint: SHA256
    seed_entity_sha256: SHA256
    source_revision: Revision
    generation: Revision
    policy_id: UUID
    policy_revision: Revision

    @model_validator(mode="after")
    def native_topic_is_explicit(self) -> Self:
        if self.resource_key != self.resource.resource_key:
            raise ValueError("Selector must name its exact owner-reviewed resource")
        if self.selector_kind == "native_topic" and self.topic_sha256 is None:
            raise ValueError("A native topic selector requires an authenticated topic")
        return self


class SourceSubscription(SubscriptionModel):
    schema_version: Literal["source-subscription-v1"] = SUBSCRIPTION_VERSION
    subscription_id: UUID
    project_id: ProjectId
    revision: Revision
    anchor: SubscriptionAnchor
    target: TargetRef
    role: SubscriptionRole
    state: SubscriptionState
    authority_sha256: SHA256
    valid_from: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def bounded_lease(self) -> Self:
        if (self.target.project_id != self.project_id
                or self.anchor.resource.project_id != self.project_id):
            raise ValueError("Subscription target belongs to another project")
        duration = (self.expires_at - self.valid_from).total_seconds()
        if not 0 < duration <= MAX_TTL_SECONDS:
            raise ValueError("Subscription requires a bounded signed lifetime")
        return self

    @property
    def bound_run_id(self) -> UUID:
        return self.target.run_id


class SubscriptionInputFacts(SubscriptionModel):
    """Trusted current Engine facts for pure rules; never an HTTP request DTO."""

    input: CorrelationInputRef
    unit_key: SHA256
    scope_sha256: SHA256
    conversation_sha256: SHA256
    topic_sha256: SHA256 | None = None
    resource_keys: Annotated[tuple[SHA256, ...], Field(max_length=16)] = ()
    reply_entity_sha256: SHA256 | None = None
    actor_kind: Literal["human", "bot", "service", "unknown"]
    context_only: bool
    historical: bool
    is_current: bool
    receive_allowed: bool
    read_allowed: bool
    task_controller: bool
    follow_up_allowed: bool
    purpose: Literal["context", "follow_up", "status_query", "new_work", "control"]
    expires_at: AwareDatetime

    @field_validator("resource_keys")
    @classmethod
    def unique_resources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Exact resources must be distinct")
        return value


class SubscriptionRunFacts(SubscriptionModel):
    target: TargetRef
    status: Literal[
        "queued", "running", "waiting_input", "waiting_approval", "paused",
        "succeeded", "failed", "cancelled",
    ]
    ticket_open: bool
    original_primary: bool
    authority_current: bool
    policy_mode: Literal["disabled", "shadow", "auto_investigate", "auto_repair"]


class SubscriptionDecision(RefOnlyOutcome):
    subscription_id: UUID
    subscription_revision: Revision
    input_id: UUID
    unit_key: SHA256
    outcome: Literal[
        "deliver_ref", "observe_ref", "would_deliver", "no_match", "ambiguous",
        "blocked", "closed_followup", "needs_explicit_rebind",
    ]
    lane: SubscriptionRole
    purpose: Literal["context", "directive_proposal", "observer", "status_query"]
    reason_code: ReasonCode

    @model_validator(mode="after")
    def readonly_purpose(self) -> Self:
        if self.lane != "primary" and self.purpose == "directive_proposal":
            raise ValueError("Readonly Source lanes cannot carry a directive")
        if (self.lane == "observer") != (self.purpose == "observer"):
            raise ValueError("Observer decisions remain outside worker input lanes")
        return self


class SubscriptionMatches(RefOnlyOutcome):
    subscription_ids: Annotated[tuple[UUID, ...], Field(max_length=64)]
    ambiguous: bool
    reason_code: ReasonCode


class SourceDeliveryRef(SubscriptionModel):
    delivery_id: UUID
    subscription_id: UUID
    subscription_revision: Revision
    project_id: ProjectId
    run_id: UUID
    sequence: Revision
    input: CorrelationInputRef
    unit_key: SHA256
    lane: SubscriptionRole
    purpose: Literal["context", "directive_proposal", "observer", "status_query"]
    disposition: Literal["ready", "blocked"]
    reason_code: ReasonCode
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def readonly_lanes(self) -> Self:
        if self.input.project_id != self.project_id:
            raise ValueError("Delivery input belongs to another project")
        if self.lane != "primary" and self.purpose == "directive_proposal":
            raise ValueError("A readonly lane cannot hold a directive")
        if (self.lane == "observer") != (self.purpose == "observer"):
            raise ValueError("Observer receipts are separate from worker input lanes")
        return self


class SubscriptionHeadRef(SubscriptionModel):
    subscription_id: UUID
    revision: Revision
    authority_sha256: SHA256


class SourceIntakeSnapshot(SubscriptionModel):
    schema_version: Literal["source-intake-v1"] = INTAKE_VERSION
    snapshot_id: UUID
    project_id: ProjectId
    run_id: UUID
    epoch: Counter
    lane: SubscriptionRole
    from_cursor: Counter
    through_cursor: Revision
    items: Annotated[tuple[SourceDeliveryRef, ...], Field(min_length=1, max_length=MAX_PAGE_ITEMS)]
    subscription_heads: Annotated[
        tuple[SubscriptionHeadRef, ...], Field(min_length=1, max_length=MAX_PAGE_ITEMS)
    ]
    consumer_state_sha256: SHA256
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def exact_page(self) -> Self:
        sequences = tuple(item.sequence for item in self.items)
        if sequences != tuple(sorted(set(sequences))) or sequences[0] <= self.from_cursor:
            raise ValueError("Intake page must preserve exact ordered cursor membership")
        if sequences[-1] != self.through_cursor:
            raise ValueError("Intake cursor must name the final frozen delivery")
        if len({item.delivery_id for item in self.items}) != len(self.items):
            raise ValueError("A delivery may occur only once in a page")
        if len({item.input.input_id for item in self.items}) != len(self.items):
            raise ValueError("A canonical Source input may occur only once in a page")
        if any(item.run_id != self.run_id or item.project_id != self.project_id
               or item.lane != self.lane for item in self.items):
            raise ValueError("Page deliveries must share this exact Run, project and lane")
        heads = {head.subscription_id: head for head in self.subscription_heads}
        if len(heads) != len(self.subscription_heads):
            raise ValueError("Subscription heads must be distinct")
        if set(heads) != {item.subscription_id for item in self.items} or any(
            heads[item.subscription_id].revision != item.subscription_revision
            for item in self.items
        ):
            raise ValueError("Page must bind every exact subscription revision")
        if len(self.model_dump_json().encode("utf-8")) > MAX_PAGE_BYTES:
            raise ValueError("Metadata intake exceeds its byte bound")
        return self

    @property
    def snapshot_sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


class HandbackFacts(SubscriptionModel):
    input_id: UUID
    version_fingerprint: SHA256
    problem_key: SHA256
    window_key: SHA256
    already_handed_back: bool
    cycle_count: Counter


class HandbackDecision(RefOnlyOutcome):
    input_id: UUID
    allowed: bool
    reason_code: Literal["NEEDS_ROUTING", "INPUT_ALREADY_HANDED_BACK", "HANDBACK_CYCLE_LIMIT"]
    semantic_evaluation: Literal["NOT_RUN"] = "NOT_RUN"


class SourceConsumptionReceipt(RefOnlyOutcome):
    receipt_id: UUID
    snapshot_id: UUID
    snapshot_sha256: SHA256
    run_id: UUID
    epoch: Counter
    lane: SubscriptionRole
    from_cursor: Counter
    through_cursor: Revision
    delivery_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=MAX_PAGE_ITEMS)]
    status: Literal["acknowledged_refs"] = "acknowledged_refs"

    @model_validator(mode="after")
    def exact_receipt(self) -> Self:
        if self.through_cursor <= self.from_cursor:
            raise ValueError("Receipt must acknowledge forward progress")
        if len(set(self.delivery_ids)) != len(self.delivery_ids):
            raise ValueError("A delivery may be acknowledged only once per receipt")
        return self


@dataclass(frozen=True)
class MaterializedSourceItem:
    delivery_id: UUID
    input_id: UUID
    text: str = field(repr=False)


@dataclass(frozen=True)
class MaterializedSourcePage:
    snapshot: SourceIntakeSnapshot
    items: tuple[MaterializedSourceItem, ...] = field(repr=False)
    materialized_at: datetime
