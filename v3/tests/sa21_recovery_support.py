"""Exclusive SA21 PG fault objects. No main database or other CLI object is touched."""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import Settings
from mgar_v3.store.migrate import migrate
from mgar_v3.super_agent.source_operations_backup import BackupConfiguration

IMAGE = (
    "pgvector/pgvector:0.8.1-pg16-bookworm@sha256:"
    "33198da2828a14c30348d2ccb4750833d5ed9a44c88d840a0e523d7417120337"
)


class Cluster:
    def __init__(self, name, identifier, dsn):
        self.name, self.identifier, self.dsn = name, identifier, dsn
        self.ports = []

    def owned(self):
        info = json.loads(subprocess.check_output(["docker", "inspect", self.identifier]))[0]
        assert info["Id"] == self.identifier
        assert info["Name"] == "/" + self.name
        assert info["Config"]["Labels"]["mgar.sa21.owner"] == self.name

    def crash(self):
        self.owned()
        subprocess.run(
            ["docker", "kill", "--signal=KILL", self.identifier],
            check=True,
            capture_output=True,
            timeout=15,
        )

    def start(self):
        self.owned()
        subprocess.run(
            ["docker", "start", self.identifier], check=True, capture_output=True, timeout=15
        )
        port = (
            subprocess.check_output(
                ["docker", "port", self.identifier, "5432/tcp"], text=True, timeout=10
            )
            .strip()
            .split(":")[-1]
        )
        self.ports.append(port)
        self.dsn = make_conninfo(self.dsn, port=port)
        if hasattr(self, "settings"):
            self.settings = self.settings.model_copy(update={"database_url": SecretStr(self.dsn)})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.dsn, connect_timeout=1, autocommit=True):
                    return
            except psycopg.Error:
                time.sleep(0.1)
        raise AssertionError("Exclusive SA21 PostgreSQL did not recover")


@pytest.fixture
def crash_cluster(tmp_path):
    name = "sa21-fault-" + uuid4().hex
    identifier = subprocess.check_output(
        [
            "docker",
            "run",
            "-d",
            "--pull=never",
            "--name",
            name,
            "--label",
            "mgar.card=SAV1-21",
            "--label",
            "mgar.sa21.owner=" + name,
            "--cpus=1",
            "--memory=512m",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        ],
        text=True,
        timeout=30,
    ).strip()
    assert len(identifier) == 64
    cluster = None
    try:
        port = (
            subprocess.check_output(
                ["docker", "port", identifier, "5432/tcp"], text=True, timeout=10
            )
            .strip()
            .split(":")[-1]
        )
        cluster = Cluster(name, identifier, f"postgresql://postgres@127.0.0.1:{port}/postgres")
        cluster.start()
        migrate(cluster.dsn)
        cluster.settings = Settings(
            _env_file=None,
            database_url=cluster.dsn,
            artifact_root=tmp_path / "artifacts",
            repository="example/mgar-test",
            local_actor="owner",
            allowed_actors=["owner"],
            local_token="sa21-exclusive-controller-only-token",
            graph_version="r1-quotes-v1",
            allow_paid=False,
            allow_external_write=False,
        )
        yield cluster
    finally:
        if cluster is not None:
            cluster.owned()
        logs = subprocess.run(["docker", "logs", identifier], capture_output=True, timeout=10)
        (tmp_path / "exclusive-pg.log").write_bytes(logs.stdout + logs.stderr)
        subprocess.run(["docker", "rm", "-f", identifier], check=True, capture_output=True)


def open_engine(settings):
    return Engine(settings, ArtifactStore(settings.artifact_root))


@contextmanager
def fresh_restore_database(base):
    name = "sa21_restore_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(base, dbname=name)
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def backup_config(engine, tmp_path, source_key):
    keys = {}
    for name in ("source", "console", "web"):
        key = tmp_path / ("sa21-" + name + ".key")
        key.write_bytes(source_key if name == "source" else Fernet.generate_key())
        key.chmod(0o600)
        keys[name] = key
    encryption = tmp_path / "sa21-backup.key"
    encryption.write_bytes(os.urandom(32))
    encryption.chmod(0o600)
    return BackupConfiguration(
        database_url=engine.settings.database_url,
        project_id=engine.settings.project_id,
        artifact_root=engine.settings.artifact_root,
        source_key=keys["source"],
        console_key=keys["console"],
        web_key=keys["web"],
        encryption_key=encryption,
        offline_confirmation="ALL_SERVICES_AND_JOBS_STOPPED",
    )


def assert_activation_held(settings):
    env = dict(
        os.environ,
        MGAR_V3_DATABASE_URL=settings.database_url.get_secret_value(),
        MGAR_V3_ARTIFACT_ROOT=str(settings.artifact_root),
        MGAR_V3_ALLOW_PAID="0",
        MGAR_V3_ALLOW_EXTERNAL_WRITE="0",
        MGAR_V3_LOCAL_TOKEN="sa21-synthetic-activation-probe-token",
    )
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "from mgar_v3.super_agent.source_operations_runtime "
            "import create_application; create_application()",
        ],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert done.returncode != 0 and b"Restored installation is held" in done.stderr


def recovered_settings(original, dsn, root):
    return original.model_copy(
        update={"database_url": SecretStr(dsn), "artifact_root": root / "artifacts"}
    )
