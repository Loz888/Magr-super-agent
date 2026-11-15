"""Bounded console metadata over the original Engine-held connection only."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.domain import SourceInstance
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.super_agent.source_console_credentials import ConsoleVault

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine

Row = dict[str, Any]
MAX_SOURCES = 100
MAX_POLICIES = 1000


class ConsoleStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.identities = SourceIdentities(engine)
        self.vault = ConsoleVault(engine)

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    def _owner(self, conn: Connection, cookie: str) -> UUID:
        user = self.identities._console(conn, cookie)
        row = conn.execute("SELECT to_regclass('app.sa_console_credentials') AS t").fetchone()
        if not row or row["t"] is None:
            raise DomainError("CONSOLE_NOT_INSTALLED", "来源控制台尚未完成安装。", 503)
        return user

    @staticmethod
    def _now(conn: Connection) -> Any:
        row = conn.execute("SELECT clock_timestamp() AS t").fetchone()
        assert row is not None
        return row["t"]

    def _audit(
        self,
        conn: Connection,
        user: UUID,
        event: str,
        target: UUID | None,
        review_ref: str,
        **details: int,
    ) -> None:
        conn.execute(
            "INSERT INTO app.sa_console_audit(event_id,project_id,actor_user_id,event,"
            "target_id,review_ref,details) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (uuid4(), self.project, user, event, target, review_ref, Jsonb(details)),
        )

    def _instances(self, conn: Connection) -> list[SourceInstance]:
        rows = conn.execute(
            "SELECT source_instance_id FROM app.source_instances WHERE project_id=%s "
            "ORDER BY source_instance_id LIMIT %s",
            (self.project, MAX_SOURCES + 1),
        ).fetchall()
        if len(rows) > MAX_SOURCES:
            raise DomainError(
                "CONSOLE_SCOPE_BOUND", "来源数量超过本版控制台上限，请先收窄范围。", 409
            )
        return [self.identities.registry._instance(conn, row["source_instance_id"]) for row in rows]

    def _config(self, conn: Connection, instance: SourceInstance) -> Row | None:
        table = {"github": "github_source_settings", "feishu": "feishu_source_settings"}.get(
            instance.scope.source_kind
        )
        if table is None:
            return None
        return conn.execute(
            "SELECT revision,source_revision,definition FROM app."
            + table
            + " WHERE source_instance_id=%s AND project_id=%s",
            (instance.scope.source_instance_id, self.project),
        ).fetchone()

    def _credential(self, conn: Connection, identifier: UUID) -> Row | None:
        return conn.execute(
            "SELECT * FROM app.sa_console_credentials WHERE "
            "source_instance_id=%s AND project_id=%s",
            (identifier, self.project),
        ).fetchone()

    def _credential_public(self, conn: Connection, identifier: UUID) -> Row:
        row = self._credential(conn, identifier)
        active = bool(
            row and row["ciphertext"] is not None and row["valid_until"] > self._now(conn)
        )
        return {
            "revision": row["revision"] if row else 0,
            "configured": active,
            "field_names": row["field_names"] if row is not None and active else [],
            "valid_until": row["valid_until"] if row else None,
            "revoked": bool(row and row["revoked_at"] is not None),
            "write_only": True,
        }

    def _policies(self, conn: Connection) -> list[Row]:
        rows = conn.execute(
            "SELECT p.policy_id,p.current_revision,v.definition,v.definition_sha256,v.revoked "
            "FROM app.source_policies p JOIN app.source_policy_versions v "
            "ON v.policy_id=p.policy_id AND v.revision=p.current_revision "
            "WHERE p.project_id=%s ORDER BY p.policy_id LIMIT %s",
            (self.project, MAX_POLICIES + 1),
        ).fetchall()
        if len(rows) > MAX_POLICIES:
            raise DomainError("CONSOLE_SCOPE_BOUND", "规则数量超过本版控制台上限。", 409)
        return rows

    def _snapshot_hash(self, conn: Connection) -> str:
        sources = []
        for instance in self._instances(conn):
            identifier = instance.scope.source_instance_id
            config = self._config(conn, instance)
            control = conn.execute(
                "SELECT generation FROM app.source_ingress_controls WHERE source_instance_id=%s "
                "AND project_id=%s",
                (identifier, self.project),
            ).fetchone()
            credential = self._credential(conn, identifier)
            sources.append(
                [
                    str(identifier),
                    instance.revision,
                    config["revision"] if config else 0,
                    control["generation"] if control else 0,
                    credential["revision"] if credential else 0,
                ]
            )
        roles = conn.execute(
            "SELECT b.binding_id,b.revision,b.active,b.roles,i.revision AS identity_revision,"
            "i.revoked_at,i.principal_id FROM app.source_role_bindings b "
            "JOIN app.source_identities i USING(identity_id,project_id) "
            "WHERE b.project_id=%s ORDER BY b.binding_id LIMIT 1001",
            (self.project,),
        ).fetchall()
        if len(roles) > 1000:
            raise DomainError("CONSOLE_SCOPE_BOUND", "身份授权数量超过本版上限。", 409)
        identities = conn.execute(
            "SELECT identity_id,revision,revoked_at FROM app.source_identities "
            "WHERE project_id=%s ORDER BY identity_id LIMIT 1001",
            (self.project,),
        ).fetchall()
        if len(identities) > 1000:
            raise DomainError("CONSOLE_SCOPE_BOUND", "来源身份数量超过本版上限。", 409)
        return digest(
            {
                "project": self.project,
                "sources": sources,
                "identities": [
                    [str(i["identity_id"]), i["revision"], i["revoked_at"] is not None]
                    for i in identities
                ],
                "roles": [
                    [
                        str(r["binding_id"]),
                        r["revision"],
                        r["active"],
                        r["roles"],
                        r["identity_revision"],
                        r["revoked_at"] is not None,
                        str(r["principal_id"]),
                    ]
                    for r in roles
                ],
                "policies": [
                    [
                        str(p["policy_id"]),
                        p["current_revision"],
                        p["definition_sha256"],
                        p["revoked"],
                    ]
                    for p in self._policies(conn)
                ],
            }
        )

    def _run_context(self, conn: Connection, identifier: UUID) -> Row:
        row = conn.execute(
            "SELECT r.run_id,r.ticket_id,r.status,r.outcome,r.reason_code,r.execution_epoch,"
            "r.execution_mode,r.graph_version,t.thread_id,t.status AS ticket_status "
            "FROM app.runs r JOIN app.tickets t USING(ticket_id) "
            "JOIN app.threads th ON th.thread_id=t.thread_id WHERE "
            "r.run_id=%s AND th.project_id=%s",
            (identifier, self.project),
        ).fetchone()
        if row is None:
            raise DomainError("CONSOLE_TASK_NOT_FOUND", "当前项目中没有这个任务。", 404)
        return row
