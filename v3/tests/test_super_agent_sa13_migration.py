"""Formal 0046 six-table guards, original composition and migration identity."""

import hashlib
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from sa13_support import setup

from mgar_v3.super_agent.source_coding import SourceCoding

APP = Path(__file__).resolve().parents[1]
SQL_NAME = "0046_source_coding.sql"
SQL_SHA = "379e9de4a018c7f38cc3615eae31b9c9d9e44ebdf16cbcf3c101c81d9874f212"
TABLES = (
    "sa_coding_bindings",
    "sa_coding_attempts",
    "sa_coding_operations",
    "sa_coding_payloads",
    "sa_coding_verifications",
    "sa_coding_events",
)


def test_formal0046_exact_approved_sql_and_original_connection(engine):
    formal = APP / "migrations" / SQL_NAME
    assert hashlib.sha256(formal.read_bytes()).hexdigest() == SQL_SHA
    assert (
        formal.read_bytes() == (APP / "docs/super-agent-v1/sa13-proposal" / SQL_NAME).read_bytes()
    )
    expected = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (APP / "migrations").glob("*.sql")
    }
    assert len(expected) == 44
    assert not any(name.startswith(("0037", "0040", "0045")) for name in expected)
    assert isinstance(engine.source_coding, SourceCoding)
    assert engine.source_coding.engine is engine
    assert engine.source_coding.runtime is None and engine.source_coding.cipher is None
    with engine.transaction() as conn:
        installed = {
            row["name"]: row["sha256"]
            for row in conn.execute("SELECT name,sha256 FROM app.schema_migrations")
        }
        assert installed == expected
        assert (
            conn.execute("SELECT current_database() AS n")
            .fetchone()["n"]
            .startswith("mgar_v3_test_")
        )
        for name in TABLES:
            assert conn.execute("SELECT to_regclass(%s) AS n", ("app." + name,)).fetchone()["n"]
            assert conn.execute("SELECT count(*) AS n FROM app." + name).fetchone()["n"] == 0


@pytest.mark.parametrize("name", TABLES)
def test_all_six_tables_have_enabled_exact_row_guard(engine, name):
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM pg_trigger WHERE tgrelid=to_regclass(%s) "
            "AND NOT tgisinternal AND tgenabled='O'",
            ("app." + name,),
        ).fetchone()
        assert row["n"] == 1


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_coding_audit_metadata_is_permanent(engine, tmp_path, operation):
    setup(engine, tmp_path, ready=False)
    query = (
        "UPDATE app.sa_coding_events SET event_type=event_type"
        if operation == "UPDATE"
        else "DELETE FROM app.sa_coding_events"
    )
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(query)


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM app.sa_coding_bindings",
        "UPDATE app.sa_coding_bindings SET state='revoked',revision=revision+1,"
        "invalidated_at=clock_timestamp(),reason_code='TEST',updated_at=clock_timestamp(),"
        "profile_sha256=repeat('0',64)",
        "UPDATE app.sa_coding_bindings SET revision=revision+1,updated_at=clock_timestamp()",
    ],
)
def test_binding_cannot_be_deleted_or_change_identity(engine, tmp_path, mutation):
    env = setup(engine, tmp_path, ready=False)
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(mutation)
    assert env.service.get(env.cookie, env.mandate.coding_id)["state"] == "admitted"


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM app.sa_coding_attempts",
        "UPDATE app.sa_coding_attempts SET state='result_recorded',revision=revision+1,"
        "updated_at=clock_timestamp()",
        "UPDATE app.sa_coding_attempts SET workspace_id=gen_random_uuid(),revision=revision+1,"
        "updated_at=clock_timestamp()",
    ],
)
def test_attempt_identity_and_result_need_original_trusted_facts(engine, tmp_path, mutation):
    env = setup(engine, tmp_path)
    with engine.transaction() as conn:
        env.service.begin_attempt(conn, env.worker["bearer"], env.run_request)
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(mutation)


def test_sql_current_function_rejects_foreign_assignment_or_epoch(engine, tmp_path):
    env = setup(engine, tmp_path)
    request = env.run_request
    with engine.transaction() as conn:

        def current(assignment, session, epoch):
            return conn.execute(
                "SELECT app.sa_coding_current(%s,%s,%s,%s,%s) AS ok",
                (request.coding_id, request.run_id, assignment, session, epoch),
            ).fetchone()["ok"]

        assert current(request.assignment_id, request.session_id, request.epoch)
        assert not current(uuid4(), request.session_id, request.epoch)
        assert not current(request.assignment_id, uuid4(), request.epoch)
        assert not current(request.assignment_id, request.session_id, request.epoch + 1)


def test_assignment_failure_rolls_back_attempt_and_events_together(engine, tmp_path):
    env = setup(engine, tmp_path)
    with pytest.raises(RuntimeError), engine.transaction() as conn:
        env.service.begin_attempt(conn, env.worker["bearer"], env.run_request)
        raise RuntimeError("test rollback original transaction")
    with engine.transaction() as conn:
        assert not conn.execute("SELECT 1 FROM app.sa_coding_attempts").fetchone()
        assert conn.execute("SELECT count(*) AS n FROM app.sa_coding_events").fetchone()["n"] == 1
