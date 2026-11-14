"""A bounded virtual index over existing PG identities, ContextEdge, Memory and CAS.

No relations are persisted here. A key, edge, title or summary never grants access.
The store never performs filesystem IO or opens a database connection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from mgar_v3.domain import DomainError, digest
from mgar_v3.memory.store import MemoryStore
from mgar_v3.super_agent.source_recall_access import RecallAccess
from mgar_v3.super_agent.source_recall_domain import (
    NodeKind,
    RecallEdge,
    RecallFact,
    RecallNode,
    RecallPath,
    RecallRef,
)
from mgar_v3.super_agent.source_recall_rules import denied

Row = dict[str, Any]
MAX_SCAN = 64


def fingerprint(value: Any) -> str:
    return digest(json.loads(json.dumps(value, default=str)))


def reference(kind: NodeKind, object_id: Any) -> RecallRef:
    return RecallRef(kind=kind, id=str(object_id))


@dataclass(frozen=True)
class RecallEntry:
    node: RecallNode
    expires_at: datetime
    cas_hash: str | None = None
    source_bytes: bytes | None = None

    @property
    def content_key(self) -> str:
        return fingerprint([self.node.model_dump(mode="json"), self.cas_hash])


class RecallStore:
    def __init__(self, access: RecallAccess):
        self.access = access
        self.conn, self.engine = access.conn, access.engine
        self.project = self.engine.settings.project_id
        self.repository = self.engine.settings.repository.lower()

    def _one(self, sql: str, parameters: tuple[Any, ...]) -> Row:
        self.access.check()
        row = self.conn.execute(sql, parameters).fetchone()
        if row is None:
            raise denied()
        return row

    def _rows(self, sql: str, parameters: tuple[Any, ...]) -> list[Row]:
        self.access.check()
        rows = self.conn.execute(sql, parameters).fetchmany(MAX_SCAN + 1)
        if len(rows) > MAX_SCAN:
            raise DomainError("SOURCE_RECALL_LIMIT", "Relation scan exceeds bounded scope", 429)
        return rows

    def _entry(
        self,
        ref: RecallRef,
        state: str,
        version: Any,
        *,
        facts: Row | None = None,
        availability: str = "available",
        cas_hash: str | None = None,
        source_bytes: bytes | None = None,
        expires_at: datetime | None = None,
    ) -> RecallEntry:
        return RecallEntry(
            RecallNode(
                ref=ref,
                state=state[:64],
                version_sha256=fingerprint(version),
                availability=cast(Any, availability),
                content_available=cas_hash is not None or source_bytes is not None,
                facts=tuple(
                    RecallFact(name=k, value=str(v)[:256])
                    for k, v in (facts or {}).items()
                    if v is not None
                ),
            ),
            min(self.access.scope.expires_at, expires_at or self.access.scope.expires_at),
            cas_hash,
            source_bytes,
        )

    def source(self, ref: RecallRef) -> RecallEntry:
        row = self._one(
            "SELECT * FROM app.source_ingress_inputs WHERE input_id=%s AND project_id=%s",
            (UUID(ref.id), self.project),
        )
        # A current owner may see only a known local tombstone, never its old text.
        if (
            row["unavailable_at"] is not None
            or row["metadata"] is None
            or row["metadata_expires_at"] <= self.access.now
        ):
            if self.access.caller.kind != "owner":
                raise denied()
            return self._entry(
                ref,
                "unavailable",
                [ref.id, row["unavailable_at"], row["reason_code"], row["metadata_expires_at"]],
                facts={"reason": row["reason_code"]},
                availability="unavailable",
            )
        try:
            value = self.access.source(UUID(ref.id))
        except DomainError as exc:
            if self.access.caller.kind != "owner" or exc.code not in {
                "SOURCE_CORRELATION_CONTENT_UNAVAILABLE",
                "SOURCE_CORRELATION_INPUT_EXPIRED",
            }:
                raise
            return self._entry(
                ref,
                "unavailable",
                [ref.id, row["body_sha256"], row["metadata_expires_at"], "body_retention_expired"],
                facts={"reason": "BODY_RETENTION_EXPIRED"},
                availability="unavailable",
            )
        return self._entry(
            ref,
            "current",
            value.ref.model_dump(mode="json"),
            facts={
                "source_instance_id": value.ref.source_instance_id,
                "conversation_key": value.item.event.source_thread.conversation_key,
                "received_at": value.row["received_at"],
                "context_only": value.row["context_only"],
            },
            source_bytes=value.clear,
            expires_at=value.expires,
        )

    def _sources(self, run_id: UUID) -> tuple[UUID, ...]:
        """All retained contributing origins, including prior correlation versions.

        Removed links cannot launder already-derived output. Subscription deliveries
        are conservatively dependencies even before their consumption is confirmed.
        """
        ids = {
            row["input_id"]
            for row in self._rows(
                "SELECT input_id FROM app.sa_coding_bindings WHERE run_id=%s AND project_id=%s "
                "UNION SELECT input_id FROM app.sa_source_subscription_deliveries "
                "WHERE run_id=%s AND project_id=%s "
                "UNION SELECT input_id FROM app.sa_scheduler_queue "
                "WHERE run_id=%s AND project_id=%s AND input_id IS NOT NULL "
                "UNION SELECT i.source_input_id AS input_id FROM app.sa_async_inputs i "
                "JOIN app.sa_async_bindings b USING(async_id,project_id) WHERE b.run_id=%s "
                "AND b.project_id=%s AND i.source_input_id IS NOT NULL "
                "AND i.state='applied' LIMIT 65",
                (run_id, self.project) * 4,
            )
        }
        correlations = self._rows(
            "SELECT payload FROM app.sa_task_correlation_versions "
            "WHERE run_id=%s AND project_id=%s ORDER BY correlation_id,revision LIMIT 65",
            (run_id, self.project),
        )
        for row in correlations:
            for anchor in row["payload"]["anchors"]:
                ids.add(UUID(anchor["input_id"]))
        if len(ids) > MAX_SCAN:
            raise denied()
        return tuple(sorted(ids, key=str))

    def _evidence_origin(self, evidence_id: UUID) -> str:
        row = self._one(
            "SELECT e.*,r.origin FROM app.evidence e LEFT JOIN app.r4_reads r "
            "USING(evidence_id) WHERE e.evidence_id=%s AND e.project_id=%s",
            (evidence_id, self.project),
        )
        origin = row["origin"]
        if not origin:
            return fingerprint(row)
        # Reuse original Knowledge membership and current document ACL. No CAS IO.
        if origin["kind"] != "knowledge":
            raise denied()
        actor = self.access.scope.actor
        if actor not in self.engine.settings.allowed_actors:
            raise denied()
        current = self._one(
            "SELECT c.*,v.raw_hash,v.source_version,v.document_id,d.source_uri,d.acl_revision "
            "FROM knowledge.snapshot_chunks m JOIN knowledge.snapshots s USING(snapshot_id) "
            "JOIN knowledge.chunks c USING(chunk_id) JOIN knowledge.document_versions v "
            "USING(version_id) JOIN knowledge.documents d USING(document_id) "
            "WHERE m.snapshot_id=%s AND c.chunk_id=%s AND s.project_id=%s "
            "AND s.repository=%s AND s.actor=%s AND s.status='ready' "
            "AND d.project_id=%s AND d.repository=%s AND d.status='active' "
            "AND (d.owner=%s OR %s=ANY(d.readers))",
            (
                UUID(origin["snapshot_id"]),
                UUID(origin["chunk_id"]),
                self.project,
                self.repository,
                actor,
                self.project,
                self.repository,
                actor,
                actor,
            ),
        )
        if (
            current["content_hash"] != row["content_hash"]
            or current["source_version"] != row["source_version"]
            or current["source_uri"] != row["source_uri"]
            or current["raw_hash"] != origin["raw_hash"]
            or current["locator"] != origin["locator"]
            or str(current["document_id"]) != origin["document_id"]
            or str(current["version_id"]) != origin["version_id"]
        ):
            raise denied()
        return fingerprint([row, current, sorted(self.engine.settings.allowed_actors)])

    def _run(self, run_id: UUID, *, content: bool = False) -> tuple[Row, list[str]]:
        row = self._one(
            "SELECT r.*,t.thread_id,t.mandate_id,t.status AS ticket_status,h.project_id,"
            "h.repository,h.revision AS thread_revision,m.actor FROM app.runs r "
            "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
            "JOIN app.mandates m USING(mandate_id) WHERE r.run_id=%s AND h.project_id=%s "
            "AND lower(h.repository)=%s AND m.project_id=h.project_id",
            (run_id, self.project, self.repository),
        )
        sources = self._sources(run_id)
        versions = []
        if content:
            for evidence in self._rows(
                "SELECT evidence_id FROM app.evidence WHERE run_id=%s "
                "ORDER BY evidence_id LIMIT 65",
                (run_id,),
            ):
                versions.append(self._evidence_origin(evidence["evidence_id"]))
        if not sources:
            # Source principals are intentionally absent from legacy allowed_actors.
            if (
                self.access.caller.kind != "owner"
                or row["actor"] != self.access.scope.actor
                or row["actor"] not in self.engine.settings.allowed_actors
                or row["graph_version"] == "source-coding-v1"
            ):
                raise denied()
            return row, [*versions, fingerprint([row["actor"], row["mandate_id"]])]
        for source_id in sources:
            try:
                if content:
                    value = self.access.source(source_id)
                    versions.append(value.ref.state_sha256)
                else:
                    metadata, _, grant = self.access.metadata(source_id)
                    versions.append(fingerprint([metadata, grant]))
            except DomainError:
                if content:
                    raise denied() from None
                continue
        if not sources or not any(self._source_visible(sid) for sid in sources):
            raise denied()
        return row, versions

    def _source_visible(self, source_id: UUID) -> bool:
        try:
            self.access.metadata(source_id)
            return True
        except DomainError:
            return False

    def _runs_for(self, ref: RecallRef) -> list[Row]:
        if ref.kind == "ticket":
            return self._rows(
                "SELECT run_id FROM app.runs WHERE ticket_id=%s ORDER BY attempt_no DESC LIMIT 65",
                (UUID(ref.id),),
            )
        return self._rows(
            "SELECT r.run_id FROM app.runs r JOIN app.tickets t USING(ticket_id) "
            "WHERE t.thread_id=%s ORDER BY r.created_at DESC,r.run_id LIMIT 65",
            (UUID(ref.id),),
        )

    def _task(self, ref: RecallRef, *, content: bool = False) -> RecallEntry:
        if ref.kind == "run":
            row, versions = self._run(UUID(ref.id), content=content)
            # Question, free-form outcome and reports are never metadata fields.
            return self._entry(
                ref,
                row["status"],
                [row, versions],
                facts={
                    "ticket_id": row["ticket_id"],
                    "thread_id": row["thread_id"],
                    "execution_epoch": row["execution_epoch"],
                    "ticket_state": row["ticket_status"],
                },
            )
        visible = []
        for run in self._runs_for(ref):
            try:
                value, versions = self._run(run["run_id"], content=content)
            except DomainError:
                continue
            visible.append((value, versions))
        if not visible:
            raise denied()
        state = visible[0][0]["ticket_status"] if ref.kind == "ticket" else "linked"
        return self._entry(
            ref,
            state,
            [[r["run_id"], r["status"], v] for r, v in visible],
            facts={"visible_attempts": len(visible)},
        )

    def _memory(self, ref: RecallRef) -> RecallEntry:
        rows = self.conn.execute(
            "SELECT to_jsonb(m) AS record FROM knowledge.memory_records m "
            "WHERE memory_id=%s UNION ALL SELECT to_jsonb(m) AS record "
            "FROM knowledge.memories m WHERE memory_id=%s LIMIT 2",
            (UUID(ref.id), UUID(ref.id)),
        ).fetchall()
        if not rows:
            raise denied()
        row = rows[0]["record"]
        if "memory_kind" in row:
            original = self._one(
                "SELECT * FROM knowledge.memory_records WHERE memory_id=%s",
                (UUID(row["memory_id"]),),
            )
            # Reuse the original kind/detail decoder on this held connection only.
            MemoryStore(self.engine.settings)._record(self.conn, original)
        if (
            row["project_id"] != self.project
            or row["repository"].lower() != self.repository
            or row.get("revoked_at") is not None
            or row.get("status", "active") != "active"
        ):
            raise denied()
        versions = []
        authorized_actor = self.access.scope.actor
        if row.get("run_id"):
            parent_run, current = self._run(UUID(row["run_id"]), content=True)
            authorized_actor = parent_run["actor"]
            versions.extend(current)
        else:
            detail = self.conn.execute(
                "SELECT run_id,expires_at FROM knowledge.memory_working WHERE memory_id=%s "
                "UNION ALL SELECT run_id,NULL FROM knowledge.memory_episodic WHERE memory_id=%s",
                (UUID(ref.id), UUID(ref.id)),
            ).fetchone()
            if detail:
                if detail["expires_at"] is not None and detail["expires_at"] <= self.access.now:
                    raise denied()
                parent_run, current = self._run(detail["run_id"], content=True)
                authorized_actor = parent_run["actor"]
                versions.extend(current)
        evidence_ids = row["evidence_ids"]
        if len(evidence_ids) > MAX_SCAN:
            raise denied()
        for eid in evidence_ids:
            evidence = self._one(
                "SELECT run_id,project_id FROM app.evidence WHERE evidence_id=%s", (UUID(eid),)
            )
            if evidence["project_id"] != self.project:
                raise denied()
            _, current = self._run(evidence["run_id"], content=True)
            versions.extend(current)
            versions.append(self._evidence_origin(UUID(eid)))
        if row["actor"] != authorized_actor:
            raise denied()
        if self.access.caller.kind == "worker" and not versions:
            raise denied()
        # A semantic/procedural summary without evidence is only legacy owner data.
        return self._entry(
            ref,
            "untrusted_summary",
            [row, versions],
            facts={
                "verification": row.get("verification_status", "confirmed"),
                "authority": "none",
            },
            cas_hash=row["summary_hash"],
        )

    def resolve(self, ref: RecallRef, parent: RecallRef | None = None) -> RecallEntry:
        self.access.check()
        if ref.kind == "source":
            return self.source(ref)
        if ref.kind in {"thread", "ticket", "run"}:
            return self._task(ref)
        if ref.kind == "memory":
            return self._memory(ref)
        if ref.kind == "gap":
            row = self._one(
                "SELECT g.* FROM app.source_ingress_gaps g JOIN app.source_instances s "
                "USING(source_instance_id) WHERE gap_id=%s AND s.project_id=%s",
                (UUID(ref.id), self.project),
            )
            if self.access.caller.kind != "owner" or parent is None or parent.kind != "source":
                raise denied()
            source = self.access.source(UUID(parent.id))
            if source.ref.source_instance_id != row["source_instance_id"]:
                raise denied()
            return self._entry(
                ref,
                "resolved" if row["resolved_at"] else "gap",
                row,
                facts={"uncertainty": "history_not_proven_complete"},
            )
        if ref.kind == "artifact":
            if parent is None or ref not in [e.target for e in self.neighbours(parent)]:
                raise denied()
            content_version = self.content_authority(parent)
            original = self.resolve(parent)
            row = self._one(
                "SELECT content_hash,byte_count FROM app.artifacts WHERE content_hash=%s", (ref.id,)
            )
            return self._entry(
                ref,
                "untrusted_content",
                [row, original.content_key, content_version],
                cas_hash=ref.id,
                expires_at=original.expires_at,
            )
        table, id_column = {
            "evidence": ("app.evidence", "evidence_id"),
            "job": ("app.sa_async_jobs", "job_id"),
            "review": ("app.actions", "action_id"),
            "delivery": ("app.outbox", "outbox_id"),
        }[ref.kind]
        if ref.kind == "delivery":
            row = self._one(
                "SELECT o.*,a.run_id FROM app.outbox o JOIN app.actions a USING(action_id) "
                "WHERE o.outbox_id=%s",
                (UUID(ref.id),),
            )
        else:
            # Identifiers are constants selected above, never caller SQL.
            row = self._one(f"SELECT * FROM {table} WHERE {id_column}=%s", (UUID(ref.id),))
        run, versions = self._run(row["run_id"])
        if row.get("project_id", self.project) != self.project:
            raise denied()
        if ref.kind == "evidence":
            origin_version = self._evidence_origin(UUID(ref.id))
            return self._entry(
                ref,
                "reference",
                [row, versions, origin_version],
                facts={
                    "run_id": run["run_id"],
                    "start_line": row["start_line"],
                    "end_line": row["end_line"],
                },
            )
        state = str(row.get("state") or row.get("status") or "unknown")
        return self._entry(
            ref,
            state,
            [row, versions],
            facts={
                "run_id": run["run_id"],
                "result_code": row.get("result_code"),
                "result_known": state in {"known", "succeeded", "failed"},
            },
        )

    def content_authority(self, ref: RecallRef) -> str:
        """Derived bytes need every contributing origin, not one public edge."""
        if ref.kind == "memory":
            return self._memory(ref).content_key
        if ref.kind == "run":
            row, versions = self._run(UUID(ref.id), content=True)
            return fingerprint([row, versions])
        if ref.kind == "evidence":
            row = self._one("SELECT run_id FROM app.evidence WHERE evidence_id=%s", (UUID(ref.id),))
            run, versions = self._run(row["run_id"], content=True)
            return fingerprint([run, versions, self._evidence_origin(UUID(ref.id))])
        raise denied()

    def _source_runs(self, input_id: UUID) -> list[Row]:
        return self._rows(
            "SELECT run_id FROM app.sa_coding_bindings WHERE input_id=%s AND project_id=%s "
            "UNION SELECT l.run_id FROM app.sa_task_correlation_versions l "
            "JOIN app.sa_task_correlations h USING(correlation_id,project_id,revision) "
            "JOIN app.sa_thread_link_inputs i USING(correlation_id,project_id,revision) "
            "WHERE i.input_id=%s AND l.project_id=%s AND l.payload->>'state'='bound' "
            "UNION SELECT run_id FROM app.sa_source_subscription_deliveries "
            "WHERE input_id=%s AND project_id=%s LIMIT 65",
            (input_id, self.project) * 3,
        )

    def neighbours(self, ref: RecallRef) -> tuple[RecallEdge, ...]:
        """Untrusted candidate edges; callers must authorize each target before output."""
        self.access.check()
        candidates: list[tuple[RecallRef, str]] = []
        if ref.kind == "source":
            value = self.access.source(UUID(ref.id))
            for row in self._source_runs(UUID(ref.id)):
                candidates.append((reference("run", row["run_id"]), "origin"))
                run, _ = self._run(row["run_id"])
                candidates.append((reference("thread", run["thread_id"]), "origin"))
            if self.access.caller.kind == "owner":
                for row in self._rows(
                    "SELECT gap_id FROM app.source_ingress_gaps WHERE source_instance_id=%s "
                    "ORDER BY observed_at DESC,gap_id LIMIT 65",
                    (value.ref.source_instance_id,),
                ):
                    candidates.append((reference("gap", row["gap_id"]), "gap"))
        elif ref.kind in {"thread", "ticket"}:
            self._task(ref)
            for row in self._runs_for(ref):
                run, _ = self._run(row["run_id"])
                target = (
                    reference("ticket", run["ticket_id"])
                    if ref.kind == "thread"
                    else reference("run", run["run_id"])
                )
                candidates.append((target, "contains" if ref.kind == "thread" else "attempt"))
        elif ref.kind == "run":
            row, _ = self._run(UUID(ref.id))
            candidates.extend(
                (reference("source", sid), "origin") for sid in self._sources(row["run_id"])
            )
            candidates.append((reference("ticket", row["ticket_id"]), "contains"))
            for kind, sql, column, relation in (
                (
                    "evidence",
                    "SELECT evidence_id FROM app.evidence WHERE run_id=%s "
                    "ORDER BY evidence_id LIMIT 65",
                    "evidence_id",
                    "evidence",
                ),
                (
                    "job",
                    "SELECT job_id FROM app.sa_async_jobs WHERE run_id=%s ORDER BY job_id LIMIT 65",
                    "job_id",
                    "resource",
                ),
                (
                    "review",
                    "SELECT action_id FROM app.actions WHERE run_id=%s ORDER BY action_id LIMIT 65",
                    "action_id",
                    "review",
                ),
                (
                    "delivery",
                    "SELECT o.outbox_id FROM app.outbox o JOIN app.actions a "
                    "USING(action_id) WHERE a.run_id=%s ORDER BY outbox_id LIMIT 65",
                    "outbox_id",
                    "delivery",
                ),
                (
                    "memory",
                    "SELECT memory_id FROM knowledge.memories WHERE run_id=%s "
                    "ORDER BY memory_id LIMIT 65",
                    "memory_id",
                    "memory",
                ),
            ):
                candidates.extend(
                    (reference(cast(NodeKind, kind), r[column]), relation)
                    for r in self._rows(sql, (row["run_id"],))
                )
            if row["report_hash"]:
                candidates.append((reference("artifact", row["report_hash"]), "artifact"))
        elif ref.kind == "evidence":
            row = self._one("SELECT * FROM app.evidence WHERE evidence_id=%s", (UUID(ref.id),))
            self._run(row["run_id"])
            candidates.append((reference("artifact", row["content_hash"]), "artifact"))
        elif ref.kind == "memory":
            memory = self._memory(ref)
            if memory.cas_hash:
                candidates.append((reference("artifact", memory.cas_hash), "artifact"))
        if ref.kind in {"ticket", "run", "evidence", "memory"}:
            edge_actor = self.access.scope.actor
            if ref.kind == "run":
                parent_run, _ = self._run(UUID(ref.id))
                edge_actor = parent_run["actor"]
            elif ref.kind == "evidence":
                original = self._one(
                    "SELECT run_id FROM app.evidence WHERE evidence_id=%s", (UUID(ref.id),)
                )
                parent_run, _ = self._run(original["run_id"])
                edge_actor = parent_run["actor"]
            elif ref.kind == "ticket":
                self._task(ref)
                original = self._one(
                    "SELECT m.actor FROM app.tickets t JOIN app.mandates m "
                    "USING(mandate_id) WHERE t.ticket_id=%s",
                    (UUID(ref.id),),
                )
                edge_actor = original["actor"]
            else:
                self._memory(ref)
                original = self._one(
                    "SELECT actor FROM knowledge.memory_records "
                    "WHERE memory_id=%s UNION ALL SELECT actor FROM "
                    "knowledge.memories WHERE memory_id=%s LIMIT 1",
                    (UUID(ref.id), UUID(ref.id)),
                )
                edge_actor = original["actor"]
            for row in self._rows(
                "SELECT target_kind,target_id FROM knowledge.context_edges WHERE project_id=%s "
                "AND lower(repository)=%s AND actor=%s AND source_kind=%s AND source_id=%s "
                "ORDER BY edge_id LIMIT 65",
                (self.project, self.repository, edge_actor, ref.kind, UUID(ref.id)),
            ):
                if row["target_kind"] in {"ticket", "run", "evidence", "memory"}:
                    candidates.append((reference(row["target_kind"], row["target_id"]), "context"))
        # Small finite candidate set. Private IDs never leave without target resolution.
        unique = {(target.key, relation): (target, relation) for target, relation in candidates}
        return tuple(
            RecallEdge(source=ref, target=target, relation=cast(Any, relation))
            for target, relation in list(unique.values())[:129]
        )

    def path(self, path: RecallPath) -> RecallEntry:
        last = None
        versions = []
        for ref in path.refs:
            if last is not None and ref not in [e.target for e in self.neighbours(last)]:
                raise denied()
            value = self.resolve(ref, last)
            versions.append(value.content_key)
            last = ref
        # Bind all provenance hops, not just a globally shared content hash.
        node = value.node.model_copy(update={"version_sha256": fingerprint(versions)})
        return RecallEntry(node, value.expires_at, value.cas_hash, value.source_bytes)
