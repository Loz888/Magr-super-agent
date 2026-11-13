"""Trusted test-only process boundaries and independent loopback claim client."""

import json
import os
import signal
import sys
from pathlib import Path
from uuid import UUID

import httpx
from sa12_support import FilesystemOnlyDriver

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import Settings
from mgar_v3.super_agent.source_scheduler_domain import SchedulerClaimRequest


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(value, output, default=str)
        output.flush()
        os.fsync(output.fileno())


class ReopenedFilesystemDriver(FilesystemOnlyDriver):
    """Reconstruct the same controlled port without recreating its root."""

    def __init__(self, root):
        self.root = Path(root)
        assert self.root.is_dir()
        assert self.root.stat().st_mode & 0o777 == 0o700
        self.receipts = {}
        self.available = True


def crash(state):
    settings = Settings(**state["settings"])
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    service = engine.source_scheduler
    service.driver = ReopenedFilesystemDriver(state["driver_root"])
    request = SchedulerClaimRequest(
        request_id=UUID(state["request_id"]),
        session_id=UUID(state["session_id"]),
        expected_config_revision=1,
    )

    def kill(assignment_id, operation_id=None, request_sha256=None):
        private_json(
            state["marker"],
            {
                "point": state["point"],
                "pid": os.getpid(),
                "backend_pid": engine.connection.info.backend_pid,
                "transaction_status": engine.connection.info.transaction_status.name,
                "assignment_id": assignment_id,
                "operation_id": operation_id,
                "request_sha256": request_sha256,
            },
        )
        os.kill(os.getpid(), signal.SIGKILL)

    if state["point"] == "before_claim_commit":
        original = service._event

        def event(conn, kind, row, **details):
            original(conn, kind, row, **details)
            if kind == "ASSIGNMENT_CLAIMED":
                assert engine.connection.info.transaction_status.name == "INTRANS"
                kill(row["assignment_id"])

        service._event = event
    elif state["point"] == "after_claim_commit_before_prepare":

        def before_prepare(context):
            assert context.kind == "prepare"
            assert engine.connection.info.transaction_status.name == "IDLE"
            kill(context.assignment_id, context.operation_id, context.request_sha256)

        service._perform = before_prepare
    elif state["point"] != "after_prepare_receipt_commit":
        raise ValueError("Unsupported controlled crash point")
    claimed = service.claim(state["bearer"], request)
    assert engine.connection.info.transaction_status.name == "IDLE"
    prepared = claimed["prepare_operation"]
    kill(claimed["assignment_id"], prepared["operation_id"], prepared["request_sha256"])


def claim_client(state):
    # No Engine is constructed in this branch. Only the parent owns the writer.
    with httpx.Client(trust_env=False, timeout=15) as client:
        response = client.post(
            state["url"],
            headers={"Authorization": "Bearer " + state["bearer"]},
            json={
                "request_id": state["request_id"],
                "session_id": state["session_id"],
                "expected_config_revision": 1,
            },
        )
    private_json(
        state["result"],
        {
            "pid": os.getpid(),
            "status": response.status_code,
            "result": response.json(),
        },
    )


def main():
    state = json.loads(Path(sys.argv[2]).read_text())
    try:
        if sys.argv[1] == "crash":
            crash(state)
        elif sys.argv[1] == "claim-client":
            claim_client(state)
        else:
            raise ValueError("Unsupported controlled actor mode")
    except Exception as error:
        # Child stdout/tracebacks must never expose the private DSN or bearers.
        failure = Path(state.get("failure", str(sys.argv[2]) + ".failure.json"))
        private_json(
            failure,
            {
                "error_type": type(error).__name__,
                "code": getattr(error, "code", None),
            },
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
