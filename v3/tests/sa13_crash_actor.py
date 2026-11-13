"""Private process boundaries for actual SA13 SIGKILL; stdout never contains secrets."""

import json
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path

from sa12_crash_actor import private_json
from sa13_runtime_support import RepairModel, run, runtime_env

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import Settings
from mgar_v3.super_agent.source_coding_journal import SourceCodingJournal


def crash(state):
    settings = Settings(**state["settings"])
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    with runtime_env(
        engine, Path(state["root"]), model=RepairModel(wrong_first=False, two_files=True)
    ) as env:

        def kill(operation_id=None, path=None):
            assert engine.connection.info.transaction_status.name == "IDLE"
            driver_profile = asdict(env.driver.profile)
            driver_profile["grant"] = env.driver.profile.grant.model_dump(mode="json")
            driver_profile["request"] = env.driver.profile.request.model_dump(mode="json")
            private_json(
                Path(state["marker"]),
                {
                    "point": state["point"],
                    "pid": os.getpid(),
                    "backend_pid": engine.connection.info.backend_pid,
                    "transaction_status": engine.connection.info.transaction_status.name,
                    "operation_id": operation_id,
                    "path": path,
                    "coding_id": env.mandate.coding_id,
                    "run_id": env.target.run_id,
                    "assignment_id": env.run_request.assignment_id,
                    "epoch": env.run_request.epoch,
                    "tree_before": env.mandate.repository.materialized_tree_sha256,
                    "workspace_root": str(env.raw.root),
                    "model_calls": len(env.model.requests),
                    "driver": {
                        "registry_root": str(env.driver.registry_root),
                        "registration": env.registration.model_dump(mode="json"),
                        "source_root": str(env.driver.source_root),
                        "receipt_root": str(env.driver.receipt_root),
                        "image_lock": str(env.driver.image_lock),
                        "profile": driver_profile,
                    },
                },
            )
            os.kill(os.getpid(), signal.SIGKILL)

        if state["point"] == "durable_images_before_write":
            dispatch = SourceCodingJournal.dispatch

            def committed(journal, operation):
                result = dispatch(journal, operation)
                if operation["capability"] == "write":
                    kill(operation["operation_id"])
                return result

            SourceCodingJournal.dispatch = committed
        elif state["point"] == "first_physical_write_before_receipt":
            env.service.runtime.write_boundary = kill
        elif state["point"] == "known_model_before_worker_receipt":
            put_payload = SourceCodingJournal.put_payload

            def stored(journal, operation, kind, clear):
                put_payload(journal, operation, kind, clear)
                if kind == "model_response":
                    kill(operation["operation_id"])

            SourceCodingJournal.put_payload = stored
        else:
            raise ValueError("Unsupported SA13 crash point")
        run(env)
        raise AssertionError("Actual CodingLoop did not reach the frozen crash point")


def main():
    state = json.loads(Path(sys.argv[1]).read_text())
    try:
        crash(state)
    except Exception as error:
        private_json(
            Path(state["failure"]),
            {
                "error_type": type(error).__name__,
                "code": getattr(error, "code", None),
            },
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
