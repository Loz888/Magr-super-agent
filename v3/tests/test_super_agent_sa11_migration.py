"""Formal installation, original writer composition and durable fact constraints."""

import hashlib
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from sa11_support import TABLES, ack_request, followup, prepare, setup

from mgar_v3.api import create_app
from mgar_v3.super_agent.source_subscription import SourceSubscriptions
from mgar_v3.super_agent.source_subscription_domain import SubscriptionHandbackRequest

APP = Path(__file__).resolve().parents[1]
APPROVED_SQL_SHA = "3e29900f22472c4d8b0c5b2bc60a64a798134532b2e47ab09427cc5006d4c8d0"


def test_formal0042_checksum_and_original40_migration_catalogue(engine):
    formal = APP / "migrations/0042_source_subscriptions.sql"
    proposal = APP / "docs/super-agent-v1/sa11-proposal/0042_source_subscriptions.sql"
    assert formal.read_bytes() == proposal.read_bytes()
    assert hashlib.sha256(formal.read_bytes()).hexdigest() == APPROVED_SQL_SHA
    source = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (APP / "migrations").glob("*.sql")
    }
    assert len(source) == 42
    assert not any(n.startswith(("0037", "0040")) for n in source)
    with engine.transaction() as conn:
        installed = {
            r["name"]: r["sha256"]
            for r in conn.execute(
                "SELECT name,sha256 FROM app.schema_migrations",
            )
        }
        assert installed == source
        assert all(
            conn.execute("SELECT to_regclass(%s) AS n", ("app." + t,)).fetchone()["n"] is not None
            for t in TABLES
        )


def test_original_engine_and_unpatched_api_compose_same_connection_and11_routes(engine):
    assert isinstance(engine.source_subscriptions, SourceSubscriptions)
    assert engine.source_subscriptions.engine is engine
    with engine.transaction() as conn:
        assert conn is engine.source_subscriptions.engine.connection
    app = create_app(engine.settings, engine, run_background=False)
    prefix = "/api/v3/source-subscriptions"
    actual = [
        (method, r.path[len(prefix) :])
        for r in app.routes
        if getattr(r, "path", "").startswith(prefix + "/")
        for method in getattr(r, "methods", ())
        if method not in {"HEAD", "OPTIONS"}
    ]
    assert len(actual) == 11
    assert set(actual) == {
        ("POST", "/subscriptions"),
        ("GET", "/subscriptions/{subscription_id}"),
        ("GET", "/sources/{source_id}/subscriptions"),
        ("POST", "/dispatch"),
        ("GET", "/runs/{run_id}/mailbox"),
        ("POST", "/runs/{run_id}/prepare"),
        ("POST", "/runs/{run_id}/ack"),
        *(
            ("POST", "/subscriptions/{subscription_id}/" + action)
            for action in ["renew", "rebind", "unsubscribe", "handback"]
        ),
    }


@pytest.mark.parametrize("table", TABLES[1:])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_source_facts_are_immutable_even_before_ack_or_after_handback(engine, table, operation):
    env = setup(engine)
    input_id = followup(env)
    page = prepare(env)
    env.service.acknowledge(env.cookie, env.run_id, ack_request(page))
    view = env.service.get(env.cookie, env.subscription_id)
    env.service.handback(
        env.cookie,
        env.subscription_id,
        SubscriptionHandbackRequest(
            expected_revision=view["revision"],
            snapshot_sha256=view["snapshot_sha256"],
            input_ids=(input_id,),
            review_ref="SA11-migration-explicit-handback",
        ),
    )
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"] > 0
    statement = (
        (
            "UPDATE app."
            + table
            + " SET "
            + ("snapshot_id=snapshot_id" if table.endswith("consumptions") else "payload=payload")
        )
        if operation == "UPDATE"
        else "DELETE FROM app." + table
    )
    with pytest.raises(psycopg.Error):
        with engine.transaction() as conn:
            conn.execute(statement)


def test_committed_zero_head_cannot_exist_without_reviewed_history(engine):
    env = setup(engine, subscribe=False)
    with pytest.raises(psycopg.Error):
        with engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.sa_source_subscriptions(subscription_id,project_id,"
                "source_instance_id,correlation_id,seed_input_id) VALUES(%s,%s,%s,%s,%s)",
                (
                    uuid4(),
                    engine.settings.project_id,
                    env.source_id,
                    env.correlation_id,
                    env.native_input_id,
                ),
            )


def test_head_revision_cannot_skip_and_canonical_fingerprint_cannot_be_forged(engine):
    env = setup(engine)
    with pytest.raises(psycopg.Error):
        with engine.transaction() as conn:
            conn.execute(
                "UPDATE app.sa_source_subscriptions SET revision=revision+2 "
                "WHERE subscription_id=%s",
                (env.subscription_id,),
            )
    with pytest.raises(psycopg.Error):
        with engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.sa_source_subscription_routes(input_id,"
                "source_instance_id,project_id,version_fingerprint,outcome,payload,"
                "payload_sha256) VALUES(%s,%s,%s,%s,'blocked','{}',%s)",
                (
                    env.native_input_id,
                    env.source_id,
                    engine.settings.project_id,
                    "f" * 64,
                    "b" * 64,
                ),
            )


def test_reference_facts_do_not_prevent_original_source_body_retention_cleanup(engine):
    env = setup(engine)
    input_id = followup(env)
    with engine.transaction() as conn:
        conn.execute("SELECT app.expire_source_input(%s,'INGRESS_BODY_EXPIRED')", (input_id,))
        conn.execute(
            "UPDATE app.source_ingress_inputs SET metadata=NULL,sender_ref=NULL WHERE input_id=%s",
            (input_id,),
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.source_ingress_bodies WHERE input_id=%s", (input_id,)
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.sa_source_subscription_deliveries WHERE input_id=%s",
                (input_id,),
            ).fetchone()["n"]
            == 1
        )
