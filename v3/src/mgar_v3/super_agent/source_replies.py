"""Receipt replies on original records; no coding admission, model or new business ledger."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import digest
from mgar_v3.super_agent.policy_domain import ContentClass
from mgar_v3.super_agent.source_notification_domain import (
    NotificationFacts,
    NotificationPlanRequest,
)
from mgar_v3.super_agent.source_notification_store import denied

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.super_agent.source_correlation import _Input

GRAPH = "source-receipt-reply-v1"
Row = dict[str, Any]


def eligible(source: _Input) -> bool:
    event = source.item.event
    return (
        event.scope.source_kind == "feishu"
        and event.sender.actor_kind == "human"
        and event.addressed == "mention"
        and event.event_kind == "im.message.receive_v1"
        and not event.historical_backfill
        and event.provenance == "verified_source"
        and source.definition.mode == "auto_investigate"
        and not source.definition.execution.enabled
        and not source.definition.receive.model_input
    )


def matches_reply_origin(engine: Engine, conn: Connection, run: Row, source: _Input) -> bool:
    if run["graph_version"] != GRAPH or run["status"] != "waiting_input" or not eligible(source):
        return False
    row = conn.execute(
        "SELECT scope,policy_version,execution_mode FROM app.mandates WHERE mandate_id=%s",
        (run["mandate_id"],),
    ).fetchone()
    if row is None or row["policy_version"] != GRAPH:
        return False
    scope = row["scope"]
    roles, _ = engine.source_policies._roles(
        conn, source.item.event.sender, source.item.event.source_thread, None
    )
    return (
        scope.get("schema_version") == GRAPH
        and scope.get("source_input") == source.ref.model_dump(mode="json")
        and scope.get("receipt_only") is True
        and scope.get("read_only") is True
        and scope.get("actions") == ["source_notification"]
        and row["execution_mode"] == run["execution_mode"]
        and "requester" in roles
        and "context_provider" in roles
    )


def reply_facts(
    engine: Engine, conn: Connection, run: Row, kind: ContentClass
) -> NotificationFacts:
    if kind != "verified_result":
        raise denied("RECEIPT_REPLY_ONLY")
    row = conn.execute(
        "SELECT scope FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
    ).fetchone()
    if row is None or not isinstance(row["scope"].get("source_input"), dict):
        raise denied("REPLY_ORIGIN_REQUIRED")
    source = engine.source_correlations._input(conn, UUID(row["scope"]["source_input"]["input_id"]))
    if not matches_reply_origin(engine, conn, run, source):
        raise denied("REPLY_ORIGIN_CHANGED")
    return NotificationFacts(
        run_id=run["run_id"],
        ticket_id=run["ticket_id"],
        execution_epoch=run["execution_epoch"],
        input_revision=run["input_revision"],
        thread_revision=run["thread_revision"],
        state=run["status"],
        content_class=kind,
        cause_sha256=digest([GRAPH, source.ref.model_dump(mode="json"), run["execution_epoch"]]),
        verified=True,
        source_receipt=True,
    )


def queue_reply(engine: Engine, cookie: str, input_id: UUID) -> Row:
    """Explicit server operation under Owner and current notification policy.

    The small original Run is marked as a receipt workflow, never a coding Task
    or a verified patch. An original input has one workflow across reconnects.
    """
    core = engine.source_notifications
    with engine.transaction() as conn:
        owner = core._console(conn, cookie)
        source = engine.source_correlations._input(conn, input_id)
        config, settings = core._config(conn, source.ref.source_instance_id)
        _, _, profile = core._policy(conn, settings, source.ref.source_instance_id, owner)
        if not eligible(source) or source.item.event.occurred_at < settings.valid_from:
            raise denied("RECEIPT_REPLY_SCOPE")
        if source.item.event.source_thread != settings.destination.thread:
            raise denied("SOURCE_OR_DESTINATION_CHANGED")
        key = "source-receipt:" + digest([str(source.ref.source_instance_id), str(input_id)])
        previous = conn.execute(
            "SELECT r.run_id FROM app.threads t JOIN app.tickets k USING(thread_id) "
            "JOIN app.runs r USING(ticket_id) WHERE t.project_id=%s AND t.external_key=%s",
            (engine.settings.project_id, key),
        ).fetchone()
        if previous is None:
            thread, mandate, ticket, run_id = (uuid4() for _ in range(4))
            scope = {
                "schema_version": GRAPH,
                "receipt_only": True,
                "read_only": True,
                "actions": ["source_notification"],
                "source_input": source.ref.model_dump(mode="json"),
                "profile_hash": profile.profile_sha256,
                "signed_by": str(owner),
                "valid_until": min(source.expires, settings.valid_until).isoformat(),
            }
            conn.execute(
                "INSERT INTO app.threads(thread_id,project_id,repository,external_key,revision) "
                "VALUES(%s,%s,%s,%s,1)",
                (thread, engine.settings.project_id, engine.settings.repository, key),
            )
            conn.execute(
                "INSERT INTO app.mandates(mandate_id,project_id,actor,scope,policy_version,"
                "execution_mode) VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    mandate,
                    engine.settings.project_id,
                    engine.settings.local_actor,
                    Jsonb(scope),
                    GRAPH,
                    profile.mode,
                ),
            )
            conn.execute(
                "INSERT INTO app.budgets(mandate_id,limit_micros) VALUES(%s,0)", (mandate,)
            )
            conn.execute(
                "INSERT INTO app.tickets(ticket_id,mandate_id,thread_id,status) "
                "VALUES(%s,%s,%s,'open')",
                (ticket, mandate, thread),
            )
            conn.execute(
                "INSERT INTO app.runs(run_id,ticket_id,attempt_no,graph_version,execution_mode,"
                "status,question,input_revision,reason_code) "
                "VALUES(%s,%s,1,%s,%s,'waiting_input',%s,1,'SOURCE_RECEIPT_REPLY_ONLY')",
                (run_id, ticket, GRAPH, profile.mode, "来源消息接收与状态回复；无编码或模型授权"),
            )
            engine._event(conn, run_id, "source_receipt_reply_requested", input_id=str(input_id))
        else:
            run_id = previous["run_id"]
        # Nested original transactions commit together. A failed plan leaves no
        # partially created Thread/Ticket/Run, approval or Outbox.
        result = core.plan(
            cookie,
            NotificationPlanRequest(
                run_id=run_id, source_input=source.ref, content_class="verified_result"
            ),
        )
        conn.execute(
            "UPDATE app.source_ingress_inputs "
            "SET consumed_at=coalesce(consumed_at,clock_timestamp()) "
            "WHERE input_id=%s AND project_id=%s",
            (input_id, engine.settings.project_id),
        )
        return {
            **result,
            "run_id": run_id,
            "workflow_kind": GRAPH,
            "coding_task": False,
            "notification_revision": config["revision"],
        }
