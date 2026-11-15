"""Owner metadata projections; observing never evaluates, dispatches or cancels."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import SourceInstance
from mgar_v3.super_agent.source_console_store import ConsoleStore, Row

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection


class ConsoleProjection(ConsoleStore):
    @staticmethod
    def capabilities() -> Row:
        return {
            "schema_version": "source-console-v1",
            "default_mode": "shadow",
            "automatic_repair": False,
            "worker_gate": "NOT_ACCEPTED_FOR_LIVE_AUTO_REPAIR",
            "worker_gate_message": "自动修复尚未通过能力验收。",
            "live_source_channel": "NOT_RUN",
            "paid": "NOT_RUN",
            "publication": "NOT_RUN",
            "long_running": "NOT_RUN",
            "settings_activate_runtime": False,
        }

    def _source_view(self, conn: Connection, instance: SourceInstance) -> Row:
        identifier = instance.scope.source_instance_id
        config = self._config(conn, instance)
        control = conn.execute(
            "SELECT generation,settings FROM app.source_ingress_controls "
            "WHERE source_instance_id=%s AND project_id=%s",
            (identifier, self.project),
        ).fetchone()
        fast = conn.execute(
            "SELECT c.revision,v.settings FROM app.sa_fast_path_controls c "
            "JOIN app.sa_fast_path_versions v ON v.source_instance_id=c.source_instance_id "
            "AND v.revision=c.revision WHERE c.source_instance_id=%s AND c.project_id=%s",
            (identifier, self.project),
        ).fetchone()
        return {
            "source_id": identifier,
            "scope": instance.scope.model_dump(mode="json"),
            "source_kind": instance.scope.source_kind,
            "display_name": instance.display_name,
            "source_revision": instance.revision,
            "settings_revision": config["revision"] if config else 0,
            "settings": config["definition"] if config else None,
            "stale": bool(config and config["source_revision"] != instance.revision),
            "credential": self._credential_public(conn, identifier),
            "fast_path": fast or {"revision": 0, "settings": {"enabled": False}},
            "ingress": control or {"generation": 0, "settings": {"owner": "disabled"}},
            "health": {
                "connection": "unknown",
                "permissions": "unknown",
                "reason_code": "RUNTIME_NOT_STARTED",
                "runtime_current": False,
            },
        }

    def _health(self, instance: Row, runtimes: tuple[Any, ...]) -> Row:
        from mgar_v3.sources.feishu_runtime import FeishuRuntime
        from mgar_v3.sources.github_runtime import GitHubRuntime

        fallback = {
            "connection": "disabled"
            if not (instance["settings"] or {}).get("enabled")
            else "unknown",
            "permissions": "unknown",
            "runtime_current": False,
            "reason_code": "RUNTIME_NOT_STARTED",
        }
        for runtime in runtimes:
            if isinstance(runtime, GitHubRuntime):
                same_engine = runtime.broker.sources.engine is self.engine
            elif isinstance(runtime, FeishuRuntime):
                same_engine = runtime.sources.engine is self.engine
            else:
                continue
            if not same_engine or runtime.source_id != instance["source_id"]:
                continue
            try:
                current = runtime.status()
            except DomainError:
                return fallback
            at = current.get("observed_at")
            if at is not None and (
                not isinstance(at, datetime)
                or at.tzinfo is None
                or not timedelta(seconds=-30) <= datetime.now(UTC) - at < timedelta(minutes=5)
            ):
                return fallback
            connection = current.get("connection")
            permissions = current.get("permissions")
            if connection not in {
                "unknown",
                "disabled",
                "online",
                "offline",
                "degraded",
            } or permissions not in {"unknown", "granted", "insufficient", "revoked"}:
                return fallback
            if at is None and (connection == "online" or permissions != "unknown"):
                return fallback
            if instance["stale"] and permissions not in {"insufficient", "revoked"}:
                return fallback
            code = str(current.get("reason_code", "CURRENT_RUNTIME_STATUS"))
            return {
                "connection": connection,
                "permissions": permissions,
                "runtime_current": current.get(
                    "runtime_current", at is not None and not instance["stale"]
                ),
                "observed_at": at,
                "mode": current.get("mode", "unknown"),
                "reason_code": code
                if re.fullmatch(r"[A-Z0-9_]{1,100}", code)
                else "RUNTIME_STATUS",
            }
        return fallback

    def _tasks(self, conn: Connection, limit: int = 50) -> Row:
        rows = conn.execute(
            "SELECT r.run_id,r.ticket_id,t.thread_id,r.status,r.outcome,r.reason_code,"
            "r.execution_epoch,r.execution_mode,r.graph_version,r.created_at "
            "FROM app.runs r JOIN app.tickets t USING(ticket_id) JOIN app.threads th "
            "ON th.thread_id=t.thread_id WHERE th.project_id=%s "
            "ORDER BY r.created_at DESC,r.run_id LIMIT %s",
            (self.project, limit + 1),
        ).fetchall()
        return {"items": rows[:limit], "truncated": len(rows) > limit}

    def _decisions(self, conn: Connection, limit: int = 50) -> Row:
        rows = conn.execute(
            "SELECT d.input_id,d.source_instance_id,d.disposition,d.reason_code,d.mode,"
            "d.intended_action,d.observed_at,e.outcome_code,e.call_status,e.call_id,"
            "e.router_mode,e.has_proposals,e.semantic_action,e.semantic_reason "
            "FROM app.sa_fast_path_decisions d LEFT JOIN LATERAL ("
            "SELECT r.outcome_code,c.status AS call_status,c.call_id,b.mode AS router_mode,"
            "r.proposals IS NOT NULL AS has_proposals,"
            "(SELECT v.payload->>'intended_action' FROM app.sa_source_eval_decisions v "
            "WHERE v.batch_id=s.batch_id AND v.payload->'input_ids' @> "
            "jsonb_build_array(d.input_id::text) "
            "ORDER BY v.created_at DESC,v.decision_key LIMIT 1) AS semantic_action,"
            "(SELECT v.payload->>'reason_code' FROM app.sa_source_eval_decisions v "
            "WHERE v.batch_id=s.batch_id AND v.payload->'input_ids' @> "
            "jsonb_build_array(d.input_id::text) "
            "ORDER BY v.created_at DESC,v.decision_key LIMIT 1) AS semantic_reason "
            "FROM app.sa_source_eval_inputs i JOIN app.sa_source_eval_snapshots s USING(batch_id) "
            "LEFT JOIN app.sa_source_eval_results r ON r.batch_id=s.batch_id "
            "LEFT JOIN app.router_calls c ON c.batch_id=s.batch_id "
            "LEFT JOIN app.router_batches b ON b.batch_id=s.batch_id "
            "WHERE i.input_id=d.input_id AND i.project_id=d.project_id "
            "ORDER BY s.created_at DESC LIMIT 1) e ON true WHERE d.project_id=%s "
            "ORDER BY d.observed_at DESC,d.input_id LIMIT %s",
            (self.project, limit + 1),
        ).fetchall()
        for row in rows:
            if not row["call_id"]:
                semantic = "NOT_RUN"
            elif row["call_status"] in {"unknown", "reserved"}:
                semantic = "UNKNOWN" if row["call_status"] == "unknown" else "IN_FLIGHT"
            elif row["has_proposals"]:
                semantic = (
                    "CONTROLLED_EXECUTED" if row["router_mode"] == "fixture" else "LIVE_EXECUTED"
                )
            else:
                semantic = "FAILED"
            row["semantic_evaluation"] = semantic
            row.pop("has_proposals", None)
        return {
            "items": rows[:limit],
            "truncated": len(rows) > limit,
            "observing_invokes_model": False,
        }

    def decisions(self, cookie: str, limit: int = 50) -> Row:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError("CONSOLE_PAGE_BOUND", "选择1至100条记录。", 422)
        with self.engine.transaction() as conn:
            self._owner(conn, cookie)
            return self._decisions(conn, limit)

    def source(self, cookie: str, identifier: UUID, runtimes: tuple[Any, ...] = ()) -> Row:
        with self.engine._mutex:
            with self.engine.transaction() as conn:
                self._owner(conn, cookie)
                instance = self.identities.registry._instance(conn, identifier)
                value = self._source_view(conn, instance)
                snapshot = self._snapshot_hash(conn)
            value["health"] = self._health(value, runtimes)
            with self.engine.transaction() as conn:
                self._owner(conn, cookie)
                if snapshot != self._snapshot_hash(conn):
                    raise DomainError("CONSOLE_SNAPSHOT_STALE", "配置已更新，请刷新。", 409)
            return value

    def task(self, cookie: str, identifier: UUID) -> Row:
        with self.engine.transaction() as conn:
            self._owner(conn, cookie)
            run = self._run_context(conn, identifier)
            jobs = conn.execute(
                "SELECT job_id,async_id,assignment_id,state,result_code,revision,input_revision,"
                "created_at,updated_at FROM app.sa_async_jobs WHERE run_id=%s AND project_id=%s "
                "ORDER BY created_at DESC LIMIT 51",
                (identifier, self.project),
            ).fetchall()
            assignments = conn.execute(
                "SELECT assignment_id,session_id,workspace_id,state,lease_until,"
                "last_heartbeat_at,reason_code FROM app.sa_scheduler_assignments "
                "WHERE run_id=%s AND project_id=%s ORDER BY created_at DESC LIMIT 51",
                (identifier, self.project),
            ).fetchall()
            actions = conn.execute(
                "SELECT action_id,action_type,status,expires_at FROM app.actions "
                "WHERE run_id=%s ORDER BY expires_at DESC LIMIT 51",
                (identifier,),
            ).fetchall()
            usage = conn.execute(
                "SELECT count(*) AS calls,coalesce(sum(CASE WHEN status='known' "
                "AND settled_micros IS NOT NULL "
                "THEN settled_micros ELSE reserved_micros END),0) AS total_micros,"
                "count(*) FILTER(WHERE status IN ('reserved','unknown') OR "
                "settled_micros IS NULL) AS unresolved_calls "
                "FROM app.usage_calls WHERE run_id=%s",
                (identifier,),
            ).fetchone()
            links = conn.execute(
                "SELECT v.correlation_id,v.revision,v.reason_code,v.payload->>'state' AS state "
                "FROM app.sa_task_correlation_versions v JOIN app.sa_task_correlations c "
                "ON c.correlation_id=v.correlation_id AND c.revision=v.revision "
                "WHERE v.project_id=%s AND v.run_id=%s ORDER BY v.signed_at DESC LIMIT 51",
                (self.project, identifier),
            ).fetchall()
            authorities = conn.execute(
                "SELECT p.source_instance_id AS source_id,b.policy_id,b.policy_revision,"
                "NULL::uuid AS input_id,'policy' AS kind FROM app.source_run_policies b "
                "JOIN app.source_policies p USING(policy_id) WHERE b.run_id=%s AND b.project_id=%s UNION "
                "SELECT source_instance_id,policy_id,policy_revision,input_id,'coding' "
                "FROM app.sa_coding_bindings WHERE run_id=%s AND project_id=%s UNION "
                "SELECT source_instance_id,NULL::uuid,NULL::bigint,input_id,'autonomy' "
                "FROM app.sa_autonomy_attempts WHERE run_id=%s AND project_id=%s LIMIT 51",
                (identifier, self.project, identifier, self.project, identifier, self.project),
            ).fetchall()
            return {
                "run": run,
                "jobs": jobs[:50],
                "assignments": assignments[:50],
                "actions": actions[:50],
                "usage": usage,
                "source_links": links[:50],
                "source_authorities": authorities[:50],
                "truncated": any(
                    len(rows) > 50 for rows in (jobs, assignments, actions, links, authorities)
                ),
                "pending_jobs": sum(row["state"] != "known" for row in jobs),
                "closing_browser_cancels_task": False,
            }

    def _identity_views(self, conn: Connection) -> Row:
        evidence = conn.execute(
            "SELECT e.evidence_id,e.identity_id,e.source_revision,e.verified_at,e.expires_at,"
            "e.source_thread,i.actor_ref,i.revision AS identity_revision,i.revoked_at,"
            "(e.auth_revision=i.revision AND i.revoked_at IS NULL "
            "AND e.expires_at>clock_timestamp() AND e.source_revision=(SELECT max(v.revision) "
            "FROM app.source_instance_versions v WHERE v.source_instance_id=e.source_instance_id)) AS usable "
            "FROM app.source_identity_evidence e JOIN app.source_identities i USING(identity_id,project_id) "
            "WHERE e.project_id=%s ORDER BY e.verified_at DESC,e.evidence_id DESC LIMIT 101",
            (self.project,),
        ).fetchall()
        bindings = conn.execute(
            "SELECT b.binding_id,b.identity_id,b.roles,b.revision,b.active,b.revoked_at,"
            "i.actor_ref,i.revision AS identity_revision,i.revoked_at AS identity_revoked_at,"
            "i.principal_id,(SELECT count(*) FROM app.source_task_controllers c "
            "WHERE c.binding_id=b.binding_id AND c.active) AS controlled_ticket_count "
            "FROM app.source_role_bindings b JOIN app.source_identities i USING(identity_id,project_id) "
            "WHERE b.project_id=%s ORDER BY b.binding_id LIMIT 101",
            (self.project,),
        ).fetchall()
        principals = conn.execute(
            "SELECT principal_id,principal_kind,engine_actor,console_user_id "
            "FROM app.source_principals WHERE project_id=%s ORDER BY principal_id LIMIT 101",
            (self.project,),
        ).fetchall()
        return {
            "evidence": evidence[:100],
            "bindings": bindings[:100],
            "principals": principals[:100],
            "truncated": any(len(rows) > 100 for rows in (evidence, bindings, principals)),
        }

    def snapshot(self, cookie: str, runtimes: tuple[Any, ...] = ()) -> Row:
        with self.engine._mutex:
            with self.engine.transaction() as conn:
                self._owner(conn, cookie)
                snapshot = self._snapshot_hash(conn)
                sources = [self._source_view(conn, i) for i in self._instances(conn)]
                policies = self._policies(conn)
                identities = self._identity_views(conn)
                tasks = self._tasks(conn)
                decisions = self._decisions(conn)
                actions = conn.execute(
                    "SELECT a.action_id,a.run_id,a.action_type,a.status,a.expires_at "
                    "FROM app.actions a JOIN app.runs r USING(run_id) "
                    "JOIN app.tickets t USING(ticket_id) JOIN app.threads th ON "
                    "th.thread_id=t.thread_id "
                    "WHERE th.project_id=%s ORDER BY a.expires_at DESC LIMIT 51",
                    (self.project,),
                ).fetchall()
                budget = self.engine.project_budgets.inspect(cookie, "project", self.project)
            for source in sources:
                source["health"] = self._health(source, runtimes)
            with self.engine.transaction() as conn:
                self._owner(conn, cookie)
                if snapshot != self._snapshot_hash(conn):
                    raise DomainError("CONSOLE_SNAPSHOT_STALE", "配置已更新，请刷新后继续。", 409)
                at = self._now(conn)
            return {
                "schema_version": "source-console-v1",
                "project_id": self.project,
                "repository": self.engine.settings.repository,
                "observed_at": at,
                "snapshot_sha256": snapshot,
                "capabilities": self.capabilities(),
                "sources": sources,
                "identities": identities,
                "policies": policies,
                "tasks": tasks,
                "decisions": decisions,
                "budget": budget,
                "actions": {"items": actions[:50], "truncated": len(actions) > 50},
                "closing_browser_cancels_task": False,
            }
