"""Persistent source inbox on the original Engine connection. No task creation or ACK here."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID, uuid4

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, TriggerInput, digest
from mgar_v3.sources.domain import ActorRef, SourceInstance, SourceMetadata
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.sources.ingress_cursor import apply_cursor, record_gap, stream_key
from mgar_v3.sources.ingress_domain import (
    BufferPermit,
    CursorReset,
    GapDeclaration,
    IngressConfigure,
    IngressReceipt,
    IngressSettings,
    ItemMetadata,
    ReplayRequest,
    VerifiedDelivery,
)
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicyFacts

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine, Row

T = TypeVar("T")


class SourceIngress:
    def __init__(self, engine: Engine, cipher: BodyCipher | None = None):
        self.engine, self.cipher = engine, cipher
        self.identities = SourceIdentities(engine)

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    @staticmethod
    def now(conn: Connection) -> datetime:
        row = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        assert row is not None and isinstance(row["now"], datetime)
        return row["now"]

    @contextmanager
    def root_transaction(self) -> Iterator[Connection]:
        with self.engine._mutex:
            if self.engine.connection.closed:
                raise DomainError(
                    "INGRESS_DATABASE_UNAVAILABLE",
                    "Engine connection is closed; no durable ACK",
                    503,
                )
            if self.engine.connection.info.transaction_status != TransactionStatus.IDLE:
                raise DomainError(
                    "INGRESS_OUTER_TRANSACTION",
                    "Durable intake cannot commit inside a caller savepoint",
                    409,
                )
            with self.engine.transaction() as conn:
                conn.execute("SET LOCAL synchronous_commit=on")
                yield conn

    def _audit(
        self,
        conn: Connection,
        instance_id: UUID,
        event: str,
        user: UUID | None = None,
        **details: Any,
    ) -> None:
        conn.execute(
            "INSERT INTO app.source_ingress_audit(source_instance_id,event,actor_user_id,"
            "details) VALUES (%s,%s,%s,%s)",
            (instance_id, event, user, Jsonb(details)),
        )

    def _manage(
        self, cookie: str, instance_id: UUID, event: str, fn: Callable[[Connection, UUID], T]
    ) -> T:
        with self.engine.transaction() as conn:
            user = self.identities._console(conn, cookie)
            self.identities.registry._instance(conn, instance_id)
            result = fn(conn, user)
            self._audit(conn, instance_id, event, user)
            return result

    def _control(self, conn: Connection, instance_id: UUID) -> tuple[Row, IngressSettings]:
        self.identities.registry._instance(conn, instance_id)
        row = conn.execute(
            "SELECT * FROM app.source_ingress_controls WHERE source_instance_id=%s AND "
            "project_id=%s FOR UPDATE",
            (instance_id, self.project),
        ).fetchone()
        if row is None:
            raise DomainError(
                "INGRESS_NOT_CONFIGURED", "Source ingress ownership is not configured", 409
            )
        return row, IngressSettings.model_validate(row["settings"])

    def configure(self, cookie: str, instance_id: UUID, request: IngressConfigure) -> Row:
        def change(conn: Connection, user: UUID) -> Row:
            instance = self.identities.registry._instance(conn, instance_id)
            settings = request.settings
            if (
                instance.scope.source_kind == "github"
                and settings.owner == "gateway"
                and settings.legacy_repository is None
            ):
                raise DomainError(
                    "INGRESS_LEGACY_MAPPING_REQUIRED",
                    "GitHub cutover needs an explicit legacy repository owner",
                    422,
                )
            if settings.legacy_repository is not None and (
                instance.scope.source_kind != "github"
                or settings.legacy_repository.lower() != self.engine.settings.repository.lower()
            ):
                raise DomainError(
                    "INGRESS_LEGACY_MAPPING_SCOPE",
                    "Legacy mapping is limited to this configured GitHub repository",
                    403,
                )
            if settings.owner == "gateway":
                assert settings.policy_id is not None
                policy = self.engine.source_policies._current(conn, settings.policy_id)
                if policy["current_revision"] != settings.policy_revision or policy["revoked"]:
                    raise DomainError(
                        "INGRESS_POLICY_STALE", "Configure the exact current unrevoked policy", 409
                    )
                definition = PolicyDefinition.model_validate(policy["definition"])
                self.engine.source_policies._validate_source(conn, definition)
                if definition.selector.scope != instance.scope:
                    raise DomainError(
                        "INGRESS_POLICY_SCOPE",
                        "Ingress policy belongs to another installation",
                        403,
                    )
            old = conn.execute(
                "SELECT * FROM app.source_ingress_controls WHERE source_instance_id=%s FOR UPDATE",
                (instance_id,),
            ).fetchone()
            generation = old["generation"] if old else 0
            if generation != request.expected_generation:
                raise DomainError(
                    "INGRESS_GENERATION_CONFLICT",
                    "Ingress owner changed; reload before saving",
                    409,
                )
            repository = settings.legacy_repository.lower() if settings.legacy_repository else None
            if old and old["legacy_repository"] != repository:
                raise DomainError(
                    "INGRESS_OWNER_IDENTITY_IMMUTABLE",
                    "Legacy repository identity cannot be silently changed",
                    409,
                )
            duplicate = conn.execute(
                "SELECT 1 FROM app.source_ingress_controls WHERE project_id=%s AND "
                "legacy_repository=%s AND source_instance_id<>%s",
                (self.project, repository, instance_id),
            ).fetchone()
            if duplicate:
                raise DomainError(
                    "INGRESS_OWNER_EXISTS", "Repository already has one ingress owner", 409
                )
            conn.execute(
                "INSERT INTO app.source_ingress_controls(source_instance_id,project_id,"
                "generation,settings,legacy_repository,updated_by) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(source_instance_id) DO UPDATE SET "
                "generation=EXCLUDED.generation,"
                "settings=EXCLUDED.settings,updated_by=EXCLUDED.updated_by,updated_at=now()",
                (
                    instance_id,
                    self.project,
                    generation + 1,
                    Jsonb(settings.model_dump(mode="json")),
                    repository,
                    user,
                ),
            )
            self._audit(
                conn,
                instance_id,
                "ingress_owner_saved",
                user,
                generation=generation + 1,
                review_ref=request.review_ref,
            )
            return {
                "source_instance_id": instance_id,
                "generation": generation + 1,
                "settings": settings.model_dump(mode="json"),
            }

        return self._manage(cookie, instance_id, "ingress_configure", change)

    def snapshot(
        self, instance_id: UUID, generation: int
    ) -> tuple[SourceInstance, IngressSettings]:
        with self.root_transaction() as conn:
            row, settings = self._control(conn, instance_id)
            if row["generation"] != generation or settings.owner != "gateway":
                raise DomainError(
                    "INGRESS_OWNER_FENCED", "This gateway generation no longer owns ingress", 409
                )
            return self.identities.registry._instance(conn, instance_id), settings

    def check_legacy_owner(self, conn: Connection, event: TriggerInput) -> None:
        """Read-only old-ingress guard. Missing new schema/configuration preserves old semantics."""
        if event.source != "github":
            return
        schema = conn.execute(
            "SELECT to_regclass('app.source_ingress_controls') AS present"
        ).fetchone()
        if schema is None or schema["present"] is None:
            return
        control = conn.execute(
            "SELECT * FROM app.source_ingress_controls WHERE project_id=%s AND "
            "legacy_repository=%s",
            (event.project_id, event.repository.lower()),
        ).fetchone()
        if control is None:
            return
        settings = IngressSettings.model_validate(control["settings"])
        if settings.owner == "gateway":
            raise DomainError(
                "INGRESS_NEW_OWNER", "This repository is owned by the new source gateway", 409
            )
        if settings.owner == "disabled":
            raise DomainError(
                "INGRESS_DISABLED", "Ingress is explicitly disabled for this repository", 409
            )
        instance = self.identities.registry._instance(conn, control["source_instance_id"])
        key = digest([instance.scope.key, event.external_event_id])
        prior = conn.execute(
            "SELECT 1 FROM app.source_ingress_deliveries WHERE delivery_key=%s AND "
            "disposition<>'legacy_owned'",
            (key,),
        ).fetchone()
        if prior is not None:
            raise DomainError(
                "INGRESS_DELIVERY_ALREADY_OWNED",
                "Delivery already has a durable source receipt",
                409,
            )

    def validate(
        self, instance: SourceInstance, delivery: VerifiedDelivery, *, buffered: bool = False
    ) -> None:
        meta = delivery.metadata
        if (
            len(delivery.bodies) != len(meta.items)
            or meta.source_instance_id != instance.scope.source_instance_id
            or meta.source_revision != instance.revision
        ):
            raise DomainError(
                "INGRESS_VERIFICATION_SCOPE",
                "Verified delivery does not match current source configuration",
                403,
            )
        at = datetime.now(UTC)
        if not buffered and not at - timedelta(seconds=30) <= meta.verified_at <= at + timedelta(
            seconds=30
        ):
            raise DomainError(
                "INGRESS_VERIFICATION_TIME", "Gateway verification is not current", 403
            )
        if meta.cursor is not None and instance.capabilities.incremental_cursor != "supported":
            raise DomainError(
                "INGRESS_CURSOR_UNSUPPORTED", "Cursor ordering is not a confirmed capability", 409
            )
        for item, body in zip(meta.items, delivery.bodies, strict=True):
            event = item.event
            if (
                event.scope != instance.scope
                or event.project_id != self.project
                or event.source_revision != instance.revision
                or event.delivery_id != meta.delivery_id
                or event.provenance != "verified_source"
                or event.sender.actor_kind == "unknown"
            ):
                raise DomainError(
                    "INGRESS_VERIFICATION_SCOPE", "Normalized item escapes its verified source", 403
                )
            if body is not None and (not isinstance(body, bytes) or len(body) > 65536):
                raise DomainError(
                    "INGRESS_BODY_BOUND", "Normalized body exceeds its byte bound", 413
                )
            actual = hashlib.sha256(body).hexdigest() if body is not None else None
            if actual != event.body_ref or (item.operation == "delete" and body is not None):
                raise DomainError(
                    "INGRESS_BODY_HASH", "Body bytes do not match the verified event", 403
                )
            if (
                item.native_order is not None
                and instance.capabilities.native_entity_revision != "supported"
            ):
                raise DomainError(
                    "INGRESS_VERSION_UNSUPPORTED", "Native version order is not confirmed", 409
                )
            capability = (
                "edit_events"
                if item.operation == "edit"
                else "delete_events"
                if item.operation == "delete"
                else None
            )
            if capability and getattr(instance.capabilities, capability) != "supported":
                raise DomainError(
                    "INGRESS_MUTATION_UNSUPPORTED",
                    "Source does not confirm edit/delete events",
                    409,
                )

    def _receive(
        self,
        conn: Connection,
        settings: IngressSettings,
        instance: SourceInstance,
        item: ItemMetadata,
        *,
        historical: bool = False,
    ) -> tuple[Row | None, str]:
        assert settings.policy_id is not None and settings.policy_revision is not None
        policies = self.engine.source_policies
        try:
            policy = policies._current(conn, settings.policy_id)
            if policy["revoked"] or policy["current_revision"] != settings.policy_revision:
                return None, "INGRESS_POLICY_STALE"
            definition = PolicyDefinition.model_validate(policy["definition"])
            policies._validate_source(conn, definition)
            event = item.event
            if definition.selector.scope != instance.scope:
                return None, "INGRESS_POLICY_SCOPE"
            identity = conn.execute(
                "SELECT * FROM app.source_identities WHERE identity_key=%s AND "
                "project_id=%s AND revoked_at IS NULL",
                (event.sender.key, self.project),
            ).fetchone()
            if (
                identity is None
                or identity["principal_id"] is None
                or ActorRef.model_validate(identity["actor_ref"]) != event.sender
            ):
                return None, "INGRESS_IDENTITY_DENIED"
            roles, controlled = policies._roles(conn, event.sender, event.source_thread, None)
            decision = policies._evaluate(
                definition,
                "receive",
                event.sender,
                event.source_thread,
                PolicyFacts(
                    event_kind=event.event_kind, historical=historical or event.historical_backfill
                ),
                roles,
                self.now(conn),
                controlled,
            )
            if not decision.allowed:
                return None, decision.reason_code
            expiry = min(
                self.now(conn)
                + timedelta(
                    seconds=min(definition.receive.body_ttl_seconds, settings.metadata_ttl_seconds)
                ),
                event.occurred_at + timedelta(seconds=definition.receive.body_ttl_seconds),
                definition.valid_until,
            )
            return {
                "identity_id": identity["identity_id"],
                "identity_revision": identity["revision"],
                "policy_id": settings.policy_id,
                "policy_revision": settings.policy_revision,
                "retain_body": decision.retain_body,
                "model_input": decision.model_input,
                "body_expires_at": expiry,
                "mode": definition.mode,
            }, "RECEIVE_ALLOWED"
        except DomainError:
            return None, "INGRESS_CURRENT_AUTHORITY_DENIED"

    def buffer_permit(
        self, instance: SourceInstance, generation: int, delivery: VerifiedDelivery
    ) -> BufferPermit | None:
        with self.root_transaction() as conn:
            row, settings = self._control(conn, instance.scope.source_instance_id)
            if row["generation"] != generation or settings.owner != "gateway":
                return None
            revisions, deadline = (
                [],
                self.now(conn) + timedelta(seconds=settings.buffer_ttl_seconds),
            )
            for item in delivery.metadata.items:
                grant, _ = self._receive(conn, settings, instance, item)
                if (
                    grant is None
                    or not grant["retain_body"]
                    or grant["body_expires_at"] <= self.now(conn)
                ):
                    return None
                revisions.append((str(grant["identity_id"]), int(grant["identity_revision"])))
                deadline = min(deadline, grant["body_expires_at"])
            return BufferPermit(generation, tuple(revisions), deadline, delivery)

    @staticmethod
    def _normalized(delivery: VerifiedDelivery) -> str:
        meta = delivery.metadata
        return digest(
            [
                [i.fingerprint() for i in meta.items],
                meta.cursor.model_dump(mode="json") if meta.cursor else None,
            ]
        )

    def _receipt(self, row: Row, *, duplicate: bool = False) -> IngressReceipt:
        disposition = (
            "duplicate" if duplicate and row["disposition"] != "conflict" else row["disposition"]
        )
        return IngressReceipt(
            receipt_id=row["receipt_id"],
            source_instance_id=row["source_instance_id"],
            delivery_key=row["delivery_key"],
            disposition=disposition,
            input_ids=tuple(UUID(v) for v in row["input_ids"]),
            denied_count=row["denied_count"],
            reason_code=row["reason_code"],
            committed=True,
            original_trigger_id=row["original_trigger_id"],
        )

    def _conflict(
        self, conn: Connection, instance_id: UUID, key: str, sha: str, reason: str
    ) -> None:
        count = conn.execute(
            "SELECT count(*) AS n FROM app.source_ingress_conflicts WHERE source_instance_id=%s",
            (instance_id,),
        ).fetchone()
        assert count is not None
        if count["n"] >= 1000:
            raise DomainError("INGRESS_CONFLICT_CAPACITY", "Conflict ledger is full; no ACK", 503)
        conn.execute(
            "INSERT INTO app.source_ingress_conflicts(conflict_id,source_instance_id,"
            "delivery_key,attempted_sha256,reason_code) VALUES (%s,%s,%s,%s,%s) ON "
            "CONFLICT DO NOTHING",
            (uuid4(), instance_id, key, sha, reason),
        )

    def _item_conflict(self, conn: Connection, items: tuple[ItemMetadata, ...]) -> str | None:
        events: dict[str, str] = {}
        versions: dict[str, str] = {}
        orders: dict[tuple[str, int], str] = {}
        for item in items:
            event_key, fingerprint = item.event.event_key, item.fingerprint()
            alias = conn.execute(
                "SELECT fingerprint FROM app.source_ingress_aliases WHERE event_key=%s",
                (event_key,),
            ).fetchone()
            if (alias and alias["fingerprint"] != fingerprint) or (
                event_key in events and events[event_key] != fingerprint
            ):
                return "INGRESS_EVENT_CONFLICT"
            events[event_key] = fingerprint
            version = conn.execute(
                "SELECT version_fingerprint FROM app.source_ingress_inputs WHERE version_key=%s",
                (item.version_key,),
            ).fetchone()
            vf = item.fingerprint(version=True)
            if (version and version["version_fingerprint"] != vf) or (
                item.version_key in versions and versions[item.version_key] != vf
            ):
                return "INGRESS_VERSION_CONFLICT"
            versions[item.version_key] = vf
            if item.native_order is not None:
                slot = (item.entity_key, item.native_order)
                previous = conn.execute(
                    "SELECT version_key FROM app.source_ingress_inputs WHERE entity_key=%s "
                    "AND native_order=%s",
                    slot,
                ).fetchone()
                if (previous and previous["version_key"] != item.version_key) or (
                    slot in orders and orders[slot] != item.version_key
                ):
                    return "INGRESS_ORDER_CONFLICT"
                orders[slot] = item.version_key
        return None

    @staticmethod
    def _order_ambiguous(entity: Row, item: ItemMetadata) -> bool:
        """A numeric watermark cannot order an earlier unsequenced version."""
        history = (
            entity["head_input_id"] is not None
            or entity["native_order"] is not None
            or entity["unordered_history"]
        )
        return bool(
            entity["ambiguous"]
            or (
                history
                and (
                    item.native_order is None
                    or entity["native_order"] is None
                    or entity["unordered_history"]
                )
            )
        )

    def _privacy_delete(
        self,
        conn: Connection,
        item: ItemMetadata,
        *,
        denied: bool = False,
        allow_new: bool = False,
    ) -> None:
        """Observe genuine versions without retaining denied content or reviving a version."""
        if item.operation not in {"create", "delete", "edit"}:
            return
        # _item_conflict has already proved equality. A transport alias is not a new version.
        known = conn.execute(
            "SELECT 1 FROM app.source_ingress_aliases WHERE event_key=%s UNION ALL "
            "SELECT 1 FROM app.source_ingress_inputs WHERE version_key=%s LIMIT 1",
            (item.event.event_key, item.version_key),
        ).fetchone()
        if known is not None:
            return
        entity = conn.execute(
            "SELECT e.*,EXISTS(SELECT 1 FROM app.source_ingress_inputs i "
            "WHERE i.entity_key=e.entity_key AND i.native_order IS NULL) AS unordered_history "
            "FROM app.source_ingress_entities e WHERE e.entity_key=%s AND e.project_id=%s "
            "FOR UPDATE OF e",
            (item.entity_key, self.project),
        ).fetchone()
        if entity is None:
            # Only after a bounded durable receipt is allocated may a denied first version
            # create this minimal hash/watermark fact. No inbox, payload, body hash or input.
            if denied and allow_new:
                conn.execute(
                    "INSERT INTO app.source_ingress_entities(entity_key,source_instance_id,"
                    "project_id,author_key,native_order,ambiguous,deleted) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        item.entity_key,
                        item.event.scope.source_instance_id,
                        self.project,
                        item.event.sender.key,
                        item.native_order,
                        item.operation != "delete" and item.native_order is None,
                        item.operation == "delete",
                    ),
                )
            return
        if entity["author_key"] != item.event.sender.key and not item.platform_moderation:
            return
        if item.operation != "delete" and self._order_ambiguous(entity, item):
            self._expire_entity(conn, item.entity_key, "INGRESS_UNORDERED_VERSION")
            conn.execute(
                "UPDATE app.source_ingress_entities SET ambiguous=true,"
                "native_order=greatest(native_order,%s),updated_at=now() WHERE entity_key=%s",
                (item.native_order if denied else None, item.entity_key),
            )
            return
        if (
            item.operation != "delete"
            and item.native_order is not None
            and entity["native_order"] is not None
            and item.native_order <= entity["native_order"]
        ):
            return
        self._expire_entity(
            conn,
            item.entity_key,
            "INGRESS_ENTITY_DELETED" if item.operation == "delete" else "INGRESS_ENTITY_EDITED",
        )
        if item.operation == "delete":
            conn.execute(
                "UPDATE app.source_ingress_entities SET deleted=true,"
                "native_order=greatest(native_order,%s),updated_at=now() WHERE entity_key=%s",
                (item.native_order, item.entity_key),
            )
        elif denied:
            # The existing monotonic field is the observed watermark, independent of
            # body admission. Missing order is sticky ambiguity, never arrival-time order.
            conn.execute(
                "UPDATE app.source_ingress_entities SET native_order=greatest(native_order,%s),"
                "ambiguous=ambiguous OR %s,updated_at=now() WHERE entity_key=%s",
                (item.native_order, item.native_order is None, item.entity_key),
            )

    @staticmethod
    def _expire_entity(conn: Connection, key: str, reason: str) -> None:
        rows = conn.execute(
            "SELECT input_id FROM app.source_ingress_inputs WHERE entity_key=%s", (key,)
        ).fetchall()
        for row in rows:
            conn.execute("SELECT app.expire_source_input(%s,%s)", (row["input_id"], reason))

    def _insert_item(
        self,
        conn: Connection,
        receipt_id: UUID,
        instance: SourceInstance,
        settings: IngressSettings,
        item: ItemMetadata,
        body: bytes | None,
        grant: Row,
        *,
        context_only: bool,
    ) -> UUID:
        alias = conn.execute(
            "SELECT input_id FROM app.source_ingress_aliases WHERE event_key=%s",
            (item.event.event_key,),
        ).fetchone()
        if alias:
            return UUID(str(alias["input_id"]))
        prior = conn.execute(
            "SELECT input_id FROM app.source_ingress_inputs WHERE version_key=%s",
            (item.version_key,),
        ).fetchone()
        if prior:
            conn.execute(
                "INSERT INTO app.source_ingress_aliases(event_key,fingerprint,input_id,"
                "source_instance_id) VALUES (%s,%s,%s,%s)",
                (
                    item.event.event_key,
                    item.fingerprint(),
                    prior["input_id"],
                    instance.scope.source_instance_id,
                ),
            )
            return UUID(str(prior["input_id"]))
        event, now = item.event, self.now(conn)
        entity = conn.execute(
            "SELECT e.*,EXISTS(SELECT 1 FROM app.source_ingress_inputs i "
            "WHERE i.entity_key=e.entity_key AND i.native_order IS NULL) AS unordered_history "
            "FROM app.source_ingress_entities e WHERE e.entity_key=%s FOR UPDATE OF e",
            (item.entity_key,),
        ).fetchone()
        if entity is None:
            conn.execute(
                "INSERT INTO app.source_ingress_entities(entity_key,source_instance_id,"
                "project_id,author_key) VALUES (%s,%s,%s,%s)",
                (
                    item.entity_key,
                    instance.scope.source_instance_id,
                    self.project,
                    event.sender.key,
                ),
            )
            entity = {
                "head_input_id": None,
                "native_order": None,
                "deleted": False,
                "ambiguous": False,
                "unordered_history": False,
            }
        if (
            entity.get("author_key", event.sender.key) != event.sender.key
            and not item.platform_moderation
        ):
            raise DomainError(
                "INGRESS_ENTITY_AUTHOR", "Verified editor is not this entity author", 403
            )
        unavailable, reason = False, "INGRESS_AVAILABLE"
        if item.operation == "delete" or entity["deleted"]:
            unavailable, reason, context_only = True, "INGRESS_ENTITY_DELETED", True
            self._expire_entity(conn, item.entity_key, reason)
        elif self._order_ambiguous(entity, item):
            unavailable, reason, context_only = True, "INGRESS_UNORDERED_VERSION", True
            self._expire_entity(conn, item.entity_key, reason)
            conn.execute(
                "UPDATE app.source_ingress_entities SET ambiguous=true WHERE entity_key=%s",
                (item.entity_key,),
            )
        elif (
            item.native_order is not None
            and entity["native_order"] is not None
            and item.native_order <= entity["native_order"]
        ):
            # Known canonical versions returned above. Equality here has no canonical
            # proof (e.g. previously denied version), so it cannot become current either.
            unavailable, reason, context_only = True, "INGRESS_OLDER_VERSION", True
        elif entity["head_input_id"] is not None:
            self._expire_entity(conn, item.entity_key, "INGRESS_ENTITY_EDITED")
        copied = event.model_copy(
            update={"received_at": now, "verification_ref": "durable-ingress-receipt"}
        )
        inbox_id = self.identities.registry.stage_metadata(SourceMetadata.from_event(copied))
        stored = ItemMetadata.model_validate(
            {**item.model_dump(mode="json"), "event": copied.model_dump(mode="json")}
        )
        conn.execute(
            "INSERT INTO app.source_ingress_inputs(input_id,inbox_id,receipt_id,"
            "source_instance_id,project_id,entity_key,version_key,version_fingerprint,"
            "operation,native_order,metadata,sender_ref,body_sha256,policy_id,"
            "policy_revision,identity_id,identity_revision,context_only,consumed_at,"
            "unavailable_at,reason_code,metadata_expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                inbox_id,
                inbox_id,
                receipt_id,
                instance.scope.source_instance_id,
                self.project,
                item.entity_key,
                item.version_key,
                item.fingerprint(version=True),
                item.operation,
                item.native_order,
                Jsonb(stored.model_dump(mode="json")),
                Jsonb(event.sender.model_dump(mode="json")),
                event.body_ref,
                grant["policy_id"],
                grant["policy_revision"],
                grant["identity_id"],
                grant["identity_revision"],
                context_only or item.operation != "create",
                now if item.operation == "delete" else None,
                now if unavailable else None,
                reason,
                now + timedelta(seconds=settings.metadata_ttl_seconds),
            ),
        )
        conn.execute(
            "INSERT INTO app.source_ingress_aliases(event_key,fingerprint,input_id,"
            "source_instance_id) VALUES (%s,%s,%s,%s)",
            (event.event_key, item.fingerprint(), inbox_id, instance.scope.source_instance_id),
        )
        if item.operation == "delete":
            conn.execute(
                "UPDATE app.source_ingress_entities SET deleted=true,head_input_id=%s,"
                "updated_at=now() WHERE entity_key=%s",
                (inbox_id, item.entity_key),
            )
        elif not unavailable:
            conn.execute(
                "UPDATE app.source_ingress_entities SET head_input_id=%s,native_order=%s,"
                "updated_at=now() WHERE entity_key=%s",
                (inbox_id, item.native_order, item.entity_key),
            )
        if (
            body is not None
            and grant["retain_body"]
            and not unavailable
            and grant["body_expires_at"] > now
        ):
            if self.cipher is None:
                raise DomainError(
                    "INGRESS_KEY_REQUIRED",
                    "Configure a server content cipher before retaining source bodies",
                    503,
                )
            ciphertext = self.cipher.encrypt(body, str(inbox_id))
            used = conn.execute(
                "SELECT coalesce(sum(octet_length(b.ciphertext)),0) AS n FROM "
                "app.source_ingress_bodies b JOIN app.source_ingress_inputs i "
                "USING(input_id) WHERE i.source_instance_id=%s",
                (instance.scope.source_instance_id,),
            ).fetchone()
            assert used is not None
            if used["n"] + len(ciphertext) > settings.max_retained_bytes:
                raise DomainError(
                    "INGRESS_BODY_CAPACITY", "Retained ciphertext quota exhausted; no ACK", 503
                )
            conn.execute(
                "INSERT INTO app.source_ingress_bodies(input_id,ciphertext,plaintext_bytes,"
                "expires_at) VALUES (%s,%s,%s,%s)",
                (inbox_id, ciphertext, len(body), grant["body_expires_at"]),
            )
        return inbox_id

    def submit(
        self,
        instance: SourceInstance,
        generation: int,
        delivery: VerifiedDelivery,
        *,
        permit: BufferPermit | None = None,
        historical: bool = False,
        checkpoint: Callable[[str], None] | None = None,
    ) -> IngressReceipt:
        """Return only after a successful outer commit. Fault hooks are trusted test composition."""
        self.validate(instance, delivery, buffered=permit is not None)
        meta, instance_id = delivery.metadata, instance.scope.source_instance_id
        delivery_key = digest([instance.scope.key, meta.delivery_id])
        result: IngressReceipt
        with self.root_transaction() as conn:
            control, settings = self._control(conn, instance_id)
            current = self.identities.registry._instance(conn, instance_id)
            if (
                current != instance
                or control["generation"] != generation
                or settings.owner != "gateway"
            ):
                raise DomainError(
                    "INGRESS_OWNER_FENCED", "Source configuration or ingress owner changed", 409
                )
            self._purge(conn, instance_id)
            prior = conn.execute(
                "SELECT * FROM app.source_ingress_deliveries WHERE delivery_key=%s", (delivery_key,)
            ).fetchone()
            normalized = self._normalized(delivery)
            if prior:
                if (
                    prior["payload_sha256"] != meta.payload_sha256
                    or prior["normalized_sha256"] != normalized
                ):
                    self._conflict(
                        conn,
                        instance_id,
                        delivery_key,
                        meta.payload_sha256,
                        "INGRESS_DELIVERY_CONFLICT",
                    )
                    result = IngressReceipt(
                        receipt_id=prior["receipt_id"],
                        source_instance_id=instance_id,
                        delivery_key=delivery_key,
                        disposition="conflict",
                        reason_code="INGRESS_DELIVERY_CONFLICT",
                        committed=True,
                    )
                else:
                    result = self._receipt(prior, duplicate=True)
            else:
                result = self._new_delivery(
                    conn,
                    instance,
                    settings,
                    generation,
                    delivery,
                    delivery_key,
                    normalized,
                    permit=permit,
                    historical=historical,
                    checkpoint=checkpoint,
                )
            if checkpoint:
                checkpoint("before_commit")
        if checkpoint:
            checkpoint("after_commit")
        return result

    def _new_delivery(
        self,
        conn: Connection,
        instance: SourceInstance,
        settings: IngressSettings,
        generation: int,
        delivery: VerifiedDelivery,
        delivery_key: str,
        normalized: str,
        *,
        permit: BufferPermit | None,
        historical: bool,
        checkpoint: Callable[[str], None] | None,
    ) -> IngressReceipt:
        meta, instance_id, now = (
            delivery.metadata,
            instance.scope.source_instance_id,
            self.now(conn),
        )
        count = conn.execute(
            "SELECT count(*) AS n FROM app.source_ingress_deliveries WHERE source_instance_id=%s",
            (instance_id,),
        ).fetchone()
        assert count is not None
        conflict = self._item_conflict(conn, meta.items)
        if meta.cursor is not None:
            cursor_prior = conn.execute(
                "SELECT d.payload_sha256 FROM app.source_ingress_cursor_marks m "
                "JOIN app.source_ingress_deliveries d USING(receipt_id) "
                "WHERE m.stream_key=%s AND m.epoch=%s AND m.sequence=%s",
                (
                    stream_key(instance, meta.cursor.stream_id),
                    meta.cursor.epoch,
                    meta.cursor.sequence,
                ),
            ).fetchone()
            if cursor_prior is not None and cursor_prior["payload_sha256"] != meta.payload_sha256:
                conflict = conflict or "INGRESS_CURSOR_CONFLICT"
        grants = [
            self._receive(conn, settings, instance, item, historical=historical)
            for item in meta.items
        ]
        if conflict is None:
            for item, (grant, _) in zip(meta.items, grants, strict=True):
                self._privacy_delete(conn, item, denied=grant is None)
        if count["n"] >= settings.max_receipts:
            return IngressReceipt(
                receipt_id=None,
                source_instance_id=instance_id,
                delivery_key=delivery_key,
                disposition="backpressure",
                reason_code="INGRESS_RECEIPT_CAPACITY",
                committed=False,
            )
        if any(
            body is not None and len(body) > settings.max_body_bytes for body in delivery.bodies
        ):
            raise DomainError(
                "INGRESS_BODY_BOUND", "Source body exceeds configured byte limit", 413
            )
        if permit is not None:
            revisions = tuple(
                (str(grant["identity_id"]), int(grant["identity_revision"]))
                for grant, _ in grants
                if grant
            )
            if (
                permit.generation != generation
                or permit.expires_at <= now
                or revisions != permit.identity_revisions
            ):
                raise DomainError(
                    "INGRESS_BUFFER_AUTHORITY_STALE",
                    "Buffered authority expired or changed; discard content",
                    403,
                )
        pending = conn.execute(
            "SELECT count(*) AS n FROM app.source_ingress_inputs WHERE "
            "source_instance_id=%s AND consumed_at IS NULL AND unavailable_at IS NULL",
            (instance_id,),
        ).fetchone()
        assert pending is not None
        unique = {
            item.version_key
            for item, (grant, _) in zip(meta.items, grants, strict=True)
            if grant and item.operation != "delete"
        }
        existing = conn.execute(
            "SELECT count(*) AS n FROM app.source_ingress_inputs WHERE version_key=ANY(%s)",
            (list(unique),),
        ).fetchone()
        assert existing is not None
        if pending["n"] + len(unique) - existing["n"] > settings.max_pending:
            return IngressReceipt(
                receipt_id=None,
                source_instance_id=instance_id,
                delivery_key=delivery_key,
                disposition="backpressure",
                reason_code="INGRESS_PENDING_CAPACITY",
                committed=False,
            )
        original = None
        if settings.legacy_repository is not None:
            original = conn.execute(
                "SELECT e.* FROM app.trigger_events e JOIN app.threads h USING(thread_id) "
                "WHERE e.source='github' AND e.external_event_id=%s AND h.project_id=%s "
                "AND h.repository=%s",
                (meta.delivery_id, self.project, settings.legacy_repository.lower()),
            ).fetchone()
            if original is not None:
                if (
                    len(meta.items) != 1
                    or hashlib.sha256(str(original["payload"].get("body", "")).encode()).hexdigest()
                    != meta.items[0].event.body_ref
                ):
                    conflict = "INGRESS_LEGACY_DELIVERY_CONFLICT"
        receipt_id = uuid4()
        disposition = "conflict" if conflict else "legacy_owned" if original else "accepted"
        conn.execute(
            "INSERT INTO app.source_ingress_deliveries(receipt_id,source_instance_id,"
            "project_id,delivery_key,delivery_id,payload_sha256,normalized_sha256,"
            "generation,disposition,reason_code,original_trigger_id,metadata_expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                receipt_id,
                instance_id,
                self.project,
                delivery_key,
                meta.delivery_id,
                meta.payload_sha256,
                normalized,
                generation,
                disposition,
                conflict or "INGRESS_COMMITTED",
                original["event_id"] if original else None,
                now + timedelta(seconds=settings.metadata_ttl_seconds),
            ),
        )
        ids, denied = [], 0
        if conflict:
            self._conflict(conn, instance_id, delivery_key, meta.payload_sha256, conflict)
        elif original is None:
            gap = apply_cursor(conn, instance, meta.cursor, receipt_id)
            for item, body, (grant, _reason) in zip(
                meta.items, delivery.bodies, grants, strict=True
            ):
                if grant is None:
                    self._privacy_delete(conn, item, denied=True, allow_new=True)
                    denied += 1
                    continue
                event = item.event
                old = (
                    not now - timedelta(seconds=settings.live_age_seconds)
                    <= event.occurred_at
                    <= now + timedelta(seconds=30)
                )
                context = (
                    historical
                    or permit is not None
                    or event.historical_backfill
                    or old
                    or gap
                    or grant["mode"] in {"disabled", "shadow"}
                )
                identifier = self._insert_item(
                    conn, receipt_id, instance, settings, item, body, grant, context_only=context
                )
                if identifier not in ids:
                    ids.append(identifier)
            if not ids:
                disposition = "rejected"
        reason = conflict or (
            "INGRESS_LEGACY_RECEIPT"
            if original is not None
            else "INGRESS_COMMITTED"
            if ids
            else "INGRESS_RECEIVE_DENIED"
        )
        conn.execute(
            "UPDATE app.source_ingress_deliveries SET input_ids=%s,denied_count=%s,"
            "disposition=%s,reason_code=%s,sealed=true WHERE receipt_id=%s",
            (Jsonb([str(v) for v in ids]), denied, disposition, reason, receipt_id),
        )
        if checkpoint:
            checkpoint("after_input_write")
        saved = conn.execute(
            "SELECT * FROM app.source_ingress_deliveries WHERE receipt_id=%s", (receipt_id,)
        ).fetchone()
        assert saved is not None
        return self._receipt(saved)

    def _purge(self, conn: Connection, instance_id: UUID) -> Row:
        expired = conn.execute(
            "SELECT b.input_id FROM app.source_ingress_bodies b JOIN "
            "app.source_ingress_inputs i USING(input_id) WHERE i.source_instance_id=%s AND "
            "b.expires_at<=clock_timestamp()",
            (instance_id,),
        ).fetchall()
        for row in expired:
            conn.execute(
                "SELECT app.expire_source_input(%s,'INGRESS_BODY_EXPIRED')", (row["input_id"],)
            )
        old = conn.execute(
            "SELECT input_id,inbox_id FROM app.source_ingress_inputs WHERE "
            "source_instance_id=%s AND metadata_expires_at<=clock_timestamp() AND metadata "
            "IS NOT NULL",
            (instance_id,),
        ).fetchall()
        for row in old:
            conn.execute(
                "SELECT app.expire_source_input(%s,'INGRESS_METADATA_EXPIRED')", (row["input_id"],)
            )
            conn.execute(
                "UPDATE app.source_ingress_inputs SET metadata=NULL,sender_ref=NULL WHERE "
                "input_id=%s",
                (row["input_id"],),
            )
            if row["inbox_id"] is not None:
                conn.execute(
                    "DELETE FROM app.source_inbox WHERE inbox_id=%s AND NOT EXISTS (SELECT "
                    "1 FROM app.source_business_links WHERE inbox_id=%s)",
                    (row["inbox_id"], row["inbox_id"]),
                )
        conn.execute(
            "UPDATE app.source_ingress_deliveries SET delivery_id=NULL WHERE "
            "source_instance_id=%s AND metadata_expires_at<=clock_timestamp()",
            (instance_id,),
        )
        return {"expired_bodies": len(expired), "expired_metadata": len(old)}

    def purge(self, cookie: str, instance_id: UUID) -> Row:
        return self._manage(
            cookie,
            instance_id,
            "ingress_retention_purge",
            lambda conn, user: self._purge(conn, instance_id),
        )

    def _readable(self, conn: Connection, row: Row) -> tuple[ItemMetadata, Row]:
        if (
            row["unavailable_at"] is not None
            or row["metadata"] is None
            or row["metadata_expires_at"] <= self.now(conn)
        ):
            raise DomainError(
                "INGRESS_CONTENT_UNAVAILABLE",
                "Source content is superseded, revoked or expired",
                403,
            )
        instance = self.identities.registry._instance(conn, row["source_instance_id"])
        _, settings = self._control(conn, row["source_instance_id"])
        if settings.owner != "gateway":
            raise DomainError("INGRESS_OWNER_FENCED", "Source owner is no longer the gateway", 403)
        item = ItemMetadata.model_validate(row["metadata"])
        grant, _ = self._receive(conn, settings, instance, item)
        if (
            grant is None
            or grant["identity_revision"] != row["identity_revision"]
            or grant["policy_id"] != row["policy_id"]
            or grant["policy_revision"] != row["policy_revision"]
        ):
            raise DomainError(
                "INGRESS_CURRENT_RECEIVE_DENIED",
                "Current Receive authority does not allow this source content",
                403,
            )
        entity = conn.execute(
            "SELECT * FROM app.source_ingress_entities WHERE entity_key=%s", (row["entity_key"],)
        ).fetchone()
        if (
            entity is None
            or entity["deleted"]
            or entity["ambiguous"]
            or entity["head_input_id"] != row["input_id"]
        ):
            raise DomainError(
                "INGRESS_CONTENT_NOT_CURRENT",
                "Source entity does not have this current version",
                403,
            )
        return item, grant

    def body(self, cookie: str, input_id: UUID) -> bytes:
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            row = conn.execute(
                "SELECT * FROM app.source_ingress_inputs WHERE input_id=%s AND project_id=%s",
                (input_id, self.project),
            ).fetchone()
            if row is None:
                raise DomainError("INGRESS_INPUT_NOT_FOUND", "Input not found in this project", 404)
            self._readable(conn, row)
            body = conn.execute(
                "SELECT ciphertext FROM app.source_ingress_bodies WHERE input_id=%s AND "
                "expires_at>clock_timestamp()",
                (input_id,),
            ).fetchone()
            if body is None or self.cipher is None:
                raise DomainError(
                    "INGRESS_BODY_NOT_RETAINED", "No current retained body is available", 404
                )
            clear = self.cipher.decrypt(bytes(body["ciphertext"]), str(input_id))
            if hashlib.sha256(clear).hexdigest() != row["body_sha256"]:
                raise DomainError("INGRESS_BODY_HASH", "Retained source body failed integrity", 403)
            return clear

    def inspect(self, cookie: str, instance_id: UUID, kind: str) -> list[Row]:
        targets = {
            "receipts": "source_ingress_deliveries",
            "inputs": "source_ingress_inputs",
            "gaps": "source_ingress_gaps",
            "cursors": "source_ingress_cursors",
            "conflicts": "source_ingress_conflicts",
            "audit": "source_ingress_audit",
        }
        if kind not in targets:
            raise DomainError("INGRESS_VIEW", "Unknown ingress view", 404)

        def query(conn: Connection, user: UUID) -> list[Row]:
            rows = conn.execute(
                "SELECT * FROM app." + targets[kind] + " WHERE source_instance_id=%s LIMIT 100",
                (instance_id,),
            ).fetchall()
            # No raw body, ciphertext, sender or canonical event payload in ordinary views.
            return [
                {key: value for key, value in row.items() if key not in {"metadata", "sender_ref"}}
                for row in rows
            ]

        return self._manage(cookie, instance_id, "ingress_" + kind + "_read", query)

    def replay(self, cookie: str, instance_id: UUID, request: ReplayRequest) -> Row:
        def check(conn: Connection, user: UUID) -> Row:
            control, settings = self._control(conn, instance_id)
            if control["generation"] != request.expected_generation or settings.owner != "gateway":
                raise DomainError(
                    "INGRESS_OWNER_FENCED", "Replay requires the current ingress owner", 409
                )
            results = []
            for input_id in request.input_ids:
                row = conn.execute(
                    "SELECT * FROM app.source_ingress_inputs WHERE input_id=%s AND "
                    "source_instance_id=%s AND project_id=%s",
                    (input_id, instance_id, self.project),
                ).fetchone()
                if row is None:
                    raise DomainError(
                        "INGRESS_INPUT_NOT_FOUND", "Replay input not found in this source", 404
                    )
                try:
                    self._readable(conn, row)
                    status = (
                        "already_consumed"
                        if row["consumed_at"] is not None
                        else "revalidated_context"
                    )
                    if row["consumed_at"] is None:
                        conn.execute(
                            "UPDATE app.source_ingress_inputs SET context_only=true WHERE "
                            "input_id=%s",
                            (input_id,),
                        )
                except DomainError:
                    status = "blocked_current_authority_or_retention"
                results.append({"input_id": input_id, "status": status, "context_only": True})
            self._audit(
                conn,
                instance_id,
                "ingress_replay_checked",
                user,
                review_ref=request.review_ref,
                count=len(results),
            )
            return {"new_business_inputs": 0, "items": results, "executes_tasks": False}

        return self._manage(cookie, instance_id, "ingress_replay", check)

    def gap(self, instance_id: UUID, declaration: GapDeclaration) -> UUID:
        with self.root_transaction() as conn:
            instance = self.identities.registry._instance(conn, instance_id)
            identifier = record_gap(conn, instance, declaration)
            self._audit(
                conn,
                instance_id,
                "ingress_gap_observed",
                gap_id=str(identifier),
                reason_code=declaration.reason,
            )
            return identifier

    def reset_cursor(
        self, cookie: str, instance_id: UUID, stream_id: str, request: CursorReset
    ) -> Row:
        def change(conn: Connection, user: UUID) -> Row:
            instance = self.identities.registry._instance(conn, instance_id)
            key = stream_key(instance, stream_id)
            row = conn.execute(
                "SELECT * FROM app.source_ingress_cursors WHERE stream_key=%s FOR UPDATE", (key,)
            ).fetchone()
            actual = row["revision"] if row else 0
            if actual != request.expected_revision:
                raise DomainError(
                    "INGRESS_CURSOR_REVISION", "Cursor changed; reload before reset", 409
                )
            conn.execute(
                "INSERT INTO app.source_ingress_cursors(stream_key,source_instance_id,"
                "project_id,stream_id,epoch) VALUES (%s,%s,%s,%s,%s) ON "
                "CONFLICT(stream_key) DO UPDATE SET epoch=EXCLUDED.epoch,"
                "revision=app.source_ingress_cursors.revision+1,committed_sequence=0,"
                "highest_sequence=0,cursor_value=NULL",
                (key, instance_id, self.project, stream_id, request.epoch),
            )
            record_gap(
                conn,
                instance,
                GapDeclaration(
                    stream_id=stream_id,
                    epoch=request.epoch,
                    reason="cursor_reset",
                    started_at=self.now(conn),
                ),
            )
            self._audit(
                conn, instance_id, "ingress_cursor_reset", user, review_ref=request.review_ref
            )
            return {"revision": actual + 1, "gap_recovered": False}

        return self._manage(cookie, instance_id, "ingress_cursor_reset", change)
