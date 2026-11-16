"""Current, bounded references to original source inputs and delivered encrypted replies."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from mgar_v3.domain import DomainError

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.super_agent.source_correlation import _Input


def _same(engine: Engine, conn: Connection, source: _Input, other: _Input) -> bool:
    roles, _ = engine.source_policies._roles(
        conn, other.item.event.sender, other.item.event.source_thread, None
    )
    return (
        other.ref.project_id == source.ref.project_id
        and other.ref.source_instance_id == source.ref.source_instance_id
        and other.item.event.sender == source.item.event.sender
        and other.item.event.source_thread == source.item.event.source_thread
        and other.definition.receive.model_input
        and {"requester", "context_provider"}.issubset(roles)
    )


def current(engine: Engine, conn: Connection, scope: dict[str, Any], source: _Input) -> bool:
    refs = scope.get("conversation_dependencies", [])
    if not isinstance(refs, list) or len(refs) > 16:
        return False
    try:
        for ref in refs:
            old = engine.source_correlations._input(conn, UUID(ref["input_id"]))
            if old.ref.model_dump(mode="json") != ref or not _same(engine, conn, source, old):
                return False
    except (DomainError, ValueError, TypeError, KeyError):
        return False
    return True


def capture(engine: Engine, conn: Connection, source: _Input, turns: int) -> dict[str, Any]:
    if not turns:
        return {}
    rows = conn.execute(
        "SELECT r.run_id,m.scope,u.call_id,u.result,i.input_id FROM app.runs r "
        "JOIN app.tickets t USING(ticket_id) JOIN app.mandates m USING(mandate_id) "
        "JOIN app.usage_calls u ON u.run_id=r.run_id AND u.step_key='source-chat:answer:v1' "
        "JOIN app.source_ingress_inputs i ON i.input_id::text=m.scope->'source_input'->>'input_id' "
        "WHERE m.project_id=%s AND i.source_instance_id=%s AND i.identity_id=%s "
        "AND i.received_at<%s AND i.unavailable_at IS NULL AND r.status<>'cancelled' "
        "AND r.graph_version='source-model-conversation-v1' AND u.status='known' "
        "AND u.result IS NOT NULL AND EXISTS(SELECT 1 FROM app.actions a "
        "WHERE a.run_id=r.run_id AND a.action_type='source_notification' AND a.status='succeeded') "
        "ORDER BY i.received_at DESC,i.input_id DESC LIMIT 32",
        (
            source.ref.project_id,
            source.ref.source_instance_id,
            source.row["identity_id"],
            source.row["received_at"],
        ),
    ).fetchall()
    pairs: list[dict[str, Any]] = []
    dependencies: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            previous = engine.source_correlations._input(conn, row["input_id"])
            scope, result = row["scope"], row["result"]
            if (
                scope.get("source_input") != previous.ref.model_dump(mode="json")
                or scope.get("schema_version") != "source-model-conversation-v1"
                or not _same(engine, conn, source, previous)
                or not current(engine, conn, scope, source)
                or datetime.fromisoformat(result["expires_at"]) <= datetime.now(UTC)
                or result.get("source_state_sha256") != previous.ref.state_sha256
            ):
                continue
            refs = [
                previous.ref.model_dump(mode="json"),
                *scope.get("conversation_dependencies", []),
            ]
            expanded = dict(dependencies)
            expanded.update({ref["input_id"]: ref for ref in refs})
            if len(expanded) > 16:
                continue
            dependencies = expanded
            pairs.append(
                {
                    "run_id": str(row["run_id"]),
                    "call_id": str(row["call_id"]),
                    "input_id": str(row["input_id"]),
                    "answer_sha256": result["answer_sha256"],
                    "expires_at": min(
                        previous.expires, datetime.fromisoformat(result["expires_at"])
                    ).isoformat(),
                }
            )
            if len(pairs) >= turns:
                break
        except (DomainError, ValueError, TypeError, KeyError):
            continue
    return {
        "conversation_context": list(reversed(pairs)),
        "conversation_dependencies": list(dependencies.values()),
    }
