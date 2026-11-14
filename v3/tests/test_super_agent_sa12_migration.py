"""Formal 0044, exact approved integration and original metadata authority."""

import hashlib
from pathlib import Path

import psycopg
import pytest
from sa12_support import claim, enqueue, session, setup

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_scheduler import SourceScheduler

APP = Path(__file__).resolve().parents[1]
SQL_NAME = "0044_source_scheduler.sql"
SQL_SHA = "7199a8020bd17ec069e0b18efc8c48fa3a63827cb83dafbde47fbad7bba07eb6"
TABLES = (
    "sa_scheduler_configs",
    "sa_scheduler_config_versions",
    "sa_scheduler_queue",
    "sa_scheduler_sessions",
    "sa_scheduler_assignments",
    "sa_scheduler_operations",
    "sa_scheduler_events",
)


def test_formal0044_exact_source_catalogue_and_original_engine_connection(engine):
    formal = APP / "migrations" / SQL_NAME
    assert hashlib.sha256(formal.read_bytes()).hexdigest() == SQL_SHA
    assert (
        formal.read_bytes()
        == (APP / "docs/super-agent-v1/sa12-proposal" / SQL_NAME).read_bytes()
    )
    expected = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (APP / "migrations").glob("*.sql")
    }
    assert len(expected) == 44
    assert not any(n.startswith(("0037", "0040", "0045")) for n in expected)
    assert isinstance(engine.source_scheduler, SourceScheduler)
    assert engine.source_scheduler.engine is engine
    with engine.transaction() as conn:
        installed = {
            r["name"]: r["sha256"]
            for r in conn.execute("SELECT name,sha256 FROM app.schema_migrations")
        }
        assert installed == expected
        assert all(
            conn.execute("SELECT to_regclass(%s) AS t", ("app." + t,)).fetchone()["t"]
            for t in TABLES
        )


@pytest.mark.parametrize(
    "table", ["sa_scheduler_config_versions", "sa_scheduler_events"]
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_signed_configuration_history_and_events_are_immutable(
    engine, tmp_path, table, operation
):
    env = setup(engine, tmp_path)
    if operation == "UPDATE":
        mutation = "UPDATE app." + table + " SET project_id=project_id"
    else:
        mutation = "DELETE FROM app." + table
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(mutation)
    assert env.service.get_config(env.cookie)


@pytest.mark.parametrize(
    "table",
    [
        "sa_scheduler_queue",
        "sa_scheduler_sessions",
        "sa_scheduler_assignments",
        "sa_scheduler_operations",
    ],
)
def test_resource_identity_cannot_be_deleted_after_real_claim(engine, tmp_path, table):
    env = setup(engine, tmp_path)
    enqueue(env)
    claim(env, session(env))
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute("DELETE FROM app." + table)


def test_assignment_cannot_skip_draining_or_release_without_quiescence(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    enqueue(env)
    claimed = claim(env, session(env))
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(
            "UPDATE app.sa_scheduler_assignments SET state='released',revision=revision+1,"
            "updated_at=clock_timestamp(),released_at=clock_timestamp() WHERE assignment_id=%s",
            (claimed["assignment_id"],),
        )
    assert (
        env.service.get_assignment(env.cookie, claimed["assignment_id"])["state"]
        == "preparing"
    )


def test_controller_transactions_roll_back_claim_start_and_quota_together(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    queued = enqueue(env)
    worker = session(env)
    with pytest.raises(RuntimeError):
        with engine.transaction():
            # Reject external resource IO from an uncommitted outer transaction.
            try:
                claim(env, worker)
            except DomainError:
                pass
            raise RuntimeError("explicit outer rollback")
    assert engine.get_run(queued["run_id"])["status"] == "queued"
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.sa_scheduler_assignments"
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.sa_budget_quota WHERE kind='slot'"
            ).fetchone()["n"]
            == 0
        )

    assert list(env.driver.root.iterdir()) == []
