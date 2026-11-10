"""SIGKILL fixture: sole Engine in a separate process, no production transport."""

import base64
import json
import sys
import time
from pathlib import Path
from uuid import UUID

from sa04_support import SignedIngress

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import Settings
from mgar_v3.sources.gateway import SourceGateway
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.sources.ports import TransportEnvelope

data = json.loads(Path(sys.argv[1]).read_text())
settings = Settings(
    database_url=data["dsn"],
    artifact_root=data["artifacts"],
    repository="example/mgar-test",
    local_actor="owner",
    allowed_actors=["owner"],
    graph_version="r1-quotes-v1",
)
engine = Engine(settings, ArtifactStore(settings.artifact_root))
engine.source_ingress.cipher = BodyCipher(data["key"].encode())


def checkpoint(point):
    if point == data["point"]:
        Path(data["marker"]).write_text(point)
        while True:
            time.sleep(0.05)


gateway = SourceGateway(
    engine.source_ingress,
    UUID(data["source"]),
    1,
    {("feishu", "controlled-v1"): SignedIngress(engine)},
    checkpoint=checkpoint,
)
gateway.receive(
    TransportEnvelope(headers=data["headers"], payload=base64.b64decode(data["payload"])),
    lambda _: Path(data["ack"]).write_text("ACK"),
)
engine.close()
