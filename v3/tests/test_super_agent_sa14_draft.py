"""Isolated SQL proposal checks only; not formal SA14 Engine/Job acceptance."""

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from mgar_v3.store.migrate import migrate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def draft_database(tmp_path_factory):
    dsn = os.environ.get("SA14_DRAFT_DATABASE_URL", "")
    info = conninfo_to_dict(dsn)
    # Never fall back to the configured development or acceptance database.
    assert info.get("host") == "127.0.0.1"
    assert int(info.get("port", 0)) not in {0, 5432, 55435, 55432}
    assert info.get("dbname") == "sa14_draft"
    assert info.get("user") == "sa14_draft"
    assert os.environ.get("SA14_DRAFT_CONTAINER", "").startswith("mgar-sa14-draft-")
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute("SELECT current_setting('cluster_name')").fetchone()[0]
            == os.environ["SA14_DRAFT_CONTAINER"]
        )
    staging = tmp_path_factory.mktemp("sa14-sql-proposal")
    originals = sorted((ROOT / "migrations").glob("*.sql"))
    assert len(originals) == 43
    for path in originals:
        (staging / path.name).write_bytes(path.read_bytes())
    proposal = ROOT / "docs/super-agent-v1/sa14-proposal/0047_source_async.sql"
    (staging / proposal.name).write_bytes(proposal.read_bytes())
    installed = migrate(dsn, staging)
    assert len(installed) == 44
    yield dsn


def test_draft_installs_six_tables_and_leaves_original_migrations_intact(draft_database):
    import hashlib

    with psycopg.connect(draft_database, row_factory=dict_row) as conn:
        installed = {
            row["name"]: row["sha256"]
            for row in conn.execute("SELECT name,sha256 FROM app.schema_migrations")
        }
        for path in (ROOT / "migrations").glob("*.sql"):
            assert installed[path.name] == hashlib.sha256(path.read_bytes()).hexdigest()
        tables = {
            row["tablename"]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='app' AND tablename LIKE 'sa_async_%'"
            )
        }
        assert tables == {
            "sa_async_bindings",
            "sa_async_inputs",
            "sa_async_checkpoints",
            "sa_async_jobs",
            "sa_async_operation_views",
            "sa_async_events",
        }
        assert conn.execute(
            "SELECT app.sa_async_current(%s) AS current", (uuid4(),)
        ).fetchone() == {"current": False}


@pytest.mark.parametrize(
    "restriction,valid",
    [
        ({"deny_write_paths": [], "deny_commands": []}, True),
        ({"deny_write_paths": ["src/db", "src/db.py"], "deny_commands": ["pytest"]}, True),
        ({"deny_write_paths": ["src/数据"], "deny_commands": ["python3.12"]}, True),
        ({"deny_write_paths": [""], "deny_commands": []}, False),
        ({"deny_write_paths": ["../src"], "deny_commands": []}, False),
        ({"deny_write_paths": ["src/../db"], "deny_commands": []}, False),
        ({"deny_write_paths": ["src//db"], "deny_commands": []}, False),
        ({"deny_write_paths": ["/src/db"], "deny_commands": []}, False),
        ({"deny_write_paths": ["src/"], "deny_commands": []}, False),
        ({"deny_write_paths": ["src:db"], "deny_commands": []}, False),
        ({"deny_write_paths": ["src%2fdb"], "deny_commands": []}, False),
        ({"deny_write_paths": ["src\\db"], "deny_commands": []}, False),
        ({"deny_write_paths": ["src\ndb"], "deny_commands": []}, False),
        ({"deny_write_paths": ["src/*"], "deny_commands": []}, False),
        ({"deny_write_paths": ["src/[db]"], "deny_commands": []}, False),
        ({"deny_write_paths": ["z", "a"], "deny_commands": []}, False),
        ({"deny_write_paths": ["src", "src"], "deny_commands": []}, False),
        ({"deny_write_paths": [], "deny_commands": ["pytest;echo"]}, False),
        ({"deny_write_paths": [], "deny_commands": ["pytest", "pytest"]}, False),
        ({"deny_write_paths": [], "deny_commands": [], "paid": True}, False),
        ({"deny_write_paths": []}, False),
        ({"deny_write_paths": "src", "deny_commands": []}, False),
        (None, False),
        ([], False),
    ],
)
def test_database_restrictions_are_bounded_exact_metadata(draft_database, restriction, valid):
    with psycopg.connect(draft_database) as conn:
        observed = conn.execute(
            "SELECT app.sa_async_restriction_valid(%s)", (Jsonb(restriction),)
        ).fetchone()[0]
        assert observed is valid


def test_sql_guards_are_attached_to_real_original_source_and_coding_tables(draft_database):
    expected = {
        ("sa_coding_operations", "sa_async_link_original_operation"),
        ("sa_coding_payloads", "sa_async_payload_bound"),
        ("source_ingress_inputs", "sa_async_ingress_input_changed"),
        ("source_ingress_bodies", "sa_async_ingress_body_removed"),
        ("source_role_bindings", "sa_async_role_changed"),
        ("source_task_controllers", "sa_async_controller_changed"),
        ("source_identities", "sa_async_identity_changed"),
        ("source_policies", "sa_async_policy_changed"),
        ("source_ingress_controls", "sa_async_ingress_control_changed"),
    }
    with psycopg.connect(draft_database) as conn:
        actual = set(
            conn.execute(
                "SELECT c.relname,t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='app' "
                "AND NOT t.tgisinternal AND t.tgname LIKE 'sa_async_%'"
            )
        )
        assert expected <= actual


def test_forwards_only_draft_never_adds_formal_0047_or_a_parallel_wallet(draft_database):
    assert not (ROOT / "migrations/0047_source_async.sql").exists()
    with psycopg.connect(draft_database) as conn:
        original_usage = conn.execute("SELECT count(*) FROM app.usage_calls").fetchone()[0]
        original_runs = conn.execute("SELECT count(*) FROM app.runs").fetchone()[0]
        assert original_usage == original_runs == 0
