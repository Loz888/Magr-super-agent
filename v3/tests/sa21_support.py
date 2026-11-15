"""SA21 metadata observations; all bodies and credentials are synthetic."""

import json
from uuid import uuid4

from sa04_support import envelope

from mgar_v3.sources.identity_domain import RevisionCommand
from mgar_v3.super_agent.policy_domain import PolicyRevoke


def withdraw(source, input_id, operation):
    engine = source.engine
    if operation in {"delete", "edit"}:
        with engine.transaction() as conn:
            item = conn.execute(
                "SELECT metadata FROM app.source_ingress_inputs WHERE input_id=%s", (input_id,)
            ).fetchone()["metadata"]
        event = item["event"]
        return source.gateway.receive(
            envelope(
                source,
                delivery="sa21-withdraw-" + uuid4().hex,
                entity=event["entity_id"],
                version=2,
                operation=operation,
                topic=event["source_thread"]["native_thread_id"],
                body="SA21 replacement is separate data",
            )
        )
    if operation == "identity":
        return source.identities.revoke_identity(
            source.cookie,
            source.binding["identity_id"],
            RevisionCommand(
                expected_revision=source.binding["identity_revision"],
                review_ref="SA21-control-plane-withdrawal",
            ),
        )
    assert operation == "policy"
    with engine.transaction() as conn:
        current = engine.source_policies._current(conn, source.policy_id)
    return engine.source_policies.revoke(
        source.cookie,
        source.policy_id,
        PolicyRevoke(
            expected_revision=current["current_revision"],
            review_ref="SA21-control-plane-withdrawal",
        ),
    )


def cleartext_in_database(engine, needle):
    """Inspect only this synthetic isolated database; never print stored content."""
    with engine.transaction() as conn:
        tables = conn.execute(
            "SELECT schemaname,tablename FROM pg_tables WHERE schemaname IN ('app','knowledge')"
        ).fetchall()
        return [
            row["schemaname"] + "." + row["tablename"]
            for row in tables
            if needle
            in json.dumps(
                conn.execute(
                    "SELECT to_jsonb(x) AS row FROM "
                    + row["schemaname"]
                    + "."
                    + row["tablename"]
                    + " x"
                ).fetchall(),
                default=str,
                ensure_ascii=False,
            )
        ]


def observation(kind, **facts):
    print("SA21_OBSERVATION " + json.dumps({"kind": kind, **facts}, default=str), flush=True)
