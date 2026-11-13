"""Real-PG0001-0036+0038 ->0039; retain original money and Source facts."""

import hashlib
import json
from itertools import count
from shutil import copyfile
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import SecretStr
from router_helpers import setup_router
from sa07_support import router, worker
from sa08_support import ingest, setup
from sa09_support import apply, ingest_case, prepared
from sa09_support import proposals as source_proposals
from sa09_support import setup as eval_setup

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.routing.budget import RouterBudget
from mgar_v3.settings import ROOT, Settings
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.store.migrate import migrate
from mgar_v3.super_agent import source_eval
from mgar_v3.super_agent.source_eval_domain import SourceEvalConfigure, SourceEvalSettings
from mgar_v3.super_agent.source_eval_model import SourceModelOutcome

MIGRATION = "0039_super_agent_source_eval.sql"
BASELINE_SHA256 = "4c22554312a5cc7e59498d7773437c4db6cf318f534a04d4be4ad764ea5e1b0f"
TABLES = {
    "sa_source_eval_controls",
    "sa_source_eval_versions",
    "sa_source_eval_snapshots",
    "sa_source_eval_inputs",
    "sa_source_eval_results",
    "sa_source_eval_decisions",
}


def snapshot(dsn, tables):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return {
            table: conn.execute(
                sql.SQL(
                    "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                    "'[]'::jsonb) "
                    "AS rows FROM app.{} t"
                ).format(sql.Identifier(table))
            ).fetchone()["rows"]
            for table in tables
        }


@pytest.mark.parametrize("fail_first", [False, True])
def test_37_to_38_preserves_known_unknown_source_facts_and_rolls_back_failed_ddl(
    database,
    tmp_path,
    fail_first,
):
    paths = sorted((ROOT / "migrations").glob("*.sql"))
    baseline = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.name < "0039"
    }
    manifest = "\n".join(f"{name}:{value}" for name, value in sorted(baseline.items()))
    assert len(baseline) == 37
    assert hashlib.sha256(manifest.encode()).hexdigest() == BASELINE_SHA256
    assert not any(p.name.startswith("0037") for p in paths)
    assert {p.name for p in paths} == set(baseline) | {
        MIGRATION, "0041_source_correlations.sql", "0042_source_subscriptions.sql",
        "0043_source_subscription_canonical_guard.sql",
    }
    directory = tmp_path / "before0039"
    directory.mkdir()
    for path in paths:
        if path.name in baseline:
            copyfile(path, directory / path.name)
    name = "sa09_upgrade_" + uuid4().hex
    assert name.startswith("sa09_upgrade_") and len(name) == 45
    with psycopg.connect(database, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(database, dbname=name)
    try:
        assert len(migrate(dsn, directory)) == 37
        settings = Settings(
            database_url=dsn,
            artifact_root=tmp_path / "pre-upgrade-artifacts",
            repository="example/mgar-test",
            local_actor="owner",
            allowed_actors=["owner"],
            graph_version="r2-mailbox-v1",
            allow_paid=True,
            deepseek_api_key=SecretStr("controlled-no-network-sa09-migration"),
        )
        old = Engine(settings, ArtifactStore(settings.artifact_root))
        try:
            source = setup(old)
            receipt = ingest(source, body="old authorized synthetic Source content")
            budget = SimpleNamespace(engine=old, sequence=count(100))
            entry = worker(budget)
            known = entry.reserve("pre0039-known")
            entry.settle(known, 17, {"prompt_tokens": 10, "completion_tokens": 7})
            unknown = entry.reserve("pre0039-unknown")
            routed = router(budget)
            router_known = routed.reserve("pre0039-router-known")
            routed.settle(router_known, 11, {"prompt_tokens": 6, "completion_tokens": 5})
            with old.transaction() as conn:
                batch = conn.execute(
                    "SELECT batch_id FROM app.router_calls WHERE call_id=%s",
                    (router_known,),
                ).fetchone()["batch_id"]
            old.routing.failure(batch, "MODEL_RESULT_UNAVAILABLE")
            routed_unknown = router(budget)
            router_unknown = routed_unknown.reserve("pre0039-router-unknown")
            entry.settle(unknown, None)
            routed_unknown.settle(router_unknown, None)
        finally:
            old.close()
        with psycopg.connect(dsn) as conn:
            old_tables = [
                r[0]
                for r in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='app' "
                    "AND tablename<>'schema_migrations' ORDER BY tablename",
                )
            ]
            assert conn.execute("SELECT to_regclass('app.sa_source_eval_controls')").fetchone() == (
                None,
            )
        before = snapshot(dsn, old_tables)
        assert {row["status"] for row in before["usage_calls"]} == {"known", "unknown"}
        assert {row["status"] for row in before["router_calls"]} == {"known", "unknown"}
        assert before["source_ingress_inputs"] and before["sa_fast_path_decisions"]
        if fail_first:
            failed = tmp_path / "failed0039"
            failed.mkdir()
            (failed / MIGRATION).write_text(
                (ROOT / "migrations" / MIGRATION).read_text() + "\nSELECT 1/0;\n"
            )
            with pytest.raises(psycopg.Error):
                migrate(dsn, failed)
            assert snapshot(dsn, old_tables) == before
            with psycopg.connect(dsn) as conn:
                assert (
                    dict(conn.execute("SELECT name,sha256 FROM app.schema_migrations")) == baseline
                )
                assert (
                    conn.execute(
                        "SELECT tablename FROM pg_tables WHERE schemaname='app' "
                        "AND tablename LIKE 'sa_source_eval_%'",
                    ).fetchall()
                    == []
                )
        assert migrate(dsn) == [
            MIGRATION, "0041_source_correlations.sql", "0042_source_subscriptions.sql",
            "0043_source_subscription_canonical_guard.sql",
        ]
        assert migrate(dsn) == []
        assert snapshot(dsn, old_tables) == before
        with psycopg.connect(dsn) as conn:
            checksums = dict(conn.execute("SELECT name,sha256 FROM app.schema_migrations"))
            assert len(checksums) == 41 and checksums == {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
            }
            assert {
                r[0]
                for r in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='app' "
                    "AND tablename LIKE 'sa_source_eval_%'",
                )
            } == TABLES
            assert all(
                conn.execute(
                    sql.SQL("SELECT count(*) FROM app.{}").format(
                        sql.Identifier(table),
                    )
                ).fetchone()
                == (0,)
                for table in TABLES
            )
        current = Engine(settings, ArtifactStore(settings.artifact_root))
        try:
            current.source_ingress.cipher = BodyCipher(source.key)
            assert current.source_ingress.body(source.cookie, receipt.input_ids[0]) == (
                b"old authorized synthetic Source content"
            )
            view = current.source_eval.read(
                source.cookie, source.installation.scope.source_instance_id
            )
            assert view["revision"] == 0 and view["settings"]["enabled"] is False
            assert snapshot(dsn, old_tables) == before
        finally:
            current.close()
    finally:
        with psycopg.connect(database, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def disabled_source(engine):
    env = setup(engine)
    source_eval.SourceEval(engine).configure(
        env.cookie,
        env.installation.scope.source_instance_id,
        SourceEvalConfigure(
            expected_revision=0,
            settings=SourceEvalSettings(),
            review_ref="SA09-real-owner-disabled",
        ),
    )
    return env


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_signed_eval_configuration_versions_are_immutable(engine, operation):
    disabled_source(engine)
    before = snapshot(engine.settings.database_url.get_secret_value(), TABLES)
    statement = (
        "UPDATE app.sa_source_eval_versions SET revision=revision"
        if operation == "update"
        else "DELETE FROM app.sa_source_eval_versions"
    )
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(statement)
    assert snapshot(engine.settings.database_url.get_secret_value(), TABLES) == before


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM app.sa_source_eval_controls",
        "UPDATE app.sa_source_eval_controls SET revision=revision+2",
        "UPDATE app.sa_source_eval_controls SET revision=revision+1",
    ],
)
def test_eval_control_requires_a_matching_signed_next_revision(engine, statement):
    disabled_source(engine)
    before = snapshot(engine.settings.database_url.get_secret_value(), TABLES)
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(statement)
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert snapshot(engine.settings.database_url.get_secret_value(), TABLES) == before


@pytest.mark.parametrize(
    "table",
    [
        "sa_source_eval_snapshots",
        "sa_source_eval_inputs",
        "sa_source_eval_results",
        "sa_source_eval_decisions",
    ],
)
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_real_source_snapshot_input_result_and_decision_facts_are_immutable(
    engine,
    table,
    operation,
):
    env = eval_setup(engine)
    apply(env)
    dsn = engine.settings.database_url.get_secret_value()
    before = snapshot(dsn, TABLES)
    assert before[table], "The tested immutable table must contain real fixture-pipeline facts"
    statement = (
        sql.SQL("UPDATE app.{} SET batch_id=batch_id").format(sql.Identifier(table))
        if operation == "update"
        else sql.SQL("DELETE FROM app.{}").format(sql.Identifier(table))
    )
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(statement)
    assert snapshot(dsn, TABLES) == before


@pytest.mark.parametrize("table", ["sa_source_eval_controls", "sa_source_eval_versions"])
def test_configuration_scope_foreign_keys_reject_nonexistent_source_or_project(engine, table):
    env = disabled_source(engine)
    before = snapshot(engine.settings.database_url.get_secret_value(), TABLES)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), engine.transaction() as conn:
        if table == "sa_source_eval_controls":
            conn.execute(
                "INSERT INTO app.sa_source_eval_controls(source_instance_id,project_id,revision,"
                "settings,settings_sha256,updated_by) SELECT %s,project_id,1,settings,"
                "settings_sha256,updated_by FROM app.sa_source_eval_controls",
                (uuid4(),),
            )
        else:
            conn.execute(
                "INSERT INTO app.sa_source_eval_versions(source_instance_id,project_id,revision,"
                "settings,settings_sha256,signed_by,review_ref) SELECT source_instance_id,%s,2,"
                "settings,settings_sha256,signed_by,review_ref FROM app.sa_source_eval_versions",
                ("foreign-project",),
            )
    assert snapshot(engine.settings.database_url.get_secret_value(), TABLES) == before
    assert env.engine is engine


def source_rows(env, batch):
    with env.engine.transaction() as conn:
        original = conn.execute(
            "SELECT * FROM app.router_batches WHERE batch_id=%s",
            (batch["batch_id"],),
        ).fetchone()
        source = conn.execute(
            "SELECT * FROM app.sa_source_eval_snapshots WHERE batch_id=%s",
            (batch["batch_id"],),
        ).fetchone()
    assert original is not None and source is not None
    return original, source


def insert_snapshot_clone(conn, original, source, *, change=None):
    """Only negative constraint probes clone a captured real snapshot; all roll back."""
    identifier = uuid4()
    content = {**source["snapshot"], "batch_id": str(identifier)}
    value = {
        **source,
        "batch_id": identifier,
        "snapshot": content,
        "fast_batch_revision": source["fast_batch_revision"] + 1,
    }
    value.update(change or {})
    fingerprint = hashlib.sha256(
        json.dumps(
            value["snapshot"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    value["snapshot_sha256"] = fingerprint
    conn.execute(
        "INSERT INTO app.router_batches(batch_id,project_id,policy_version,context_hash,snapshot,"
        "mode,status,expires_at) VALUES(%s,%s,%s,%s,%s,%s,'prepared',%s)",
        (
            identifier,
            value["project_id"],
            original["policy_version"],
            fingerprint,
            Jsonb({**value["snapshot"], "threads": []}),
            original["mode"],
            original["expires_at"],
        ),
    )
    conn.execute(
        "INSERT INTO app.sa_source_eval_snapshots(batch_id,source_instance_id,project_id,"
        "fast_batch_id,fast_batch_revision,eval_revision,snapshot_sha256,snapshot) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            identifier,
            value["source_instance_id"],
            value["project_id"],
            value["fast_batch_id"],
            value["fast_batch_revision"],
            value["eval_revision"],
            value["snapshot_sha256"],
            Jsonb(value["snapshot"]),
        ),
    )


@pytest.mark.parametrize("change", ["project", "fast-batch", "eval-revision"])
def test_snapshot_composite_scope_references_cannot_borrow_other_authority(engine, change):
    env = eval_setup(engine)
    batch = prepared(env)
    original, source = source_rows(env, batch)
    before = snapshot(engine.settings.database_url.get_secret_value(), TABLES | {"router_batches"})
    values = {
        "project": {"project_id": "foreign-project"},
        "fast-batch": {"fast_batch_id": uuid4()},
        "eval-revision": {"eval_revision": source["eval_revision"] + 1},
    }[change]
    with pytest.raises(psycopg.errors.ForeignKeyViolation), engine.transaction() as conn:
        insert_snapshot_clone(conn, original, source, change=values)
    assert (
        snapshot(engine.settings.database_url.get_secret_value(), TABLES | {"router_batches"})
        == before
    )


@pytest.mark.parametrize("corruption", ["tag", "hash", "original-body"])
def test_source_snapshot_requires_its_exact_original_namespace_and_hash(engine, corruption):
    env = eval_setup(engine)
    batch = prepared(env)
    original, source = source_rows(env, batch)
    identifier = uuid4()
    content = {**source["snapshot"], "batch_id": str(identifier)}
    fingerprint = digest(content)
    wrapped = {**content, "threads": []}
    if corruption == "tag":
        wrapped["schema_version"] = "synthetic-foreign-pipeline"
    elif corruption == "hash":
        fingerprint = "0" * 64
    else:
        wrapped["threads"] = [{"synthetic_namespace_corruption": True}]
    before = snapshot(engine.settings.database_url.get_secret_value(), TABLES | {"router_batches"})
    with pytest.raises(psycopg.errors.RaiseException, match="original scoped batch"):
        with engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.router_batches(batch_id,project_id,policy_version,context_hash,"
                "snapshot,mode,status,expires_at) VALUES(%s,%s,%s,%s,%s,%s,'prepared',%s)",
                (
                    identifier,
                    source["project_id"],
                    original["policy_version"],
                    fingerprint,
                    Jsonb(wrapped),
                    original["mode"],
                    original["expires_at"],
                ),
            )
            conn.execute(
                "INSERT INTO app.sa_source_eval_snapshots(batch_id,source_instance_id,project_id,"
                "fast_batch_id,fast_batch_revision,eval_revision,snapshot_sha256,snapshot) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    identifier,
                    source["source_instance_id"],
                    source["project_id"],
                    source["fast_batch_id"],
                    source["fast_batch_revision"] + 1,
                    source["eval_revision"],
                    digest(content),
                    Jsonb(content),
                ),
            )
    assert (
        snapshot(engine.settings.database_url.get_secret_value(), TABLES | {"router_batches"})
        == before
    )


def test_input_fact_must_reference_the_actual_canonical_version(engine):
    env = eval_setup(engine)
    batch = prepared(env)
    receipt = ingest_case(env, entity="second-canonical", delivery="second-canonical")
    before = snapshot(engine.settings.database_url.get_secret_value(), TABLES)
    with (
        pytest.raises(psycopg.errors.RaiseException, match="canonical version"),
        engine.transaction() as conn,
    ):
        conn.execute(
            "INSERT INTO app.sa_source_eval_inputs(batch_id,input_id,source_instance_id,project_id,"
            "version_fingerprint) VALUES(%s,%s,%s,%s,%s)",
            (
                batch["batch_id"],
                receipt.input_ids[0],
                env.installation.scope.source_instance_id,
                engine.settings.project_id,
                "0" * 64,
            ),
        )
    assert snapshot(engine.settings.database_url.get_secret_value(), TABLES) == before


def test_decision_foreign_key_cannot_bind_to_an_unknown_snapshot(engine):
    env = eval_setup(engine)
    _, decisions = apply(env)
    value = decisions[0].model_dump(mode="json")
    before = snapshot(engine.settings.database_url.get_secret_value(), TABLES)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.sa_source_eval_decisions(decision_key,batch_id,group_id,"
            "payload_sha256,payload) VALUES(%s,%s,%s,%s,%s)",
            (
                digest(["synthetic-invalid-snapshot", str(uuid4())]),
                uuid4(),
                uuid4(),
                digest(value),
                Jsonb(value),
            ),
        )
    assert snapshot(engine.settings.database_url.get_secret_value(), TABLES) == before


def controlled_financial_model(env, batch, permit):
    """A declared injected model receipt exercises PG settlement, never a network call."""
    return SimpleNamespace(
        permit=permit,
        max_context_bytes=env.eval_settings.max_context_bytes,
        max_output_bytes=env.eval_settings.max_output_bytes,
        request_body=lambda request: {"synthetic_controlled_financial_receipt": True},
        propose=lambda request: SourceModelOutcome(
            status="ok",
            reason_code="SOURCE_EVAL_CONTROLLED_RECEIPT",
            proposals=source_proposals(batch, "observe"),
            known_usage=True,
            input_tokens=1,
            output_tokens=0,
            cost_micros=1,
            response_sha256="a" * 64,
        ),
    )


def live_fixture_batch(engine):
    env = eval_setup(engine)
    _, permit = setup_router(engine)
    batch = prepared(env, mode="live")
    return env, batch, permit


def test_known_source_result_and_original_settlement_are_in_one_outer_transaction(
    engine,
    monkeypatch,
):
    env, batch, permit = live_fixture_batch(engine)
    dsn = engine.settings.database_url.get_secret_value()
    original_settle = RouterBudget.settle
    observed = []

    def observe_before_outer_commit(budget, call_id, *args, **kwargs):
        original_settle(budget, call_id, *args, **kwargs)
        with psycopg.connect(dsn, row_factory=dict_row) as reader:
            observed.append(
                reader.execute(
                    "SELECT status FROM app.router_calls WHERE call_id=%s",
                    (call_id,),
                ).fetchone()["status"]
            )
            assert (
                reader.execute(
                    "SELECT count(*) AS n FROM app.sa_source_eval_results WHERE batch_id=%s",
                    (batch["batch_id"],),
                ).fetchone()["n"]
                == 0
            )

    monkeypatch.setattr(RouterBudget, "settle", observe_before_outer_commit)
    provider = source_eval.SourceEvalProvider(
        env.service, controlled_financial_model(env, batch, permit)
    )
    assert provider.propose(batch) == source_proposals(batch, "observe")
    assert observed == ["reserved"]
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT c.status,c.settled_micros,r.usage_known FROM app.router_calls c "
            "JOIN app.sa_source_eval_results r USING(call_id) WHERE c.batch_id=%s",
            (batch["batch_id"],),
        ).fetchone()
        assert row == {"status": "known", "settled_micros": 1, "usage_known": True}


def test_failed_result_transaction_keeps_original_reservation_without_partial_settlement(
    engine,
    monkeypatch,
):
    env, batch, permit = live_fixture_batch(engine)
    original_settle = RouterBudget.settle

    def fail_after_settlement(budget, *args, **kwargs):
        original_settle(budget, *args, **kwargs)
        raise RuntimeError("controlled failure before Source result insert")

    monkeypatch.setattr(RouterBudget, "settle", fail_after_settlement)
    provider = source_eval.SourceEvalProvider(
        env.service, controlled_financial_model(env, batch, permit)
    )
    with pytest.raises(RuntimeError, match="controlled failure"):
        provider.propose(batch)
    with engine.transaction() as conn:
        call = conn.execute(
            "SELECT * FROM app.router_calls WHERE batch_id=%s",
            (batch["batch_id"],),
        ).fetchone()
        assert call["status"] == "reserved" and call["settled_micros"] is None
        assert (
            conn.execute(
                "SELECT reserved_micros FROM app.authorization_budgets WHERE profile_hash=%s",
                (call["budget_profile_hash"],),
            ).fetchone()["reserved_micros"]
            == call["reserved_micros"]
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.sa_source_eval_results WHERE batch_id=%s",
                (batch["batch_id"],),
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.parametrize("state", ["reserved", "unknown"])
def test_source_result_cannot_claim_known_without_its_original_known_call(engine, state):
    env, batch, permit = live_fixture_batch(engine)
    budget = RouterBudget(engine)
    call_id = budget.reserve(batch, permit, "a" * 64)
    if state == "unknown":
        budget.settle(call_id, None)
    before = snapshot(engine.settings.database_url.get_secret_value(), TABLES | {"router_calls"})
    with pytest.raises(psycopg.errors.RaiseException, match="original known batch call"):
        with engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.sa_source_eval_results(batch_id,call_id,outcome_code,usage_known) "
                "VALUES(%s,%s,'SYNTHETIC_INVALID_KNOWN_ASSERTION',true)",
                (batch["batch_id"], call_id),
            )
    assert (
        snapshot(engine.settings.database_url.get_secret_value(), TABLES | {"router_calls"})
        == before
    )


def test_source_result_cannot_borrow_a_different_batch_known_financial_receipt(engine):
    env, batch, _ = live_fixture_batch(engine)
    original = router(SimpleNamespace(engine=engine, sequence=count(100)))
    call_id = original.reserve("known-legacy-receipt-for-scope-negative")
    original.settle(call_id, 1, {"prompt_tokens": 1, "completion_tokens": 0})
    before = snapshot(engine.settings.database_url.get_secret_value(), TABLES | {"router_calls"})
    with pytest.raises(psycopg.errors.RaiseException, match="original known batch call"):
        with engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.sa_source_eval_results(batch_id,call_id,outcome_code,usage_known) "
                "VALUES(%s,%s,'SYNTHETIC_INVALID_FOREIGN_ASSERTION',true)",
                (batch["batch_id"], call_id),
            )
    assert (
        snapshot(engine.settings.database_url.get_secret_value(), TABLES | {"router_calls"})
        == before
    )
