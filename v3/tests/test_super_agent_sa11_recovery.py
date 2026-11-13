"""Actual process loss around metadata preparation and atomic Source acknowledgement."""

import base64
import json
import os
import signal
import subprocess
import sys
from uuid import UUID

import pytest
from sa11_support import ack_request, followup, setup

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import ROOT
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.source_subscription_domain import SourceIntakeSnapshot


@pytest.mark.parametrize(
    "point",
    [
        "after_prepare_commit",
        "during_ack_before_commit",
        "after_ack_commit",
    ],
)
def test_real_sigkill_recovers_source_snapshot_and_exactly_once_cursor(engine, tmp_path, point):
    env = setup(engine)
    followup(env)
    settings = engine.settings
    fernet = engine.source_ingress.cipher._fernet
    key = base64.urlsafe_b64encode(fernet._signing_key + fernet._encryption_key)
    directory = tmp_path / "source-recovery"
    directory.mkdir(mode=0o700)
    marker = directory / "checkpoint.json"
    state = directory / "private-state.json"
    data = {
        "settings": {
            "database_url": settings.database_url.get_secret_value(),
            "artifact_root": str(settings.artifact_root),
            "project_id": settings.project_id,
            "repository": settings.repository,
            "local_actor": settings.local_actor,
            "allowed_actors": settings.allowed_actors,
            "graph_version": settings.graph_version,
            "policy_version": settings.policy_version,
            "allow_paid": False,
            "allow_external_write": False,
        },
        "key": key.decode(),
        "cookie": env.cookie,
        "run_id": str(env.run_id),
        "point": point,
        "marker": str(marker),
    }
    fd = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(data, output)
    engine.close()
    child_env = dict(os.environ, PYTHONPATH=str(ROOT / "src") + os.pathsep + str(ROOT / "tests"))
    log = directory / "child.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        child = subprocess.run(
            [sys.executable, str(ROOT / "tests/sa11_crash_actor.py"), str(state)],
            cwd=ROOT,
            env=child_env,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    assert child.returncode == -signal.SIGKILL
    checkpoint = json.loads(marker.read_text())
    assert checkpoint["point"] == point
    assert checkpoint["transaction_status"] == (
        "INTRANS" if point == "during_ack_before_commit" else "IDLE"
    )
    recovered = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        recovered.source_ingress.cipher = BodyCipher(key)
        assert recovered.connection.info.backend_pid != checkpoint["backend_pid"]
        recovered.recover()
        with recovered.transaction() as conn:
            row = conn.execute(
                "SELECT payload FROM app.sa_source_subscription_intakes WHERE snapshot_id=%s",
                (UUID(checkpoint["snapshot_id"]),),
            ).fetchone()
            receipts = conn.execute(
                "SELECT count(*) AS n FROM app.sa_source_subscription_consumptions"
            ).fetchone()["n"]
        assert row is not None
        assert receipts == (1 if point == "after_ack_commit" else 0)
        snapshot = SourceIntakeSnapshot.model_validate_json(json.dumps(row["payload"]))
        recovered.source_subscriptions.acknowledge(env.cookie, env.run_id, ack_request(snapshot))
        with recovered.transaction() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM app.sa_source_subscription_consumptions"
                ).fetchone()["n"]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT max(cursor_to) AS n FROM app.sa_source_subscription_consumptions"
                ).fetchone()["n"]
                == (checkpoint["through_cursor"])
            )
    finally:
        recovered.close()
