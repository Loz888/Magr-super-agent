"""First SA22 soak qualification of a real owned PostgreSQL transport outage."""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from mgar_v3.settings import ROOT
from mgar_v3.store.migrate import migrate

sys.path.insert(0, str(ROOT / "scripts"))
from sa22_soak import Workload  # noqa: E402


def test_owned_postgresql_outage_before_ack_recovers_same_pending_event(tmp_path):
    name = "mgar-sa22-soak-qualification-" + uuid4().hex[:10]
    password = uuid4().hex + uuid4().hex
    docker_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "POSTGRES_PASSWORD": password}

    def docker(*args):
        return subprocess.run(
            ["docker", *args], capture_output=True, env=docker_env, timeout=30, check=True
        )

    container = docker(
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "owner=sa22-soak-qualification",
        "-e",
        "POSTGRES_PASSWORD",
        "-e",
        "POSTGRES_DB=sa22_soak_qualification",
        "-p",
        "127.0.0.1::5432",
        "pgvector/pgvector:0.8.1-pg16-bookworm",
    )
    cid = container.stdout.decode().strip()
    workload = None
    try:
        metadata = json.loads(docker("inspect", cid).stdout)[0]
        assert metadata["Config"]["Labels"]["owner"] == "sa22-soak-qualification"
        port = metadata["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]

        def ready():
            for _ in range(80):
                try:
                    with psycopg.connect(dsn, connect_timeout=1) as connection:
                        if connection.execute("SELECT 1").fetchone() == (1,):
                            return
                except psycopg.OperationalError:
                    pass
                time.sleep(0.25)
            raise AssertionError("Owned qualification PG did not become ready")

        dsn = make_conninfo(
            host="127.0.0.1",
            port=port,
            user="postgres",
            password=password,
            dbname="sa22_soak_qualification",
        )
        ready()
        migrate(dsn)
        config = {
            "purpose": "SA22_CONTROLLED_SOAK",
            "database_url": dsn,
            "project_id": "sa22-pg-qualification",
            "expires_at": (datetime.now(UTC) + timedelta(days=4)).isoformat(),
            "image_id": "sha256:efad10932c12f9d6f2408fe960e9a292c4bdbac0f8365387838acbe4f0fdd2b6",
        }
        workload = Workload(config, tmp_path)
        first = workload.cycle()
        with workload.engine.transaction() as connection:
            before_start = connection.execute("SELECT pg_postmaster_start_time() AS t").fetchone()[
                "t"
            ]
        docker("stop", "--time", "1", cid)
        with pytest.raises(httpx.HTTPError):
            workload.cycle()
        pending = json.loads((tmp_path / "pending-cycle.json").read_text())
        assert pending["phase"] == "planned"  # No success ACK or business completion.
        workload.close()
        workload = None
        docker("start", cid)
        # Docker's ephemeral published port may change after a real restart.
        restarted = json.loads(docker("inspect", cid).stdout)[0]
        assert restarted["Id"] == cid
        port = restarted["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
        dsn = make_conninfo(dsn, port=port)
        config["database_url"] = dsn
        ready()
        workload = Workload(config, tmp_path)
        resumed = workload.cycle()
        assert resumed["input_id"] != first["input_id"] and resumed["attempts"] == 1
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT pg_postmaster_start_time()").fetchone()[0] > before_start
            assert conn.execute("SELECT count(*) FROM app.source_ingress_inputs").fetchone()[0] == 3
            assert conn.execute("SELECT count(*) FROM app.actions").fetchone()[0] == 2
            assert conn.execute("SELECT max(attempts) FROM app.outbox").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM app.usage_calls").fetchone()[0] == 0
    finally:
        if workload is not None:
            workload.close()
        # This exact container was created above; no name-prefix sweeping.
        docker("rm", "-f", cid)
