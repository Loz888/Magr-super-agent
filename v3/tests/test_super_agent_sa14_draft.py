"""Isolated SQL proposal checks only; not formal SA14 Engine/Job acceptance."""

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sa13_support import setup as coding_setup

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import Settings
from mgar_v3.store.migrate import migrate
from mgar_v3.super_agent.source_async_domain import AsyncScope

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


@pytest.fixture
def draft_scope(draft_database, tmp_path):
    from psycopg import sql

    # Disposable proposal DB only. No Engine exists during fixture reset.
    with psycopg.connect(draft_database, autocommit=True) as conn:
        tables = conn.execute(
            "SELECT schemaname,tablename FROM pg_tables WHERE schemaname IN ('app','knowledge') "
            "AND tablename NOT IN ('schema_migrations','workbench_phase_transitions')"
        ).fetchall()
        conn.execute(
            sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(",").join(sql.Identifier(*row) for row in tables)
            )
        )
    settings = Settings(
        database_url=draft_database,
        artifact_root=tmp_path / "artifacts",
        repository="example/mgar-test",
        local_actor="owner",
        allowed_actors=["owner"],
        graph_version="r1-quotes-v1",
        allow_paid=False,
        allow_external_write=False,
    )
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        env = coding_setup(engine, tmp_path)
        request, mandate = env.run_request, env.mandate
        with engine.transaction() as conn:
            _, facts = engine.source_coding.current(conn, env.worker["bearer"], request)
            scope = AsyncScope(
                coding_id=mandate.coding_id,
                coding_revision=mandate.revision,
                target=mandate.target,
                assignment_id=request.assignment_id,
                session_id=request.session_id,
                workspace_id=facts.workspace_id,
                epoch=request.epoch,
                mandate_sha256=mandate.mandate_sha256,
                profile_sha256=mandate.worker_profile_sha256,
                initial_context_sha256=mandate.context_sha256,
            )
            row = conn.execute(
                "INSERT INTO app.sa_async_bindings(async_id,coding_id,project_id,run_id,coding_revision,"
                "assignment_id,session_id,workspace_id,execution_epoch,mandate_sha256,profile_sha256,"
                "initial_context_sha256,scope_sha256,request_id,request_sha256,signed_by,review_sha256,"
                "expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    uuid4(),
                    mandate.coding_id,
                    mandate.target.project_id,
                    mandate.target.run_id,
                    mandate.revision,
                    request.assignment_id,
                    request.session_id,
                    facts.workspace_id,
                    request.epoch,
                    mandate.mandate_sha256,
                    mandate.worker_profile_sha256,
                    mandate.context_sha256,
                    scope.scope_sha256,
                    uuid4(),
                    digest("SA14-draft-request"),
                    mandate.signed_by,
                    digest("SA14-draft-review"),
                    mandate.valid_until,
                ),
            ).fetchone()
        yield engine, env, row
    finally:
        engine.close()


def test_actual_draft_scope_can_only_narrow_restrictions(draft_scope):
    engine, _, row = draft_scope
    narrowed = {"deny_write_paths": ["src/db"], "deny_commands": []}
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.sa_async_bindings SET restriction=%s,revision=revision+1,"
            "updated_at=clock_timestamp() WHERE async_id=%s",
            (Jsonb(narrowed), row["async_id"]),
        )
    with pytest.raises(psycopg.errors.RaiseException, match="cannot expand authority"):
        with engine.transaction() as conn:
            conn.execute(
                "UPDATE app.sa_async_bindings SET restriction=%s,revision=revision+1,"
                "updated_at=clock_timestamp() WHERE async_id=%s",
                (Jsonb({"deny_write_paths": [], "deny_commands": []}), row["async_id"]),
            )
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT restriction FROM app.sa_async_bindings WHERE async_id=%s",
                (row["async_id"],),
            ).fetchone()["restriction"]
            == narrowed
        )


def test_original_source_erasure_fences_in_same_transaction_and_rolls_back(draft_scope):
    engine, env, row = draft_scope
    with pytest.raises(RuntimeError, match="rollback boundary"):
        with engine.transaction() as conn:
            conn.execute(
                "SELECT app.expire_source_input(%s,%s)", (env.input_id, "SA14_DRAFT_ERASURE")
            )
            assert (
                conn.execute(
                    "SELECT state FROM app.sa_async_bindings WHERE async_id=%s", (row["async_id"],)
                ).fetchone()["state"]
                == "fenced"
            )
            raise RuntimeError("rollback boundary")
    with engine.transaction() as conn:
        before = conn.execute(
            "SELECT * FROM app.sa_async_bindings WHERE async_id=%s", (row["async_id"],)
        ).fetchone()
        assert before["state"] == "active" and before["received_sequence"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.sa_async_inputs").fetchone()["n"] == 0
        conn.execute("SELECT app.expire_source_input(%s,%s)", (env.input_id, "SA14_DRAFT_ERASURE"))
    with engine.transaction() as conn:
        after = conn.execute(
            "SELECT * FROM app.sa_async_bindings WHERE async_id=%s", (row["async_id"],)
        ).fetchone()
        inputs = conn.execute("SELECT kind,origin,state FROM app.sa_async_inputs").fetchall()
        assert (
            after["state"] == "fenced"
            and after["input_revision"] == after["received_sequence"] == 1
        )
        assert inputs == [{"kind": "invalidation", "origin": "system", "state": "received"}]
    with pytest.raises(psycopg.errors.RaiseException, match="cannot expand authority"):
        with engine.transaction() as conn:
            conn.execute(
                "UPDATE app.sa_async_bindings SET state='active',reason_code=NULL,"
                "fenced_at=NULL,revision=revision+1,updated_at=clock_timestamp() WHERE async_id=%s",
                (row["async_id"],),
            )
