"""Actual SIGKILL child; fixture remote persists only an idempotency receipt hash."""

import json
import os
import signal
import sys
from pathlib import Path

from mgar_v3.coordination.engine import Engine
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import Settings
from mgar_v3.sources.domain import ActorRef
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.sources.ports import DeliveryReceipt
from mgar_v3.super_agent.source_notification_ports import NotificationProfile
from mgar_v3.super_agent.source_notification_transport import NotificationTransport


def kill():
    os.kill(os.getpid(), signal.SIGKILL)


class FileReceiptChannel:
    def __init__(self, engine, remote):
        self.engine, self.remote = engine, Path(remote)

    def send(self, instance, message, body):
        assert self.engine.connection.info.transaction_status.name == "IDLE"
        with self.remote.open("x") as f:
            json.dump(
                {
                    "scope_key": instance.scope.key,
                    "key": message.idempotency_key,
                    "remote_id": "fixture-remote-accepted",
                },
                f,
            )
            f.flush()
            os.fsync(f.fileno())
        kill()

    def query(self, instance, lookup):
        assert self.engine.connection.info.transaction_status.name == "IDLE"
        raw = json.loads(self.remote.read_text()) if self.remote.exists() else None
        if raw and raw["scope_key"] == instance.scope.key and raw["key"] == lookup.idempotency_key:
            return DeliveryReceipt(
                scope=instance.scope,
                idempotency_key=lookup.idempotency_key,
                outcome="accepted",
                remote_message_id=raw["remote_id"],
            )
        return DeliveryReceipt(
            scope=instance.scope, idempotency_key=lookup.idempotency_key, outcome="not_found"
        )


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    settings = Settings(
        database_url=config["database"],
        artifact_root=config["artifacts"],
        project_id=config["project"],
        repository=config["repository"],
        allow_paid=False,
        allow_external_write=False,
    )
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    engine.source_ingress.cipher = BodyCipher(config["source_body_key"].encode())
    with engine.transaction() as conn:
        intent = engine.source_notifications._intent(conn, config["action_id"])
        instance = engine.source_correlations.identities.registry._instance(
            conn, intent["source_instance_id"]
        )
    profile = NotificationProfile(
        instance.scope,
        instance.revision,
        ActorRef.model_validate(intent["sender"]),
        instance.channel_capabilities,
        FileReceiptChannel(engine, config["remote"]),
    )
    assert profile.profile_sha256 == intent["profile_sha256"]
    engine.source_notifications.register_profile(profile)

    class Transport(NotificationTransport):
        def send(self, action):
            if config["point"] == "after_claim":
                assert engine.connection.info.transaction_status.name == "IDLE"
                kill()
            return super().send(action)

    Dispatcher(engine, Transport(engine.source_notifications)).tick()
    raise AssertionError("Child must actually be SIGKILLed")


if __name__ == "__main__":
    main()
