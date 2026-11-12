"""Real-PG 0036 -> 0038 upgrade; 0037 belongs to Harness and is not imported here."""

import hashlib
from itertools import count
from shutil import copyfile
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from sa04_support import envelope, gateway
from sa04_support import setup as source_setup
from sa07_support import configure, router, worker
from sa08_support import ingest, setup

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import ROOT, Settings
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.store.migrate import migrate
from mgar_v3.super_agent.fast_path_domain import (
    FastPathConfigure,
    FastPathSettings,
    ObserveRequest,
)

MIGRATION = "0038_super_agent_fast_path.sql"
BASELINE_SHA256 = "a172324d4b6b00e9219c9cf6bc05652ff9b52fdd0d5320a6e22a87ecdcfbe750"
TABLES = {
    "sa_fast_path_controls", "sa_fast_path_versions", "sa_fast_path_origins",
    "sa_fast_path_batches", "sa_fast_path_decisions",
}


def snapshot(dsn, tables):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return {
            table: conn.execute(
                sql.SQL("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                        "'[]'::jsonb) AS rows FROM app.{} t").format(sql.Identifier(table))
            ).fetchone()["rows"]
            for table in tables
        }


@pytest.mark.parametrize("fail_first", [False, True])
def test_36_to_0038_preserves_old_source_and_financial_facts_and_rolls_failed_ddl_back(
    database, tmp_path, fail_first,
):
    paths = sorted((ROOT / "migrations").glob("*.sql"))
    baseline = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths if path.name < "0037"
    }
    manifest = "\n".join(f"{key}:{value}" for key, value in sorted(baseline.items()))
    assert len(baseline) == 36
    assert hashlib.sha256(manifest.encode()).hexdigest() == BASELINE_SHA256
    assert not any(path.name.startswith("0037") for path in paths)
    assert {path.name for path in paths} == set(baseline) | {MIGRATION}
    directory = tmp_path / "before0038"
    directory.mkdir()
    for path in paths:
        if path.name in baseline:
            copyfile(path, directory / path.name)
    name = "sa08_upgrade_" + uuid4().hex
    assert name.startswith("sa08_upgrade_") and len(name) == 45
    with psycopg.connect(database, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(database, dbname=name)
    try:
        assert len(migrate(dsn, directory)) == 36
        settings = Settings(
            database_url=dsn, artifact_root=tmp_path / "pre-upgrade-artifacts",
            repository="example/mgar-test", local_actor="owner", allowed_actors=["owner"],
            graph_version="r2-mailbox-v1", allow_paid=True,
            deepseek_api_key=SecretStr("controlled-no-network-migration-key"),
        )
        old = Engine(settings, ArtifactStore(settings.artifact_root))
        try:
            source = source_setup(old, mode="shadow", receive={
                "enabled": True, "retain_body": True, "model_input": True,
                "body_ttl_seconds": 300, "allow_history_context": True,
            })
            receipt = source.gateway.receive(envelope(source, body="old authorized source content"))
            budget = SimpleNamespace(
                engine=old, cookie=source.cookie, service=old.project_budgets, sequence=count(1),
            )
            configure(budget)
            entry = worker(budget)
            known = entry.reserve("before0038-known")
            entry.settle(known, 17, {"prompt_tokens": 10, "completion_tokens": 7})
            unknown = entry.reserve("before0038-unknown")
            routed = router(budget)
            router_known = routed.reserve("before0038-router-known")
            routed.settle(router_known, 11, {"prompt_tokens": 6, "completion_tokens": 5})
            with old.transaction() as conn:
                batch = conn.execute(
                    "SELECT batch_id FROM app.router_calls WHERE call_id=%s", (router_known,),
                ).fetchone()["batch_id"]
            old.routing.failure(batch, "MODEL_RESULT_UNAVAILABLE")
            routed_unknown = router(budget)
            router_unknown = routed_unknown.reserve("before0038-router-unknown")
            entry.settle(unknown, None)
            routed_unknown.settle(router_unknown, None)
        finally:
            old.close()
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            old_tables = [row["tablename"] for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='app' "
                "AND tablename<>'schema_migrations' ORDER BY tablename",
            )]
            assert conn.execute(
                "SELECT to_regclass('app.sa_fast_path_controls') AS present",
            ).fetchone()["present"] is None
        before = snapshot(dsn, old_tables)
        assert {row["status"] for row in before["usage_calls"]} == {"known", "unknown"}
        assert {row["status"] for row in before["router_calls"]} == {"known", "unknown"}
        if fail_first:
            failed = tmp_path / "failed0038"
            failed.mkdir()
            (failed / MIGRATION).write_text(
                (ROOT / "migrations" / MIGRATION).read_text() + "\nSELECT 1/0;\n",
            )
            with pytest.raises(psycopg.Error):
                migrate(dsn, failed)
            assert snapshot(dsn, old_tables) == before
            with psycopg.connect(dsn) as conn:
                recorded = dict(conn.execute("SELECT name,sha256 FROM app.schema_migrations"))
                assert recorded == baseline
                assert conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='app' "
                    "AND tablename LIKE 'sa_fast_path_%'",
                ).fetchall() == []
        assert migrate(dsn) == [MIGRATION]
        assert migrate(dsn) == []
        assert snapshot(dsn, old_tables) == before
        with psycopg.connect(dsn) as conn:
            checksums = dict(conn.execute("SELECT name,sha256 FROM app.schema_migrations"))
            assert len(checksums) == 37 and checksums == {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
            }
            assert {row[0] for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='app' "
                "AND tablename LIKE 'sa_fast_path_%'",
            )} == TABLES
            assert conn.execute("SELECT count(*) FROM app.sa_fast_path_origins").fetchone() == (0,)
            assert conn.execute("SELECT count(*) FROM app.sa_fast_path_controls").fetchone() == (0,)
        current = Engine(settings, ArtifactStore(settings.artifact_root))
        try:
            current.source_ingress.cipher = BodyCipher(source.key)
            assert current.source_ingress.body(source.cookie, receipt.input_ids[0]) == (
                b"old authorized source content"
            )
            source_id = source.installation.scope.source_instance_id
            assert current.fast_path.read(source.cookie, source_id)["revision"] == 0
            assert not current.fast_path.read(source.cookie, source_id)["settings"]["enabled"]
            current.fast_path.configure(source.cookie, source_id, FastPathConfigure(
                expected_revision=0, settings=FastPathSettings(enabled=True),
                review_ref="SA08-explicit-post-upgrade-opt-in",
            ))
            checked = current.fast_path.observe(
                source.cookie, source_id, ObserveRequest(input_ids=receipt.input_ids),
            )
            assert checked["decisions"][0]["reason_code"] == "FAST_PATH_ORIGIN_UNKNOWN"
            assert checked["decisions"][0]["batch_id"] is None
            source.engine = current
            fresh = gateway(source).receive(envelope(
                source, delivery="post-upgrade-new", entity="post-upgrade-new",
                body="a genuinely new authorized source event",
            ))
            decision = next(row for row in current.fast_path.decisions(source.cookie, source_id)
                            if row["input_id"] == fresh.input_ids[0])
            assert decision["disposition"] == "candidate"
            assert decision["intended_action"] == "would_evaluate"
        finally:
            current.close()
    finally:
        with psycopg.connect(database, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def fast_path_snapshot(env):
    with env.engine.transaction() as conn:
        return {
            table: conn.execute("SELECT * FROM app." + table + " ORDER BY 1,2").fetchall()
            for table in sorted(TABLES)
        }


@pytest.mark.parametrize("table", [
    "sa_fast_path_versions", "sa_fast_path_origins", "sa_fast_path_decisions",
])
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_signed_versions_origin_and_decisions_are_immutable(engine, table, operation):
    env = setup(engine)
    ingest(env)
    before = fast_path_snapshot(env)
    column = "revision" if table == "sa_fast_path_versions" else "input_id"
    statement = (
        sql.SQL("UPDATE app.{} SET {}={}").format(
            sql.Identifier(table), sql.Identifier(column), sql.Identifier(column),
        ) if operation == "update" else sql.SQL("DELETE FROM app.{}").format(sql.Identifier(table))
    )
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(statement)
    assert fast_path_snapshot(env) == before


@pytest.mark.parametrize("statement", [
    "DELETE FROM app.sa_fast_path_controls",
    "UPDATE app.sa_fast_path_controls SET revision=revision+2",
    "UPDATE app.sa_fast_path_controls SET revision=revision+1",
])
def test_configuration_cannot_disappear_skip_revision_or_lose_its_signed_version(engine, statement):
    env = setup(engine)
    before = fast_path_snapshot(env)
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(statement)
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert fast_path_snapshot(env) == before


@pytest.mark.parametrize("statement", [
    "DELETE FROM app.sa_fast_path_batches",
    "UPDATE app.sa_fast_path_batches SET revision=revision+2",
    "UPDATE app.sa_fast_path_batches SET topic_key=repeat('0',64),revision=revision+1",
])
def test_batch_history_cannot_be_deleted_or_retargeted(engine, statement):
    env = setup(engine)
    ingest(env)
    before = fast_path_snapshot(env)
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(statement)
    assert fast_path_snapshot(env) == before
