from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from shutil import copyfile
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sa04_support import envelope, gateway, rows, setup

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import ROOT, Settings
from mgar_v3.sources.ingress_crypto import BodyCipher, EncryptedSpool
from mgar_v3.sources.ingress_domain import GapDeclaration
from mgar_v3.store.migrate import migrate

IMAGE = (
    "pgvector/pgvector:0.8.1-pg16-bookworm@sha256:"
    "33198da2828a14c30348d2ccb4750833d5ed9a44c88d840a0e523d7417120337"
)


@pytest.mark.parametrize(
    "point,durable",
    [
        ("after_input_write", False),
        ("before_commit", False),
        ("after_commit", True),
        ("before_ack", True),
    ],
)
def test_real_sigkill_commit_boundaries_recover_exactly_once(engine, tmp_path, point, durable):
    env = setup(engine)
    incoming = envelope(env)
    marker, ack = tmp_path / "checkpoint", tmp_path / "ack"
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "dsn": engine.settings.database_url.get_secret_value(),
                "artifacts": str(engine.settings.artifact_root),
                "key": env.key.decode(),
                "source": str(env.installation.scope.source_instance_id),
                "point": point,
                "marker": str(marker),
                "ack": str(ack),
                "headers": incoming.headers,
                "payload": base64.b64encode(incoming.payload).decode(),
            }
        )
    )
    state.chmod(0o600)
    engine.close()  # No competing Engine connection/advisory lock during subprocess ownership.
    child_env = dict(
        os.environ, PYTHONPATH=str(ROOT / "tests") + os.pathsep + os.environ.get("PYTHONPATH", "")
    )
    logfile = tmp_path / "worker.log"
    with logfile.open("w") as output:
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "tests/fixtures/sa04_crash_worker.py"), str(state)],
            cwd=ROOT,
            env=child_env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert marker.exists(), "Crash worker did not reach the real transaction boundary"
            assert not ack.exists()
            child.kill()
            assert child.wait(timeout=10) == -9
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    logfile.chmod(0o600)
    recovered = Engine(engine.settings, ArtifactStore(engine.settings.artifact_root))
    try:
        env.engine = recovered
        recovered.source_ingress.cipher = BodyCipher(env.key)
        assert len(rows(env, "inputs")) == int(durable)
        result = gateway(env).receive(incoming, lambda _: ack.write_text("ACK"))
        assert result.disposition == ("duplicate" if durable else "accepted")
        assert (
            ack.read_text() == "ACK"
            and len(rows(env, "inputs")) == len(rows(env, "deliveries")) == 1
        )
    finally:
        recovered.close()


@pytest.fixture
def paused_pg(tmp_path):
    """Dedicated disposable container; never pauses this copy's or Harness' database."""
    name = "magr-sa04-fault-" + uuid4().hex
    password = secrets.token_hex(24)
    run_env = dict(os.environ, POSTGRES_PASSWORD=password)
    done = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--pull=never",
            "--name",
            name,
            "--label",
            "magr.card=SAV1-04",
            "--label",
            "magr.owner=" + name,
            "--cpus=1",
            "--memory=384m",
            "-e",
            "POSTGRES_PASSWORD",
            "-e",
            "POSTGRES_USER=sa04",
            "-e",
            "POSTGRES_DB=sa04",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        ],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, (
        "Dedicated SA04 fault container could not start (no remote pull requested)"
    )
    identifier = done.stdout.strip()
    assert len(identifier) == 64 and all(c in "0123456789abcdef" for c in identifier)

    def docker(*args):
        result = subprocess.run(
            ["docker", *args, identifier], capture_output=True, text=True, timeout=15
        )
        assert result.returncode == 0, "Owned SA04 Docker operation failed"
        return result.stdout

    try:
        info = json.loads(docker("inspect"))[0]
        assert info["Name"] == "/" + name and info["Config"]["Labels"]["magr.owner"] == name
        assert info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostIp"] == "127.0.0.1"
        port = info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
        dsn = make_conninfo(
            host="127.0.0.1",
            port=port,
            user="sa04",
            password=password,
            dbname="sa04",
            connect_timeout=2,
        )
        deadline = time.monotonic() + 40
        while True:
            try:
                with psycopg.connect(dsn):
                    pass
                break
            except psycopg.OperationalError:
                assert time.monotonic() < deadline, "Dedicated SA04 PostgreSQL readiness timeout"
                time.sleep(0.1)
        migrate(dsn)
        settings = Settings(
            database_url=dsn,
            artifact_root=tmp_path / "artifacts",
            repository="example/mgar-test",
            local_actor="owner",
            allowed_actors=["owner"],
            graph_version="r1-quotes-v1",
        )
        local = Engine(settings, ArtifactStore(settings.artifact_root))
        yield local, docker
        local.close()
    finally:
        # Exact inspected UUID container only; ensure it is running before cleanup.
        subprocess.run(["docker", "unpause", identifier], capture_output=True, timeout=15)
        docker("rm", "-f")


def test_real_isolated_postgresql_pause_never_acks_until_durable_commit(paused_pg):
    engine, docker = paused_pg
    env = setup(engine)
    incoming = envelope(env)
    ack = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        docker("pause")
        try:
            future = pool.submit(env.gateway.receive, incoming, ack.append)
            time.sleep(0.35)
            assert not future.done() and not ack
        finally:
            docker("unpause")
        result = future.result(timeout=15)
    assert result.committed and len(ack) == 1 and len(rows(env, "inputs")) == 1
    assert json.loads(docker("inspect"))[0]["State"]["Paused"] is False


def test_real_backend_disconnect_buffers_only_preapproved_content_and_no_ack(
    engine, tmp_path, monkeypatch
):
    env = setup(engine)
    directory = tmp_path / "buffer"
    directory.mkdir(mode=0o700)
    spool = EncryptedSpool(directory, BodyCipher(env.key))
    submit = engine.source_ingress.submit

    def disconnect(*args, **kwargs):
        pid = engine.connection.info.backend_pid
        with psycopg.connect(
            engine.settings.database_url.get_secret_value(), autocommit=True
        ) as admin:
            assert admin.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone() == (True,)
        return submit(*args, **kwargs)

    monkeypatch.setattr(engine.source_ingress, "submit", disconnect)
    ack = []
    with pytest.raises(DomainError) as error:
        gateway(env, spool=spool).receive(envelope(env), ack.append)
    assert error.value.code == "INGRESS_BUFFERED_NOT_ACKED" and not ack and len(spool.ids()) == 1
    engine.close()
    recovered = Engine(engine.settings, ArtifactStore(engine.settings.artifact_root))
    try:
        env.engine = recovered
        recovered.source_ingress.cipher = BodyCipher(env.key)
        assert not rows(env, "inputs")
        result = gateway(env, spool=spool).drain_buffer()
        assert result[0]["status"] == "engine_committed" and len(rows(env, "inputs")) == 1
        assert not spool.ids() and rows(env, "inputs")[0]["context_only"]
    finally:
        recovered.close()


def test_controlled_sdk_ack_boundaries_are_reproducible_and_unsafe_mode_refused(engine):
    env = setup(engine)
    automatic = []

    def sdk_after_handler(callback):
        result = callback(envelope(env))
        assert rows(env, "deliveries")[0]["sealed"]
        automatic.append("ACK")
        return result

    result = sdk_after_handler(gateway(env, ack_mode="after_handler").receive)
    assert result.committed and automatic == ["ACK"]
    with pytest.raises(DomainError) as error:
        gateway(env, ack_mode="before_handler").validate_transport()
    assert error.value.code == "INGRESS_EARLY_SDK_ACK_UNSAFE"
    # Demonstrate the uncontrolled failure: early ACK then callback loss, with no receipt.
    automatic.append("EARLY_ACK")
    lost_delivery = "sdk-lost-" + uuid4().hex
    assert lost_delivery not in {r["delivery_id"] for r in rows(env, "deliveries")}
    env.engine.source_ingress.gap(
        env.installation.scope.source_instance_id,
        GapDeclaration(
            stream_id="controlled-sdk",
            epoch="fixture",
            reason="sdk_early_ack",
            started_at=env.engine.source_ingress.now(env.engine.connection),
        ),
    )
    assert rows(env, "gaps")[0]["resolved_at"] is None


def test_real_32_to_33_upgrade_preserves_legacy_rows_and_all_checksums(database, tmp_path):
    name = "sa04_upgrade_" + uuid4().hex
    with psycopg.connect(database, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(database, dbname=name)
    legacy = tmp_path / "migrations"
    legacy.mkdir()
    paths = sorted((ROOT / "migrations").glob("*.sql"))
    for path in paths:
        if path.name < "0033":
            copyfile(path, legacy / path.name)
    try:
        assert len(migrate(dsn, legacy)) == 32
        settings = Settings(
            database_url=dsn,
            artifact_root=tmp_path / "old-artifacts",
            repository="example/mgar-test",
            local_actor="owner",
            allowed_actors=["owner"],
            graph_version="r1-quotes-v1",
        )
        old = Engine(settings, ArtifactStore(settings.artifact_root))
        from mgar_v3.domain import TriggerInput

        receipt = old.ingest(
            TriggerInput(
                source="github",
                external_event_id="old-before-0033",
                project_id=settings.project_id,
                repository=settings.repository,
                issue_number=1,
                actor="owner",
                body="/mgar investigate retained old row",
            )
        )
        old.close()
        assert migrate(dsn) == [p.name for p in paths if p.name >= "0033"] and migrate(dsn) == []
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT count(*) FROM app.trigger_events WHERE event_id=%s", (receipt.event_id,)
            ).fetchone() == (1,)
            installed = dict(
                conn.execute("SELECT name,sha256 FROM app.schema_migrations").fetchall()
            )
            assert installed == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    finally:
        assert name.startswith("sa04_upgrade_") and len(name) == 45
        with psycopg.connect(database, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
