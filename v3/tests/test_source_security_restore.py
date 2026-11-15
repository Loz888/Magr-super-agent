"""Old DB/CAS/key restoration stays held while current privacy facts are replayed."""

import copy
import errno
import json
import subprocess
import sys
from pathlib import Path

import pytest
from sa04_support import gateway
from sa15_support import legacy_memory, path, setup
from sa21_recovery_support import (
    assert_activation_held,
    backup_config,
    fresh_restore_database,
    open_engine,
    recovered_settings,
)
from sa21_support import observation, withdraw
from test_source_operations_backup import setup as unknown_setup

from mgar_v3.domain import DomainError
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.source_operations_backup import HOLD, backup, restore, unpack
from mgar_v3.super_agent.source_recall_domain import RecallPageRequest


def current_gateway(source):
    with source.engine.transaction() as conn:
        generation = conn.execute(
            "SELECT generation FROM app.source_ingress_controls WHERE source_instance_id=%s",
            (source.installation.scope.source_instance_id,),
        ).fetchone()["generation"]
    return gateway(source, generation=generation)


@pytest.mark.parametrize("operation", ["delete", "policy"])
def test_actual_old_backup_replay_of_new_privacy_fact_blocks_all_derived_reads_and_keeps_hold(
    engine, tmp_path, operation
):
    env = setup(engine, tmp_path)
    memory, _ = legacy_memory(env)
    requests = [
        RecallPageRequest(path=p)
        for p in (path(env.source_ref), env.report_path, path(env.run_ref, memory))
    ]
    assert all(engine.source_recall.page(env.owner, r)["content"] for r in requests)
    configuration = backup_config(engine, tmp_path, env.source.key)
    package = tmp_path / "before-withdrawal.encrypted"
    engine.close()
    backup(configuration, package)
    manifest, _ = unpack(package, configuration.encryption_key)
    primary = open_engine(engine.settings)
    try:
        current = copy.copy(env.source)
        current.engine = primary
        primary.source_ingress.cipher = BodyCipher(current.key)
        current.gateway = current_gateway(current)
        withdraw(current, env.input_id, operation)
        for request in requests:
            with pytest.raises(DomainError):
                primary.source_recall.page(env.owner, request)
    finally:
        primary.close()
    with fresh_restore_database(configuration.database_url.get_secret_value()) as target:
        root = tmp_path / "restored-old-state"
        report = restore(
            package,
            configuration.encryption_key,
            target_dsn=target,
            target_root=root,
            project_id=configuration.project_id,
            expected_migration_sha256=manifest.migration_sha256,
        )
        settings = recovered_settings(engine.settings, target, root)
        assert report["activation"] is False and report["latest_revocations_applied"] is False
        assert_activation_held(settings)
        recovered = open_engine(settings)  # Explicit offline maintenance only; no listener/Worker.
        try:
            restored = copy.copy(env.source)
            restored.engine = recovered
            restored.key = (root / "keys/source.key").read_bytes()
            recovered.source_ingress.cipher = BodyCipher(restored.key)
            restored.gateway = current_gateway(restored)
            assert recovered.source_recall.page(env.owner, requests[0])["content"]
            withdraw(restored, env.input_id, operation)
            for request in requests:
                with pytest.raises(DomainError):
                    recovered.source_recall.page(env.owner, request)
            with recovered.transaction() as conn:
                assert not conn.execute(
                    "SELECT 1 FROM app.source_ingress_bodies WHERE input_id=%s", (env.input_id,)
                ).fetchone()
                assert conn.execute(
                    "SELECT 1 FROM app.source_ingress_inputs WHERE input_id=%s", (env.input_id,)
                ).fetchone()
            assert (root / HOLD).exists()
            observation(
                "old_backup_privacy_replay",
                operation=operation,
                blocked_reads=3,
                original_keys=True,
                activation=False,
                minimum_input_fact_preserved=True,
            )
        finally:
            recovered.close()
        assert_activation_held(settings)


def test_real_storage_write_failure_after_db_restore_preserves_unknown_and_incomplete_hold(
    engine, tmp_path, trigger
):
    env = unknown_setup(engine, tmp_path, trigger)
    engine.close()
    backup(env.config, env.package)
    manifest, _ = unpack(env.package, env.config.encryption_key)
    with fresh_restore_database(env.config.database_url.get_secret_value()) as target:
        root = tmp_path / "restore-with-os-failure"
        config = tmp_path / "private-restore-probe.json"
        config.write_text(
            json.dumps(
                {
                    "package": str(env.package),
                    "key": str(env.config.encryption_key),
                    "target_dsn": target,
                    "target_root": str(root),
                    "project_id": env.config.project_id,
                    "migration_sha256": manifest.migration_sha256,
                }
            )
        )
        config.chmod(0o600)
        done = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("sa21_restore_actor.py")), str(config)],
            capture_output=True,
            timeout=90,
        )
        assert done.returncode == 23, done.stderr.decode(errors="replace")
        facts = json.loads(done.stdout)
        assert facts == {"errno": errno.EFBIG, "database_restored": True}
        assert json.loads((root / HOLD).read_text())["state"] == "INCOMPLETE_RESTORE_HOLD"
        recovered = open_engine(recovered_settings(engine.settings, target, root))
        try:
            with recovered.transaction() as conn:
                action = conn.execute(
                    "SELECT status FROM app.actions WHERE action_id=%s", (env.action_id,)
                ).fetchone()
                assert action["status"] == "outcome_unknown"
                assert (
                    conn.execute(
                        "SELECT attempts FROM app.outbox WHERE action_id=%s", (env.action_id,)
                    ).fetchone()["attempts"]
                    == 1
                )
        finally:
            recovered.close()
        assert_activation_held(recovered_settings(engine.settings, target, root))
        observation(
            "actual_restore_storage_fault",
            errno=errno.EFBIG,
            restored_unknown_preserved=True,
            activation=False,
            resend_attempts=0,
        )
