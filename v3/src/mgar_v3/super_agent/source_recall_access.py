"""Current server-side Recall principals, using only the original Engine transaction."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from psycopg.pq import TransactionStatus

from mgar_v3.domain import DomainError, digest
from mgar_v3.super_agent.source_coding_domain import CodingRunRequest
from mgar_v3.super_agent.source_recall_rules import denied

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.super_agent.source_correlation import _Input


@dataclass(frozen=True)
class RecallCaller:
    """Trusted Python port. HTTP only provides a credential, never these facts."""

    kind: Literal["owner", "worker"]
    credential: str = field(repr=False)
    worker: CodingRunRequest | None = None


@dataclass(frozen=True)
class RecallScope:
    caller_sha256: str
    actor: str
    expires_at: datetime
    source_instance_id: UUID | None = None
    conversation_key: str | None = None


class RecallAccess:
    def __init__(self, engine: Engine, conn: Connection, caller: RecallCaller):
        if (
            conn is not engine.connection
            or conn.info.transaction_status != TransactionStatus.INTRANS
        ):
            raise denied()
        self.engine, self.conn, self.caller = engine, conn, caller
        self.deadline = time.monotonic() + 5
        # Local to the existing short transaction, restored automatically at commit.
        conn.execute("SET LOCAL statement_timeout='1000ms'")
        conn.execute("SET LOCAL lock_timeout='250ms'")
        self.now = engine.source_ingress.now(conn)
        self.scope = self._authorize()

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise DomainError("SOURCE_RECALL_LIMIT", "Bounded Recall deadline exceeded", 429)

    def _authorize(self) -> RecallScope:
        engine, conn, caller = self.engine, self.conn, self.caller
        settings = engine.settings
        if caller.kind == "owner" and caller.worker is None:
            user = engine.source_correlations.identities._console(conn, caller.credential)
            return RecallScope(
                digest(
                    [
                        str(user),
                        hashlib.sha256(caller.credential.encode()).hexdigest(),
                        settings.project_id,
                        settings.repository.lower(),
                    ]
                ),
                settings.local_actor,
                self.now + timedelta(seconds=60),
            )
        if caller.kind != "worker" or not isinstance(caller.worker, CodingRunRequest):
            raise denied()
        request = caller.worker
        managed = conn.execute(
            "SELECT b.*,c.tree_sha256 FROM app.sa_async_bindings b "
            "LEFT JOIN app.sa_async_checkpoints c ON c.checkpoint_id=b.current_checkpoint_id "
            "WHERE b.coding_id=%s AND b.project_id=%s",
            (request.coding_id, settings.project_id),
        ).fetchone()
        if managed:
            tree = managed["tree_sha256"]
            if tree is None:
                _, original = engine.source_coding._mandate(conn, request.run_id)
                tree = original.repository.materialized_tree_sha256
            _, async_facts, mandate, facts = engine.source_async._current(
                conn, caller.credential, request, tree
            )
            expiry = async_facts.expires_at
            context = [
                async_facts.scope.scope_sha256,
                async_facts.input_revision,
                async_facts.restriction.model_dump(mode="json"),
            ]
        else:
            mandate, facts = engine.source_coding.current(conn, caller.credential, request)
            expiry = min(
                facts.lease_until,
                facts.session_expires_at,
                facts.configuration_expires_at,
                mandate.origin.expires_at,
            )
            context = [mandate.context_sha256]
        source = engine.source_correlations._input(conn, mandate.origin.input.input_id)
        conversation = source.item.event.source_thread.conversation_key
        # Authority is original current Source Coding, never a caller-supplied actor.
        scope = RecallScope(
            digest(
                [
                    request.model_dump(mode="json"),
                    mandate.mandate_sha256,
                    source.ref.state_sha256,
                    context,
                    hashlib.sha256(caller.credential.encode()).hexdigest(),
                ]
            ),
            mandate.origin.engine_actor,
            min(expiry, source.expires, self.now + timedelta(seconds=60)),
            source.ref.source_instance_id,
            conversation,
        )
        self.scope = scope
        self.source(mandate.origin.input.input_id)
        return scope

    def source(self, input_id: UUID) -> _Input:
        self.check()
        if self.caller.kind == "worker":
            # Filter the caller's installation/conversation before decrypting any
            # candidate body. The original _input subsequently rechecks version,
            # retention, Source and policy again on this same held connection.
            row = self.conn.execute(
                "SELECT * FROM app.source_ingress_inputs WHERE input_id=%s AND project_id=%s",
                (input_id, self.engine.settings.project_id),
            ).fetchone()
            if row is None or row["source_instance_id"] != self.scope.source_instance_id:
                raise denied()
            item, grant = self.engine.source_ingress._readable(self.conn, row)
            event = item.event
            if event.source_thread.conversation_key != self.scope.conversation_key:
                raise denied()
            roles, _ = self.engine.source_policies._roles(
                self.conn, event.sender, event.source_thread, None
            )
            if "context_provider" not in roles or not grant["model_input"]:
                raise denied()
        value = self.engine.source_correlations._input(self.conn, input_id)
        # Native GitHub references must belong to the sole configured repository.
        if any(
            r.resource.repository.lower() != self.engine.settings.repository.lower()
            for r in value.native
        ):
            raise denied()
        return value
