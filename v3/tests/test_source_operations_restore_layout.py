"""First restored-key layout check through the original credential consumer."""

import json

from pydantic import SecretStr

from test_source_operations_backup import fresh_database, setup

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.source_console_credentials import ConsoleVault
from mgar_v3.super_agent.source_operations_backup import HOLD, backup, restore, unpack


def test_restored_console_key_is_usable_at_original_vault_path_while_activation_stays_held(
    engine, tmp_path, trigger
):
    env = setup(engine, tmp_path, trigger)
    ciphertext = BodyCipher(env.config.console_key.read_bytes()).encrypt(
        b'{"fixture_token":"synthetic-only"}', "original-vault-binding"
    )
    content_hash = engine.artifacts.put(ciphertext)
    engine.close()
    backup(env.config, env.package)
    manifest, _ = unpack(env.package, env.config.encryption_key)
    with fresh_database(env.config.database_url.get_secret_value()) as target:
        root = tmp_path / "restored-canonical"
        restore(
            env.package,
            env.config.encryption_key,
            target_dsn=target,
            target_root=root,
            project_id=env.config.project_id,
            expected_migration_sha256=manifest.migration_sha256,
        )
        settings = env.settings.model_copy(
            update={"database_url": SecretStr(target), "artifact_root": root / "artifacts"}
        )
        # An explicit offline read probe; no API/runtime is started.
        recovered = Engine(settings, ArtifactStore(settings.artifact_root))
        try:
            saved = recovered.artifacts.read(content_hash)
            assert ConsoleVault(recovered).decrypt(saved, "original-vault-binding") == {
                "fixture_token": "synthetic-only"
            }
            assert (root / "web/credentials.key").read_bytes() == env.config.web_key.read_bytes()
            assert (root / "source-console-credentials/key.bin").stat().st_nlink == 1
            assert json.loads((root / HOLD).read_text())["state"] == "RESTORED_ON_HOLD"
            with recovered.transaction() as conn:
                assert (
                    conn.execute(
                        "SELECT status FROM app.actions WHERE action_id=%s", (env.action_id,)
                    ).fetchone()["status"]
                    == "outcome_unknown"
                )
        finally:
            recovered.close()
