"""First isolated 0051 SQL-only guard/integrity checks, not formal installation."""

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

A = Path(__file__).resolve().parents[1]
P = A / "docs/super-agent-v1/sa18-proposal/0051_source_review_async_intake.sql"


@pytest.fixture
def draft(database):
    with psycopg.connect(database) as conn:
        with conn.transaction(force_rollback=True):
            before = conn.execute(
                "SELECT oid::regprocedure::text,pg_get_functiondef(oid) FROM pg_proc "
                "WHERE pronamespace='app'::regnamespace AND proname IN "
                "('sa_async_current','sa_async_job_guard','sa_async_checkpoint_guard',"
                "'sa_async_payload_bound','sa_coding_operation_guard') ORDER BY proname"
            ).fetchall()
            conn.execute(P.read_text(), prepare=False)
            yield conn, before


def test_draft_installs_only_received_input_exception_and_changes_no_migration_ledger(draft):
    conn, _ = draft
    assert conn.execute(
        "SELECT app.sa_source_review_intake_current(%s)", (uuid4(),)
    ).fetchone() == (False,)
    assert conn.execute("SELECT count(*) FROM app.schema_migrations").fetchone() == (47,)
    assert not (A / "migrations/0051_source_review_async_intake.sql").exists()


def test_running_apply_checkpoint_model_and_job_guards_remain_byte_identical(draft):
    conn, before = draft
    after = conn.execute(
        "SELECT oid::regprocedure::text,pg_get_functiondef(oid) FROM pg_proc "
        "WHERE pronamespace='app'::regnamespace AND proname IN "
        "('sa_async_current','sa_async_job_guard','sa_async_checkpoint_guard',"
        "'sa_async_payload_bound','sa_coding_operation_guard') ORDER BY proname"
    ).fetchall()
    assert len(after) == 5 and after == before
    original = (A / "migrations/0047_source_async.sql").read_text()
    original = original.split("CREATE FUNCTION app.sa_async_input_guard()", 1)[1].split(
        "CREATE TRIGGER sa_async_input_guard", 1
    )[0]
    actual = P.read_text().split("CREATE OR REPLACE FUNCTION app.sa_async_input_guard()", 1)[1]
    replacement = (
        "OR (NEW.state='received' AND NEW.origin<>'system'\n"
        "                AND NOT app.sa_async_current(NEW.async_id)\n"
        "                AND NOT app.sa_source_review_intake_current(NEW.async_id))"
    )
    assert (
        actual.replace(
            replacement,
            "OR (NEW.state='received' AND NEW.origin<>'system' "
            "AND NOT app.sa_async_current(NEW.async_id))",
        )
        == original
    )
