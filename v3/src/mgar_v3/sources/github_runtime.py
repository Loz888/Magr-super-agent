"""Explicit server composition, never auto-started by saved settings or secret presence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.sources.gateway import SourceGateway
from mgar_v3.sources.github import GitHubAdapter
from mgar_v3.sources.github_broker import GitHubReadBroker
from mgar_v3.sources.github_domain import VERSION
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.ingress_domain import IngressReceipt
from mgar_v3.sources.ports import TransportEnvelope


class GitHubRuntime:
    def __init__(self, broker: GitHubReadBroker):
        self.broker = broker
        self._health: dict[str, object] | None = None

    def receive(self, envelope: TransportEnvelope, generation: int) -> IngressReceipt:
        # One original Engine mutex linearizes SA02 proof observation and durable SA04
        # intake. HMAC work is local and no SQL transaction spans verification.
        with self.broker.sources.engine._mutex:
            return self._receive(envelope, generation)

    def _receive(self, envelope: TransportEnvelope, generation: int) -> IngressReceipt:
        broker, sources = self.broker, self.broker.sources
        settings = broker.current("webhook")
        instance = sources.identities.registry.get_instance(broker.source_id)
        adapter = GitHubAdapter(
            settings, broker._secrets.webhook_secret, lambda: broker.current("webhook")
        )
        # Verify once before observing a new identity. Only minimal identity facts persist;
        # roles are still a separate authenticated owner's decision.
        subject = adapter.verify_identity(instance, envelope)
        with sources.engine.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM app.source_identity_evidence WHERE source_instance_id=%s "
                "AND remote_event_id=%s",
                (broker.source_id, subject.remote_event_id),
            ).fetchone()
        now = datetime.now(UTC)
        selected = (
            subject.actor.actor_kind == "human"
            and subject.actor.actor_id in {str(i) for i in settings.sender_ids}
        ) or (
            subject.actor.actor_kind == "bot"
            and subject.actor.actor_id in {str(i) for i in settings.context_bot_ids}
        )
        if (
            selected
            and exists is None
            and now - timedelta(seconds=300) <= subject.occurred_at <= now + timedelta(seconds=30)
        ):
            identities = SourceIdentities(sources.engine, {("github", VERSION): adapter})
            identities.authenticate(broker.source_id, envelope)
        gateway = SourceGateway(
            sources.engine.source_ingress,
            broker.source_id,
            generation,
            {("github", VERSION): adapter},
            ack_mode="after_handler",
        )
        return gateway.receive(envelope)

    @property
    def source_id(self) -> UUID:
        return self.broker.source_id

    def probe_permissions(self) -> dict[str, object]:
        self._health = self.broker.probe_permissions()
        return self._health

    def status(self) -> dict[str, object]:
        if self._health is not None:
            at = self._health["observed_at"]
            if isinstance(at, datetime) and datetime.now(UTC) - at < timedelta(minutes=5):
                try:
                    self.broker.current("health")
                    return self._health
                except DomainError:
                    pass
        return {
            "connection": "disabled" if self.broker.activation is None else "unknown",
            "permissions": "unknown",
            "live_source_channel_gate": "NOT_RUN",
        }
