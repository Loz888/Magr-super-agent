"""Owner-only, body-free retention inspection on the original Engine connection.

This is an explicit maintenance read, not a security verdict, new store, listener,
or activation path. Cleanup continues through SourceIngress.purge; restoration
continues to require the existing hold and fresh operator reconciliation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine


class SourceSecurity:
    def __init__(self, engine: Engine):
        self.engine = engine

    def inspect(self, cookie: str) -> dict[str, Any]:
        with self.engine.transaction() as conn:
            self.engine.source_ingress.identities._console(conn, cookie)
            conn.execute("SET LOCAL statement_timeout='2s'")
            rows = conn.execute(
                "SELECT s.source_instance_id AS source_id,"
                "(SELECT count(*) FROM app.source_ingress_inputs i WHERE "
                "i.source_instance_id=s.source_instance_id) AS input_facts,"
                "(SELECT count(*) FROM app.source_ingress_entities e WHERE "
                "e.source_instance_id=s.source_instance_id AND e.deleted) AS tombstones,"
                "(SELECT count(*) FROM app.source_ingress_inputs i JOIN "
                "app.source_ingress_bodies b USING(input_id) WHERE "
                "i.source_instance_id=s.source_instance_id) AS retained_ciphertexts,"
                "(SELECT count(*) FROM app.source_ingress_inputs i JOIN "
                "app.source_ingress_bodies b USING(input_id) WHERE "
                "i.source_instance_id=s.source_instance_id AND "
                "b.expires_at<=clock_timestamp()) AS ciphertexts_due_for_purge,"
                "(SELECT count(*) FROM app.source_ingress_inputs i JOIN "
                "app.source_ingress_bodies b USING(input_id) WHERE "
                "i.source_instance_id=s.source_instance_id AND "
                "i.unavailable_at IS NOT NULL) AS unavailable_ciphertexts,"
                "(SELECT count(*) FROM app.source_ingress_inputs i WHERE "
                "i.source_instance_id=s.source_instance_id AND i.metadata IS NOT NULL "
                "AND i.metadata_expires_at<=clock_timestamp()) AS metadata_due_for_purge "
                "FROM app.source_instances s WHERE s.project_id=%s "
                "ORDER BY s.source_instance_id LIMIT 33",
                (self.engine.settings.project_id,),
            ).fetchall()
            return {
                "schema_version": "source-security-inspection-v1",
                "project_id": self.engine.settings.project_id,
                "sources": rows[:32],
                "partial": len(rows) > 32,
                "read_only": True,
                "contains_body": False,
                "security_gate": "NOT_ESTABLISHED_BY_INSPECTION",
                "cleanup": "existing_owner_source_ingress_purge",
                "restore": "keep_hold_until_fresh_deletions_and_revocations_reconciled",
            }
