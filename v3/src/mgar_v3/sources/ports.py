"""Transport/context and channel ports. Implementations obtain no business authority here."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field

from mgar_v3.domain import Model
from mgar_v3.sources.domain import (
    ChannelCapabilities,
    OpaqueId,
    Sha256,
    SourceCapabilities,
    SourceEvent,
    SourceHealth,
    SourceInstance,
    SourceScope,
    SourceThreadRef,
)


@dataclass(frozen=True)
class TransportEnvelope:
    headers: dict[str, str] = field(repr=False)
    payload: bytes = field(repr=False)


@dataclass(frozen=True)
class VerifiedEnvelope:
    """Produced by trusted adapter verification, not deserialized from source JSON."""

    scope: SourceScope
    verification_ref: str
    payload: bytes = field(repr=False)


class ContextRequest(Model):
    source_thread: SourceThreadRef
    entity_ids: tuple[OpaqueId, ...] = Field(default=(), max_length=100)
    cursor: OpaqueId | None = None
    limit: int = Field(strict=True, ge=1, le=100)


class ContextPage(Model):
    events: tuple[SourceEvent, ...] = Field(max_length=100)
    next_cursor: OpaqueId | None = None
    complete: bool = Field(strict=True)


class ChannelMessage(Model):
    """Only a Dispatcher with a current authorized Action may use this port."""

    action_id: UUID
    destination: SourceThreadRef
    payload_hash: Sha256
    body_ref: Sha256
    idempotency_key: OpaqueId


class DeliveryLookup(Model):
    scope: SourceScope
    idempotency_key: OpaqueId
    remote_message_id: OpaqueId | None = None


class DeliveryReceipt(Model):
    scope: SourceScope
    idempotency_key: OpaqueId
    outcome: Literal["accepted", "rejected", "unknown", "not_found"]
    remote_message_id: OpaqueId | None = None


class SourceAdapter(Protocol):
    @property
    def source_kind(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    @property
    def capabilities(self) -> SourceCapabilities: ...

    def configuration_schema(self) -> dict[str, Any]: ...

    def verify(self, instance: SourceInstance, envelope: TransportEnvelope) -> VerifiedEnvelope: ...

    def normalize(
        self, instance: SourceInstance, envelope: VerifiedEnvelope
    ) -> tuple[SourceEvent, ...]: ...

    def fetch_context(self, instance: SourceInstance, request: ContextRequest) -> ContextPage: ...

    def health(self, instance: SourceInstance) -> SourceHealth: ...


class Channel(Protocol):
    @property
    def source_kind(self) -> str: ...

    @property
    def capabilities(self) -> ChannelCapabilities: ...

    def send(self, instance: SourceInstance, message: ChannelMessage) -> DeliveryReceipt: ...

    def query(self, instance: SourceInstance, lookup: DeliveryLookup) -> DeliveryReceipt: ...
