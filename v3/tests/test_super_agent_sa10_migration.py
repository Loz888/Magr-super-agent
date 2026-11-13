"""SA10 migration regression mirrored from the preserved temporary-PG SQL probes.

Business assertions retain the original fourteen-case AST. The schema fixture
uses installed formal tables, or loads the exact proposal only in its own test DB.
Original component evidence: sql-proposal-20261112T145346Z.
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from sa05_support import BASE, HEAD, payload, setup, signed

from mgar_v3.api import create_app
from mgar_v3.domain import digest
from mgar_v3.super_agent.source_correlation import SourceCorrelations
from mgar_v3.super_agent.source_correlation_domain import CodeRef, ResourceRef, TargetRef

APP = Path(__file__).resolve().parents[1]
PROPOSAL = APP / "docs/super-agent-v1/sa10-proposal/0041_source_correlations.sql"
FORMAL_SQL_NAME = "0041_source_correlations.sql"
PROPOSAL_SHA256 = "d6aead811237e532664adf62b15521a16369fd29b8dfe9d26156f0d74fec1ee3"
TABLES = (
    "sa_correlation_resources",
    "sa_task_correlations",
    "sa_task_correlation_versions",
    "sa_task_correlation_claims",
    "sa_thread_link_inputs",
)


@pytest.fixture(scope="session", autouse=True)
def proposed_schema(database):
    with psycopg.connect(database) as conn:
        row = conn.execute(
            "SELECT current_database(),to_regclass('app.sa_task_correlations') IS NOT NULL"
        ).fetchone()
        if row is None or not row[0].startswith("mgar_v3_test_"):
            raise AssertionError("Migration probes require their disposable database")
        if not row[1]:
            conn.execute(PROPOSAL.read_text())


@pytest.fixture
def native(engine):
    env = setup(engine)
    receipt = env.runtime.receive(signed(payload("pull_request"), "pull_request"), 1)
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM app.source_ingress_inputs WHERE input_id=%s", (receipt.input_ids[0],)
        ).fetchone()
        user = conn.execute("SELECT user_id FROM app.web_accounts").fetchone()["user_id"]
    resource = ResourceRef(
        project_id=engine.settings.project_id,
        scope=env.instance.scope,
        repository_id=900,
        repository=engine.settings.repository,
        kind="pull_request",
        resource_id="7",
    )
    code = CodeRef(
        resource_key=resource.resource_key,
        proof_sha256=row["body_sha256"],
        head_repository_id=900,
        head_sha=HEAD,
        base_sha=BASE,
    )
    return SimpleNamespace(
        engine=engine,
        env=env,
        row=row,
        user=user,
        resource=resource,
        code=code,
        key=resource.resource_key,
        corr=uuid4(),
        unit="b" * 64,
    )


def counts(conn):
    return {
        name: conn.execute("SELECT count(*) AS n FROM app." + name).fetchone()["n"]
        for name in (*TABLES, "tickets", "runs", "trigger_events")
    }


def observe(conn, state, **changes):
    values = dict(
        input_id=state.row["input_id"],
        source_instance_id=state.row["source_instance_id"],
        project_id=state.row["project_id"],
        version_fingerprint=state.row["version_fingerprint"],
        resource_key=state.key,
        resource=Jsonb(state.resource.model_dump(mode="json")),
        code=Jsonb(state.code.model_dump(mode="json")),
        proof_sha256=state.code.proof_sha256,
        code_version_key=state.code.code_version_key,
    )
    values.update(changes)
    conn.execute(
        "INSERT INTO app.sa_correlation_resources("
        + ",".join(values)
        + ") VALUES ("
        + ",".join(["%s"] * len(values))
        + ")",
        tuple(values.values()),
    )


def placeholder(conn, state, *, claim=True):
    observe(conn, state)
    conn.execute(
        "INSERT INTO app.sa_task_correlations(correlation_id,project_id) VALUES(%s,%s)",
        (state.corr, state.row["project_id"]),
    )
    if claim:
        conn.execute(
            "INSERT INTO app.sa_task_correlation_claims(project_id,resource_key,correlation_id,"
            "original_input_id,source_instance_id,version_fingerprint) VALUES(%s,%s,%s,%s,%s,%s)",
            (
                state.row["project_id"],
                state.key,
                state.corr,
                state.row["input_id"],
                state.row["source_instance_id"],
                state.row["version_fingerprint"],
            ),
        )


def target(conn, state, receipt):
    row = conn.execute(
        "SELECT h.thread_id,h.revision,h.project_id,h.repository,t.ticket_id,"
        "m.mandate_id,to_jsonb(m) AS mandate,r.run_id,r.execution_epoch FROM app.threads h "
        "JOIN app.tickets t USING(thread_id) JOIN app.mandates m USING(mandate_id) "
        "JOIN app.runs r USING(ticket_id) WHERE r.run_id=%s",
        (receipt.run_id,),
    ).fetchone()
    return TargetRef(
        ticket_id=row["ticket_id"],
        run_id=row["run_id"],
        thread_id=row["thread_id"],
        thread_revision=row["revision"],
        execution_epoch=row["execution_epoch"],
        mandate_id=row["mandate_id"],
        mandate_sha256=digest(row["mandate"]),
        project_id=row["project_id"],
        repository=row["repository"],
        allowed=True,
    )


def version(
    conn,
    state,
    exact,
    *,
    revision=1,
    previous=None,
    anchor=True,
    bad_epoch=False,
    bad_payload=False,
):
    reference = exact.model_dump(mode="json")
    if bad_payload:
        reference["ticket_id"] = str(uuid4())
    if bad_epoch:
        reference["execution_epoch"] += 1
    body = {
        "schema_version": "source-correlation-v1",
        "state": "bound",
        "target": reference,
        "anchors": [{"input_id": str(state.row["input_id"]), "unit_key": state.unit}],
        "resource_keys": [state.key],
        "snapshot_sha256": "e" * 64,
    }
    conn.execute(
        "INSERT INTO app.sa_task_correlation_versions(correlation_id,revision,project_id,"
        "previous_revision,thread_id,ticket_id,run_id,thread_revision,execution_epoch,mandate_id,"
        "mandate_sha256,repository,snapshot_sha256,payload,payload_sha256,reason_code,review_sha256,"
        "signed_by) VALUES(" + ",".join(["%s"] * 18) + ")",
        (
            state.corr,
            revision,
            state.row["project_id"],
            previous,
            exact.thread_id,
            exact.ticket_id,
            exact.run_id,
            exact.thread_revision,
            exact.execution_epoch + (1 if bad_epoch else 0),
            exact.mandate_id,
            exact.mandate_sha256,
            exact.repository,
            "e" * 64,
            Jsonb(body),
            digest(body),
            "EXACT_RESOURCE",
            "f" * 64,
            state.user,
        ),
    )
    if anchor:
        conn.execute(
            "INSERT INTO app.sa_thread_link_inputs(correlation_id,revision,input_id,"
            "source_instance_id,project_id,version_fingerprint,unit_key,"
            "conversation_sha256,resource_key) "
            "VALUES(" + ",".join(["%s"] * 9) + ")",
            (
                state.corr,
                revision,
                state.row["input_id"],
                state.row["source_instance_id"],
                state.row["project_id"],
                state.row["version_fingerprint"],
                state.unit,
                "c" * 64,
                state.key,
            ),
        )
    conn.execute(
        "UPDATE app.sa_task_correlations SET revision=revision+1 WHERE correlation_id=%s "
        "AND revision=%s",
        (state.corr, revision - 1),
    )


def full(conn, state, trigger, **changes):
    placeholder(conn, state, claim=changes.pop("claim", True))
    # A separate trusted console request uses the old supported CLI route on the
    # original connection. It neither spoofs the gateway's legacy GitHub owner
    # nor manufactures an Issue from an IM message or a native PR callback.
    receipt = state.engine.ingest(trigger.model_copy(update={"source": "cli"}))
    exact = target(conn, state, receipt)
    version(conn, state, exact, **changes)
    return exact


def test_only_five_new_tables_and_no_fk_to_purgeable_inbox(native):
    with native.engine.transaction() as conn:
        found = {
            row["tablename"]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE "
                "schemaname='app' AND (tablename LIKE 'sa_correlation_%' OR tablename LIKE "
                "'sa_task_correlation%' OR tablename LIKE 'sa_thread_link_%')"
            )
        }
        assert found == set(TABLES)
        assert not conn.execute(
            "SELECT 1 FROM pg_constraint WHERE contype='f' "
            "AND conrelid=ANY(%s::regclass[]) AND confrelid='app.source_inbox'::regclass",
            ([f"app.{name}" for name in TABLES],),
        ).fetchall()


def test_original_connection_factory_claim_and_complete_revision_commit(native, trigger):
    with native.engine.transaction() as conn:
        exact = full(conn, native, trigger)
    with native.engine.transaction() as conn:
        assert all(value == 1 for value in counts(conn).values())
        assert (
            conn.execute("SELECT ticket_id FROM app.sa_task_correlation_versions").fetchone()[
                "ticket_id"
            ]
            == exact.ticket_id
        )


def test_unfinished_zero_head_cannot_commit(native):
    with pytest.raises(psycopg.Error):
        with native.engine.transaction() as conn:
            placeholder(conn, native)
    with native.engine.transaction() as conn:
        assert not any(counts(conn).values())


@pytest.mark.parametrize("missing", ["claim", "anchor"])
def test_complete_history_requires_native_claim_and_anchor(native, trigger, missing):
    with pytest.raises(psycopg.Error):
        with native.engine.transaction() as conn:
            full(conn, native, trigger, **{missing: False})
    with native.engine.transaction() as conn:
        assert not any(counts(conn).values())


@pytest.mark.parametrize("wrong", ["epoch", "payload-target"])
def test_original_target_snapshot_and_payload_must_match(native, trigger, wrong):
    with pytest.raises(psycopg.Error):
        with native.engine.transaction() as conn:
            full(
                conn,
                native,
                trigger,
                bad_epoch=wrong == "epoch",
                bad_payload=wrong == "payload-target",
            )
    with native.engine.transaction() as conn:
        assert not any(counts(conn).values())


@pytest.mark.parametrize("wrong", ["fingerprint", "scope"])
def test_resource_cannot_forge_canonical_fingerprint_or_owner_scope(native, wrong):
    with pytest.raises(psycopg.Error):
        with native.engine.transaction() as conn:
            observe(
                conn,
                native,
                **(
                    {"version_fingerprint": "0" * 64}
                    if wrong == "fingerprint"
                    else {"source_instance_id": uuid4()}
                ),
            )
    with native.engine.transaction() as conn:
        assert not any(counts(conn).values())


def test_all_fact_rows_immutable_and_head_identity_or_jump_rejected(native, trigger):
    with native.engine.transaction() as conn:
        full(conn, native, trigger)
    for table in TABLES:
        commands = ["DELETE FROM app." + table]
        if table == "sa_task_correlations":
            commands += [
                "UPDATE app." + table + " SET correlation_id=gen_random_uuid()",
                "UPDATE app." + table + " SET revision=revision+2",
            ]
        else:
            commands += ["UPDATE app." + table + " SET project_id=project_id"]
        for command in commands:
            with pytest.raises(psycopg.Error):
                with native.engine.transaction() as conn:
                    conn.execute(command)
    with native.engine.transaction() as conn:
        assert all(value == 1 for value in counts(conn).values())


def test_correction_anchors_old_revision_and_future_epoch_does_not_erase_history(native, trigger):
    with native.engine.transaction() as conn:
        old = full(conn, native, trigger)
    next_trigger = trigger.model_copy(
        update={"external_event_id": "second-factory", "issue_number": 2}
    )
    with native.engine.transaction() as conn:
        receipt = native.engine.ingest(next_trigger.model_copy(update={"source": "cli"}))
        new = target(conn, native, receipt)
        version(conn, native, new, revision=2, previous=1)
    native.engine.cancel(old.run_id)
    with native.engine.transaction() as conn:
        rows = conn.execute(
            "SELECT l.revision,v.ticket_id FROM app.sa_thread_link_inputs l JOIN "
            "app.sa_task_correlation_versions v USING(correlation_id,revision,project_id) "
            "ORDER BY l.revision"
        ).fetchall()
        assert [(row["revision"], row["ticket_id"]) for row in rows] == [
            (1, old.ticket_id),
            (2, new.ticket_id),
        ]


def test_missing_previous_revision_or_orphan_future_history_rejected(native, trigger):
    with native.engine.transaction() as conn:
        exact = full(conn, native, trigger)
    with pytest.raises(psycopg.Error):
        with native.engine.transaction() as conn:
            version(conn, native, exact, revision=2, previous=None)
    with pytest.raises(psycopg.Error):
        with native.engine.transaction() as conn:
            version(conn, native, exact, revision=3, previous=2)
    with native.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.sa_task_correlation_versions").fetchone()[
                "n"
            ]
            == 1
        )


def test_failed_factory_rolls_back_claim_and_real_ticket_together(native, trigger):
    class SyntheticFailure(Exception):
        pass

    with pytest.raises(SyntheticFailure):
        with native.engine.transaction() as conn:
            full(conn, native, trigger)
            raise SyntheticFailure()
    with native.engine.transaction() as conn:
        assert not any(counts(conn).values())


def test_retention_cleanup_operations_leave_only_permanent_canonical_links(native, trigger):
    with native.engine.transaction() as conn:
        full(conn, native, trigger)
    with native.engine.transaction() as conn:
        conn.execute(
            "SELECT app.expire_source_input(%s,'SA10_TEST_RETENTION')", (native.row["input_id"],)
        )
        conn.execute(
            "UPDATE app.source_ingress_inputs SET metadata=NULL,sender_ref=NULL WHERE input_id=%s",
            (native.row["input_id"],),
        )
        conn.execute("DELETE FROM app.source_inbox WHERE inbox_id=%s", (native.row["inbox_id"],))
    with native.engine.transaction() as conn:
        row = conn.execute(
            "SELECT inbox_id,metadata,sender_ref FROM app.source_ingress_inputs WHERE input_id=%s",
            (native.row["input_id"],),
        ).fetchone()
        assert row == {"inbox_id": None, "metadata": None, "sender_ref": None}
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_bodies").fetchone()["n"] == 0
        )
        assert all(value == 1 for value in counts(conn).values())


def test_formal0041_is_installed_with_exact_approved_source_checksums(engine):
    """The private proposal fallback cannot count as formal migration integration."""
    formal = APP / "migrations" / FORMAL_SQL_NAME
    assert formal.is_file()
    assert hashlib.sha256(PROPOSAL.read_bytes()).hexdigest() == PROPOSAL_SHA256
    assert formal.read_bytes() == PROPOSAL.read_bytes()
    sources = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((APP / "migrations").glob("*.sql"))
    }
    assert len(sources) == 39 and FORMAL_SQL_NAME in sources
    assert not any(name.startswith(("0037", "0040")) for name in sources)
    with engine.transaction() as conn:
        installed = {
            row["name"]: row["sha256"]
            for row in conn.execute("SELECT name,sha256 FROM app.schema_migrations ORDER BY name")
        }
    assert installed == sources


def test_real_engine_and_unpatched_application_compose_original_connection_and_six_routes(engine):
    """No temporary API composition or replacement service can satisfy this guard."""
    held = engine.connection
    service = getattr(engine, "source_correlations", None)
    assert isinstance(service, SourceCorrelations)
    assert service.engine is engine
    assert service.engine.connection is held
    with engine.transaction() as conn:
        assert conn is held
        assert conn.info.backend_pid == held.info.backend_pid
    app = create_app(engine.settings, engine, run_background=False)
    prefix = "/api/v3/source-correlations"
    routes = {
        (method, route.path)
        for route in app.routes
        if getattr(route, "path", "").startswith(prefix)
        for method in getattr(route, "methods", ())
        if method not in {"HEAD", "OPTIONS"}
    }
    assert routes == {
        ("POST", prefix + "/resolve"),
        ("POST", prefix + "/links"),
        ("POST", prefix + "/links/{correlation_id}/correct"),
        ("POST", prefix + "/links/{correlation_id}/remove"),
        ("GET", prefix + "/links/{correlation_id}"),
        ("GET", prefix + "/inputs/{input_id}/links"),
    }
    assert engine.connection is held
