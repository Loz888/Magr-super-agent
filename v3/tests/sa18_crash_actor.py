"""SIGKILL the real Engine owner at SA18 durable publication boundaries."""

import json
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path

from sa12_crash_actor import private_json
from sa18_runtime_support import approve, reviewed

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import Settings
from mgar_v3.super_agent.source_review_transport import ReviewLedger, ReviewTransport


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    settings = Settings(**config["settings"])
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    with reviewed(engine, Path(config["root"])) as e:
        approve(e)

        def kill():
            assert engine.connection.info.transaction_status.name == "IDLE"
            p = asdict(e.worker.driver.profile)
            p["grant"] = e.worker.driver.profile.grant.model_dump(mode="json")
            p["request"] = e.worker.driver.profile.request.model_dump(mode="json")
            private_json(
                Path(config["marker"]),
                {
                    "point": config["point"],
                    "pid": os.getpid(),
                    "backend_pid": engine.connection.info.backend_pid,
                    "transaction_status": engine.connection.info.transaction_status.name,
                    "review_id": e.review["review_id"],
                    "action_id": e.review["action_id"],
                    "run_id": e.review["run_id"],
                    "cookie": e.cookie,
                    "remote": str(e.remote.root),
                    "writes": e.remote.writes,
                    "profile": {
                        k: v
                        for k, v in e.profile.__dict__.items()
                        if k not in {"capture", "reader", "writer"}
                    },
                    "driver": {
                        "registry_root": str(e.worker.driver.registry_root),
                        "registration": e.worker.registration.model_dump(mode="json"),
                        "source_root": str(e.worker.driver.source_root),
                        "receipt_root": str(e.worker.driver.receipt_root),
                        "image_lock": str(e.worker.driver.image_lock),
                        "profile": p,
                    },
                },
            )
            os.kill(os.getpid(), signal.SIGKILL)

        if config["point"] == "after_claim":
            ReviewTransport.send = lambda self, action: kill()
        elif config["point"] == "after_step_commit":
            begin = ReviewLedger.begin_step

            def reserve(*args, **kw):
                begin(*args, **kw)
                kill()

            ReviewLedger.begin_step = reserve
        elif config["point"] == "after_remote_acceptance":
            accepted = e.remote.accepted

            def remote_accept(kind):
                if kind == "pull_request":
                    kill()
                accepted(kind)

            e.remote.accepted = remote_accept
        else:
            raise AssertionError("unknown crash boundary")
        e.service.tick(e.cookie)
    raise AssertionError("child must be SIGKILLed")


if __name__ == "__main__":
    main()
