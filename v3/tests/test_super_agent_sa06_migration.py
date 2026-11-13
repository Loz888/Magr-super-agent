"""Forward-only 0034 -> 0035 upgrade and SQL transaction rollback on isolated PG."""

import hashlib
from shutil import copyfile
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import TriggerInput
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import ROOT, Settings
from mgar_v3.store.migrate import migrate

BASELINE_SHA256 = "c4cd090d04cca48b95eb08f8d76c8fa3d0271cfdef881de53e61a47a65cd8a84"


@pytest.mark.parametrize("fail_first", [False, True])
def test_34_to_35_keeps_old_rows_checksums_and_all_or_nothing(database, tmp_path, fail_first):
    name = "sa06_upgrade_" + uuid4().hex
    assert name.startswith("sa06_upgrade_") and len(name) == 45
    with psycopg.connect(database, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(database, dbname=name)
    directory = tmp_path / "before35"
    directory.mkdir()
    paths = sorted((ROOT / "migrations").glob("*.sql"))
    baseline = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.name < "0035"
    }
    manifest = "\n".join(f"{name}:{sha}" for name, sha in sorted(baseline.items()))
    assert len(baseline) == 34
    assert hashlib.sha256(manifest.encode()).hexdigest() == BASELINE_SHA256
    for path in paths:
        if path.name < "0035":
            copyfile(path, directory / path.name)
    try:
        assert len(migrate(dsn, directory)) == 34
        settings = Settings(
            database_url=dsn,
            artifact_root=tmp_path / "artifacts",
            repository="example/mgar-test",
            local_actor="owner",
            allowed_actors=["owner"],
            graph_version="r1-quotes-v1",
        )
        old = Engine(settings, ArtifactStore(settings.artifact_root))
        try:
            receipt = old.ingest(
                TriggerInput(
                    source="github",
                    external_event_id="before35",
                    project_id=settings.project_id,
                    repository=settings.repository,
                    issue_number=1,
                    actor="owner",
                    body="/mgar investigate retained before Feishu migration",
                )
            )
        finally:
            old.close()
        if fail_first:
            failed = tmp_path / "failed35"
            failed.mkdir()
            path = ROOT / "migrations" / "0035_feishu_source.sql"
            (failed / path.name).write_text(path.read_text() + "\nSELECT 1/0;\n")
            with pytest.raises(psycopg.Error):
                migrate(dsn, failed)
            with psycopg.connect(dsn) as conn:
                assert conn.execute(
                    "SELECT to_regclass('app.feishu_source_settings')"
                ).fetchone() == (None,)
                assert conn.execute("SELECT count(*) FROM app.schema_migrations").fetchone() == (
                    34,
                )
                assert conn.execute(
                    "SELECT count(*) FROM app.trigger_events WHERE event_id=%s", (receipt.event_id,)
                ).fetchone() == (1,)
        assert migrate(dsn) == [
            "0035_feishu_source.sql", "0036_super_agent_budgets.sql",
            "0038_super_agent_fast_path.sql",
            "0039_super_agent_source_eval.sql",
            "0041_source_correlations.sql",
        ]
        assert migrate(dsn) == []
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT count(*) FROM app.trigger_events WHERE event_id=%s", (receipt.event_id,)
            ).fetchone() == (1,)
            assert dict(
                conn.execute("SELECT name,sha256 FROM app.schema_migrations").fetchall()
            ) == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
            tables = conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='app' "
                "AND tablename LIKE 'feishu_%'"
            ).fetchall()
            assert {row[0] for row in tables} == {
                "feishu_source_settings",
                "feishu_source_versions",
                "feishu_source_audit",
                "feishu_transport_filters",
                "feishu_source_health",
            }
            assert conn.execute(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema='app' "
                "AND table_name LIKE 'feishu_%' AND column_name IN "
                "('app_secret','verification_token','encrypt_key','payload','body')"
            ).fetchone() == (0,)
    finally:
        with psycopg.connect(database, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
