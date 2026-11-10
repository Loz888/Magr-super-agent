"""Source identity and metadata are facts, never execution or notification grants."""

from datetime import UTC, datetime
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, Field, field_validator, model_validator

from mgar_v3.domain import Model, TriggerInput, digest

SOURCE_CONTRACT_VERSION = "source-contract-v1"
SOURCE_EVENT_VERSION: Final = "source-event-v1"


def _opaque(value: str) -> str:
    if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("An opaque identifier must be nonblank and contain no control characters")
    return value  # Do not trim, case-fold, split or otherwise reinterpret platform identifiers.


OpaqueId = Annotated[str, Field(strict=True, min_length=1, max_length=256), AfterValidator(_opaque)]
SourceKind = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{0,31}$")]
ProjectId = Annotated[str, Field(strict=True, min_length=1, max_length=100)]
Sha256 = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
Revision = Annotated[int, Field(strict=True, gt=0)]
Capability = Literal["supported", "unsupported", "unknown"]


class SourceScope(Model):
    source_kind: SourceKind
    source_instance_id: UUID
    tenant_id: OpaqueId
    installation_id: OpaqueId

    @property
    def key(self) -> str:
        return digest(self.model_dump(mode="json"))


class ActorRef(Model):
    scope: SourceScope
    id_type: OpaqueId
    actor_id: OpaqueId
    actor_kind: Literal["human", "bot", "service", "unknown"]

    @property
    def key(self) -> str:
        # A changed display name or claimed kind cannot create a second identity.
        return digest([self.scope.key, self.id_type, self.actor_id])


class SourceThreadRef(Model):
    scope: SourceScope
    conversation_id: OpaqueId
    native_thread_id: OpaqueId | None = None

    @property
    def conversation_key(self) -> str:
        return digest([self.scope.key, self.conversation_id])

    @property
    def topic_key(self) -> str | None:
        if self.native_thread_id is None:
            return None  # A whole chat is not a fallback business Thread.
        return digest([self.conversation_key, self.native_thread_id])


class SourceCapabilities(Model):
    # Required tri-state declarations: absent/unknown never silently becomes supported.
    history_read: Capability
    incremental_cursor: Capability
    reconnect_replay: Capability
    edit_events: Capability
    delete_events: Capability
    native_entity_revision: Capability


class ChannelCapabilities(Model):
    send: Capability
    idempotent_send: Capability
    query_delivery: Capability
    delete_message: Capability


class SourceInstance(Model):
    scope: SourceScope
    project_id: ProjectId
    revision: Revision = 1
    display_name: Annotated[str, Field(strict=True, max_length=200)] = ""
    adapter_version: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    capabilities: SourceCapabilities
    channel_capabilities: ChannelCapabilities
    # Only a vault reference is allowed here; configuration never includes a platform secret.
    credential_ref: Annotated[str, Field(pattern=r"^vault://[A-Za-z0-9_./-]{1,200}$")] | None = None


class SourceGap(Model):
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    reason: Literal["disconnect", "permission", "unreplayable", "unknown"]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("Gap end precedes its start")
        return self


class SourceHealth(Model):
    scope: SourceScope
    observed_at: AwareDatetime
    connection: Literal["unknown", "disabled", "online", "offline", "degraded"]
    permissions: Literal["unknown", "granted", "insufficient", "revoked"]
    last_event_at: AwareDatetime | None = None
    gaps: tuple[SourceGap, ...] = Field(default=(), max_length=100)


class EntityRevision(Model):
    origin: Literal["native", "derived", "legacy", "unavailable"]
    value: OpaqueId | None

    @model_validator(mode="after")
    def explicit_availability(self) -> Self:
        if (self.origin == "unavailable") != (self.value is None):
            raise ValueError("Only an explicitly unavailable revision can have no value")
        return self


class RepositoryHint(Model):
    full_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    resource_kind: Literal["issue", "pull_request", "workflow", "check", "repository"]
    number: Annotated[int, Field(strict=True, gt=0)] | None = None
    head_sha: Annotated[str, Field(pattern=r"^[a-fA-F0-9]{40}$")] | None = None
    # This is a hint from external data, not repository admission or a workspace grant.


class LegacyTriggerRef(Model):
    """Original legacy identifiers; never interpreted as verified platform identity."""

    source: Literal["github", "cli", "web"]
    external_event_id: Annotated[str, Field(min_length=1, max_length=200)]
    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    issue_number: Annotated[int, Field(strict=True, gt=0)]
    actor: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
    execution_mode: Literal["fixture", "live"]

    @classmethod
    def from_trigger(cls, trigger: TriggerInput) -> Self:
        return cls.model_validate(trigger.model_dump(exclude={"body", "project_id"}))


class SourceEvent(Model):
    schema_version: Literal["source-event-v1"] = SOURCE_EVENT_VERSION
    scope: SourceScope
    project_id: ProjectId
    source_revision: Revision
    delivery_id: OpaqueId
    remote_event_id: OpaqueId
    entity_id: OpaqueId
    entity_revision: EntityRevision
    event_kind: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")]
    source_thread: SourceThreadRef
    reply_to_entity_id: OpaqueId | None = None
    sender: ActorRef
    addressed: Literal["mention", "direct", "subscription", "ambient", "unknown"]
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    repository_hint: RepositoryHint | None = None
    body_ref: Sha256 | None = None
    verification_ref: OpaqueId
    historical_backfill: bool = Field(strict=True, default=False)
    provenance: Literal["verified_source", "legacy_trigger"]
    legacy_context: LegacyTriggerRef | None = None

    @field_validator("occurred_at", "received_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def consistent_scope(self) -> Self:
        if self.sender.scope != self.scope or self.source_thread.scope != self.scope:
            raise ValueError("Sender and conversation must belong to the same installation scope")
        if (self.provenance == "legacy_trigger") != (self.legacy_context is not None):
            raise ValueError("Legacy context is required only for legacy provenance")
        return self

    @property
    def event_key(self) -> str:
        return digest([self.scope.key, self.event_kind, self.remote_event_id])

    @property
    def version_key(self) -> str:
        return digest([self.event_key, self.entity_id, self.entity_revision.model_dump()])

    @property
    def delivery_key(self) -> str:
        return digest([self.scope.key, self.delivery_id])

    @property
    def entity_key(self) -> str:
        return digest([self.scope.key, self.entity_id])


class SourceMetadata(Model):
    """Unassociated inbox facts only. No body, artifact reference, actor or grant is retained.

    Staging these facts is not a durable platform ACK, a complete ingress receipt,
    or an admission decision. SA04 will add its authorized body/receipt protocol.
    """

    schema_version: Literal["source-metadata-v1"] = "source-metadata-v1"
    scope: SourceScope
    project_id: ProjectId
    source_revision: Revision
    delivery_id: OpaqueId
    remote_event_id: OpaqueId
    entity_id: OpaqueId
    entity_revision: EntityRevision
    event_kind: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")]
    source_thread: SourceThreadRef
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    provenance: Literal["verified_source", "legacy_trigger"]
    event_fingerprint: Sha256

    @model_validator(mode="after")
    def consistent_scope(self) -> Self:
        if self.source_thread.scope != self.scope:
            raise ValueError("Conversation does not belong to the metadata installation")
        return self

    @classmethod
    def from_event(cls, event: SourceEvent) -> Self:
        fields = set(cls.model_fields) - {"schema_version", "event_fingerprint"}
        data = event.model_dump(include=fields)
        return cls(**data, event_fingerprint=digest(event.model_dump(mode="json")))

    @property
    def version_key(self) -> str:
        event_key = digest([self.scope.key, self.event_kind, self.remote_event_id])
        return digest([event_key, self.entity_id, self.entity_revision.model_dump()])


class BusinessReference(Model):
    """A relation to existing business records; no new task lifecycle is defined."""

    inbox_id: UUID
    revision: Revision = 1
    thread_id: UUID
    trigger_event_id: UUID | None = None
    ticket_id: UUID | None = None
    run_id: UUID | None = None

    @model_validator(mode="after")
    def run_requires_ticket(self) -> Self:
        if self.run_id is not None and self.ticket_id is None:
            raise ValueError("A Run relation must include its existing Ticket")
        return self
