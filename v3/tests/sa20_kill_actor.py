"""SIGKILL point strictly after original PG reserved the operations tick."""

import json
import os
import sys
import time
from pathlib import Path
from uuid import UUID

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import Settings
from mgar_v3.super_agent.source_operations import OperationsPort, TickOutcome
from mgar_v3.super_agent.source_operations_domain import ComponentTick


class InterruptedPort(OperationsPort):
    role = "worker"

    def __init__(self, engine, marker):
        self.engine, self.marker = engine, marker

    def invoke(self, guard):
        guard()
        assert self.engine.connection.info.transaction_status.name == "IDLE"
        self.marker.write_text(json.dumps({"pid": os.getpid(), "phase": "reserved_before_result"}))
        time.sleep(120)
        return TickOutcome()


def main():
    values = json.loads(Path(sys.argv[1]).read_text())
    settings = Settings(_env_file=None)
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    engine.source_operations.bind(InterruptedPort(engine, Path(values["marker"])))
    engine.source_operations.tick(
        UUID(values["component_id"]), ComponentTick.model_validate(values["request"])
    )


if __name__ == "__main__":
    main()
