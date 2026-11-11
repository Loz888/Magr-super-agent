"""Native container membership facts, never an authorization or root-ID guess."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.domain import SourceInstance
from mgar_v3.sources.feishu import FeishuAdapter, opaque
from mgar_v3.sources.ingress_domain import HistoryRequest, IngressReceipt
from mgar_v3.sources.ports import TransportEnvelope

if TYPE_CHECKING:
    from mgar_v3.sources.feishu_runtime import FeishuRuntime


def record_verified_topic(
    runtime: FeishuRuntime,
    adapter: FeishuAdapter,
    instance: SourceInstance,
    envelope: TransportEnvelope,
    transport: str,
    receipt: object,
) -> None:
    if not isinstance(receipt, IngressReceipt) or not receipt.input_ids:
        return
    raw, _, _ = adapter.authenticated(instance, envelope, transport=transport)
    message = raw.get("event", {}).get("message")
    if not isinstance(message, dict) or not message.get("thread_id"):
        return
    thread, chat = opaque(message["thread_id"]), opaque(message["chat_id"])
    key = digest([str(runtime.source_id), runtime.revision, instance.revision, chat, thread])
    ingress = runtime.sources.engine.source_ingress
    with ingress.root_transaction() as conn:
        current = runtime.sources.identities.registry._instance(conn, runtime.source_id)
        if current != instance:
            raise DomainError("FEISHU_TOPIC_PROOF_STALE", "Source changed before topic proof", 409)
        row = conn.execute(
            "SELECT input_id,metadata_expires_at FROM app.source_ingress_inputs "
            "WHERE input_id=ANY(%s) AND source_instance_id=%s AND metadata IS NOT NULL "
            "AND metadata_expires_at>clock_timestamp() "
            "AND metadata->'event'->'source_thread'->>'conversation_id'=%s "
            "AND metadata->'event'->'source_thread'->>'native_thread_id'=%s LIMIT 1",
            (list(receipt.input_ids), runtime.source_id, chat, thread),
        ).fetchone()
        if row is None:
            return
        exists = conn.execute(
            "SELECT 1 FROM app.feishu_source_audit WHERE source_instance_id=%s "
            "AND event='native_thread_observed' AND details->>'key'=%s "
            "AND (details->>'expires_at')::timestamptz>clock_timestamp()",
            (runtime.source_id, key),
        ).fetchone()
        if exists:
            return
        count = conn.execute(
            "SELECT count(*) AS n FROM app.feishu_source_audit WHERE source_instance_id=%s "
            "AND event='native_thread_observed'",
            (runtime.source_id,),
        ).fetchone()
        if count and count["n"] >= 10000:
            return  # No proof is minted; a later scoped history read must reject.
        conn.execute(
            "INSERT INTO app.feishu_source_audit(source_instance_id,event,reason_code,details) "
            "VALUES(%s,'native_thread_observed','FEISHU_NATIVE_THREAD_VERIFIED',%s)",
            (
                runtime.source_id,
                Jsonb(
                    {
                        "key": key,
                        "config_revision": runtime.revision,
                        "source_revision": instance.revision,
                        "input_id": str(row["input_id"]),
                        "payload_sha256": digest(raw),
                        "expires_at": row["metadata_expires_at"].isoformat(),
                    }
                ),
            ),
        )


def require_native_topic(
    runtime: FeishuRuntime,
    instance: SourceInstance,
    request: HistoryRequest,
    conn: Connection[dict[str, Any]],
) -> None:
    thread = request.source_thread.native_thread_id
    if thread is None:
        return
    key = digest(
        [
            str(runtime.source_id),
            runtime.revision,
            instance.revision,
            request.source_thread.conversation_id,
            thread,
        ]
    )
    proof = conn.execute(
        "SELECT 1 FROM app.feishu_source_audit a "
        "JOIN app.source_ingress_inputs i ON i.input_id::text=a.details->>'input_id' "
        "AND i.source_instance_id=a.source_instance_id "
        "WHERE a.source_instance_id=%s AND a.event='native_thread_observed' "
        "AND a.reason_code='FEISHU_NATIVE_THREAD_VERIFIED' AND a.details->>'key'=%s "
        "AND (a.details->>'expires_at')::timestamptz>clock_timestamp() "
        "AND i.metadata IS NOT NULL AND i.metadata_expires_at>clock_timestamp() LIMIT 1",
        (runtime.source_id, key),
    ).fetchone()
    if proof is None:
        raise DomainError(
            "FEISHU_HISTORY_THREAD_UNPROVEN",
            "No current verified native thread container; no remote read",
            403,
        )
