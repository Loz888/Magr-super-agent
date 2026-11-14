"""Three actual SIGKILL points; marker IO is outside the business transaction."""

import json
import os
import signal
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from sa16_support import cycle, decision, setup

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import Settings
from mgar_v3.super_agent import source_autonomy
from mgar_v3.super_agent.source_autonomy_domain import AutonomyRevisionRequest


def main():
    cfg = json.loads(Path(sys.argv[1]).read_text())
    key = cfg["source_key"].encode()
    Fernet.generate_key = staticmethod(lambda: key)
    settings = Settings(**cfg["settings"])
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    env = setup(engine, Path(cfg["root"]))
    decision(env)
    assert engine.connection.info.transaction_status.name == "IDLE"
    marker = {
        "cookie": env.cookie,
        "rule_id": str(env.rule_id),
        "point": cfg["point"],
        "pid": os.getpid(),
        "backend_pid": engine.connection.info.backend_pid,
    }
    path = Path(cfg["marker"])
    with path.open("x") as handle:
        os.chmod(path, 0o600)
        json.dump(marker, handle)
    if cfg["point"] == "before_admission_commit":
        original = source_autonomy.create_original

        def killed(*args, **kwargs):
            original(*args, **kwargs)
            assert engine.connection.info.transaction_status.name == "INTRANS"
            os.kill(os.getpid(), signal.SIGKILL)

        source_autonomy.create_original = killed
    result = cycle(env)
    assert result["outcomes"][0]["created"], result
    if cfg["point"] == "after_disable_commit":
        env.autonomy.disable_rule(
            env.cookie,
            env.rule_id,
            AutonomyRevisionRequest(
                expected_revision=1, review_ref="durable-stop-before-real-SIGKILL"
            ),
        )
    assert engine.connection.info.transaction_status.name == "IDLE"
    os.kill(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":
    main()
