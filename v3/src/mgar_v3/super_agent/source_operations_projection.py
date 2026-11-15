"""Bounded, body-free projections of original ledgers; no heartbeat as progress."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mgar_v3.domain import digest
from mgar_v3.super_agent.source_operations_domain import (
    ComponentObservation,
    OperationsSnapshot,
    RunObservation,
    SourceObservation,
)

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection
    from mgar_v3.super_agent.source_operations import SourceOperations


def cursor(rows: Any) -> str:
    return digest(json.loads(json.dumps(rows, default=str, sort_keys=True)))


def snapshot(core: SourceOperations, conn: Connection) -> OperationsSnapshot:
    now = core.now(conn)
    version, definition = core._current(conn)
    current_ids = [g.component_id for g in definition.components] if definition else []
    components = conn.execute(
        "SELECT component_id,role,generation,boot_id,state,expires_at,sequence,config_revision "
        "FROM app.sa_operations_components WHERE project_id=%s AND component_id=ANY(%s::uuid[]) "
        "ORDER BY component_id LIMIT 17",
        (core.project, current_ids),
    ).fetchall()
    # A changed configuration invalidates old liveness immediately. Keep its
    # generation in the snapshot so a client can open the next fenced lease.
    cviews = [
        ComponentObservation.model_validate(
            {
                **{k: r[k] for k in ComponentObservation.model_fields},
                "expires_at": r["expires_at"]
                if version and r["config_revision"] == version["revision"]
                else min(r["expires_at"], version["signed_at"] if version else now),
            }
        )
        for r in components[:16]
    ]
    sources = conn.execute(
        "SELECT s.source_instance_id AS source_id,v.revision,c.settings,h.connection,h.permissions,"
        "h.observed_at AS health_at FROM app.source_instances s JOIN LATERAL "
        "(SELECT revision FROM app.source_instance_versions WHERE source_i"
        "nstance_id=s.source_instance_id "
        "ORDER BY revision DESC LIMIT 1) v ON true LEFT JOIN app.source_ingress_controls c "
        "USING(source_instance_id) LEFT JOIN app.feishu_source_health h USING(source_instance_id) "
        "WHERE s.project_id=%s ORDER BY s.source_instance_id LIMIT 33",
        (core.project,),
    ).fetchall()
    backlog = conn.execute(
        "SELECT input_id,source_instance_id,received_at FROM app.source_ingress_inputs "
        "WHERE project_id=%s AND consumed_at IS NULL AND unavailable_at IS NULL "
        "ORDER BY input_id LIMIT 10001",
        (core.project,),
    ).fetchall()
    gaps = conn.execute(
        "SELECT g.gap_id,g.source_instance_id,g.observed_at FROM app.source_ingress_gaps g "
        "JOIN app.source_instances s USING(source_instance_id) WHERE s.project_id=%s "
        "AND g.resolved_at IS NULL ORDER BY gap_id LIMIT 10001",
        (core.project,),
    ).fetchall()
    sviews = []
    for row in sources[:32]:
        sg = [g for g in gaps if g["source_instance_id"] == row["source_id"]]
        pending = sum(b["source_instance_id"] == row["source_id"] for b in backlog)
        enabled = (row["settings"] or {}).get("owner") == "gateway"
        sviews.append(
            SourceObservation(
                source_id=row["source_id"],
                revision=row["revision"],
                enabled=enabled,
                connection=row["connection"] or ("unknown" if enabled else "disabled"),
                permissions=row["permissions"] or "unknown",
                health_at=row["health_at"],
                open_gaps=len(sg),
                pending_inputs=pending,
                cursor=cursor([row, sg]),
            )
        )
    # Count facts from each stream; their independent sequence IDs cannot be
    # compared as one clock. Within the current Coding epoch/version, an
    # appended completion advances this cursor even with no new run_events.
    runs = conn.execute(
        "SELECT r.run_id,r.execution_epoch,r.status,r.created_at,"
        "e.completed+c.completed AS progress_cursor,"
        "greatest(r.created_at,e.created_at,c.created_at) AS progress_at "
        "FROM app.runs r JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
        "LEFT JOIN LATERAL (SELECT count(*) AS completed,max(created_at) AS created_at "
        "FROM app.run_events WHERE run_id=r.run_id "
        "AND kind NOT IN ('worker_heartbeat','watchdog_observed')) e ON true "
        "LEFT JOIN LATERAL (SELECT count(*) AS completed,"
        "max(ce.created_at) AS created_at FROM app.sa_coding_events ce "
        "JOIN app.sa_coding_bindings cb USING(coding_id,project_id) "
        "JOIN app.sa_coding_attempts ca ON ca.attempt_id=ce.attempt_id "
        "AND ca.coding_id=cb.coding_id AND ca.project_id=cb.project_id "
        "JOIN app.source_policies p ON p.policy_id=cb.policy_id AND p.project_id=cb.project_id "
        "JOIN app.source_policy_versions pv ON pv.policy_id=p.policy_id "
        "AND pv.revision=p.current_revision WHERE cb.run_id=r.run_id AND ca.run_id=r.run_id "
        "AND cb.project_id=h.project_id AND ca.execution_epoch=r.execution_epoch "
        "AND cb.state='admitted' AND ce.revision=cb.revision AND cb.revision=cb.mandate_revision "
        "AND p.current_revision=cb.policy_revision AND NOT pv.revoked "
        "AND pv.definition_sha256=cb.policy_sha256 AND ca.fenced_at IS NULL "
        "AND ca.state IN ('running','result_recorded') "
        "AND ce.event_type IN ('OPERATION_KNOWN','VERIFICATION_RECORDED',"
        "'ATTEMPT_RESULT_RECORDED')) c ON true WHERE h.project_id=%s AND ("
        "r.status NOT IN ('cancelled','failed','succeeded') "
        "OR EXISTS(SELECT 1 FROM app.actions WHERE run_id=r.run_id AND status='outcome_unknown') "
        "OR EXISTS(SELECT 1 FROM app.usage_calls WHERE run_id=r.run_id AND"
        " status IN ('reserved','unknown'))) "
        "ORDER BY r.run_id LIMIT 33",
        (core.project,),
    ).fetchall()
    rviews = []
    for row in runs[:32]:
        run_id = row["run_id"]
        jobs = conn.execute(
            "SELECT j.job_id,j.revision,j.state,least(b.valid_until,j.created_at+"
            "((m.scope->'grant'->>'max_seconds')::int*interval '1 second')) AS deadline "
            "FROM app.sa_async_jobs j JOIN app.sa_coding_bindings b USING(codi"
            "ng_id,project_id,run_id) "
            "JOIN app.mandates m USING(mandate_id) "
            "WHERE j.run_id=%s AND j.project_id=%s AND j.state<>'known' ORDER "
            "BY j.job_id LIMIT 257",
            (run_id, core.project),
        ).fetchall()
        leases = conn.execute(
            "SELECT assignment_id,revision,lease_until FROM app.sa_scheduler_assignments "
            "WHERE project_id=%s AND run_id=%s AND state<>'released' AND lease_until<=%s "
            "ORDER BY assignment_id LIMIT 257",
            (core.project, run_id, now),
        ).fetchall()
        actions = conn.execute(
            "SELECT action_id FROM app.actions WHERE run_id=%s AND status='outcome_unknown' "
            "ORDER BY action_id LIMIT 257",
            (run_id,),
        ).fetchall()
        calls = conn.execute(
            "SELECT call_id,status FROM app.usage_calls WHERE run_id=%s "
            "AND status IN ('reserved','unknown') ORDER BY call_id LIMIT 257",
            (run_id,),
        ).fetchall()
        rviews.append(
            RunObservation(
                run_id=run_id,
                epoch=row["execution_epoch"],
                state=row["status"],
                progress_cursor=row["progress_cursor"],
                progress_at=row["progress_at"],
                pending_jobs=len(jobs),
                overdue_jobs=sum(j["deadline"] <= now for j in jobs),
                expired_assignments=len(leases),
                unknown_actions=len(actions),
                unknown_calls=len(calls),
                job_cursor=cursor(jobs),
                lease_cursor=cursor(leases),
                unknown_actions_cursor=cursor(actions),
                unknown_calls_cursor=cursor(calls),
            )
        )
    outgoing = conn.execute(
        "SELECT a.action_id FROM app.actions a JOIN app.runs r USING(run_id) "
        "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
        "WHERE h.project_id=%s AND a.status='outcome_unknown' ORDER BY a.action_id LIMIT 10001",
        (core.project,),
    ).fetchall()
    usage = conn.execute(
        "SELECT b.call_id,coalesce(u.status,r.status) AS status FROM app.sa_budget_calls b "
        "LEFT JOIN app.usage_calls u ON b.ledger_kind='usage' AND u.call_id=b.call_id "
        "LEFT JOIN app.router_calls r ON b.ledger_kind='router' AND r.call_id=b.call_id "
        "WHERE b.project_id=%s AND coalesce(u.status,r.status) IN ('reserved','unknown') "
        "ORDER BY b.call_id LIMIT 10001",
        (core.project,),
    ).fetchall()
    dependency = conn.execute(
        "SELECT state,generation FROM app.dependency_health WHERE project_id=%s AND repository=%s "
        "AND dependency='redis' AND checked_at>%s-interval '60 seconds'",
        (core.project, core.engine.settings.repository.lower(), now),
    ).fetchone()
    partial = any(
        (
            len(components) > 16,
            len(sources) > 32,
            len(runs) > 32,
            len(gaps) > 10000,
            len(backlog) > 10000,
            len(outgoing) > 10000,
            len(usage) > 10000,
        )
    )
    return OperationsSnapshot(
        project_id=core.project,
        observed_at=now,
        config_revision=version["revision"] if version else 0,
        components=tuple(cviews),
        sources=tuple(sviews),
        runs=tuple(rviews),
        runner_available="worker" in core._ports
        and any(c.role == "worker" and c.state == "ready" and c.expires_at > now for c in cviews),
        source_backlog=len(backlog),
        notification_unknown=len(outgoing),
        usage_unknown=len(usage),
        redis=dependency["state"]
        if dependency and dependency["state"] in {"ready", "unavailable"}
        else "unknown",
        backlog_cursor=cursor(backlog),
        notification_cursor=cursor(outgoing),
        usage_cursor=cursor(usage),
        dependency_cursor=cursor(dependency),
        partial=partial,
    )
