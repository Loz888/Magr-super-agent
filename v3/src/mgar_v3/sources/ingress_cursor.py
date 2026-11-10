"""Cursor and gap facts on the caller's Engine transaction connection."""

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.domain import SourceInstance
from mgar_v3.sources.ingress_domain import CursorPosition, GapDeclaration

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection


def stream_key(instance: SourceInstance, stream: str) -> str:
    return digest([instance.scope.key, stream])


def record_gap(conn: "Connection", instance: SourceInstance, declaration: GapDeclaration) -> UUID:
    key = stream_key(instance, declaration.stream_id)
    prior = conn.execute(
        "SELECT gap_id FROM app.source_ingress_gaps WHERE source_instance_id=%s AND stream_key=%s "
        "AND epoch=%s AND declaration->>'reason'=%s AND resolved_at IS NULL "
        "AND sequence_start IS NOT DISTINCT FROM %s AND sequence_end IS NOT DISTINCT FROM %s",
        (
            instance.scope.source_instance_id,
            key,
            declaration.epoch,
            declaration.reason,
            declaration.sequence_start,
            declaration.sequence_end,
        ),
    ).fetchone()
    if prior:
        return UUID(str(prior["gap_id"]))
    count = conn.execute(
        "SELECT count(*) AS n FROM app.source_ingress_gaps WHERE source_instance_id=%s",
        (instance.scope.source_instance_id,),
    ).fetchone()
    assert count is not None
    if count["n"] >= 1000:
        raise DomainError(
            "INGRESS_GAP_CAPACITY", "Gap ledger is full; operator review is required", 503
        )
    identifier = uuid4()
    data = declaration.model_dump(mode="json")
    inserted = conn.execute(
        "INSERT INTO app.source_ingress_gaps(gap_id,gap_key,source_instance_id,declaration,"
        "stream_key,epoch,sequence_start,sequence_end) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(gap_key) DO NOTHING RETURNING gap_id",
        (
            identifier,
            digest([instance.scope.key, data]),
            instance.scope.source_instance_id,
            Jsonb(data),
            key,
            declaration.epoch,
            declaration.sequence_start,
            declaration.sequence_end,
        ),
    ).fetchone()
    if inserted is not None:
        return UUID(str(inserted["gap_id"]))
    row = conn.execute(
        "SELECT gap_id FROM app.source_ingress_gaps WHERE gap_key=%s",
        (digest([instance.scope.key, data]),),
    ).fetchone()
    assert row is not None
    return UUID(str(row["gap_id"]))


def apply_cursor(
    conn: "Connection", instance: SourceInstance, cursor: CursorPosition | None, receipt_id: UUID
) -> bool:
    """Return whether a source gap is open; only actual committed positions fill a gap."""
    if cursor is not None:
        key = stream_key(instance, cursor.stream_id)
        row = conn.execute(
            "SELECT * FROM app.source_ingress_cursors WHERE stream_key=%s FOR UPDATE", (key,)
        ).fetchone()
        now = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        assert now is not None
        if row is None:
            conn.execute(
                "INSERT INTO app.source_ingress_cursors(stream_key,source_instance_id,"
                "project_id,stream_id,epoch) VALUES (%s,%s,%s,%s,%s)",
                (
                    key,
                    instance.scope.source_instance_id,
                    instance.project_id,
                    cursor.stream_id,
                    cursor.epoch,
                ),
            )
            row = {"epoch": cursor.epoch, "highest_sequence": 0, "committed_sequence": 0}
        if row["epoch"] != cursor.epoch:
            record_gap(
                conn,
                instance,
                GapDeclaration(
                    stream_id=cursor.stream_id,
                    epoch=cursor.epoch,
                    reason="cursor_reset",
                    started_at=now["now"],
                ),
            )
        else:
            prior = conn.execute(
                "SELECT d.payload_sha256 FROM app.source_ingress_cursor_marks m JOIN "
                "app.source_ingress_deliveries d USING(receipt_id) "
                "WHERE m.stream_key=%s AND m.epoch=%s AND m.sequence=%s",
                (key, cursor.epoch, cursor.sequence),
            ).fetchone()
            actual = conn.execute(
                "SELECT payload_sha256 FROM app.source_ingress_deliveries WHERE receipt_id=%s",
                (receipt_id,),
            ).fetchone()
            assert actual is not None
            if prior is not None and prior["payload_sha256"] != actual["payload_sha256"]:
                raise DomainError(
                    "INGRESS_CURSOR_CONFLICT", "Cursor position has another immutable delivery", 409
                )
            if cursor.sequence > row["highest_sequence"] + 1:
                record_gap(
                    conn,
                    instance,
                    GapDeclaration(
                        stream_id=cursor.stream_id,
                        epoch=cursor.epoch,
                        reason="cursor_jump",
                        started_at=now["now"],
                        sequence_start=row["highest_sequence"] + 1,
                        sequence_end=cursor.sequence - 1,
                    ),
                )
            conn.execute(
                "INSERT INTO app.source_ingress_cursor_marks(stream_key,epoch,sequence,"
                "cursor_value,receipt_id) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT(stream_key,epoch,sequence) DO NOTHING",
                (key, cursor.epoch, cursor.sequence, cursor.value, receipt_id),
            )
            committed, value = row["committed_sequence"], None
            marks = conn.execute(
                "SELECT sequence,cursor_value FROM app.source_ingress_cursor_marks WHERE "
                "stream_key=%s AND epoch=%s AND sequence>%s ORDER BY sequence",
                (key, cursor.epoch, committed),
            ).fetchall()
            for mark in marks:
                if mark["sequence"] != committed + 1:
                    break
                committed, value = mark["sequence"], mark["cursor_value"]
            conn.execute(
                "UPDATE app.source_ingress_cursors SET revision=revision+1,"
                "committed_sequence=%s,highest_sequence=greatest(highest_sequence,%s),"
                "cursor_value=CASE WHEN %s::bigint>committed_sequence THEN %s ELSE "
                "cursor_value END WHERE stream_key=%s",
                (committed, cursor.sequence, committed, value, key),
            )
            gaps = conn.execute(
                "SELECT * FROM app.source_ingress_gaps WHERE stream_key=%s AND epoch=%s "
                "AND resolved_at IS NULL AND sequence_start IS NOT NULL",
                (key, cursor.epoch),
            ).fetchall()
            for gap in gaps:
                count = conn.execute(
                    "SELECT count(*) AS n FROM app.source_ingress_cursor_marks WHERE "
                    "stream_key=%s AND epoch=%s AND sequence BETWEEN %s AND %s",
                    (key, cursor.epoch, gap["sequence_start"], gap["sequence_end"]),
                ).fetchone()
                assert count is not None
                if count["n"] == gap["sequence_end"] - gap["sequence_start"] + 1:
                    conn.execute(
                        "UPDATE app.source_ingress_gaps SET resolved_at=now(),"
                        "resolution_code='COVERED_BY_COMMITTED_RECEIPTS' WHERE gap_id=%s",
                        (gap["gap_id"],),
                    )
    return (
        conn.execute(
            "SELECT 1 FROM app.source_ingress_gaps WHERE source_instance_id=%s AND "
            "resolved_at IS NULL LIMIT 1",
            (instance.scope.source_instance_id,),
        ).fetchone()
        is not None
    )
