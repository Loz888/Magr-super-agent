"""SA11 real PostgreSQL and signed synthetic Source fixtures; no live authority."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from sa05_support import payload
from sa10_support import ingest_native, link, link_id, resolved
from sa10_support import setup as native_setup

from mgar_v3.super_agent.source_subscription_domain import (
    SubscriptionAckRequest,
    SubscriptionPrepareRequest,
    SubscriptionSaveRequest,
)

TABLES = (
    "sa_source_subscriptions",
    "sa_source_subscription_versions",
    "sa_source_subscription_routes",
    "sa_source_subscription_deliveries",
    "sa_source_subscription_intakes",
    "sa_source_subscription_consumptions",
    "sa_source_subscription_handbacks",
)


def install_test_schema(engine):
    """Only normal formal migrations may satisfy this card's database fixture."""
    formal = Path(__file__).resolve().parents[1] / "migrations/0042_source_subscriptions.sql"
    assert formal.is_file()
    with engine.transaction() as conn:
        row = conn.execute("SELECT current_database() AS name").fetchone()
        assert row["name"].startswith("mgar_v3_test_")
        assert all(
            conn.execute("SELECT to_regclass(%s) AS name", ("app." + t,)).fetchone()["name"]
            is not None
            for t in TABLES
        )
    return False


def setup(
    engine,
    *,
    role="primary",
    selector_kind="resource",
    subscribe=True,
    ttl_seconds=3600,
    body_ttl_seconds=300,
):
    install_test_schema(engine)
    source = native_setup(engine, body_ttl_seconds=body_ttl_seconds)
    correlation = link(source)
    env = SimpleNamespace(
        engine=engine,
        source=source,
        correlations=source.service,
        service=engine.source_subscriptions,
        cookie=source.cookie,
        source_id=source.sourceenv.source_id,
        correlation_id=UUID(str(link_id(correlation))),
        native_input_id=source.native_input_id,
        resource_key=source.resource_key,
        target=source.target,
        run_id=source.target.run_id,
        subscription_id=uuid4(),
        subscription=None,
        role=role,
        selector_kind=selector_kind,
    )
    if subscribe:
        env.subscription = env.service.save(
            env.cookie, env.subscription_id, save_request(env, ttl_seconds=ttl_seconds)
        )
    return env


def save_request(env, *, role=None, ttl_seconds=3600, selector_kind=None):
    current = resolved(env.source)
    target = env.correlations.target(env.cookie, env.target.ticket_id)
    return SubscriptionSaveRequest(
        correlation_id=env.correlation_id,
        expected_correlation_revision=1,
        seed_input_id=env.native_input_id,
        resource_key=env.resource_key,
        target=target,
        role=role or env.role,
        selector_kind=selector_kind or env.selector_kind,
        ttl_seconds=ttl_seconds,
        snapshot_sha256=current["snapshot_sha256"],
        expected_revision=0,
        review_ref="SA11-reviewed-existing-native-target",
    )


def followup(
    env, text="/mgar investigate SA11-PRIVATE-FOLLOWUP", *, raw=None, delivery=None, number=7
):
    value = raw or payload("issue_comment")
    value["comment"]["body"] = text
    receipt, _resource = ingest_native(
        env.source,
        family="issue_comment",
        number=number,
        raw=value,
        delivery=delivery,
    )
    return receipt.input_ids[0]


def prepare(env, *, lane=None, cursor=0, limit=16, max_bytes=65536, epoch=None):
    if epoch is None:
        with env.engine.transaction() as conn:
            epoch = env.engine._run(conn, env.run_id)["execution_epoch"]
    return env.service.prepare(
        env.cookie,
        env.run_id,
        SubscriptionPrepareRequest(
            epoch=epoch,
            expected_cursor=cursor,
            lane=lane or env.role,
            limit=limit,
            max_bytes=max_bytes,
        ),
    )


def ack_request(page):
    from mgar_v3.super_agent.source_subscription_domain import SourceIntakeSnapshot

    value = page.get("snapshot", page) if isinstance(page, dict) else page
    if isinstance(value, dict):
        value = SourceIntakeSnapshot.model_validate_json(json.dumps(value, default=str))
    return SubscriptionAckRequest(
        epoch=value.epoch,
        expected_cursor=value.from_cursor,
        lane=value.lane,
        snapshot_id=value.snapshot_id,
        snapshot_sha256=value.snapshot_sha256,
    )


def deliveries(env):
    with env.engine.transaction() as conn:
        return conn.execute(
            "SELECT * FROM app.sa_source_subscription_deliveries WHERE run_id=%s "
            "ORDER BY source_seq",
            (env.run_id,),
        ).fetchall()


def business(env):
    """Source sequence allocation is allowed; original Run/legacy inputs are unchanged."""
    names = (
        "mandates",
        "tickets",
        "runs",
        "subscriptions",
        "mailbox",
        "trigger_events",
        "run_inputs",
        "actions",
        "approvals",
        "outbox",
        "usage_calls",
        "router_calls",
    )
    with env.engine.transaction() as conn:
        return {
            name: conn.execute(
                "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                "'[]'::jsonb) AS v "
                "FROM app." + name + " t"
            ).fetchone()["v"]
            for name in names
        }
