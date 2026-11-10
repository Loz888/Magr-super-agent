"""Engine-owned source catalogue and minimal unassociated metadata.

No connection is opened here. Adapters are registered as ports; obtaining a port
does not call it or grant Receive/Execution/Notification permission.
"""

from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, EventReceipt, TriggerInput
from mgar_v3.sources.domain import (
    BusinessReference,
    ChannelCapabilities,
    SourceCapabilities,
    SourceEvent,
    SourceInstance,
    SourceMetadata,
)
from mgar_v3.sources.legacy import LegacyTriggerAdapter
from mgar_v3.sources.ports import Channel, SourceAdapter, TransportEnvelope

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine

SourceCapabilityName = Literal[
    "history_read",
    "incremental_cursor",
    "reconnect_replay",
    "edit_events",
    "delete_events",
    "native_entity_revision",
]
ChannelCapabilityName = Literal["send", "idempotent_send", "query_delivery", "delete_message"]


class SourceRegistry:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self._adapters: dict[tuple[str, str], SourceAdapter] = {}
        self._channels: dict[str, Channel] = {}

    def add_adapter(self, adapter: SourceAdapter) -> None:
        key = (adapter.source_kind, adapter.adapter_version)
        if key in self._adapters:
            raise DomainError("SOURCE_ADAPTER_EXISTS", "Adapter already registered")
        self._adapters[key] = adapter

    def add_channel(self, channel: Channel) -> None:
        if channel.source_kind in self._channels:
            raise DomainError("SOURCE_CHANNEL_EXISTS", "Channel already registered")
        self._channels[channel.source_kind] = channel

    def register_instance(self, instance: SourceInstance) -> SourceInstance:
        """Append a configuration revision. This never enables a source connection."""
        self._project(instance.project_id)
        scope = instance.scope
        try:
            with self.engine.transaction() as conn:
                conn.execute(
                    "INSERT INTO app.source_instances(source_instance_id,project_id,source_kind,"
                    "tenant_id,installation_id) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT(source_instance_id) DO NOTHING",
                    (
                        scope.source_instance_id,
                        instance.project_id,
                        scope.source_kind,
                        scope.tenant_id,
                        scope.installation_id,
                    ),
                )
                identity = conn.execute(
                    "SELECT * FROM app.source_instances WHERE source_instance_id=%s FOR UPDATE",
                    (scope.source_instance_id,),
                ).fetchone()
                assert identity is not None
                if (
                    identity["project_id"] != instance.project_id
                    or identity["source_kind"] != scope.source_kind
                    or identity["tenant_id"] != scope.tenant_id
                    or identity["installation_id"] != scope.installation_id
                ):
                    raise DomainError(
                        "SOURCE_IDENTITY_IMMUTABLE", "Installation identity cannot change"
                    )
                prior = conn.execute(
                    "SELECT revision,definition FROM app.source_instance_versions "
                    "WHERE source_instance_id=%s ORDER BY revision DESC LIMIT 1",
                    (scope.source_instance_id,),
                ).fetchone()
                if prior and prior["revision"] == instance.revision:
                    if SourceInstance.model_validate(prior["definition"]) == instance:
                        return instance
                    raise DomainError(
                        "SOURCE_REVISION_CONFLICT", "Configuration revision already exists"
                    )
                expected = int(prior["revision"]) + 1 if prior else 1
                if instance.revision != expected:
                    raise DomainError(
                        "SOURCE_REVISION_CONFLICT", "Configuration must append the next revision"
                    )
                conn.execute(
                    "INSERT INTO app.source_instance_versions"
                    "(source_instance_id,revision,definition) "
                    "VALUES (%s,%s,%s)",
                    (
                        scope.source_instance_id,
                        instance.revision,
                        Jsonb(instance.model_dump(mode="json")),
                    ),
                )
        except UniqueViolation as exc:
            raise DomainError(
                "SOURCE_INSTALLATION_EXISTS", "Installation already has a source identity"
            ) from exc
        return instance

    def _project(self, project_id: str) -> None:
        if project_id != self.engine.settings.project_id:
            raise DomainError("PROJECT_DENIED", "Source is outside the configured project", 403)

    def _instance(
        self, conn: "Connection", instance_id: UUID, revision: int | None = None
    ) -> SourceInstance:
        row = conn.execute(
            "SELECT v.definition FROM app.source_instance_versions v "
            "JOIN app.source_instances s USING(source_instance_id) "
            "WHERE s.source_instance_id=%s AND s.project_id=%s "
            "AND (%s::bigint IS NULL OR v.revision=%s) ORDER BY v.revision DESC LIMIT 1",
            (instance_id, self.engine.settings.project_id, revision, revision),
        ).fetchone()
        if row is None:
            raise DomainError(
                "SOURCE_NOT_FOUND", "Source version was not found in this project", 404
            )
        return SourceInstance.model_validate(row["definition"])

    def get_instance(self, instance_id: UUID, revision: int | None = None) -> SourceInstance:
        with self.engine.transaction() as conn:
            return self._instance(conn, instance_id, revision)

    def stage_metadata(self, metadata: SourceMetadata) -> UUID:
        """Hold content-free facts; do not ACK, route or create a business input."""
        self._project(metadata.project_id)
        inbox_id = uuid4()
        try:
            with self.engine.transaction() as conn:
                instance = self._instance(
                    conn, metadata.scope.source_instance_id, metadata.source_revision
                )
                if instance.scope != metadata.scope:
                    raise DomainError(
                        "SOURCE_SCOPE_MISMATCH", "Metadata installation does not match", 403
                    )
                conn.execute(
                    "INSERT INTO app.source_inbox(inbox_id,source_instance_id,source_revision,"
                    "project_id,event_kind,remote_event_id,entity_id,revision_origin,revision_value,"
                    "version_key,metadata,occurred_at,received_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        inbox_id,
                        metadata.scope.source_instance_id,
                        metadata.source_revision,
                        metadata.project_id,
                        metadata.event_kind,
                        metadata.remote_event_id,
                        metadata.entity_id,
                        metadata.entity_revision.origin,
                        metadata.entity_revision.value,
                        metadata.version_key,
                        Jsonb(metadata.model_dump(mode="json")),
                        metadata.occurred_at,
                        metadata.received_at,
                    ),
                )
        except UniqueViolation as exc:
            raise DomainError(
                "SOURCE_METADATA_EXISTS", "Metadata version is already staged; no ACK was issued"
            ) from exc
        return inbox_id

    def _metadata(self, conn: "Connection", inbox_id: UUID) -> SourceMetadata:
        row = conn.execute(
            "SELECT metadata FROM app.source_inbox WHERE inbox_id=%s AND project_id=%s",
            (inbox_id, self.engine.settings.project_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                "SOURCE_METADATA_NOT_FOUND", "Metadata was not found in this project", 404
            )
        return SourceMetadata.model_validate(row["metadata"])

    def get_metadata(self, inbox_id: UUID) -> SourceMetadata:
        with self.engine.transaction() as conn:
            return self._metadata(conn, inbox_id)

    def link_legacy_receipt(self, inbox_id: UUID, receipt: EventReceipt) -> BusinessReference:
        """Relate an already-ingested legacy event; never infer or create a new Task."""
        with self.engine.transaction() as conn:
            metadata = self._metadata(conn, inbox_id)
            row = conn.execute(
                "SELECT e.* FROM app.trigger_events e JOIN app.threads h USING(thread_id) "
                "WHERE e.event_id=%s AND h.project_id=%s",
                (receipt.event_id, self.engine.settings.project_id),
            ).fetchone()
            if row is None:
                raise DomainError("SOURCE_RECEIPT_MISMATCH", "Existing trigger was not found", 409)
            original = TriggerInput.model_validate(row["payload"])
            expected = SourceMetadata.from_event(
                LegacyTriggerAdapter.wrap(original, received_at=metadata.received_at)
            )
            actual = (row["thread_id"], row["ticket_id"], row["run_id"], row["route"])
            supplied = (receipt.thread_id, receipt.ticket_id, receipt.run_id, receipt.route)
            if metadata != expected or actual != supplied:
                raise DomainError(
                    "SOURCE_RECEIPT_MISMATCH",
                    "Metadata does not match the existing legacy receipt",
                    409,
                )
            reference = BusinessReference(
                inbox_id=inbox_id,
                thread_id=receipt.thread_id,
                trigger_event_id=receipt.event_id,
                ticket_id=receipt.ticket_id,
                run_id=receipt.run_id,
            )
            conn.execute(
                "INSERT INTO app.source_business_links(inbox_id,project_id,revision,thread_id,"
                "trigger_event_id,ticket_id,run_id) VALUES (%s,%s,1,%s,%s,%s,%s) "
                "ON CONFLICT(inbox_id,revision) DO NOTHING",
                (
                    inbox_id,
                    metadata.project_id,
                    receipt.thread_id,
                    receipt.event_id,
                    receipt.ticket_id,
                    receipt.run_id,
                ),
            )
            return reference

    def adapter_for(
        self, instance_id: UUID, *, require: SourceCapabilityName | None = None
    ) -> SourceAdapter:
        instance = self.get_instance(instance_id)
        adapter = self._adapter(instance)
        if require is not None and (
            require not in SourceCapabilities.model_fields
            or getattr(instance.capabilities, require) != "supported"
            or getattr(adapter.capabilities, require) != "supported"
        ):
            raise DomainError(
                "SOURCE_CAPABILITY_UNAVAILABLE",
                "Source capability is unavailable or unverified",
                409,
            )
        return adapter

    def _adapter(self, instance: SourceInstance) -> SourceAdapter:
        adapter = self._adapters.get((instance.scope.source_kind, instance.adapter_version))
        if adapter is None:
            raise DomainError("SOURCE_ADAPTER_MISSING", "Source adapter is not installed", 409)
        return adapter

    def verify_and_normalize(
        self, instance_id: UUID, envelope: TransportEnvelope
    ) -> tuple[SourceEvent, ...]:
        """Check adapter output against trusted registration, with no DB transaction open.

        This does not check ReceivePolicy, persist content, ACK a platform, or route
        a task. The future gateway must apply the SA03/SA04 receive protocol.
        """
        instance = self.get_instance(instance_id)
        adapter = self._adapter(instance)
        verified = adapter.verify(instance, envelope)
        if verified.scope != instance.scope:
            raise DomainError(
                "SOURCE_VERIFICATION_MISMATCH", "Verified installation does not match", 403
            )
        events = adapter.normalize(instance, verified)
        if len(events) > 100:
            raise DomainError("SOURCE_EVENT_BATCH_LIMIT", "Adapter returned too many events", 422)
        for event in events:
            if (
                event.scope != instance.scope
                or event.project_id != instance.project_id
                or event.source_revision != instance.revision
                or event.verification_ref != verified.verification_ref
                or event.provenance != "verified_source"
            ):
                raise DomainError(
                    "SOURCE_NORMALIZATION_MISMATCH",
                    "Normalized event escapes its verified binding",
                    403,
                )
        return events

    def channel_for(self, instance_id: UUID, *, require: ChannelCapabilityName) -> Channel:
        instance = self.get_instance(instance_id)
        channel = self._channels.get(instance.scope.source_kind)
        if channel is None:
            raise DomainError("SOURCE_CHANNEL_MISSING", "Channel is not installed", 409)
        if (
            require not in ChannelCapabilities.model_fields
            or getattr(instance.channel_capabilities, require) != "supported"
            or getattr(channel.capabilities, require) != "supported"
        ):
            raise DomainError(
                "SOURCE_CAPABILITY_UNAVAILABLE",
                "Channel capability is unavailable or unverified",
                409,
            )
        return channel
