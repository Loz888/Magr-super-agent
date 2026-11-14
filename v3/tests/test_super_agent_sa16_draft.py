"""SA16 draft install only: no official 0048, business activation or shared patch."""

import hashlib
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

APP = Path(__file__).resolve().parents[1]
PROPOSAL = APP / "docs/super-agent-v1/sa16-proposal"
TABLES = ("rules", "rule_versions", "ticks", "claims", "attempts", "events")


@pytest.fixture(scope="session")
def draft_database(database):
    with psycopg.connect(database, autocommit=True, row_factory=dict_row) as conn:
        before = conn.execute(
            "SELECT oid FROM pg_proc WHERE pronamespace='app'::regnamespace "
            "AND proname='sa_coding_current'"
        ).fetchone()["oid"]
        with conn.transaction():
            conn.execute((PROPOSAL / "0048_source_autonomy.sql").read_text())
        after = conn.execute(
            "SELECT oid FROM pg_proc WHERE pronamespace='app'::regnamespace "
            "AND proname='sa_coding_current'"
        ).fetchone()["oid"]
    return {"before_oid": before, "after_oid": after}


@pytest.fixture(autouse=True)
def installed(draft_database):
    return draft_database


def test_draft_preserves_original44_migrations_and_coding_oid(engine, installed):
    assert installed["before_oid"] == installed["after_oid"]
    assert not (APP / "migrations/0048_source_autonomy.sql").exists()
    expected = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (APP / "migrations").glob("*.sql")
    }
    assert len(expected) == 44
    with engine.transaction() as conn:
        actual = {
            r["name"]: r["sha256"]
            for r in conn.execute("SELECT name,sha256 FROM app.schema_migrations")
        }
    assert actual == expected


@pytest.mark.parametrize("suffix", TABLES)
def test_draft_all_six_tables_have_original_pg_guards(engine, suffix):
    with engine.transaction() as conn:
        name = "app.sa_autonomy_" + suffix
        assert conn.execute("SELECT to_regclass(%s) AS t", (name,)).fetchone()["t"]
        assert conn.execute(
            "SELECT count(*) AS n FROM pg_trigger "
            "WHERE tgrelid=to_regclass(%s) AND NOT tgisinternal AND tgenabled='O'",
            (name,),
        ).fetchone()["n"] >= 1
        assert conn.execute("SELECT count(*) AS n FROM " + name).fetchone()["n"] == 0


def test_draft_legacy_coding_predicate_retains_every_original_conjunct(engine):
    original = (APP / "migrations/0046_source_coding.sql").read_text()
    original = original.split("CREATE FUNCTION app.sa_coding_current(", 1)[1]
    original = original.split("$$;", 1)[0].split("AS $$", 1)[1]
    with engine.transaction() as conn:
        actual = conn.execute(
            "SELECT prosrc FROM pg_proc WHERE pronamespace='app'::regnamespace "
            "AND proname='sa_coding_current'"
        ).fetchone()["prosrc"]
        assert conn.execute(
            "SELECT app.sa_autonomy_run_current(%s) AS ok", (uuid4(),)
        ).fetchone()["ok"]
    assert actual.replace("        AND app.sa_autonomy_run_current(b.run_id)\n", "") == original


def test_draft_keeps_late_known_receipt_guard_unchanged(engine):
    published = (APP / "migrations/0046_source_coding.sql").read_text()
    expected = published.split("CREATE FUNCTION app.sa_coding_operation_guard()", 1)[1]
    expected = expected.split("$$;", 1)[0].split("AS $$", 1)[1]
    with engine.transaction() as conn:
        actual = conn.execute(
            "SELECT prosrc FROM pg_proc WHERE pronamespace='app'::regnamespace "
            "AND proname='sa_coding_operation_guard'"
        ).fetchone()["prosrc"]
    assert actual == expected
    assert "Late known receipts are facts" in actual


def test_shared_proposal_is_exactly_applicable_and_not_applied():
    metadata = json.loads((PROPOSAL / "patch-metadata.json").read_text())
    patch = PROPOSAL / "integration.patch"
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == metadata["patch_sha256"]
    assert hashlib.sha256((PROPOSAL / "0048_source_autonomy.sql").read_bytes()).hexdigest() == (
        metadata["sql_sha256"]
    )
    assert len(metadata["files"]) == 5
    for name, hashes in metadata["files"].items():
        assert hashlib.sha256((APP / name).read_bytes()).hexdigest() == hashes["before_sha256"]
    check = subprocess.run(
        ["git", "apply", "--check", str(patch)], cwd=APP.parent, capture_output=True, text=True
    )
    assert check.returncode == 0, check.stderr
