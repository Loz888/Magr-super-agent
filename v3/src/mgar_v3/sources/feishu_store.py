"""Feishu facts, configuration and filtering through the original Engine only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.feishu import CAPABILITIES
from mgar_v3.sources.feishu_domain import (
    VERSION,
    FeishuConfigure,
    FeishuSettings,
    FeishuTransportReceipt,
)
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.ingress_domain import GapDeclaration, VerifiedDelivery
from mgar_v3.super_agent.policy_domain import PolicyDefinition

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine

Row = dict[str, Any]


class FeishuSources:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.identities = SourceIdentities(engine)

    def _audit(
        self, conn: Connection, source_id: UUID, event: str, code: str, **details: Any
    ) -> None:
        conn.execute(
            "INSERT INTO app.feishu_source_audit"
            "(source_instance_id,event,reason_code,details) VALUES(%s,%s,%s,%s)",
            (source_id, event, code, Jsonb(details)),
        )

    def _current(self, conn: Connection, source_id: UUID) -> tuple[Row, FeishuSettings]:
        row = conn.execute(
            "SELECT * FROM app.feishu_source_settings WHERE source_instance_id=%s "
            "AND project_id=%s",
            (source_id, self.engine.settings.project_id),
        ).fetchone()
        if row is None:
            raise DomainError("FEISHU_NOT_CONFIGURED", "Feishu source is not configured", 409)
        instance = self.identities.registry._instance(conn, source_id)
        if instance.revision != row["source_revision"]:
            raise DomainError(
                "FEISHU_SOURCE_STALE", "Reconfirm source and policy after fencing", 409
            )
        return row, FeishuSettings.model_validate(row["definition"])

    def current(self, source_id: UUID) -> tuple[Row, FeishuSettings]:
        with self.engine.transaction() as conn:
            return self._current(conn, source_id)

    def configure(self, cookie: str, source_id: UUID, request: FeishuConfigure) -> Row:
        with self.engine.transaction() as conn:
            user = self.identities._console(conn, cookie)
            instance = self.identities.registry._instance(conn, source_id)
            value = request.settings
            if (
                instance.scope.source_kind != "feishu"
                or instance.adapter_version != VERSION
                or instance.capabilities != CAPABILITIES
                or instance.scope.tenant_id != value.tenant_key
                or instance.scope.installation_id != value.app_id
                or instance.credential_ref is None
            ):
                raise DomainError(
                    "FEISHU_INSTALLATION_SCOPE", "Exact configured Feishu source required", 403
                )
            old = conn.execute(
                "SELECT * FROM app.feishu_source_settings WHERE source_instance_id=%s FOR UPDATE",
                (source_id,),
            ).fetchone()
            if (old["revision"] if old else 0) != request.expected_revision:
                raise DomainError("FEISHU_REVISION_CONFLICT", "Reload settings before saving", 409)
            if old:
                previous = FeishuSettings.model_validate(old["definition"])
                if (previous.app_id, previous.tenant_key, previous.bot_open_id) != (
                    value.app_id,
                    value.tenant_key,
                    value.bot_open_id,
                ):
                    raise DomainError(
                        "FEISHU_INSTALLATION_IMMUTABLE", "Register a new installation", 409
                    )
            revision = request.expected_revision + 1
            updated = instance.model_copy(update={"revision": instance.revision + 1})
            self.identities.registry.register_instance(updated)  # Existing revoke/content triggers.
            params = (
                source_id,
                instance.project_id,
                revision,
                updated.revision,
                Jsonb(value.model_dump(mode="json")),
                user,
                request.review_ref,
            )
            conn.execute(
                "INSERT INTO app.feishu_source_settings(source_instance_id,project_id,"
                "revision,source_revision,definition,signed_by,review_ref) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source_instance_id) DO UPDATE SET "
                "revision=excluded.revision,source_revision=excluded.source_revision,"
                "definition=excluded.definition,signed_by=excluded.signed_by,"
                "signed_at=now(),review_ref=excluded.review_ref",
                params,
            )
            conn.execute(
                "INSERT INTO app.feishu_source_versions(source_instance_id,revision,"
                "source_revision,definition,signed_by,review_ref) VALUES(%s,%s,%s,%s,%s,%s)",
                (source_id, revision, updated.revision, params[4], user, request.review_ref),
            )
            conn.execute(
                "DELETE FROM app.feishu_source_health WHERE source_instance_id=%s", (source_id,)
            )
            self._audit(
                conn,
                source_id,
                "configure",
                "FEISHU_CONFIG_SAVED",
                revision=revision,
                review_ref=request.review_ref,
            )
            return {
                "revision": revision,
                "source_revision": updated.revision,
                "settings": value.model_dump(mode="json"),
                "runtime": "NOT_ACTIVATED_BY_SETTINGS",
            }

    def inspect(self, cookie: str, source_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            row = conn.execute(
                "SELECT * FROM app.feishu_source_settings WHERE source_instance_id=%s "
                "AND project_id=%s",
                (source_id, self.engine.settings.project_id),
            ).fetchone()
            if row is None:
                raise DomainError("FEISHU_NOT_CONFIGURED", "Feishu source is not configured", 409)
            instance = self.identities.registry._instance(conn, source_id)
            health = conn.execute(
                "SELECT * FROM app.feishu_source_health WHERE source_instance_id=%s", (source_id,)
            ).fetchone()
            audit = conn.execute(
                "SELECT event,reason_code,details,created_at "
                "FROM app.feishu_source_audit WHERE source_instance_id=%s "
                "ORDER BY audit_id DESC LIMIT 100",
                (source_id,),
            ).fetchall()
            filters = conn.execute(
                "SELECT count(*) AS n FROM app.feishu_transport_filters "
                "WHERE source_instance_id=%s",
                (source_id,),
            ).fetchone()
            assert filters
            return {
                "revision": row["revision"],
                "source_revision": row["source_revision"],
                "settings": row["definition"],
                "stale": row["source_revision"] != instance.revision,
                "health": health,
                "connection": "disabled",
                "permissions": "unknown",
                "transport_filtered_count": filters["n"],
                "audit": audit,
                "live_source_channel_gate": "NOT_RUN",
            }

    def assert_policy_ceiling(
        self, source_id: UUID, value: FeishuSettings, generation: int
    ) -> None:
        with self.engine.transaction() as conn:
            control, ingress = self.engine.source_ingress._control(conn, source_id)
            if (
                control["generation"] != generation
                or ingress.owner != "gateway"
                or ingress.policy_id is None
            ):
                raise DomainError("FEISHU_OWNER_FENCED", "Feishu ingress owner changed", 403)
            policy = self.engine.source_policies._current(conn, ingress.policy_id)
            definition = PolicyDefinition.model_validate(policy["definition"])
            self.engine.source_policies._validate_source(conn, definition)
            scope = self.identities.registry._instance(conn, source_id).scope
            if (
                definition.selector.scope != scope
                or policy["revoked"]
                or policy["current_revision"] != ingress.policy_revision
                or not set(definition.selector.conversations).issubset(value.chat_ids)
            ):
                raise DomainError(
                    "FEISHU_POLICY_SCOPE", "Policy exceeds current App/chat ceiling", 403
                )
            for actor in definition.selector.actors:
                allowed = (
                    value.sender_open_ids
                    if actor.actor_kind == "human"
                    else value.context_bot_open_ids
                    if actor.actor_kind == "bot"
                    else ()
                )
                if actor.id_type != "open_id" or actor.actor_id not in allowed:
                    raise DomainError(
                        "FEISHU_POLICY_SCOPE", "Actor exceeds current App ceiling", 403
                    )
            if not set(definition.selector.event_kinds).issubset(value.events):
                raise DomainError("FEISHU_POLICY_SCOPE", "Event exceeds selected events", 403)

    def filtered(
        self,
        source_id: UUID,
        event_id: str,
        payload_sha: str,
        code: str,
        generation: int,
        *,
        observation: VerifiedDelivery | None = None,
    ) -> FeishuTransportReceipt:
        ingress = self.engine.source_ingress
        with ingress.root_transaction() as conn:
            _, settings = self._current(conn, source_id)
            self.assert_policy_ceiling(source_id, settings, generation)
            key = digest([str(source_id), event_id])
            old = conn.execute(
                "SELECT * FROM app.feishu_transport_filters "
                "WHERE source_instance_id=%s AND event_key=%s",
                (source_id, key),
            ).fetchone()
            if old:
                if old["payload_sha256"] != payload_sha:
                    raise DomainError(
                        "FEISHU_FILTER_CONFLICT", "Conflicting filtered transport", 409
                    )
                return FeishuTransportReceipt(
                    receipt_id=old["receipt_id"], reason_code=old["reason_code"]
                )
            count = conn.execute(
                "SELECT count(*) AS n FROM app.feishu_transport_filters "
                "WHERE source_instance_id=%s",
                (source_id,),
            ).fetchone()
            assert count
            if count["n"] >= 10000:
                raise DomainError(
                    "FEISHU_FILTER_CAPACITY", "Filtered transport ledger is full; no ACK", 503
                )
            if observation is not None:
                instance = self.identities.registry._instance(conn, source_id)
                ingress.validate(instance, observation)
                if (
                    observation.metadata.payload_sha256 != payload_sha
                    or any(b is not None for b in observation.bodies)
                    or any(i.event.remote_event_id != event_id for i in observation.metadata.items)
                ):
                    raise DomainError("FEISHU_FILTER_OBSERVATION", "Invalid bodyless fact", 403)
                conflict = ingress._item_conflict(conn, observation.metadata.items)
                if conflict:
                    raise DomainError(
                        conflict, "Filtered observation conflicts with prior input", 409
                    )
                # Known resources only: no new inbox, identity, author or content.
                for item in observation.metadata.items:
                    ingress._privacy_delete(conn, item, denied=True, allow_new=False)
            identifier = uuid4()
            conn.execute(
                "INSERT INTO app.feishu_transport_filters"
                "(receipt_id,source_instance_id,event_key,payload_sha256,reason_code) "
                "VALUES(%s,%s,%s,%s,%s)",
                (identifier, source_id, key, payload_sha, code),
            )
            return FeishuTransportReceipt(receipt_id=identifier, reason_code=code)

    def observe_health(
        self,
        source_id: UUID,
        revision: int,
        mode: str,
        *,
        connection: str,
        permissions: str,
        code: str,
        last_event: bool = False,
        gap_start: datetime | None = None,
    ) -> None:
        with self.engine.transaction() as conn:
            config = conn.execute(
                "SELECT revision FROM app.feishu_source_settings WHERE source_instance_id=%s "
                "AND project_id=%s",
                (source_id, self.engine.settings.project_id),
            ).fetchone()
            if config is None or config["revision"] != revision:
                return
            old = conn.execute(
                "SELECT * FROM app.feishu_source_health WHERE source_instance_id=%s FOR UPDATE",
                (source_id,),
            ).fetchone()
            at = datetime.now(UTC)
            preserve = bool(
                old
                and old["config_revision"] == revision
                and old["mode"] == mode
                and old["permissions"] in {"insufficient", "revoked"}
                and timedelta(seconds=-30) <= at - old["observed_at"] < timedelta(minutes=5)
                and permissions not in {"insufficient", "revoked"}
            )
            diagnostic_permission = old["permissions"] if preserve and old else permissions
            diagnostic_code = old["reason_code"] if preserve and old else code
            conn.execute(
                "INSERT INTO app.feishu_source_health(source_instance_id,config_revision,"
                "connection,permissions,mode,reason_code,last_event_at,gap_started_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN now() END,%s) "
                "ON CONFLICT(source_instance_id) DO UPDATE SET "
                "config_revision=excluded.config_revision,connection=excluded.connection,"
                "permissions=excluded.permissions,mode=excluded.mode,reason_code=excluded.reason_code,"
                "observed_at=CASE WHEN %s THEN app.feishu_source_health.observed_at "
                "ELSE clock_timestamp() END,last_event_at=coalesce(excluded.last_event_at,"
                "app.feishu_source_health.last_event_at),gap_started_at=excluded.gap_started_at",
                (
                    source_id,
                    revision,
                    connection,
                    diagnostic_permission,
                    mode,
                    diagnostic_code,
                    last_event,
                    gap_start,
                    preserve,
                ),
            )
            self._audit(
                conn,
                source_id,
                "health",
                code,
                connection=connection,
                permissions=permissions,
                mode=mode,
                config_revision=revision,
                connection_observed_at=at.isoformat(),
                preserved_permission_diagnosis=preserve,
            )

    def fence(self, source_id: UUID, revision: int, mode: str, code: str, permission: str) -> None:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.feishu_source_settings WHERE source_instance_id=%s "
                "AND project_id=%s",
                (source_id, self.engine.settings.project_id),
            ).fetchone()
            if row is None or row["revision"] != revision:
                return
            instance = self.identities.registry._instance(conn, source_id)
            if instance.revision == row["source_revision"]:
                self.identities.registry.register_instance(
                    instance.model_copy(update={"revision": instance.revision + 1})
                )
            self.observe_health(
                source_id,
                revision,
                mode,
                connection="degraded",
                permissions=permission,
                code=code,
                gap_start=datetime.now(UTC),
            )
        self.engine.source_ingress.gap(
            source_id,
            GapDeclaration(
                stream_id="feishu",
                epoch="permission",
                reason="permission",
                started_at=datetime.now(UTC),
            ),
        )
