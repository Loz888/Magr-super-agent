"""Forward-only 0035 -> 0036 retains old money and rolls failed DDL back."""

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
from sa07_support import budget_env as budget_env
from sa07_support import configure, consume, release, router, worker

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import ROOT, Settings
from mgar_v3.store.migrate import migrate

BASELINE_SHA256 = "2fc8ec833020cf0ac5761e3302ce6f574703bea5616bc25c5c5fb427f45a9df5"
MIGRATION = "0036_super_agent_budgets.sql"
FINANCIAL_TABLES = ("usage_calls", "router_calls", "authorization_budgets", "budgets")


def financial_snapshot(dsn):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return {
            table: conn.execute("SELECT * FROM app." + table + " ORDER BY 1").fetchall()
            for table in FINANCIAL_TABLES
        }


@pytest.mark.parametrize("fail_first", [False, True])
def test_35_to_36_preserves_known_unknown_original_fees_and_checksums(
    database, tmp_path, fail_first,
):
    name = "sa07_upgrade_" + uuid4().hex
    assert name.startswith("sa07_upgrade_") and len(name) == 45
    with psycopg.connect(database, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(database, dbname=name)
    directory = tmp_path / "before36"
    directory.mkdir()
    paths = sorted((ROOT / "migrations").glob("*.sql"))
    baseline = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths if path.name < "0036"
    }
    manifest = "\n".join(f"{key}:{value}" for key, value in sorted(baseline.items()))
    assert len(baseline) == 35
    assert hashlib.sha256(manifest.encode()).hexdigest() == BASELINE_SHA256
    for path in paths:
        if path.name < "0036":
            copyfile(path, directory / path.name)
    try:
        assert len(migrate(dsn, directory)) == 35
        settings = Settings(
            database_url=dsn, artifact_root=tmp_path / "old-artifacts",
            repository="example/mgar-test", local_actor="owner", allowed_actors=["owner"],
            graph_version="r2-mailbox-v1", allow_paid=True,
            deepseek_api_key=SecretStr("controlled-migration-key-not-a-real-secret"),
        )
        old = Engine(settings, ArtifactStore(settings.artifact_root))
        try:
            env = SimpleNamespace(engine=old, sequence=count(1))
            entry = worker(env)
            known = entry.reserve("known-before-shared-budget")
            entry.settle(known, 17, {"prompt_tokens": 10, "completion_tokens": 7})
            unknown = entry.reserve("unknown-before-shared-budget")
            old_router = router(env)
            router_known = old_router.reserve("router-known-before-shared-budget")
            old_router.settle(router_known, 11, {"prompt_tokens": 6, "completion_tokens": 5})
            # A known paid response with no usable proposal is a genuine Router
            # failure. Close its batch through the original service before the
            # next event; do not rewrite a batch state to bypass its guard.
            with old.transaction() as conn:
                batch_id = conn.execute(
                    "SELECT batch_id FROM app.router_calls WHERE call_id=%s", (router_known,)
                ).fetchone()["batch_id"]
            old.routing.failure(batch_id, "MODEL_RESULT_UNAVAILABLE")
            old_router = router(env)
            router_unknown = old_router.reserve("router-unknown-before-shared-budget")
            # Preserve unknowns only after every real preoccupation exists: the
            # inherited global ledger correctly prevents new calls afterwards.
            entry.settle(unknown, None)
            old_router.settle(router_unknown, None)
        finally:
            old.close()
        original = financial_snapshot(dsn)
        assert {row["status"] for row in original["usage_calls"]} == {"known", "unknown"}
        assert {row["status"] for row in original["router_calls"]} == {"known", "unknown"}
        for row in original["authorization_budgets"]:
            assert row["used_calls"] == 2
            assert row["reserved_micros"] == entry.amount
        if fail_first:
            failed = tmp_path / "failed36"
            failed.mkdir()
            path = ROOT / "migrations" / MIGRATION
            (failed / MIGRATION).write_text(path.read_text() + "\nSELECT 1/0;\n")
            with pytest.raises(psycopg.Error):
                migrate(dsn, failed)
            assert financial_snapshot(dsn) == original
            with psycopg.connect(dsn) as conn:
                assert conn.execute(
                    "SELECT to_regclass('app.sa_budget_policies')"
                ).fetchone() == (None,)
                assert conn.execute(
                    "SELECT count(*) FROM app.schema_migrations"
                ).fetchone() == (35,)
        assert migrate(dsn) == [
            MIGRATION, "0038_super_agent_fast_path.sql", "0039_super_agent_source_eval.sql",
            "0041_source_correlations.sql",
            "0042_source_subscriptions.sql",
        ]
        assert migrate(dsn) == []
        assert financial_snapshot(dsn) == original
        with psycopg.connect(dsn) as conn:
            checksums = dict(conn.execute(
                "SELECT name,sha256 FROM app.schema_migrations"
            ).fetchall())
            assert checksums == {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
            }
            assert {row[0] for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='app' "
                "AND tablename LIKE 'sa_budget_%'",
            ).fetchall()} == {
                "sa_budget_policies", "sa_budget_versions", "sa_budget_calls",
                "sa_budget_quota", "sa_budget_audit",
            }
            assert conn.execute(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema='app' "
                "AND table_name='sa_budget_calls' AND column_name IN "
                "('settled_micros','result','usage','payload','body','token','api_key')",
            ).fetchone() == (0,)
    finally:
        with psycopg.connect(database, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def signed_snapshot(env):
    with env.engine.transaction() as conn:
        return {
            table: conn.execute("SELECT * FROM app." + table + " ORDER BY 1,2").fetchall()
            for table in (
                "sa_budget_policies", "sa_budget_versions", "sa_budget_calls",
                "sa_budget_quota", "sa_budget_audit",
            )
        }


@pytest.mark.parametrize("table", ["sa_budget_versions", "sa_budget_calls", "sa_budget_audit"])
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_budget_signed_history_call_attribution_and_audit_are_immutable(
    budget_env, table, operation,
):
    env = budget_env
    configure(env)
    worker(env).reserve("schema-immutability")
    before = signed_snapshot(env)
    column = {
        "sa_budget_versions": "review_ref", "sa_budget_calls": "component",
        "sa_budget_audit": "event",
    }[table]
    statement = (
        sql.SQL("UPDATE app.{} SET {}={}").format(
            sql.Identifier(table), sql.Identifier(column), sql.Identifier(column),
        )
        if operation == "update" else sql.SQL("DELETE FROM app.{}").format(sql.Identifier(table))
    )
    with pytest.raises(psycopg.Error), env.engine.transaction() as conn:
        conn.execute(statement)
    assert signed_snapshot(env) == before


def test_budget_current_head_requires_exact_immutable_signature_including_timestamp(budget_env):
    env = budget_env
    configure(env)
    before = signed_snapshot(env)
    with pytest.raises(psycopg.Error), env.engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.sa_budget_versions(scope_type,scope_id,organization_id,revision,"
            "definition,definition_sha256,signed_by,review_ref,signed_at) "
            "SELECT scope_type,scope_id,organization_id,revision+1,definition,definition_sha256,"
            "signed_by,review_ref,signed_at+interval '1 second' FROM app.sa_budget_policies "
            "WHERE scope_type='project'"
        )
        conn.execute(
            "UPDATE app.sa_budget_policies SET revision=revision+1 WHERE scope_type='project'"
        )
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert signed_snapshot(env) == before


@pytest.mark.parametrize("statement", [
    "DELETE FROM app.sa_budget_policies",
    "UPDATE app.sa_budget_policies SET revision=revision+2",
    "UPDATE app.sa_budget_policies SET scope_id=scope_id||'-new',revision=revision+1",
])
def test_budget_schema_does_not_delete_scopes_rebind_identity_or_skip_revisions(
    budget_env, statement,
):
    env = budget_env
    configure(env)
    before = signed_snapshot(env)
    with pytest.raises(psycopg.Error), env.engine.transaction() as conn:
        conn.execute(statement)
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert signed_snapshot(env) == before


@pytest.mark.parametrize("statement", [
    "DELETE FROM app.sa_budget_quota",
    "UPDATE app.sa_budget_quota SET units=units+1",
    "UPDATE app.sa_budget_quota SET released_at=NULL",
    "UPDATE app.sa_budget_quota SET released_at=released_at+interval '1 second'",
])
def test_released_occupancy_cannot_be_erased_changed_or_reactivated_in_schema(
    budget_env, statement,
):
    env = budget_env
    configure(env)
    consume(env, "queue", "schema-quota")
    release(env, "queue", "schema-quota")
    before = signed_snapshot(env)
    with pytest.raises(psycopg.Error), env.engine.transaction() as conn:
        conn.execute(statement)
    assert signed_snapshot(env) == before
