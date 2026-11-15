"""Actual pg_dump/restore, encrypted CAS/keys and held recovery into fresh PG."""

import io
import json
import os
import tarfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sa20_support import configured

from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.graph.runner import Runner
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.source_operations_backup import (
    HEADER,
    HOLD,
    BackupConfiguration,
    BackupError,
    backup,
    installed_sha,
    restore,
    unpack,
)


class LostReply:
    mode = "fixture"

    def send(self, action):
        raise OSError("synthetic accepted write with lost response")

    def find(self, action):
        raise AssertionError("Restore must not query or resend")


def setup(engine, tmp_path, trigger):
    env = configured(engine, tmp_path)
    receipt = engine.ingest(trigger)
    action_id = Runner(engine).execute(receipt.run_id)
    action = engine.actions(receipt.run_id)[0]
    engine.approve(action_id, action["payload_hash"], "owner")
    Dispatcher(engine, LostReply()).tick()
    keys = []
    for name in ["source.key", "console.key", "web.key"]:
        path = tmp_path / name
        path.write_bytes(Fernet.generate_key())
        path.chmod(0o600)
        keys.append(path)
    encryption = tmp_path / "backup.key"
    encryption.write_bytes(os.urandom(32))
    encryption.chmod(0o600)
    body = BodyCipher(keys[0].read_bytes()).encrypt(
        b"SA20 synthetic protected source body", "scope-v1"
    )
    content_hash = engine.artifacts.put(body)
    env.config = BackupConfiguration(
        database_url=engine.settings.database_url,
        project_id=engine.settings.project_id,
        artifact_root=engine.settings.artifact_root,
        source_key=keys[0],
        console_key=keys[1],
        web_key=keys[2],
        encryption_key=encryption,
        offline_confirmation="ALL_SERVICES_AND_JOBS_STOPPED",
    )
    env.package = tmp_path / "backup.encrypted"
    env.content_hash, env.action_id = content_hash, action_id
    return env


@contextmanager
def fresh_database(base):
    name = "sa20_restore_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(base, dbname=name)
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_real_encrypted_pg_cas_three_keys_restore_preserves_unknown_and_never_activates(
    engine, tmp_path, trigger
):
    env = setup(engine, tmp_path, trigger)
    with pytest.raises(BackupError, match="active Engine"):
        backup(env.config, env.package)
    assert not env.package.exists()
    engine.close()
    metadata = backup(env.config, env.package)
    raw = env.package.read_bytes()
    assert metadata["encrypted"] and b"scope-v1" not in raw and b"PGDMP" not in raw
    manifest, entries = unpack(env.package, env.config.encryption_key)
    expected = installed_sha(Path(__file__).resolve().parents[1] / "migrations")
    assert manifest.migration_sha256 == expected
    with fresh_database(env.config.database_url.get_secret_value()) as target:
        root = tmp_path / "restored"
        report = restore(
            env.package,
            env.config.encryption_key,
            target_dsn=target,
            target_root=root,
            project_id=env.config.project_id,
            expected_migration_sha256=expected,
        )
        assert report["activation"] is False and (root / HOLD).exists()
        with psycopg.connect(target, autocommit=True) as conn:
            assert (
                conn.execute(
                    "SELECT status FROM app.actions WHERE action_id=%s", (env.action_id,)
                ).fetchone()[0]
                == "outcome_unknown"
            )
            assert conn.execute("SELECT count(*) FROM app.schema_migrations").fetchone()[0] == 50
            assert (
                conn.execute(
                    "SELECT attempts FROM app.outbox WHERE action_id=%s", (env.action_id,)
                ).fetchone()[0]
                == 1
            )
        key = (root / "keys/source.key").read_bytes()
        encrypted = (root / "artifacts" / env.content_hash[:2] / env.content_hash).read_bytes()
        assert (
            BodyCipher(key).decrypt(encrypted, "scope-v1")
            == b"SA20 synthetic protected source body"
        )
        for name in ("console.key", "web.key"):
            assert (root / "keys" / name).read_bytes() == (tmp_path / name).read_bytes()
            assert (root / "keys" / name).stat().st_mode & 0o777 == 0o600
        assert (
            report["latest_revocations_applied"] is False
            and report["remote_writes_reconciled"] is False
        )


@pytest.mark.parametrize(
    "change", ["wrong_key", "tampered_ciphertext", "missing_key", "symlink_key"]
)
def test_backup_or_unpack_rejects_key_and_integrity_faults_without_a_usable_package(
    engine, tmp_path, trigger, change
):
    env = setup(engine, tmp_path, trigger)
    engine.close()
    if change == "missing_key":
        env.config.console_key.unlink()
        with pytest.raises(OSError):
            backup(env.config, env.package)
        assert not env.package.exists()
        return
    if change == "symlink_key":
        original = env.config.console_key.read_bytes()
        env.config.console_key.unlink()
        elsewhere = tmp_path / "other.key"
        elsewhere.write_bytes(original)
        elsewhere.chmod(0o600)
        env.config.console_key.symlink_to(elsewhere)
        with pytest.raises(OSError):
            backup(env.config, env.package)
        assert not env.package.exists()
        return
    backup(env.config, env.package)
    if change == "wrong_key":
        env.config.encryption_key.write_bytes(os.urandom(32))
    else:
        raw = bytearray(env.package.read_bytes())
        raw[-1] ^= 1
        env.package.write_bytes(raw)
    with pytest.raises(BackupError):
        unpack(env.package, env.config.encryption_key)


@pytest.mark.parametrize(
    "binding", ["project", "migration", "nonempty_database", "existing_directory"]
)
def test_restore_requires_matching_binding_and_new_empty_targets(
    engine, tmp_path, trigger, binding
):
    env = setup(engine, tmp_path, trigger)
    engine.close()
    backup(env.config, env.package)
    manifest, _ = unpack(env.package, env.config.encryption_key)
    with fresh_database(env.config.database_url.get_secret_value()) as target:
        root = tmp_path / "restore-target"
        if binding == "nonempty_database":
            with psycopg.connect(target, autocommit=True) as conn:
                conn.execute("CREATE TABLE public.keep_me(value int)")
        if binding == "existing_directory":
            root.mkdir()
            (root / "keep.txt").write_text("user file")
        expected = manifest.migration_sha256 if binding != "migration" else "0" * 64
        selected = env.config.project_id if binding != "project" else "other-project"
        with pytest.raises((BackupError, FileExistsError)):
            restore(
                env.package,
                env.config.encryption_key,
                target_dsn=target,
                target_root=root,
                project_id=selected,
                expected_migration_sha256=expected,
            )
        with psycopg.connect(target, autocommit=True) as conn:
            assert conn.execute("SELECT to_regclass('app.actions')").fetchone()[0] is None
            if binding == "nonempty_database":
                assert conn.execute("SELECT to_regclass('public.keep_me')").fetchone()[0]
        if binding == "existing_directory":
            assert (root / "keep.txt").read_text() == "user file"


@pytest.mark.parametrize("entry", ["../../outside", "keys/source.key", "cas/aa/bogus"])
def test_authenticated_but_noncanonical_tar_is_rejected_before_any_extraction(tmp_path, entry):
    secret = tmp_path / "backup.key"
    secret.write_bytes(os.urandom(32))
    secret.chmod(0o600)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in [(entry, b"not a declared part"), ("manifest.json", b"{}")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    nonce = os.urandom(12)
    package = tmp_path / "bad.encrypted"
    package.write_bytes(
        HEADER + nonce + AESGCM(secret.read_bytes()).encrypt(nonce, raw.getvalue(), HEADER)
    )
    package.chmod(0o600)
    with pytest.raises(BackupError):
        unpack(package, secret)
    assert not (tmp_path.parent / "outside").exists()


def test_default_deployment_refuses_restored_hold_before_opening_engine(tmp_path, monkeypatch):
    from mgar_v3.settings import Settings
    from mgar_v3.super_agent import source_operations_runtime

    (tmp_path / HOLD).write_text(json.dumps({"activation": False}))
    monkeypatch.setattr(Settings, "__init__", lambda self, **kw: None)
    # model_construct avoids loading any real .env or opening a database.
    fake = Settings.model_construct(artifact_root=tmp_path / "artifacts")
    monkeypatch.setattr("mgar_v3.settings.Settings", lambda **kwargs: fake)
    with pytest.raises(RuntimeError, match="Restored installation"):
        source_operations_runtime.create_application()
