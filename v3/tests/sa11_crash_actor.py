"""Trusted test-only process: kill at real Source intake commit boundaries."""

import json
import os
import signal
import sys
from pathlib import Path
from uuid import UUID

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import Settings
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.source_subscription_domain import (
    SourceIntakeSnapshot,
    SubscriptionAckRequest,
    SubscriptionPrepareRequest,
)


def main():
    state = json.loads(Path(sys.argv[1]).read_text())
    settings = Settings(**state["settings"])
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    engine.source_ingress.cipher = BodyCipher(state["key"].encode())
    run_id = UUID(state["run_id"])
    page = engine.source_subscriptions.prepare(
        state["cookie"],
        run_id,
        SubscriptionPrepareRequest(
            epoch=0,
            expected_cursor=0,
            lane="primary",
        ),
    )
    raw = page.get("snapshot", page) if isinstance(page, dict) else page
    snapshot = (
        raw
        if isinstance(raw, SourceIntakeSnapshot)
        else (SourceIntakeSnapshot.model_validate_json(json.dumps(raw, default=str)))
    )
    request = SubscriptionAckRequest(
        epoch=snapshot.epoch,
        expected_cursor=0,
        lane="primary",
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.snapshot_sha256,
    )

    def kill():
        marker = Path(state["marker"])
        data = {
            "point": state["point"],
            "backend_pid": engine.connection.info.backend_pid,
            "transaction_status": engine.connection.info.transaction_status.name,
            "snapshot_id": str(snapshot.snapshot_id),
            "snapshot_sha256": snapshot.snapshot_sha256,
            "through_cursor": snapshot.through_cursor,
        }
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(data, out)
            out.flush()
            os.fsync(out.fileno())
        os.kill(os.getpid(), signal.SIGKILL)

    if state["point"] == "after_prepare_commit":
        kill()
    if state["point"] == "during_ack_before_commit":
        with engine.transaction():
            engine.source_subscriptions.acknowledge(state["cookie"], run_id, request)
            kill()
    engine.source_subscriptions.acknowledge(state["cookie"], run_id, request)
    kill()


if __name__ == "__main__":
    main()
