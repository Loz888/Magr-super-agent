"""Project original Run, verifier and resource facts; never mint task success."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_autonomy_domain import AutonomyRuntimeFacts
from mgar_v3.super_agent.source_autonomy_rules import plan_maintenance
from mgar_v3.super_agent.source_autonomy_store import decode_rule

if TYPE_CHECKING:
    from uuid import UUID

    from mgar_v3.coordination.engine import Connection
    from mgar_v3.super_agent.source_autonomy_store import AutonomyStore, Row


def pending(core: AutonomyStore, conn: Connection, run_id: UUID) -> tuple[bool, bool]:
    core._conn(conn)
    money = (
        conn.execute(
            "SELECT 1 FROM app.usage_calls WHERE run_id=%s AND status IN ('reserved','unknown') "
            "UNION ALL SELECT 1 FROM app.actions WHERE run_id=%s "
            "AND status IN ('dispatching','outcome_unknown') LIMIT 1",
            (run_id, run_id),
        ).fetchone()
        is not None
    )
    resource = (
        conn.execute(
            "SELECT 1 FROM app.sa_scheduler_operations WHERE run_id=%s AND state<>'known_terminal' "
            "UNION ALL SELECT 1 FROM app.sa_coding_operations WHERE run_id=%s AND state<>'known' "
            "UNION ALL SELECT 1 FROM app.sa_async_jobs WHERE run_id=%s AND state<>'known' LIMIT 1",
            (run_id, run_id, run_id),
        ).fetchone()
        is not None
    )
    return money, resource


def facts(core: AutonomyStore, conn: Connection, attempt: Row) -> AutonomyRuntimeFacts:
    run = core.engine._run(conn, attempt["run_id"])
    money, resource = pending(core, conn, attempt["run_id"])
    current = True
    try:
        core._delegation(conn, attempt)
    except DomainError:
        current = False
    # Count committed evidence/tool/verification facts, never worker heartbeats,
    # arbitrary progress text, scheduler wakeups or model assertions of success.
    progress = conn.execute(
        "SELECT max(at) AS at FROM (SELECT created_at AS at FROM app.run_events "
        "WHERE run_id=%s AND kind IN ('evidence_read','readonly_report_completed') "
        "UNION ALL SELECT observed_at AS at FROM app.sa_coding_operations WHERE run_id=%s "
        "AND state='known' AND capability IN ('read','write','execute','verify')) x",
        (attempt["run_id"], attempt["run_id"]),
    ).fetchone()
    verified = (
        conn.execute(
            "SELECT 1 FROM app.verdicts WHERE run_id=%s AND report_hash=%s AND passed "
            "UNION ALL SELECT 1 FROM app.sa_coding_verifications v JOIN app.sa_coding_bindings b "
            "USING(coding_id,project_id) WHERE b.run_id=%s AND v.result='passed' "
            "AND v.mandate_sha256=%s LIMIT 1",
            (attempt["run_id"], run["report_hash"], attempt["run_id"], attempt["mandate_sha256"]),
        ).fetchone()
        is not None
    )
    return AutonomyRuntimeFacts(
        original_run_state=run["status"],
        authority_current=current,
        expires_at=attempt["authority_until"],
        created_at=attempt["created_at"],
        semantic_progress_at=progress["at"] if progress else None,
        unknown_usage=money,
        resource_unconfirmed=resource,
        original_result_known=verified,
        original_result_passed=verified,
    )


def view(core: AutonomyStore, conn: Connection, attempt: Row) -> Row:
    f = facts(core, conn, attempt)
    rule = decode_rule(core._rule(conn, attempt["rule_id"]))
    plan = plan_maintenance(rule, f, at=core.now(conn))
    run = core.engine._run(conn, attempt["run_id"])
    slots = conn.execute(
        "SELECT assignment_id,state,reason_code FROM app.sa_scheduler_assignments "
        "WHERE run_id=%s ORDER BY created_at DESC LIMIT 16",
        (attempt["run_id"],),
    ).fetchall()
    return {
        **{k: v for k, v in attempt.items() if k not in {"identity_id", "role_binding_id"}},
        "original_run_state": run["status"],
        "original_outcome": run["outcome"],
        "original_reason_code": run["reason_code"],
        "report_hash": run["report_hash"],
        "terminal_success": plan.terminal_success,
        "known_verified_result": f.original_result_known,
        "unknown_usage": f.unknown_usage,
        "resource_unconfirmed": f.resource_unconfirmed,
        "current_authority": f.authority_current,
        "maintenance": plan.model_dump(mode="json"),
        "assignments": slots,
        "notification_sent": False,
        "published": False,
    }


def stop(core: AutonomyStore, conn: Connection, attempt: Row, reason: str) -> Row:
    core._conn(conn)
    if attempt["control_state"] != "active":
        return attempt
    state = "expired" if reason == "AUTONOMY_TASK_EXPIRED" else "stopped"
    changed = conn.execute(
        "UPDATE app.sa_autonomy_attempts SET control_state=%s,reason_code=%s,"
        "revision=revision+1,updated_at=clock_timestamp() WHERE attempt_id=%s RETURNING *",
        (state, reason, attempt["attempt_id"]),
    ).fetchone()
    assert changed is not None
    engine = core.engine
    engine.cancel(attempt["run_id"])
    coding = conn.execute(
        "SELECT * FROM app.sa_coding_bindings WHERE run_id=%s AND state='admitted'",
        (attempt["run_id"],),
    ).fetchone()
    if coding is not None:
        fenced = conn.execute(
            "UPDATE app.sa_coding_bindings SET state='revoked',revision=revision+1,"
            "invalidated_at=clock_timestamp(),reason_code=%s,updated_at=clock_timestamp() "
            "WHERE coding_id=%s RETURNING *",
            (reason, coding["coding_id"]),
        ).fetchone()
        assert fenced is not None
        engine.source_coding._fence(conn, fenced, reason)
        binding = conn.execute(
            "SELECT * FROM app.sa_async_bindings WHERE coding_id=%s", (coding["coding_id"],)
        ).fetchone()
        if binding is not None:
            for job in conn.execute(
                "SELECT job_id,revision FROM app.sa_async_jobs WHERE coding_id=%s "
                "AND state IN ('prepared','running')",
                (coding["coding_id"],),
            ).fetchall():
                engine.source_async._cancel(conn, binding, job["job_id"], job["revision"])
    for queue in conn.execute(
        "SELECT * FROM app.sa_scheduler_queue WHERE run_id=%s AND state='queued'",
        (attempt["run_id"],),
    ).fetchall():
        updated = conn.execute(
            "UPDATE app.sa_scheduler_queue SET state='closed',revision=revision+1,"
            "closed_at=clock_timestamp(),reason_code=%s WHERE queue_id=%s RETURNING *",
            (reason, queue["queue_id"]),
        ).fetchone()
        assert updated is not None
        engine.project_budgets.release_quota(conn, "queue", queue["queue_quota_key"])
        engine.source_scheduler._event(conn, "QUEUE_CANCELLED", updated, state="closed")
    for assignment in engine.source_scheduler._occupied(conn):
        if assignment["run_id"] == attempt["run_id"]:
            engine.source_scheduler._drain(conn, assignment, "RUN_STOPPED")
    core._event(
        conn,
        {"rule_id": attempt["rule_id"]},
        state,
        reason,
        claim_id=attempt["claim_id"],
        attempt_id=attempt["attempt_id"],
    )
    return changed


def maintain(
    core: AutonomyStore, conn: Connection, *, limit: int, rule_id: UUID | None = None
) -> Row:
    core._conn(conn)
    attempts = conn.execute(
        "SELECT * FROM app.sa_autonomy_attempts WHERE project_id=%s AND control_state='active' "
        "AND (%s::uuid IS NULL OR rule_id=%s) ORDER BY created_at,attempt_id LIMIT %s FOR UPDATE",
        (core.project, rule_id, rule_id, limit + 1),
    ).fetchall()
    outcomes: list[dict[str, Any]] = []
    for attempt in attempts[:limit]:
        rule_row = core._rule(conn, attempt["rule_id"])
        f = facts(core, conn, attempt)
        plan = plan_maintenance(decode_rule(rule_row), f, at=core.now(conn))
        if plan.control == "stop":
            stop(core, conn, attempt, plan.reason_code)
        elif plan.control == "release" and plan.may_release_resource:
            for assignment in core.engine.source_scheduler._occupied(conn):
                if assignment["run_id"] == attempt["run_id"]:
                    reason = (
                        "WAITING_APPROVAL"
                        if f.original_run_state == "waiting_approval"
                        else (
                            "WAITING_INPUT"
                            if f.original_run_state == "waiting_input"
                            else "RUN_STOPPED"
                        )
                    )
                    core.engine.source_scheduler._drain(conn, assignment, reason)
        kind = (
            "original_terminal"
            if f.original_run_state in {"succeeded", "failed", "cancelled"}
            else ("waiting" if plan.control in {"hold", "release"} else None)
        )
        if (
            kind is not None
            and not conn.execute(
                "SELECT 1 FROM app.sa_autonomy_events WHERE attempt_id=%s AND kind=%s AND reason_code=%s",
                (attempt["attempt_id"], kind, plan.reason_code),
            ).fetchone()
        ):
            core._event(conn, rule_row, kind, plan.reason_code, attempt_id=attempt["attempt_id"])
        outcomes.append({"attempt_id": attempt["attempt_id"], **plan.model_dump(mode="json")})
    return {
        "outcomes": outcomes,
        "partial": len(attempts) > limit,
        "model_calls": 0,
        "notification_sent": False,
        "unknown_refunded": False,
    }
