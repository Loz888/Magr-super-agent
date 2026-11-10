"""Trusted transport composition: verify outside SQL, Engine commit, then transport ACK."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import ActorRef
from mgar_v3.sources.ingress import SourceIngress
from mgar_v3.sources.ingress_crypto import EncryptedSpool
from mgar_v3.sources.ingress_domain import (
    GapDeclaration,
    HistoryPage,
    HistoryProvider,
    HistoryRequest,
    IngressReceipt,
    IngressVerifier,
)
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.super_agent.policy_domain import PolicyDefinition


class SourceGateway:
    def __init__(
        self,
        ingress: SourceIngress,
        instance_id: UUID,
        generation: int,
        verifiers: Mapping[tuple[str, str], IngressVerifier] | None = None,
        *,
        ack_mode: Literal["manual", "after_handler", "before_handler"] = "manual",
        spool: EncryptedSpool | None = None,
        checkpoint: Callable[[str], None] | None = None,
    ):
        self.ingress, self.instance_id, self.generation = ingress, instance_id, generation
        self.verifiers, self.ack_mode, self.spool = dict(verifiers or {}), ack_mode, spool
        self.checkpoint = checkpoint

    def validate_transport(self) -> None:
        if self.ack_mode not in {"manual", "after_handler"}:
            raise DomainError(
                "INGRESS_EARLY_SDK_ACK_UNSAFE",
                "This SDK ACKs before the durable handler; do not start it",
                409,
            )

    def _gap(
        self,
        reason: Literal["no_replay", "permission", "buffer_expired", "capacity", "sdk_early_ack"],
    ) -> None:
        self.ingress.gap(
            self.instance_id,
            GapDeclaration(
                stream_id="transport", epoch="gateway", reason=reason, started_at=datetime.now(UTC)
            ),
        )

    def receive(
        self,
        envelope: TransportEnvelope,
        acknowledge: Callable[[IngressReceipt], None] | None = None,
    ) -> IngressReceipt:
        self.validate_transport()
        if len(envelope.payload) > 262144:
            raise DomainError("INGRESS_ENVELOPE_BOUND", "Raw source envelope exceeds 256 KiB", 413)
        permit = None
        try:
            instance, _ = self.ingress.snapshot(self.instance_id, self.generation)
            verifier = self.verifiers.get((instance.scope.source_kind, instance.adapter_version))
            if verifier is None:
                raise DomainError(
                    "INGRESS_VERIFIER_MISSING", "No trusted ingress verifier is installed", 503
                )
            try:
                delivery = verifier.verify_ingress(instance, envelope)
                if delivery.metadata.payload_sha256 != hashlib.sha256(envelope.payload).hexdigest():
                    raise ValueError
                self.ingress.validate(instance, delivery)
            except (ValueError, TypeError, AttributeError):
                raise DomainError(
                    "INGRESS_INVALID_VERIFIED_DELIVERY",
                    "Verifier returned invalid or unbound source data",
                    403,
                ) from None
            if self.spool is not None:
                permit = self.ingress.buffer_permit(instance, self.generation, delivery)
            receipt = self.ingress.submit(
                instance, self.generation, delivery, checkpoint=self.checkpoint
            )
        except psycopg.Error:
            if permit is not None and self.spool is not None:
                self.spool.put(permit)
                raise DomainError(
                    "INGRESS_BUFFERED_NOT_ACKED",
                    "Approved transport data buffered for Engine retry; no ACK",
                    503,
                ) from None
            raise DomainError(
                "INGRESS_DATABASE_UNAVAILABLE",
                "Durable receipt is unconfirmed; no ACK and no content buffered",
                503,
            ) from None
        if not receipt.committed or receipt.disposition == "backpressure":
            self._gap("capacity")
            raise DomainError(receipt.reason_code, "Ingress capacity reached; no durable ACK", 503)
        if receipt.disposition == "conflict":
            raise DomainError(receipt.reason_code, "Conflicting delivery was recorded; no ACK", 409)
        if self.checkpoint:
            self.checkpoint("before_ack")
        if self.ingress.engine.connection.info.transaction_status != TransactionStatus.IDLE:
            raise DomainError(
                "INGRESS_ACK_BEFORE_COMMIT", "Cannot ACK while the Engine transaction is open", 500
            )
        if acknowledge is not None:
            acknowledge(receipt)  # Transport only, never a notification or business publication.
        return receipt

    def drain_buffer(self, *, limit: int = 100) -> list[dict[str, str]]:
        if not 1 <= limit <= 100 or self.spool is None:
            raise DomainError(
                "INGRESS_BUFFER_REPLAY_BOUND",
                "Configure a buffer and a replay limit of 1..100",
                422,
            )
        results = []
        for identifier in self.spool.ids()[:limit]:
            permit = self.spool.read(identifier)
            if permit.delivery.metadata.source_instance_id != self.instance_id:
                raise DomainError(
                    "INGRESS_BUFFER_SOURCE", "Buffer record belongs to another source", 403
                )
            try:
                instance, _ = self.ingress.snapshot(self.instance_id, self.generation)
                if permit.expires_at <= datetime.now(UTC):
                    raise DomainError(
                        "INGRESS_BUFFER_AUTHORITY_STALE", "Buffered authority expired", 403
                    )
                receipt = self.ingress.submit(
                    instance, self.generation, permit.delivery, permit=permit, historical=True
                )
                if receipt.disposition in {"conflict", "backpressure"}:
                    results.append({"buffer_id": identifier, "status": receipt.disposition})
                    continue
                self.spool.remove(identifier)
                results.append({"buffer_id": identifier, "status": "engine_committed"})
            except DomainError as error:
                if error.code in {
                    "INGRESS_BUFFER_AUTHORITY_STALE",
                    "INGRESS_OWNER_FENCED",
                    "INGRESS_VERIFICATION_SCOPE",
                }:
                    self.spool.remove(identifier)
                    self._gap(
                        "buffer_expired" if permit.expires_at <= datetime.now(UTC) else "permission"
                    )
                    results.append({"buffer_id": identifier, "status": "discarded_stale_authority"})
                else:
                    raise
        return results

    def history(self, provider: HistoryProvider, request: HistoryRequest) -> HistoryPage:
        """Bounded provider read after current Receive preflight; all replay is context-only."""
        instance, settings = self.ingress.snapshot(self.instance_id, self.generation)
        if instance.capabilities.history_read != "supported":
            self._gap("no_replay")
            raise DomainError(
                "INGRESS_HISTORY_UNAVAILABLE",
                "History/replay is unavailable; gap remains visible",
                409,
            )
        assert settings.policy_id is not None
        with self.ingress.root_transaction() as conn:
            policies = self.ingress.engine.source_policies
            row = policies._current(conn, settings.policy_id)
            definition = PolicyDefinition.model_validate(row["definition"])
            policies._validate_source(conn, definition)
            selector, thread = definition.selector, request.source_thread
            at = self.ingress.now(conn)
            if (
                row["revoked"]
                or row["current_revision"] != settings.policy_revision
                or not definition.valid_from <= at < definition.valid_until
                or not definition.receive.enabled
                or not definition.receive.allow_history_context
                or thread.scope != instance.scope
                or thread.scope != selector.scope
                or thread.conversation_id not in selector.conversations
                or (
                    selector.native_threads
                    and thread.native_thread_id not in selector.native_threads
                )
            ):
                raise DomainError(
                    "INGRESS_HISTORY_RECEIVE_DENIED",
                    "History request is outside current Receive scope",
                    403,
                )
            active = False
            for actor in selector.actors:
                identity = conn.execute(
                    "SELECT actor_ref FROM app.source_identities WHERE identity_key=%s AND "
                    "project_id=%s AND revoked_at IS NULL",
                    (actor.key, self.ingress.project),
                ).fetchone()
                roles, _ = policies._roles(conn, actor, thread, None)
                if (
                    identity
                    and ActorRef.model_validate(identity["actor_ref"]) == actor
                    and "context_provider" in roles
                ):
                    active = True
            if not active:
                raise DomainError(
                    "INGRESS_HISTORY_IDENTITY_DENIED",
                    "No current scoped context identity permits this read",
                    403,
                )
            count = conn.execute(
                "SELECT count(*) AS n FROM app.source_ingress_inputs WHERE "
                "source_instance_id=%s AND consumed_at IS NULL AND unavailable_at IS NULL",
                (self.instance_id,),
            ).fetchone()
            assert count is not None
            if count["n"] + request.limit > settings.max_pending:
                raise DomainError(
                    "INGRESS_HISTORY_BACKPRESSURE",
                    "History request exceeds current backlog headroom",
                    503,
                )
        page = provider.fetch(instance, request)  # Outside the Engine transaction.
        if (
            len(page.deliveries) > request.limit
            or sum(len(d.metadata.items) for d in page.deliveries) > request.limit
        ):
            raise DomainError(
                "INGRESS_HISTORY_BOUND", "History provider exceeded the explicit page limit", 413
            )
        for delivery in page.deliveries:
            for item in delivery.metadata.items:
                thread = item.event.source_thread
                if thread.conversation_key != request.source_thread.conversation_key or (
                    request.source_thread.native_thread_id is not None
                    and thread.native_thread_id != request.source_thread.native_thread_id
                ):
                    raise DomainError(
                        "INGRESS_HISTORY_SCOPE",
                        "History provider returned another conversation",
                        403,
                    )
        for delivery in page.deliveries:
            # The actual original timestamp is retained; callbacks cannot make old work fresh.
            items = tuple(
                item.model_copy(
                    update={"event": item.event.model_copy(update={"historical_backfill": True})}
                )
                for item in delivery.metadata.items
            )
            historical = replace(
                delivery, metadata=delivery.metadata.model_copy(update={"items": items})
            )
            receipt = self.ingress.submit(instance, self.generation, historical, historical=True)
            if not receipt.committed or receipt.disposition == "conflict":
                raise DomainError(
                    "INGRESS_HISTORY_NOT_COMMITTED",
                    "History page did not receive all durable receipts",
                    409,
                )
        if not page.complete and page.next_cursor is None:
            self._gap("no_replay")
        return page
