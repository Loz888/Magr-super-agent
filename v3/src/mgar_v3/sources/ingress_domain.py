"""Durable source input contracts. VerifiedDelivery is a server port, never HTTP JSON."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from mgar_v3.domain import Model, digest
from mgar_v3.sources.domain import (
    OpaqueId,
    Revision,
    Sha256,
    SourceEvent,
    SourceInstance,
    SourceThreadRef,
)
from mgar_v3.sources.ports import TransportEnvelope

INGRESS_VERSION: Literal["source-ingress-v1"] = "source-ingress-v1"
Counter = Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]
Operation = Literal["create", "edit", "delete"]


class IngressSettings(Model):
    owner: Literal["disabled", "gateway", "legacy"] = "disabled"
    policy_id: UUID | None = None
    policy_revision: Revision | None = None
    legacy_repository: (
        Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")] | None
    ) = None
    max_pending: Annotated[int, Field(strict=True, ge=1, le=10000)] = 256
    max_receipts: Annotated[int, Field(strict=True, ge=1, le=100000)] = 10000
    max_body_bytes: Annotated[int, Field(strict=True, ge=1, le=65536)] = 65536
    max_retained_bytes: Annotated[int, Field(strict=True, ge=1024, le=67108864)] = 4194304
    live_age_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = 300
    metadata_ttl_seconds: Annotated[int, Field(strict=True, ge=60, le=2592000)] = 2592000
    buffer_ttl_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 30

    @model_validator(mode="after")
    def explicit_policy(self) -> Self:
        if (self.policy_id is None) != (self.policy_revision is None):
            raise ValueError("Policy ID and revision must be declared together")
        if self.owner == "gateway" and self.policy_id is None:
            raise ValueError("Gateway ownership requires an exact signed policy")
        return self


class IngressConfigure(Model):
    expected_generation: Counter
    settings: IngressSettings
    review_ref: OpaqueId


class CursorPosition(Model):
    stream_id: OpaqueId
    epoch: OpaqueId
    sequence: Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]
    value: OpaqueId | None = None


class ItemMetadata(Model):
    event: SourceEvent
    entity_namespace: OpaqueId
    operation: Operation
    native_order: Counter | None = None
    platform_moderation: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def native_order_requires_revision(self) -> Self:
        if self.native_order is not None and self.event.entity_revision.origin != "native":
            raise ValueError("Native ordering requires a native revision")
        if self.operation == "delete" and self.event.body_ref is not None:
            raise ValueError("Deletion cannot carry body content")
        return self

    @property
    def entity_key(self) -> str:
        return digest([self.event.scope.key, self.entity_namespace, self.event.entity_id])

    @property
    def version_key(self) -> str:
        revision = self.event.entity_revision
        key = revision.model_dump() if revision.value is not None else self.event.event_key
        return digest([self.entity_key, key])

    def fingerprint(self, *, version: bool = False) -> str:
        data = self.event.model_dump(
            mode="json",
            exclude={
                "delivery_id",
                "received_at",
                "verification_ref",
                "source_revision",
                "historical_backfill",
            },
        )
        if version:
            data.pop("remote_event_id")
        return digest(
            [
                data,
                self.entity_namespace,
                self.operation,
                self.native_order,
                self.platform_moderation,
            ]
        )


class DeliveryMetadata(Model):
    schema_version: Literal["source-ingress-v1"] = INGRESS_VERSION
    source_instance_id: UUID
    source_revision: Revision
    delivery_id: OpaqueId
    payload_sha256: Sha256
    verified_at: AwareDatetime
    verification_method: Literal["platform_signature", "installation_proof"]
    items: Annotated[tuple[ItemMetadata, ...], Field(min_length=1, max_length=100)]
    cursor: CursorPosition | None = None


@dataclass(frozen=True)
class VerifiedDelivery:
    metadata: DeliveryMetadata
    bodies: tuple[bytes | None, ...] = field(repr=False)


class IngressVerifier(Protocol):
    def verify_ingress(
        self, instance: SourceInstance, envelope: TransportEnvelope
    ) -> VerifiedDelivery: ...


class IngressReceipt(Model):
    receipt_id: UUID | None
    source_instance_id: UUID
    delivery_key: Sha256
    disposition: Literal[
        "accepted", "duplicate", "rejected", "conflict", "legacy_owned", "backpressure"
    ]
    input_ids: tuple[UUID, ...] = ()
    denied_count: Counter = 0
    reason_code: str
    committed: bool
    original_trigger_id: UUID | None = None


class GapDeclaration(Model):
    stream_id: OpaqueId
    epoch: OpaqueId
    reason: Literal[
        "disconnect",
        "permission",
        "no_replay",
        "sdk_early_ack",
        "cursor_jump",
        "cursor_reset",
        "buffer_expired",
        "capacity",
    ]
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    sequence_start: Counter | None = None
    sequence_end: Counter | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("Gap end precedes start")
        if (self.sequence_start is None) != (self.sequence_end is None):
            raise ValueError("Gap sequence bounds must be declared together")
        if (
            self.sequence_start is not None
            and self.sequence_end is not None
            and self.sequence_end < self.sequence_start
        ):
            raise ValueError("Gap range is reversed")
        return self


class ReplayRequest(Model):
    expected_generation: Revision
    input_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=100)]
    review_ref: OpaqueId


class CursorReset(Model):
    expected_revision: Counter
    epoch: OpaqueId
    review_ref: OpaqueId


@dataclass(frozen=True)
class BufferPermit:
    generation: int
    identity_revisions: tuple[tuple[str, int], ...]
    expires_at: datetime
    delivery: VerifiedDelivery = field(repr=False)


class HistoryRequest(Model):
    source_thread: SourceThreadRef
    cursor: OpaqueId | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 100


@dataclass(frozen=True)
class HistoryPage:
    deliveries: tuple[VerifiedDelivery, ...] = field(repr=False)
    next_cursor: str | None = None
    complete: bool = False


class HistoryProvider(Protocol):
    def fetch(self, instance: SourceInstance, request: HistoryRequest) -> HistoryPage: ...
