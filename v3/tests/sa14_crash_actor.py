"""Real process kill points; no live calls or plaintext printed to stdout."""

import json
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from sa12_crash_actor import private_json
from sa14_support import AsyncRepair, actual, restrict, rows, start

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.broker import IsolatedExecutionBroker
from mgar_v3.settings import Settings


def crash(state):
    settings = Settings(**state["settings"])
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    patch = pytest.MonkeyPatch()
    model = AsyncRepair(gate=state["point"] == "model_dispatched")
    with actual(engine, Path(state["root"]), patch, model=model, slow_tests=True) as env:

        def kill():
            assert engine.connection.info.transaction_status.name == "IDLE"
            profile = asdict(env.driver.profile)
            profile["grant"] = env.driver.profile.grant.model_dump(mode="json")
            profile["request"] = env.driver.profile.request.model_dump(mode="json")
            private_json(
                Path(state["marker"]),
                {
                    "point": state["point"],
                    "pid": os.getpid(),
                    "backend_pid": engine.connection.info.backend_pid,
                    "transaction_status": engine.connection.info.transaction_status.name,
                    "coding_id": env.mandate.coding_id,
                    "run_id": env.target.run_id,
                    "assignment_id": env.run_request.assignment_id,
                    "epoch": env.run_request.epoch,
                    "model_dispatches": len(model.requests),
                    "inputs": len(rows(env, "sa_async_inputs")),
                    "jobs": len(rows(env, "sa_async_jobs")),
                    "workspace_root": str(env.raw.root),
                    "driver": {
                        "registry_root": str(env.driver.registry_root),
                        "registration": env.registration.model_dump(mode="json"),
                        "source_root": str(env.driver.source_root),
                        "receipt_root": str(env.driver.receipt_root),
                        "image_lock": str(env.driver.image_lock),
                        "profile": profile,
                    },
                },
            )
            os.kill(os.getpid(), signal.SIGKILL)

        if state["point"] == "input_committed":
            restrict(env)
            kill()
        elif state["point"] == "model_dispatched":
            dispatch = model.dispatch

            def sent(request, cancellation, prepared):
                pending = dispatch(request, cancellation, prepared)
                kill()
                return pending

            model.dispatch = sent
            start(env)
        elif state["point"] == "job_running":
            inspect = IsolatedExecutionBroker.inspect_bound_command

            def running(broker, name, labels):
                status = inspect(broker, name, labels)
                if status.state == "running" and any(
                    job["state"] == "running" for job in rows(env, "sa_async_jobs")
                ):
                    kill()
                return status

            patch.setattr(IsolatedExecutionBroker, "inspect_bound_command", running)
            start(env)
        else:
            raise ValueError("Unknown SA14 crash point")
        assert env.async_runtime.join(env.mandate.coding_id, 35)
        raise AssertionError(
            "SA14 did not reach its actual crash boundary: " + str(env.async_runtime.failures)
        )


def main():
    state = json.loads(Path(sys.argv[1]).read_text())
    try:
        crash(state)
    except BaseException as exc:
        private_json(
            Path(state["failure"]),
            {"error_type": type(exc).__name__, "code": getattr(exc, "code", None)},
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
