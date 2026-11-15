"""Trusted server channel registration; no HTTP provider or credential injection."""

from dataclasses import dataclass
from typing import Protocol

from mgar_v3.domain import digest
from mgar_v3.sources.domain import ActorRef, ChannelCapabilities, SourceInstance, SourceScope
from mgar_v3.sources.ports import ChannelMessage, DeliveryLookup, DeliveryReceipt


class NotificationChannel(Protocol):
    """Versioned Channel extension: plaintext exists only for the immediate send."""

    def send(
        self, instance: SourceInstance, message: ChannelMessage, body: str
    ) -> DeliveryReceipt: ...

    def query(self, instance: SourceInstance, lookup: DeliveryLookup) -> DeliveryReceipt: ...


@dataclass(frozen=True)
class NotificationProfile:
    scope: SourceScope
    instance_revision: int
    sender: ActorRef
    capabilities: ChannelCapabilities
    channel: NotificationChannel
    mode: str = "fixture"
    version: str = "source-notification-channel-v1"

    def validate(self) -> None:
        if (
            self.mode != "fixture"
            or self.version != "source-notification-channel-v1"
            or self.instance_revision < 1
            or self.sender.scope != self.scope
            or self.sender.actor_kind not in {"bot", "service"}
            or self.capabilities.send != "supported"
            or self.scope.source_kind not in {"github", "feishu"}
        ):
            raise ValueError("Explicit controlled channel and exact verified installation required")

    @property
    def profile_sha256(self) -> str:
        return digest(
            {
                "scope": self.scope.model_dump(mode="json"),
                "instance_revision": self.instance_revision,
                "sender": self.sender.model_dump(mode="json"),
                "capabilities": self.capabilities.model_dump(mode="json"),
                "mode": self.mode,
                "version": self.version,
            }
        )
