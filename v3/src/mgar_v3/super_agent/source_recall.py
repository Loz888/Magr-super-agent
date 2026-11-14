"""SA15 read-only, current-authority Recall on the original Engine.

No constructor IO, background work, model, listener or new business state. Raw CAS
IO is outside PG; every provenance hop and dependency is checked again afterwards.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_recall_access import RecallAccess, RecallCaller
from mgar_v3.super_agent.source_recall_cursor import RecallCursors
from mgar_v3.super_agent.source_recall_domain import (
    RECALL_VERSION,
    RecallCursor,
    RecallPageRequest,
    RecallPath,
    RecallResolveRequest,
    RecallSearchRequest,
    RecallWalkRequest,
)
from mgar_v3.super_agent.source_recall_rules import denied, utf8_page, walk
from mgar_v3.super_agent.source_recall_store import (
    MAX_SCAN,
    RecallStore,
    fingerprint,
    reference,
)

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine

Row = dict[str, Any]


class SourceRecall:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.cursors = RecallCursors(engine.settings.local_token.get_secret_value())

    def capabilities(self, cookie: str) -> Row:
        with self.engine.transaction() as conn:
            RecallAccess(self.engine, conn, RecallCaller("owner", cookie))
            return {
                "version": RECALL_VERSION,
                "read_only": True,
                "bodyless_metadata_default": True,
                "max_hops": 4,
                "max_nodes": 32,
                "max_edges": 128,
                "max_bytes": 16384,
                "page_bytes": [128, 4096],
                "max_candidates": MAX_SCAN,
                "index": "original_pg_relations_context_memory_cas",
                "migration": None,
                "search": "bounded_deterministic_lexical_with_explicit_time_range",
                "model_calls": 0,
                "paid": False,
                "external_write": False,
                "worker_cross_task": "same_current_source_installation_and_conversation",
                "live_semantic_quality": "NOT_RUN",
            }

    def resolve(self, caller: RecallCaller, request: RecallResolveRequest) -> Row:
        with self.engine.transaction() as conn:
            access = RecallAccess(self.engine, conn, caller)
            entry = RecallStore(access).path(request.path)
            return {
                "version": RECALL_VERSION,
                "node": entry.node.model_dump(mode="json"),
                "read_only": True,
            }

    def walk(self, caller: RecallCaller, request: RecallWalkRequest) -> Row:
        with self.engine.transaction() as conn:
            store = RecallStore(RecallAccess(self.engine, conn, caller))
            store.path(request.path)
            # BFS remembers each verified path so CAS and gaps have real parents.
            paths = {request.path.refs[-1].key: request.path}

            def resolve(ref: Any) -> Any:
                return store.path(paths[ref.key]).node

            def neighbours(ref: Any) -> Any:
                path = paths[ref.key]
                if len(path.refs) >= 5:
                    return ()
                try:
                    edges = store.neighbours(ref)
                except DomainError:
                    return ()
                for edge in edges:
                    if edge.target.key in paths or edge.target in path.refs:
                        continue
                    paths[edge.target.key] = RecallPath(refs=(*path.refs, edge.target))
                return tuple(edge for edge in edges if edge.target.key in paths)

            return walk(request, resolve, neighbours).model_dump(mode="json")

    def page(self, caller: RecallCaller, request: RecallPageRequest) -> Row:
        request_hash = fingerprint([request.path.model_dump(mode="json"), request.max_bytes])
        with self.engine.transaction() as conn:
            access = RecallAccess(self.engine, conn, caller)
            entry = RecallStore(access).path(request.path)
            if not entry.node.content_available:
                raise denied()
            caller_hash = access.scope.caller_sha256
            offset = 0
            expiry = entry.expires_at
            if request.cursor is not None:
                cursor = self.cursors.decode(request.cursor, caller_hash, now=access.now)
                if (
                    cursor.purpose != "page"
                    or cursor.request_sha256 != request_hash
                    or cursor.version_sha256 != entry.content_key
                    or cursor.after_id is not None
                ):
                    raise denied()
                offset, expiry = cursor.offset, min(expiry, cursor.expires_at)
            raw = entry.source_bytes
        # The sole content IO boundary. No SQL transaction is held here.
        if entry.cas_hash is not None:
            raw = self.engine.artifacts.read(entry.cas_hash)
        if raw is None:
            raise denied()
        text, end = utf8_page(raw, offset, request.max_bytes)
        content_hash = hashlib.sha256(raw).hexdigest()
        # Do not rely on a cached grant or on holding an in-memory mutex across IO.
        with self.engine.transaction() as conn:
            current = RecallAccess(self.engine, conn, caller)
            checked = RecallStore(current).path(request.path)
            if (
                current.scope.caller_sha256 != caller_hash
                or checked.content_key != entry.content_key
                or checked.cas_hash != entry.cas_hash
                or current.now >= expiry
                or (
                    checked.source_bytes is not None
                    and hashlib.sha256(checked.source_bytes).hexdigest() != content_hash
                )
            ):
                raise denied()
            next_cursor = None
            if end < len(raw):
                next_cursor = self.cursors.encode(
                    RecallCursor(
                        purpose="page",
                        caller_sha256=caller_hash,
                        request_sha256=request_hash,
                        version_sha256=checked.content_key,
                        offset=end,
                        expires_at=expiry,
                    )
                )
            return {
                "version": RECALL_VERSION,
                "ref": checked.node.ref.model_dump(mode="json"),
                "version_sha256": checked.node.version_sha256,
                "content_sha256": content_hash,
                "content": text,
                "offset": offset,
                "next_offset": end,
                "eof": end == len(raw),
                "cursor": next_cursor,
                "read_only": True,
                "grants_authority": False,
                "content_is_untrusted": True,
            }

    @staticmethod
    def _terms(query: str) -> tuple[str, ...]:
        # Bounded lexical matching, not an LLM interpretation or guessed Task ID.
        parts = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", query.casefold())
        ignored = {"昨天", "今天", "问题", "怎样", "怎样了", "怎么样", "了", "the", "status"}
        terms = []
        for part in parts:
            for noise in ("昨天的", "今天的", "问题怎样了", "问题怎么样了", "怎么样了", "怎样了"):
                part = part.replace(noise, "")
            if part and part not in ignored:
                terms.append(part)
        return tuple(terms[:16])

    def search(self, caller: RecallCaller, request: RecallSearchRequest) -> Row:
        with self.engine.transaction() as conn:
            access = RecallAccess(self.engine, conn, caller)
            store = RecallStore(access)
            after = request.after or access.now - timedelta(days=7)
            before = request.before or access.now
            query_hash = fingerprint(request.model_dump(mode="json", exclude={"cursor"}))
            # Bind a continuation to its original time window, even with default dates.
            search_version = fingerprint([RECALL_VERSION, request.after, request.before])
            after_id = UUID(int=0)
            expiry = access.scope.expires_at
            if request.cursor:
                cursor = self.cursors.decode(
                    request.cursor, access.scope.caller_sha256, now=access.now
                )
                if (
                    cursor.purpose != "search"
                    or cursor.request_sha256 != query_hash
                    or cursor.version_sha256 != search_version
                    or cursor.after_id is None
                ):
                    raise denied()
                after_id, expiry = cursor.after_id, min(expiry, cursor.expires_at)
            installation = request.source_instance_id or access.scope.source_instance_id
            if access.caller.kind == "worker" and installation != access.scope.source_instance_id:
                raise denied()
            if (
                access.caller.kind == "worker"
                and request.conversation_key is not None
                and request.conversation_key != access.scope.conversation_key
            ):
                raise denied()
            rows = conn.execute(
                "SELECT input_id FROM app.source_ingress_inputs WHERE project_id=%s "
                "AND input_id>%s AND received_at>=%s AND received_at<%s "
                "AND (%s::uuid IS NULL OR source_instance_id=%s) "
                "AND unavailable_at IS NULL AND metadata_expires_at>clock_timestamp() "
                "ORDER BY input_id LIMIT 65",
                (store.project, after_id, after, before, installation, installation),
            ).fetchall()
            results = []
            paths = []
            scanned_id = None
            more = len(rows) > MAX_SCAN
            terms = self._terms(request.query)
            for row in rows[:MAX_SCAN]:
                access.check()
                scanned_id = row["input_id"]
                try:
                    source = access.source(row["input_id"])
                    if (
                        request.conversation_key is not None
                        and source.item.event.source_thread.conversation_key
                        != request.conversation_key
                    ):
                        continue
                    if not terms or not all(t in source.text.casefold() for t in terms):
                        continue
                    root = reference("source", row["input_id"])
                    path = RecallPath(refs=(root,))
                    source_entry = store.path(path)
                    tasks = []
                    for target in store._source_runs(row["input_id"]):
                        try:
                            task_path = RecallPath(refs=(root, reference("run", target["run_id"])))
                            entry = store.path(task_path)
                        except DomainError:
                            continue
                        tasks.append(
                            {
                                "path": task_path.model_dump(mode="json"),
                                "node": entry.node.model_dump(mode="json"),
                            }
                        )
                    results.append(
                        {
                            "path": path.model_dump(mode="json"),
                            "node": source_entry.node.model_dump(mode="json"),
                            "tasks": tasks[:4],
                        }
                    )
                    paths.append(path)
                except DomainError:
                    continue
                if len(results) >= request.limit:
                    more = scanned_id != rows[-1]["input_id"] or more
                    break
            # Recheck each selected input immediately before producing the response.
            for path in paths:
                store.path(path)
            next_cursor = None
            if more and scanned_id:
                next_cursor = self.cursors.encode(
                    RecallCursor(
                        purpose="search",
                        caller_sha256=access.scope.caller_sha256,
                        request_sha256=query_hash,
                        version_sha256=search_version,
                        after_id=scanned_id,
                        offset=0,
                        expires_at=expiry,
                    )
                )
            clarification = len(results) != 1 or more or not results[0]["tasks"]
            return {
                "version": RECALL_VERSION,
                "results": results,
                "cursor": next_cursor,
                "truncated": more,
                "clarification_required": clarification,
                "clarification": (
                    "请提供任务 ID、来源会话或更精确的时间与关键词。" if clarification else None
                ),
                "retrieval": "bounded_deterministic_lexical",
                "model_calls": 0,
                "read_only": True,
                "grants_authority": False,
            }
