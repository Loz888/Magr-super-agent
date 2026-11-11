"""Explicit server composition: durable sole-Engine intake, never an automatic listener."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx

from mgar_v3.domain import DomainError
from mgar_v3.sources.feishu import FeishuAdapter, Filtered, SocketProof
from mgar_v3.sources.feishu_domain import (
    VERSION,
    FeishuActivation,
    FeishuSecrets,
    FeishuSettings,
    FeishuTransportReceipt,
)
from mgar_v3.sources.feishu_store import FeishuSources
from mgar_v3.sources.feishu_topics import record_verified_topic
from mgar_v3.sources.gateway import SourceGateway
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.ingress_domain import GapDeclaration, IngressReceipt
from mgar_v3.sources.ports import TransportEnvelope

if TYPE_CHECKING:
    from mgar_v3.sources.feishu_broker import FeishuReadBroker

Receipt = IngressReceipt | FeishuTransportReceipt


class FeishuRuntime:
    def __init__(
        self,
        sources: FeishuSources,
        source_id: UUID,
        revision: int,
        generation: int,
        secrets: FeishuSecrets,
        activation: FeishuActivation | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        checkpoint: Callable[[str], None] | None = None,
    ):
        self.sources, self.source_id = sources, source_id
        self.revision, self.generation = revision, generation
        self.secrets, self.activation = secrets, activation
        self.proof, self.checkpoint = SocketProof(), checkpoint
        self._lost_at: datetime | None = None
        from mgar_v3.sources.feishu_broker import FeishuReadBroker

        self.broker: FeishuReadBroker = FeishuReadBroker(self, transport=transport)

    def current(self, operation: str) -> FeishuSettings:
        row, value = self.sources.current(self.source_id)
        if self.activation is None:
            raise DomainError("FEISHU_ACTIVATION_REQUIRED", "Feishu runtime is not activated", 403)
        self.activation.check(self.source_id, row["revision"], self.generation, operation)
        if not value.enabled or row["revision"] != self.revision:
            raise DomainError("FEISHU_CONFIG_FENCED", "Feishu settings changed", 403)
        self.sources.assert_policy_ceiling(self.source_id, value, self.generation)
        return value

    def adapter(self, transport: str = "webhook") -> FeishuAdapter:
        value = self.current(transport)
        return FeishuAdapter(
            value,
            self.secrets,
            socket_proof=self.proof if transport == "socket" else None,
            check_current=lambda: self.current(transport),
        )

    def receive(self, envelope: TransportEnvelope, *, transport: str = "webhook") -> Receipt:
        with self.sources.engine._mutex:
            adapter = self.adapter(transport)
            instance = self.sources.identities.registry.get_instance(self.source_id)
            try:
                delivery = adapter.verify_ingress(instance, envelope)
            except Filtered as error:
                # Authentication succeeded before the permanent filter. This is bounded
                # transport evidence only, not another business inbox or source input.
                observation = (
                    adapter.delivery(instance, envelope, transport, observation_only=True)
                    if error.code
                    in {
                        "FEISHU_SENDER_FILTERED",
                        "FEISHU_ADDRESS_FILTERED",
                        "FEISHU_CONTENT_UNSUPPORTED",
                    }
                    else None
                )
                return self.sources.filtered(
                    self.source_id,
                    error.event_id,
                    hashlib.sha256(envelope.payload).hexdigest(),
                    error.code,
                    self.generation,
                    observation=observation,
                )
            item = delivery.metadata.items[0]
            if item.operation != "delete":
                subject = adapter.verify_identity(instance, envelope)
                with self.sources.engine.transaction() as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM app.source_identity_evidence WHERE source_instance_id=%s "
                        "AND remote_event_id=%s",
                        (self.source_id, subject.remote_event_id),
                    ).fetchone()
                now = datetime.now(UTC)
                if exists is None and now - timedelta(seconds=300) <= subject.occurred_at <= (
                    now + timedelta(seconds=30)
                ):
                    SourceIdentities(
                        self.sources.engine, {("feishu", VERSION): adapter}
                    ).authenticate(self.source_id, envelope)
            gateway = SourceGateway(
                self.sources.engine.source_ingress,
                self.source_id,
                self.generation,
                {("feishu", VERSION): adapter},
                ack_mode="after_handler",
                checkpoint=self.checkpoint,
            )
            result = gateway.receive(envelope)
            record_verified_topic(self, adapter, instance, envelope, transport, result)
            return result

    def webhook(self, envelope: TransportEnvelope) -> dict[str, Any]:
        # No socket-proof header is ever consumed by this public transport.
        with self.sources.engine._mutex:
            adapter = self.adapter("webhook")
            instance = self.sources.identities.registry.get_instance(self.source_id)
            _, challenge, method = adapter.authenticated(instance, envelope, transport="webhook")
            if method == "challenge":
                return {"challenge": challenge}  # Authenticated protocol handshake, not a message.
            result = self.receive(envelope, transport="webhook")
            return {"code": 0, "receipt": result.model_dump(mode="json")}

    def receive_socket(self, payload: bytes, delivery_id: str, epoch: str) -> Receipt:
        with self.sources.engine._mutex:
            self.current("socket")
            envelope = self.proof.seal(payload, delivery_id, epoch)
            result = self.receive(envelope, transport="socket")
            if not result.committed:
                raise DomainError("FEISHU_NOT_COMMITTED", "No durable SDK success ACK", 503)
            self.sources.observe_health(
                self.source_id,
                self.revision,
                self._mode(),
                connection="online",
                permissions="unknown",
                code="FEISHU_EVENT_COMMITTED",
                last_event=True,
                gap_start=self._lost_at,
            )
            return result

    def _mode(self) -> str:
        return self.activation.mode if self.activation is not None else "controlled"

    def socket_connected(self) -> str:
        with self.sources.engine._mutex:
            self.current("socket")
            epoch = self.proof.connected()
            self.sources.observe_health(
                self.source_id,
                self.revision,
                self._mode(),
                connection="online",
                permissions="unknown",
                code="FEISHU_CONNECTED",
                gap_start=self._lost_at,
            )
            if self._lost_at is not None:
                # A new connection ends an observed outage; it does not establish replay.
                self.sources.engine.source_ingress.gap(
                    self.source_id,
                    GapDeclaration(
                        stream_id="feishu-socket",
                        epoch=epoch,
                        reason="no_replay",
                        started_at=self._lost_at,
                        ended_at=datetime.now(UTC),
                    ),
                )
            return epoch

    def socket_lost(self, code: str = "FEISHU_DISCONNECTED") -> None:
        with self.sources.engine._mutex:
            self.proof.disconnected()
            self._lost_at = datetime.now(UTC)
            self.sources.observe_health(
                self.source_id,
                self.revision,
                self._mode(),
                connection="offline",
                permissions="unknown",
                code=code,
                gap_start=self._lost_at,
            )
            self.sources.engine.source_ingress.gap(
                self.source_id,
                GapDeclaration(
                    stream_id="feishu-socket",
                    epoch="disconnected",
                    reason="disconnect",
                    started_at=self._lost_at,
                ),
            )

    def socket_reconnected(self) -> str:
        return self.socket_connected()

    def fence(self, code: str, permission: str = "revoked") -> None:
        self.proof.disconnected()
        self.sources.fence(self.source_id, self.revision, self._mode(), code, permission)

    def status(self) -> dict[str, Any]:
        with self.sources.engine._mutex:
            current = False
            try:
                self.current("health")
                current = True
            except DomainError:
                pass
            # Fencing revokes authority, not a trustworthy diagnosis already observed.
            # Re-read the actual settings revision: an old Runtime cannot project facts
            # into a newly configured source, nor turn stale granted into permission.
            with self.sources.engine.transaction() as conn:
                row = conn.execute(
                    "SELECT h.*,s.revision AS settings_revision FROM app.feishu_source_health h "
                    "JOIN app.feishu_source_settings s USING(source_instance_id) "
                    "WHERE h.source_instance_id=%s AND s.project_id=%s",
                    (self.source_id, self.sources.engine.settings.project_id),
                ).fetchone()
            if row is not None:
                age = datetime.now(UTC) - row["observed_at"]
                if (
                    row["config_revision"] == row["settings_revision"] == self.revision
                    and row["mode"] == self._mode()
                    and timedelta(seconds=-30) <= age < timedelta(minutes=5)
                    and (current or row["permissions"] in {"insufficient", "revoked"})
                ):
                    return {
                        "connection": row["connection"]
                        if current or row["connection"] in {"offline", "degraded", "disabled"}
                        else "degraded",
                        "permissions": row["permissions"],
                        "observed_at": row["observed_at"],
                        "mode": row["mode"],
                        "reason_code": row["reason_code"],
                        "gap_started_at": row["gap_started_at"],
                        "runtime_current": current,
                        "live_source_channel_gate": "NOT_RUN",
                    }
            return {
                "connection": "disabled" if self.activation is None else "unknown",
                "permissions": "unknown",
                "mode": self._mode(),
                "runtime_current": False,
                "live_source_channel_gate": "NOT_RUN",
            }
