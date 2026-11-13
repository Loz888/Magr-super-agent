"""Source-native, reference-only subscriptions on the original Engine connection.

This companion mailbox never writes legacy Trigger/Mailbox/RunInputs, a Run's
question, input revision or legacy cursor. It never starts/reopens a Run, invokes
a model or grants tools. Bodies are freshly authorized and materialized only in
memory. An existing live Run additionally needs its original Source model bridge.
"""

from __future__ import annotations

import builtins
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.super_agent.fast_path_rules import parse_intent
from mgar_v3.super_agent.policy_domain import PolicyFacts
from mgar_v3.super_agent.source_correlation import SourceCorrelations
from mgar_v3.super_agent.source_correlation_domain import (
    CorrelationInputRef,
    TargetRef,
    snapshot_hash,
)
from mgar_v3.super_agent.source_correlation_rules import exact_url_resources
from mgar_v3.super_agent.source_subscription_domain import (
    MAX_HANDBACK_CYCLES,
    MAX_PAGE_BYTES,
    MAX_PAGE_ITEMS,
    MaterializedSourceItem,
    MaterializedSourcePage,
    SourceConsumptionReceipt,
    SourceDeliveryRef,
    SourceIntakeSnapshot,
    SourceSubscription,
    SubscriptionAckRequest,
    SubscriptionAnchor,
    SubscriptionCloseRequest,
    SubscriptionDispatchRequest,
    SubscriptionHandbackRequest,
    SubscriptionHeadRef,
    SubscriptionInputFacts,
    SubscriptionPrepareRequest,
    SubscriptionRebindRequest,
    SubscriptionRenewRequest,
    SubscriptionRole,
    SubscriptionRunFacts,
    SubscriptionSaveRequest,
)
from mgar_v3.super_agent.source_subscription_rules import classify, match_anchor, validate_ack

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.super_agent.source_correlation import _Input

Row = dict[str, Any]
TERMINAL = {"succeeded", "failed", "cancelled"}
MAX_SUBSCRIPTIONS = 128
MAX_PENDING = 64
MAX_INTAKES = 256


def _error(code: str, status: int = 409) -> DomainError:
    return DomainError(code, "Source subscription requires current exact references", status)


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _decode[T: Model](model: type[T], raw: Any) -> T:
    try:
        return model.model_validate_json(json.dumps(raw, default=str))
    except (ValueError, TypeError, RecursionError):
        raise _error("SOURCE_SUBSCRIPTION_FACT_INVALID", 403) from None


def _bound(left: TargetRef, right: TargetRef, *, same_run: bool = True) -> bool:
    """Epoch/thread revision fence snapshots, not a subscription's stable identity."""
    fields = ("ticket_id", "thread_id", "mandate_id", "mandate_sha256", "project_id", "repository")
    return all(getattr(left, field) == getattr(right, field) for field in fields) and (
        not same_run or left.run_id == right.run_id
    )


class SourceSubscriptions:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.correlations = SourceCorrelations(engine)
        self.identities = self.correlations.identities

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    def _now(self, conn: Connection) -> datetime:
        return self.engine.source_ingress.now(conn)

    def _conn(self, conn: Connection) -> None:
        if conn is not self.engine.connection or (
            conn.info.transaction_status != TransactionStatus.INTRANS
        ):
            raise _error("SOURCE_SUBSCRIPTION_ORIGINAL_TRANSACTION_REQUIRED", 403)

    @staticmethod
    def _ready(conn: Connection) -> bool:
        row = conn.execute(
            "SELECT to_regclass('app.sa_source_subscriptions') IS NOT NULL AS ready"
        ).fetchone()
        return bool(row and row["ready"])

    def _console(self, conn: Connection, cookie: str) -> UUID:
        self._conn(conn)
        user = self.identities._console(conn, cookie)
        if not self._ready(conn):
            raise _error("SOURCE_SUBSCRIPTION_NOT_MIGRATED")
        return user

    def _record(self, conn: Connection, subscription_id: UUID) -> tuple[Row, SourceSubscription]:
        row = conn.execute(
            "SELECT v.*,h.revision AS current_revision FROM app.sa_source_subscriptions h "
            "JOIN app.sa_source_subscription_versions v ON v.subscription_id=h.subscription_id "
            "AND v.revision=h.revision WHERE h.subscription_id=%s AND h.project_id=%s "
            "FOR UPDATE OF h",
            (subscription_id, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_SUBSCRIPTION_NOT_FOUND", 404)
        if digest(row["payload"]) != row["payload_sha256"]:
            raise _error("SOURCE_SUBSCRIPTION_HISTORY_INVALID", 403)
        value = _decode(SourceSubscription, row["payload"])
        if (
            value.project_id != self.project
            or value.subscription_id != subscription_id
            or value.revision != row["revision"]
        ):
            raise _error("SOURCE_SUBSCRIPTION_HISTORY_INVALID", 403)
        return row, value

    @staticmethod
    def _cas(value: SourceSubscription, expected: int) -> None:
        if value.revision != expected:
            raise _error("SOURCE_SUBSCRIPTION_REVISION_CONFLICT")

    @staticmethod
    def _head_token(row: Row) -> str:
        value = row["payload_sha256"]
        if not isinstance(value, str):
            raise _error("SOURCE_SUBSCRIPTION_HISTORY_INVALID", 403)
        return value

    def _head_request(self, row: Row, value: SourceSubscription, request: Any) -> None:
        self._cas(value, request.expected_revision)
        if request.snapshot_sha256 != self._head_token(row):
            raise _error("SOURCE_SUBSCRIPTION_HEAD_STALE")

    def _target(self, conn: Connection, run_id: UUID) -> tuple[TargetRef, Row]:
        scoped = conn.execute(
            "SELECT 1 FROM app.runs r JOIN app.tickets t USING(ticket_id) "
            "JOIN app.threads h USING(thread_id) WHERE r.run_id=%s AND h.project_id=%s",
            (run_id, self.project),
        ).fetchone()
        if scoped is None:
            raise _error("SOURCE_SUBSCRIPTION_TARGET_NOT_FOUND", 404)
        run = self.engine._run(conn, run_id)
        mandate = conn.execute(
            "SELECT to_jsonb(m) AS mandate FROM app.mandates m "
            "WHERE mandate_id=%s AND project_id=%s",
            (run["mandate_id"], self.project),
        ).fetchone()
        if mandate is None or run["repository"].lower() != self.engine.settings.repository.lower():
            raise _error("SOURCE_SUBSCRIPTION_TARGET_DENIED", 403)
        return TargetRef(
            ticket_id=run["ticket_id"],
            run_id=run_id,
            thread_id=run["thread_id"],
            thread_revision=run["thread_revision"],
            execution_epoch=run["execution_epoch"],
            mandate_id=run["mandate_id"],
            mandate_sha256=digest(mandate["mandate"]),
            project_id=self.project,
            repository=run["repository"].lower(),
            allowed=True,
        ), run

    def _correlation(self, conn: Connection, value: SourceSubscription) -> Row:
        row = self.correlations._record(conn, value.anchor.correlation_id)
        target = _decode(TargetRef, row["payload"]["target"])
        if (
            row["revision"] != value.anchor.correlation_revision
            or row["payload"]["state"] != "bound"
            or not _bound(target, value.target, same_run=False)
            or value.anchor.resource_key not in row["payload"]["resource_keys"]
        ):
            raise _error("SOURCE_SUBSCRIPTION_CORRELATION_STALE", 403)
        return row

    def _config(self, conn: Connection, value: SourceSubscription) -> None:
        anchor = value.anchor
        instance = self.identities.registry._instance(conn, anchor.source_instance_id)
        control, settings = self.engine.source_ingress._control(conn, anchor.source_instance_id)
        policy = self.engine.source_policies._current(conn, anchor.policy_id)
        if (
            instance.scope.key != anchor.scope_sha256
            or instance.revision != anchor.source_revision
            or control["generation"] != anchor.generation
            or settings.owner != "gateway"
            or settings.policy_id != anchor.policy_id
            or settings.policy_revision != anchor.policy_revision
            or policy["current_revision"] != anchor.policy_revision
            or policy["revoked"]
        ):
            raise _error("SOURCE_SUBSCRIPTION_SOURCE_FENCED", 403)

    def _lease(self, conn: Connection, value: SourceSubscription) -> tuple[TargetRef, Row]:
        if value.state != "active" or value.expires_at <= self._now(conn):
            raise _error("SOURCE_SUBSCRIPTION_INACTIVE", 403)
        self._config(conn, value)
        self._correlation(conn, value)
        target, run = self._target(conn, value.target.run_id)
        if not _bound(target, value.target):
            raise _error("SOURCE_SUBSCRIPTION_TARGET_STALE", 403)
        self.engine.source_policies.check_run(conn, run)
        return target, run

    def _seed(self, conn: Connection, input_id: UUID, expected_hash: str) -> tuple[_Input, Any]:
        snapshot, loaded = self.correlations._build(conn, (input_id,))
        if snapshot_hash(snapshot) != expected_hash:
            raise _error("SOURCE_SUBSCRIPTION_SNAPSHOT_STALE")
        return loaded[input_id], snapshot

    def _anchor(
        self,
        conn: Connection,
        seed: _Input,
        snapshot: Any,
        correlation_id: UUID,
        correlation_revision: int,
        resource_key: str,
        selector_kind: Any,
    ) -> SubscriptionAnchor:
        proofs = [
            proof
            for proof in snapshot.resources
            if proof.input_id == seed.ref.input_id
            and proof.resource.resource_key == resource_key
            and proof.proof_kind in {"verified_resource", "signed_alias"}
        ]
        if len(proofs) != 1:
            raise _error("SOURCE_SUBSCRIPTION_RESOURCE_UNPROVEN", 403)
        thread = seed.item.event.source_thread
        if selector_kind == "native_topic" and thread.topic_key is None:
            raise _error("SOURCE_SUBSCRIPTION_TOPIC_REQUIRED", 422)
        control, _ = self.engine.source_ingress._control(conn, seed.ref.source_instance_id)
        instance = self.identities.registry._instance(conn, seed.ref.source_instance_id)
        return SubscriptionAnchor(
            source_instance_id=seed.ref.source_instance_id,
            scope_sha256=thread.scope.key,
            conversation_sha256=thread.conversation_key,
            topic_sha256=thread.topic_key,
            resource_key=resource_key,
            resource=proofs[0].resource,
            correlation_id=correlation_id,
            correlation_revision=correlation_revision,
            selector_kind=selector_kind,
            seed_input_id=seed.ref.input_id,
            seed_version_fingerprint=seed.ref.version_fingerprint,
            seed_entity_sha256=seed.row["entity_key"],
            source_revision=instance.revision,
            generation=control["generation"],
            policy_id=seed.row["policy_id"],
            policy_revision=seed.row["policy_revision"],
        )

    @staticmethod
    def _selector_equal(left: SubscriptionAnchor, right: SubscriptionAnchor) -> bool:
        fields = (
            "source_instance_id",
            "scope_sha256",
            "conversation_sha256",
            "topic_sha256",
            "resource_key",
            "correlation_id",
            "correlation_revision",
            "selector_kind",
        )
        return all(getattr(left, field) == getattr(right, field) for field in fields) and (
            left.selector_kind != "reply_chain"
            or left.seed_entity_sha256 == right.seed_entity_sha256
        )

    def _value(
        self,
        conn: Connection,
        subscription_id: UUID,
        revision: int,
        anchor: SubscriptionAnchor,
        target: TargetRef,
        role: SubscriptionRole,
        seed: _Input,
        ttl: int,
    ) -> SourceSubscription:
        self.correlations._context(seed, target)
        at = self._now(conn)
        expiry = min(at + timedelta(seconds=ttl), seed.definition.valid_until)
        if expiry <= at:
            raise _error("SOURCE_SUBSCRIPTION_EXPIRED", 403)
        return SourceSubscription(
            subscription_id=subscription_id,
            project_id=self.project,
            revision=revision,
            anchor=anchor,
            target=target,
            role=role,
            state="active",
            valid_from=at,
            expires_at=expiry,
            authority_sha256=digest(
                [
                    anchor.model_dump(mode="json"),
                    target.model_dump(mode="json"),
                    role,
                    revision,
                    at.isoformat(),
                    expiry.isoformat(),
                    seed.ref.state_sha256,
                ]
            ),
        )

    def _append(self, conn: Connection, value: SourceSubscription, user: UUID, review: str) -> Row:
        payload = value.model_dump(mode="json")
        target, anchor = value.target, value.anchor
        conn.execute(
            "INSERT INTO app.sa_source_subscription_versions(subscription_id,revision,"
            "previous_revision,project_id,source_instance_id,correlation_id,correlation_revision,"
            "seed_input_id,version_fingerprint,thread_id,ticket_id,run_id,mandate_id,"
            "mandate_sha256,role,state,expires_at,payload,payload_sha256,review_sha256,signed_by) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                value.subscription_id,
                value.revision,
                value.revision - 1 if value.revision > 1 else None,
                self.project,
                anchor.source_instance_id,
                anchor.correlation_id,
                anchor.correlation_revision,
                anchor.seed_input_id,
                anchor.seed_version_fingerprint,
                target.thread_id,
                target.ticket_id,
                target.run_id,
                target.mandate_id,
                target.mandate_sha256,
                value.role,
                value.state,
                value.expires_at,
                Jsonb(payload),
                digest(payload),
                digest(review),
                user,
            ),
        )
        result = conn.execute(
            "UPDATE app.sa_source_subscriptions SET revision=%s WHERE subscription_id=%s "
            "AND project_id=%s AND revision=%s RETURNING revision",
            (value.revision, value.subscription_id, self.project, value.revision - 1),
        ).fetchone()
        if result is None:
            raise _error("SOURCE_SUBSCRIPTION_REVISION_CONFLICT")
        return self._view(*self._record(conn, value.subscription_id))

    def _view(self, row: Row, value: SourceSubscription) -> Row:
        return {
            "subscription_id": value.subscription_id,
            "revision": value.revision,
            "state": value.state,
            "role": value.role,
            "target": value.target.model_dump(mode="json"),
            "subscription": value.model_dump(mode="json"),
            "snapshot_sha256": self._head_token(row),
            "expires_at": value.expires_at,
            "eligible_for_execution": False,
            "executes_tasks": False,
            "calls_model": False,
        }

    def save(self, cookie: str, subscription_id: UUID, request: SubscriptionSaveRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            existing = conn.execute(
                "SELECT 1 FROM app.sa_source_subscriptions WHERE subscription_id=%s",
                (subscription_id,),
            ).fetchone()
            if request.expected_revision != 0 or existing:
                raise _error("SOURCE_SUBSCRIPTION_REVISION_CONFLICT")
            seed, snapshot = self._seed(conn, request.seed_input_id, request.snapshot_sha256)
            target, _ = self._target(conn, request.target.run_id)
            if target != request.target or not target.allowed:
                raise _error("SOURCE_SUBSCRIPTION_TARGET_STALE")
            anchor = self._anchor(
                conn,
                seed,
                snapshot,
                request.correlation_id,
                request.expected_correlation_revision,
                request.resource_key,
                request.selector_kind,
            )
            value = self._value(
                conn, subscription_id, 1, anchor, target, request.role, seed, request.ttl_seconds
            )
            self._correlation(conn, value)
            count = conn.execute(
                "SELECT count(*) AS n FROM app.sa_source_subscriptions WHERE project_id=%s "
                "AND source_instance_id=%s",
                (self.project, anchor.source_instance_id),
            ).fetchone()
            if count and count["n"] >= MAX_SUBSCRIPTIONS:
                raise _error("SOURCE_SUBSCRIPTION_CAPACITY")
            self._primary_conflict(conn, value)
            conn.execute(
                "INSERT INTO app.sa_source_subscriptions(subscription_id,project_id,"
                "source_instance_id,correlation_id,seed_input_id) VALUES(%s,%s,%s,%s,%s)",
                (
                    subscription_id,
                    self.project,
                    anchor.source_instance_id,
                    anchor.correlation_id,
                    anchor.seed_input_id,
                ),
            )
            return self._append(conn, value, user, request.review_ref)

    def _primary_conflict(self, conn: Connection, value: SourceSubscription) -> None:
        if value.role != "primary":
            return
        for row in self._source_rows(conn, value.anchor.source_instance_id):
            other = _decode(SourceSubscription, row["payload"])
            if (
                other.subscription_id != value.subscription_id
                and other.state == "active"
                and other.role == "primary"
                and other.expires_at > self._now(conn)
                and self._selector_equal(other.anchor, value.anchor)
            ):
                raise _error("SOURCE_SUBSCRIPTION_PRIMARY_CONFLICT")

    def _renew(
        self,
        conn: Connection,
        user: UUID,
        subscription_id: UUID,
        request: SubscriptionRenewRequest | SubscriptionRebindRequest,
    ) -> Row:
        _, old = self._record(conn, subscription_id)
        self._cas(old, request.expected_revision)
        seed, snapshot = self._seed(conn, request.seed_input_id, request.snapshot_sha256)
        anchor = self._anchor(
            conn,
            seed,
            snapshot,
            old.anchor.correlation_id,
            old.anchor.correlation_revision,
            old.anchor.resource_key,
            old.anchor.selector_kind,
        )
        if not self._selector_equal(old.anchor, anchor):
            raise _error("SOURCE_SUBSCRIPTION_SELECTOR_IMMUTABLE", 403)
        requested = request.target if isinstance(request, SubscriptionRebindRequest) else old.target
        target, _ = self._target(conn, requested.run_id)
        if not _bound(target, old.target, same_run=False):
            raise _error("SOURCE_SUBSCRIPTION_TASK_IMMUTABLE", 403)
        if isinstance(request, SubscriptionRebindRequest) and target != request.target:
            raise _error("SOURCE_SUBSCRIPTION_TARGET_STALE")
        value = self._value(
            conn,
            subscription_id,
            old.revision + 1,
            anchor,
            target,
            old.role,
            seed,
            request.ttl_seconds,
        )
        self._correlation(conn, value)
        self._primary_conflict(conn, value)
        return self._append(conn, value, user, request.review_ref)

    def renew(self, cookie: str, subscription_id: UUID, request: SubscriptionRenewRequest) -> Row:
        with self.engine.transaction() as conn:
            return self._renew(conn, self._console(conn, cookie), subscription_id, request)

    def rebind(self, cookie: str, subscription_id: UUID, request: SubscriptionRebindRequest) -> Row:
        with self.engine.transaction() as conn:
            return self._renew(conn, self._console(conn, cookie), subscription_id, request)

    def _closed(self, old: SourceSubscription, state: Any) -> SourceSubscription:
        return _decode(
            SourceSubscription,
            {
                **old.model_dump(mode="json"),
                "revision": old.revision + 1,
                "state": state,
                "authority_sha256": digest([old.authority_sha256, old.revision + 1, state]),
            },
        )

    def close(self, cookie: str, subscription_id: UUID, request: SubscriptionCloseRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            row, old = self._record(conn, subscription_id)
            self._head_request(row, old, request)
            return self._append(conn, self._closed(old, "unsubscribed"), user, request.review_ref)

    def get(self, cookie: str, subscription_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            row, value = self._record(conn, subscription_id)
            result = self._view(row, value)
            try:
                _, run = self._lease(conn, value)
                result["status"] = "closed_followup" if run["status"] in TERMINAL else "active"
            except DomainError as error:
                result["status"] = "expired" if value.expires_at <= self._now(conn) else "fenced"
                result["reason_code"] = error.code
            return result

    def _source_rows(self, conn: Connection, source_id: UUID) -> builtins.list[Row]:
        rows = conn.execute(
            "SELECT v.* FROM app.sa_source_subscriptions h JOIN "
            "app.sa_source_subscription_versions v ON v.subscription_id=h.subscription_id "
            "AND v.revision=h.revision WHERE h.source_instance_id=%s AND h.project_id=%s "
            "ORDER BY h.subscription_id LIMIT %s",
            (source_id, self.project, MAX_SUBSCRIPTIONS + 1),
        ).fetchall()
        if len(rows) > MAX_SUBSCRIPTIONS:
            raise _error("SOURCE_SUBSCRIPTION_CAPACITY")
        if any(digest(row["payload"]) != row["payload_sha256"] for row in rows):
            raise _error("SOURCE_SUBSCRIPTION_HISTORY_INVALID", 403)
        return rows

    def list(self, cookie: str, source_id: UUID, limit: int = 100) -> Row:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise _error("SOURCE_SUBSCRIPTION_PAGE_INVALID", 422)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            self.identities.registry._instance(conn, source_id)
            rows = self._source_rows(conn, source_id)
            return {
                "subscriptions": [
                    self._view(row, _decode(SourceSubscription, row["payload"]))
                    for row in rows[:limit]
                ],
                "has_more": len(rows) > limit,
                "eligible_for_execution": False,
            }

    def _input_row(self, conn: Connection, input_id: UUID) -> Row:
        row = conn.execute(
            "SELECT * FROM app.source_ingress_inputs WHERE input_id=%s AND project_id=%s",
            (input_id, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_SUBSCRIPTION_INPUT_NOT_FOUND", 404)
        return row

    def _facts(
        self,
        conn: Connection,
        value: SourceSubscription,
        loaded: _Input,
    ) -> SubscriptionInputFacts:
        event = loaded.item.event
        roles, controlled = self.engine.source_policies._roles(
            conn,
            event.sender,
            event.source_thread,
            value.target.ticket_id,
        )
        admitted = self.engine.source_policies._evaluate(
            loaded.definition,
            "admission",
            event.sender,
            event.source_thread,
            PolicyFacts(
                event_kind=event.event_kind,
                request_kind="follow_up",
                ticket_id=value.target.ticket_id,
                historical=event.historical_backfill,
            ),
            roles,
            self._now(conn),
            controlled,
        )
        native = tuple(proof.resource.resource_key for proof in loaded.native)
        keys = native or tuple(
            ref.resource_key
            for ref in exact_url_resources(
                loaded.text,
                (value.anchor.resource,),
            )
        )
        intent = parse_intent(loaded.item, loaded.text)
        purpose = (
            "status_query"
            if loaded.text.strip().startswith("/mgar status")
            else "control"
            if intent.control_action
            else "new_work"
            if intent.request_kind == "explicit_request"
            else "context"
        )
        return SubscriptionInputFacts(
            input=loaded.ref,
            unit_key=digest([str(loaded.ref.input_id), value.anchor.resource_key]),
            scope_sha256=event.scope.key,
            conversation_sha256=event.source_thread.conversation_key,
            topic_sha256=event.source_thread.topic_key,
            resource_keys=tuple(sorted(set(keys))),
            reply_entity_sha256=self._reply_key(conn, value, loaded),
            actor_kind=event.sender.actor_kind,
            context_only=bool(loaded.row["context_only"]),
            historical=event.historical_backfill,
            is_current=True,
            receive_allowed=True,
            read_allowed=True,
            task_controller=controlled and "task_controller" in roles,
            follow_up_allowed=admitted.allowed,
            purpose=cast(Any, purpose),
            expires_at=loaded.expires,
        )

    def _reply_key(
        self,
        conn: Connection,
        value: SourceSubscription,
        loaded: _Input,
    ) -> str | None:
        event = loaded.item.event
        parent = event.reply_to_entity_id
        if parent is None:
            return None
        direct = digest([event.scope.key, loaded.item.entity_namespace, parent])
        if value.anchor.selector_kind != "reply_chain":
            return direct
        seen = {event.entity_id}
        for _ in range(4):
            if parent in seen:
                return direct
            seen.add(parent)
            key = digest([event.scope.key, loaded.item.entity_namespace, parent])
            if key == value.anchor.seed_entity_sha256:
                return key
            row = conn.execute(
                "SELECT i.input_id FROM app.source_ingress_inputs i "
                "JOIN app.source_ingress_entities e ON e.head_input_id=i.input_id "
                "WHERE i.source_instance_id=%s AND i.project_id=%s "
                "AND i.metadata->'event'->>'entity_id'=%s "
                "AND i.metadata->>'entity_namespace'=%s LIMIT 1",
                (loaded.ref.source_instance_id, self.project, parent, loaded.item.entity_namespace),
            ).fetchone()
            if row is None:
                return direct
            try:
                ancestor = self.correlations._input(conn, row["input_id"])
            except DomainError:
                return direct
            if (
                ancestor.item.event.source_thread.conversation_key
                != event.source_thread.conversation_key
            ):
                return direct
            parent = ancestor.item.event.reply_to_entity_id
            if parent is None:
                return direct
        return direct

    def _decision(
        self,
        conn: Connection,
        value: SourceSubscription,
        loaded: _Input,
    ) -> Any:
        target, run = self._lease(conn, value)
        primary = (
            conn.execute(
                "SELECT 1 FROM app.subscriptions WHERE run_id=%s AND thread_id=%s "
                "AND active AND role='primary'",
                (target.run_id, target.thread_id),
            ).fetchone()
            is not None
        )
        facts = self._facts(conn, value, loaded)
        run_facts = SubscriptionRunFacts(
            target=target,
            status=run["status"],
            ticket_open=run["ticket_status"] == "open",
            original_primary=primary,
            authority_current=True,
            policy_mode=loaded.definition.mode,
        )
        return classify(value, facts, run_facts, at=self._now(conn))

    def _matches(
        self,
        conn: Connection,
        loaded: _Input,
    ) -> tuple[builtins.list[SourceSubscription], bool]:
        matched = []
        for row in self._source_rows(conn, loaded.ref.source_instance_id):
            value = _decode(SourceSubscription, row["payload"])
            if match_anchor(value.anchor, self._facts(conn, value, loaded)):
                matched.append(value)
        # Closed/fenced selectors remain minimal follow-up facts, not a new Task.
        identities = {
            (
                value.anchor.correlation_id,
                value.target.ticket_id,
                value.target.thread_id,
                value.target.mandate_id,
            )
            for value in matched
        }
        return matched, len(identities) > 1

    def _route(self, conn: Connection, input_id: UUID) -> Row | None:
        row = conn.execute(
            "SELECT * FROM app.sa_source_subscription_routes WHERE input_id=%s AND project_id=%s",
            (input_id, self.project),
        ).fetchone()
        if row is not None and digest(row["payload"]) != row["payload_sha256"]:
            raise _error("SOURCE_SUBSCRIPTION_ROUTE_INVALID", 403)
        return row

    def _handed_back(self, conn: Connection, row: Row) -> bool:
        handbacks = conn.execute(
            "SELECT x.*,h.revision AS current_revision,v.payload AS current_payload "
            "FROM app.sa_source_subscription_handbacks x "
            "JOIN app.sa_source_subscriptions h USING(subscription_id) "
            "JOIN app.sa_source_subscription_versions v ON v.subscription_id=h.subscription_id "
            "AND v.revision=h.revision WHERE x.input_id=%s AND x.project_id=%s "
            "AND x.version_fingerprint=%s ORDER BY x.created_at,x.handback_id",
            (row["input_id"], self.project, row["version_fingerprint"]),
        ).fetchall()
        if any(h["outcome"] == "loop_blocked" for h in handbacks):
            return False
        for handback in handbacks:
            if handback["current_revision"] != handback["subscription_revision"]:
                continue
            value = _decode(SourceSubscription, handback["current_payload"])
            if handback["outcome"] != "needs_routing" or value.state != "handed_back":
                continue
            try:
                self._config(conn, value)
                self._correlation(conn, value)
                self.correlations._input(conn, row["input_id"])
            except DomainError:
                continue
            return True
        return False

    def handled(self, conn: Connection, row: Row) -> bool:
        """Read-only current gate, including SourceEval's final dispatch recheck."""
        self._conn(conn)
        if not self._ready(conn) or row["project_id"] != self.project:
            return False
        if self._handed_back(conn, row):
            return False  # This does not rewrite an immutable prior FastPath decision.
        if self._route(conn, row["input_id"]) is not None:
            return True
        try:
            loaded = self.correlations._input(conn, row["input_id"])
            matched, _ = self._matches(conn, loaded)
            return bool(matched)
        except DomainError:
            return False  # FastPath's earlier current Receive/body gate still rejects it.

    @staticmethod
    def _route_view(row: Row | None, input_id: UUID) -> Row:
        return {
            "input_id": input_id,
            "outcome": row["outcome"] if row else "unmatched",
            "route": row["payload"] if row else None,
            "eligible_for_execution": False,
            "calls_model": False,
        }

    def _save_route(
        self,
        conn: Connection,
        row: Row,
        outcome: str,
        values: builtins.list[SourceSubscription],
        reason: str,
    ) -> Row:
        payload = {
            "input_id": str(row["input_id"]),
            "version_fingerprint": row["version_fingerprint"],
            "subscription_heads": [
                {"subscription_id": str(value.subscription_id), "revision": value.revision}
                for value in values
            ],
            "outcome": outcome,
            "reason_code": reason,
        }
        conn.execute(
            "INSERT INTO app.sa_source_subscription_routes(input_id,source_instance_id,"
            "project_id,version_fingerprint,outcome,payload,payload_sha256) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(input_id) DO NOTHING",
            (
                row["input_id"],
                row["source_instance_id"],
                self.project,
                row["version_fingerprint"],
                outcome,
                Jsonb(payload),
                digest(payload),
            ),
        )
        result = self._route(conn, row["input_id"])
        assert result is not None
        return result

    def _cursor(self, conn: Connection, run_id: UUID, lane: str) -> int:
        row = conn.execute(
            "SELECT coalesce(max(cursor_to),0) AS cursor FROM "
            "app.sa_source_subscription_consumptions WHERE run_id=%s AND lane=%s "
            "AND project_id=%s",
            (run_id, lane, self.project),
        ).fetchone()
        assert row is not None
        return int(row["cursor"])

    def _deliver(
        self,
        conn: Connection,
        value: SourceSubscription,
        loaded: _Input,
        decision: Any,
    ) -> bool:
        old = conn.execute(
            "SELECT 1 FROM app.sa_source_subscription_deliveries WHERE "
            "(subscription_id=%s OR (run_id=%s AND lane=%s)) AND input_id=%s",
            (value.subscription_id, value.target.run_id, decision.lane, loaded.ref.input_id),
        ).fetchone()
        if old:
            return True
        cursor = self._cursor(conn, value.target.run_id, decision.lane)
        pending = conn.execute(
            "SELECT count(*) AS n FROM app.sa_source_subscription_deliveries WHERE "
            "run_id=%s AND lane=%s AND source_seq>%s",
            (value.target.run_id, decision.lane, cursor),
        ).fetchone()
        if pending is not None and pending["n"] >= MAX_PENDING:
            return False
        sequence = conn.execute(
            "UPDATE app.threads SET latest_seq=latest_seq+1 WHERE thread_id=%s AND "
            "project_id=%s RETURNING latest_seq",
            (value.target.thread_id, self.project),
        ).fetchone()
        assert sequence is not None
        ref = SourceDeliveryRef(
            delivery_id=uuid4(),
            subscription_id=value.subscription_id,
            subscription_revision=value.revision,
            project_id=self.project,
            run_id=value.target.run_id,
            sequence=sequence["latest_seq"],
            input=loaded.ref,
            unit_key=decision.unit_key,
            lane=decision.lane,
            purpose=decision.purpose,
            disposition="ready",
            reason_code=decision.reason_code,
            expires_at=min(value.expires_at, loaded.expires),
        )
        payload = ref.model_dump(mode="json")
        conn.execute(
            "INSERT INTO app.sa_source_subscription_deliveries(delivery_id,subscription_id,"
            "subscription_revision,input_id,source_instance_id,project_id,version_fingerprint,"
            "thread_id,ticket_id,run_id,mandate_id,source_seq,lane,payload,payload_sha256) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                ref.delivery_id,
                value.subscription_id,
                value.revision,
                loaded.ref.input_id,
                loaded.ref.source_instance_id,
                self.project,
                loaded.ref.version_fingerprint,
                value.target.thread_id,
                value.target.ticket_id,
                value.target.run_id,
                value.target.mandate_id,
                ref.sequence,
                ref.lane,
                Jsonb(payload),
                digest(payload),
            ),
        )
        return True

    def accepted_conn(
        self,
        conn: Connection,
        input_id: UUID,
        *,
        receipt_id: UUID | None = None,
    ) -> Row:
        """Only a new canonical from this receipt is automatically dispatched."""
        self._conn(conn)
        if not self._ready(conn):
            return self._route_view(None, input_id)
        row = self._input_row(conn, input_id)
        old = self._route(conn, input_id)
        if receipt_id is not None and row["receipt_id"] != receipt_id:
            return self._route_view(old, input_id)
        if self._handed_back(conn, row):
            return {
                "input_id": input_id,
                "outcome": "needs_routing",
                "eligible_for_execution": False,
                "calls_model": False,
            }
        if old is not None and old["outcome"] != "pending":
            return self._route_view(old, input_id)
        try:
            loaded = self.correlations._input(conn, input_id)
        except DomainError as error:
            if old is None:
                return {
                    "input_id": input_id,
                    "outcome": "blocked",
                    "reason_code": error.code,
                    "eligible_for_execution": False,
                    "calls_model": False,
                }
            return self._route_view(old, input_id)
        matched, ambiguous = self._matches(conn, loaded)
        if not matched:
            return self._route_view(old, input_id)
        if old is not None:
            expected = {
                (head["subscription_id"], head["revision"])
                for head in old["payload"]["subscription_heads"]
            }
            if expected != {(str(value.subscription_id), value.revision) for value in matched}:
                return self._route_view(old, input_id)
        if ambiguous:
            return self._route_view(
                self._save_route(
                    conn,
                    row,
                    "ambiguous",
                    matched,
                    "SOURCE_SUBSCRIPTION_AMBIGUOUS",
                ),
                input_id,
            )
        outcome, reason, delivered = "blocked", "SOURCE_SUBSCRIPTION_INACTIVE", False
        for value in matched:
            try:
                decision = self._decision(conn, value, loaded)
            except DomainError as error:
                if not delivered and outcome != "pending":
                    if value.state != "active" or value.expires_at <= self._now(conn):
                        outcome, reason = "closed_followup", "SOURCE_SUBSCRIPTION_CLOSED_FOLLOWUP"
                    else:
                        outcome, reason = "blocked", error.code
                continue
            if decision.outcome in {"deliver_ref", "observe_ref"}:
                if self._deliver(conn, value, loaded, decision):
                    delivered = True
                    if outcome != "pending":
                        outcome, reason = "delivered", decision.reason_code
                else:
                    outcome, reason = "pending", "SOURCE_SUBSCRIPTION_MAILBOX_FULL"
            elif not delivered and outcome != "pending":
                outcome = {
                    "would_deliver": "shadow",
                    "closed_followup": "closed_followup",
                    "needs_explicit_rebind": "closed_followup",
                }.get(
                    decision.outcome,
                    "blocked",
                )
                reason = decision.reason_code
        route = old or self._save_route(conn, row, outcome, matched, reason)
        result = self._route_view(route, input_id)
        # The original receipt remains immutable when a pending queue recovers.
        result["current_outcome"] = outcome
        return result

    def dispatch(self, cookie: str, request: SubscriptionDispatchRequest) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return {
                "results": [self.accepted_conn(conn, input_id) for input_id in request.input_ids],
                "eligible_for_execution": False,
                "calls_model": False,
            }

    def handback(
        self,
        cookie: str,
        subscription_id: UUID,
        request: SubscriptionHandbackRequest,
    ) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            row, old = self._record(conn, subscription_id)
            self._head_request(row, old, request)
            self._lease(conn, old)
            loaded = [self.correlations._input(conn, input_id) for input_id in request.input_ids]
            if any(not match_anchor(old.anchor, self._facts(conn, old, item)) for item in loaded):
                raise _error("SOURCE_SUBSCRIPTION_HANDBACK_SCOPE", 403)
            key = digest(
                [
                    old.anchor.scope_sha256,
                    old.anchor.resource_key,
                    str(old.target.ticket_id),
                    str(old.anchor.correlation_id),
                ]
            )
            window = self._now(conn) - timedelta(hours=1)
            count = conn.execute(
                "SELECT count(*) AS n FROM app.sa_source_subscription_handbacks WHERE "
                "project_id=%s AND loop_key=%s AND outcome='needs_routing' AND created_at>=%s",
                (self.project, key, window),
            ).fetchone()
            assert count is not None
            outcomes = []
            new_value = self._closed(old, "handed_back")
            self._append(conn, new_value, user, request.review_ref)
            for item in loaded:
                previous = conn.execute(
                    "SELECT * FROM app.sa_source_subscription_handbacks WHERE project_id=%s "
                    "AND loop_key=%s AND input_id=%s AND version_fingerprint=%s LIMIT 1",
                    (self.project, key, item.ref.input_id, item.ref.version_fingerprint),
                ).fetchone()
                if previous:
                    outcomes.append(
                        {
                            "input_id": item.ref.input_id,
                            "outcome": "loop_blocked",
                            "reason_code": "INPUT_ALREADY_HANDED_BACK",
                        }
                    )
                    continue
                allowed = count["n"] < MAX_HANDBACK_CYCLES
                outcome = "needs_routing" if allowed else "loop_blocked"
                reason = request.reason_code if allowed else "HANDBACK_CYCLE_LIMIT"
                payload = {
                    "input_id": str(item.ref.input_id),
                    "reason_code": reason,
                    "semantic_evaluation": "NOT_RUN",
                    "eligible_for_execution": False,
                }
                conn.execute(
                    "INSERT INTO app.sa_source_subscription_handbacks(handback_id,subscription_id,"
                    "subscription_revision,input_id,source_instance_id,project_id,"
                    "version_fingerprint,loop_key,outcome,payload,payload_sha256,review_sha256,"
                    "signed_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        uuid4(),
                        subscription_id,
                        new_value.revision,
                        item.ref.input_id,
                        item.ref.source_instance_id,
                        self.project,
                        item.ref.version_fingerprint,
                        key,
                        outcome,
                        Jsonb(payload),
                        digest(payload),
                        digest(request.review_ref),
                        user,
                    ),
                )
                count["n"] += int(allowed)
                outcomes.append(
                    {"input_id": item.ref.input_id, "outcome": outcome, "reason_code": reason}
                )
            return {
                **self._view(*self._record(conn, subscription_id)),
                "handbacks": outcomes,
                "semantic_evaluation": "NOT_RUN",
            }

    def _runtime(
        self,
        conn: Connection,
        run_id: UUID,
        epoch: int | None = None,
    ) -> tuple[TargetRef, Row]:
        target, run = self._target(conn, run_id)
        if epoch is not None and (run["execution_epoch"] != epoch or run["status"] != "running"):
            raise _error("SOURCE_SUBSCRIPTION_RUN_FENCED")
        self.engine.source_policies.check_run(conn, run)
        return target, run

    def _consumer_hash(self, conn: Connection, run_id: UUID) -> str:
        target, run = self._runtime(conn, run_id)
        primary = conn.execute(
            "SELECT active,role FROM app.subscriptions WHERE run_id=%s AND thread_id=%s",
            (run_id, target.thread_id),
        ).fetchone()
        if not primary or not primary["active"] or primary["role"] != "primary":
            raise _error("SOURCE_SUBSCRIPTION_PRIMARY_LOST", 403)
        return digest(
            [target.model_dump(mode="json"), run["status"], run["ticket_status"], primary]
        )

    def _blocked_input(self, conn: Connection, row: Row, reason: str) -> CorrelationInputRef:
        """Hash current rejection facts without decrypting or disclosing Source bodies."""
        facts = conn.execute(
            "SELECT to_jsonb(i) AS canonical,to_jsonb(e) AS entity,to_jsonb(s) AS source,"
            "to_jsonb(g) AS ingress,to_jsonb(p) AS policy,to_jsonb(a) AS identity,"
            "b.expires_at AS body_expiry,"
            "(SELECT jsonb_agg(to_jsonb(x) ORDER BY x.binding_id) FROM app.source_role_bindings x "
            "WHERE x.identity_id=i.identity_id AND x.project_id=i.project_id) AS bindings "
            "FROM app.source_ingress_inputs i LEFT JOIN app.source_ingress_entities e "
            "ON e.entity_key=i.entity_key LEFT JOIN app.source_instances s "
            "ON s.source_instance_id=i.source_instance_id LEFT JOIN app.source_ingress_controls g "
            "ON g.source_instance_id=i.source_instance_id LEFT JOIN app.source_policies p "
            "ON p.policy_id=i.policy_id LEFT JOIN app.source_identities a "
            "ON a.identity_id=i.identity_id LEFT JOIN app.source_ingress_bodies b "
            "ON b.input_id=i.input_id WHERE i.input_id=%s AND i.project_id=%s",
            (row["input_id"], self.project),
        ).fetchone()
        return CorrelationInputRef(
            input_id=row["input_id"],
            source_instance_id=row["source_instance_id"],
            project_id=self.project,
            version_fingerprint=row["version_fingerprint"],
            state_sha256=digest(_json([reason, facts])),
        )

    def _delivery_view(self, conn: Connection, row: Row) -> SourceDeliveryRef:
        if row["project_id"] != self.project or digest(row["payload"]) != row["payload_sha256"]:
            raise _error("SOURCE_SUBSCRIPTION_DELIVERY_INVALID", 403)
        saved = _decode(SourceDeliveryRef, row["payload"])
        _, value = self._record(conn, saved.subscription_id)
        try:
            if saved.subscription_revision != value.revision:
                raise _error("SOURCE_SUBSCRIPTION_HEAD_STALE", 403)
            if saved.run_id != value.target.run_id:
                raise _error("SOURCE_SUBSCRIPTION_REBIND_REQUIRED", 403)
            loaded = self.correlations._input(conn, saved.input.input_id)
            decision = self._decision(conn, value, loaded)
            if decision.outcome not in {"deliver_ref", "observe_ref"}:
                raise _error(decision.reason_code, 403)
            if decision.lane != saved.lane or decision.purpose != saved.purpose:
                raise _error("SOURCE_SUBSCRIPTION_ROLE_CHANGED", 403)
            return saved.model_copy(
                update={
                    "input": loaded.ref,
                    "expires_at": min(value.expires_at, loaded.expires),
                }
            )
        except DomainError as error:
            original = self._input_row(conn, saved.input.input_id)
            return saved.model_copy(
                update={
                    "subscription_revision": value.revision,
                    "input": self._blocked_input(conn, original, error.code),
                    "disposition": "blocked",
                    "reason_code": error.code,
                }
            )

    def mailbox(
        self,
        cookie: str,
        run_id: UUID,
        *,
        lane: SubscriptionRole = "primary",
        cursor: int = 0,
        limit: int = MAX_PAGE_ITEMS,
    ) -> Row:
        if lane not in {"primary", "context_only", "observer"} or (
            type(cursor) is not int
            or cursor < 0
            or type(limit) is not int
            or not 1 <= limit <= MAX_PAGE_ITEMS
        ):
            raise _error("SOURCE_SUBSCRIPTION_PAGE_INVALID", 422)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            self._runtime(conn, run_id)
            rows = self._delivery_rows(conn, run_id, lane, cursor, limit + 1)
            return {
                "items": [
                    self._delivery_view(conn, row).model_dump(mode="json") for row in rows[:limit]
                ],
                "cursor": self._cursor(conn, run_id, lane),
                "requested_cursor": cursor,
                "has_more": len(rows) > limit,
                "eligible_for_execution": False,
            }

    def _delivery_rows(
        self,
        conn: Connection,
        run_id: UUID,
        lane: str,
        cursor: int,
        limit: int,
    ) -> builtins.list[Row]:
        return conn.execute(
            "SELECT * FROM app.sa_source_subscription_deliveries WHERE run_id=%s AND lane=%s "
            "AND project_id=%s AND source_seq>%s ORDER BY source_seq LIMIT %s",
            (run_id, lane, self.project, cursor, limit),
        ).fetchall()

    @staticmethod
    def _snapshot_identity(
        snapshot: SourceIntakeSnapshot,
        request: SubscriptionPrepareRequest,
        watermark: int,
    ) -> UUID:
        # Bind paging parameters and all pending arrivals without adding mutable facts.
        payload = snapshot.model_dump(mode="json", exclude={"snapshot_id"})
        return uuid5(
            NAMESPACE_URL,
            digest(["mgar-source-intake-v1", request.limit, request.max_bytes, watermark, payload]),
        )

    def _prepared(
        self,
        conn: Connection,
        snapshot: SourceIntakeSnapshot,
        request: SubscriptionPrepareRequest,
        watermark: int,
    ) -> SourceIntakeSnapshot | None:
        rows = conn.execute(
            "SELECT s.* FROM app.sa_source_subscription_intakes s WHERE s.run_id=%s "
            "AND s.project_id=%s AND s.lane=%s AND s.execution_epoch=%s AND s.cursor_from=%s "
            "AND s.expires_at>%s AND NOT EXISTS "
            "(SELECT 1 FROM app.sa_source_subscription_consumptions c "
            "WHERE c.snapshot_id=s.snapshot_id) ORDER BY s.created_at DESC LIMIT %s",
            (
                snapshot.run_id,
                self.project,
                snapshot.lane,
                snapshot.epoch,
                snapshot.from_cursor,
                self._now(conn),
                MAX_INTAKES,
            ),
        ).fetchall()
        current = snapshot.model_dump(mode="json", exclude={"snapshot_id", "expires_at"})
        for row in rows:
            previous = _decode(SourceIntakeSnapshot, row["payload"])
            if previous.snapshot_sha256 != row["payload_sha256"]:
                raise _error("SOURCE_SUBSCRIPTION_SNAPSHOT_INVALID", 403)
            if previous.expires_at > snapshot.expires_at or current != previous.model_dump(
                mode="json", exclude={"snapshot_id", "expires_at"}
            ):
                continue
            candidate = snapshot.model_copy(update={"expires_at": previous.expires_at})
            if self._snapshot_identity(candidate, request, watermark) == previous.snapshot_id:
                return previous
        return None

    def prepare(
        self,
        cookie: str,
        run_id: UUID,
        request: SubscriptionPrepareRequest,
    ) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            self._runtime(conn, run_id, request.epoch)
            cursor = self._cursor(conn, run_id, request.lane)
            if cursor != request.expected_cursor:
                raise _error("SOURCE_SUBSCRIPTION_CURSOR_STALE")
            rows = self._delivery_rows(conn, run_id, request.lane, cursor, request.limit + 1)
            if not rows:
                return {
                    "snapshot_id": None,
                    "snapshot_sha256": None,
                    "items": [],
                    "cursor": cursor,
                    "has_more": False,
                    "eligible_for_execution": False,
                }
            consumer = self._consumer_hash(conn, run_id)
            selected: builtins.list[SourceDeliveryRef] = []
            heads: dict[UUID, SubscriptionHeadRef] = {}
            snapshot = None
            for row in rows[: request.limit]:
                item = self._delivery_view(conn, row)
                _, value = self._record(conn, item.subscription_id)
                next_heads = {
                    **heads,
                    item.subscription_id: SubscriptionHeadRef(
                        subscription_id=value.subscription_id,
                        revision=value.revision,
                        authority_sha256=value.authority_sha256,
                    ),
                }
                deadline = min(
                    [self._now(conn) + timedelta(seconds=60)]
                    + [
                        entry.expires_at
                        for entry in [*selected, item]
                        if entry.disposition == "ready"
                    ]
                )
                candidate = SourceIntakeSnapshot(
                    snapshot_id=uuid4(),
                    project_id=self.project,
                    run_id=run_id,
                    epoch=request.epoch,
                    lane=request.lane,
                    from_cursor=cursor,
                    through_cursor=item.sequence,
                    items=tuple([*selected, item]),
                    subscription_heads=tuple(
                        sorted(next_heads.values(), key=lambda h: str(h.subscription_id))
                    ),
                    consumer_state_sha256=consumer,
                    expires_at=deadline,
                )
                if len(candidate.model_dump_json().encode()) > request.max_bytes:
                    if snapshot is None:
                        raise _error("SOURCE_SUBSCRIPTION_PAGE_BYTES", 413)
                    break
                selected, heads, snapshot = [*selected, item], next_heads, candidate
            assert snapshot is not None
            latest = conn.execute(
                "SELECT coalesce(max(source_seq),0) AS sequence "
                "FROM app.sa_source_subscription_deliveries "
                "WHERE run_id=%s AND lane=%s AND project_id=%s",
                (run_id, request.lane, self.project),
            ).fetchone()
            assert latest is not None
            watermark = int(latest["sequence"])
            previous = self._prepared(conn, snapshot, request, watermark)
            if previous is not None:
                snapshot = previous
            else:
                snapshot = snapshot.model_copy(
                    update={"snapshot_id": self._snapshot_identity(snapshot, request, watermark)}
                )
                pending = conn.execute(
                    "SELECT count(*) AS n FROM app.sa_source_subscription_intakes s WHERE "
                    "s.run_id=%s AND s.project_id=%s AND s.expires_at>clock_timestamp() "
                    "AND NOT EXISTS (SELECT 1 FROM app.sa_source_subscription_consumptions c "
                    "WHERE c.snapshot_id=s.snapshot_id)",
                    (run_id, self.project),
                ).fetchone()
                if pending is not None and pending["n"] >= MAX_INTAKES:
                    raise _error("SOURCE_SUBSCRIPTION_INTAKE_CAPACITY")
                conn.execute(
                    "INSERT INTO app.sa_source_subscription_intakes(snapshot_id,project_id,run_id,"
                    "lane,execution_epoch,cursor_from,cursor_to,payload,payload_sha256,expires_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        snapshot.snapshot_id,
                        self.project,
                        run_id,
                        snapshot.lane,
                        snapshot.epoch,
                        snapshot.from_cursor,
                        snapshot.through_cursor,
                        Jsonb(snapshot.model_dump(mode="json")),
                        snapshot.snapshot_sha256,
                        snapshot.expires_at,
                    ),
                )
            payload = snapshot.model_dump(mode="json")
            return {
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_sha256": snapshot.snapshot_sha256,
                "snapshot": payload,
                "items": payload["items"],
                "cursor": cursor,
                "has_more": len(rows) > len(selected),
                "eligible_for_execution": False,
            }

    def _intake(
        self, conn: Connection, run_id: UUID, request: SubscriptionAckRequest
    ) -> SourceIntakeSnapshot:
        row = conn.execute(
            "SELECT * FROM app.sa_source_subscription_intakes WHERE snapshot_id=%s AND "
            "run_id=%s AND project_id=%s",
            (request.snapshot_id, run_id, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_SUBSCRIPTION_SNAPSHOT_NOT_FOUND", 404)
        value = _decode(SourceIntakeSnapshot, row["payload"])
        if value.snapshot_sha256 != row["payload_sha256"]:
            raise _error("SOURCE_SUBSCRIPTION_SNAPSHOT_INVALID", 403)
        return value

    def _checked_intake(
        self,
        conn: Connection,
        run_id: UUID,
        request: SubscriptionAckRequest,
    ) -> tuple[SourceIntakeSnapshot, bool]:
        _, run = self._runtime(conn, run_id, request.epoch)
        snapshot = self._intake(conn, run_id, request)
        already = (
            conn.execute(
                "SELECT 1 FROM app.sa_source_subscription_consumptions WHERE snapshot_id=%s",
                (snapshot.snapshot_id,),
            ).fetchone()
            is not None
        )
        current = []
        heads = {}
        for item in snapshot.items:
            row = conn.execute(
                "SELECT * FROM app.sa_source_subscription_deliveries WHERE delivery_id=%s "
                "AND run_id=%s AND project_id=%s",
                (item.delivery_id, run_id, self.project),
            ).fetchone()
            if row is None:
                raise _error("SOURCE_SUBSCRIPTION_DELIVERY_INVALID", 403)
            fresh = self._delivery_view(conn, row)
            if fresh != item:
                raise _error("SOURCE_SUBSCRIPTION_INPUT_STALE", 403)
            current.append(fresh.input)
            _, subscription = self._record(conn, item.subscription_id)
            heads[subscription.subscription_id] = SubscriptionHeadRef(
                subscription_id=subscription.subscription_id,
                revision=subscription.revision,
                authority_sha256=subscription.authority_sha256,
            )
        validate_ack(
            snapshot,
            request,
            run_id=run_id,
            epoch=run["execution_epoch"],
            cursor=self._cursor(conn, run_id, request.lane),
            subscription_heads=tuple(heads.values()),
            inputs=tuple(current),
            consumer_state_sha256=self._consumer_hash(conn, run_id),
            at=self._now(conn),
            already_acknowledged=already,
        )
        return snapshot, already

    @staticmethod
    def _consumption(snapshot: SourceIntakeSnapshot) -> Row:
        return SourceConsumptionReceipt(
            receipt_id=snapshot.snapshot_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_sha256=snapshot.snapshot_sha256,
            run_id=snapshot.run_id,
            epoch=snapshot.epoch,
            lane=snapshot.lane,
            from_cursor=snapshot.from_cursor,
            through_cursor=snapshot.through_cursor,
            delivery_ids=tuple(item.delivery_id for item in snapshot.items),
        ).model_dump(mode="json")

    def acknowledge(
        self,
        cookie: str,
        run_id: UUID,
        request: SubscriptionAckRequest,
    ) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            snapshot, already = self._checked_intake(conn, run_id, request)
            if not already:
                conn.execute(
                    "INSERT INTO app.sa_source_subscription_consumptions(snapshot_id,project_id,"
                    "run_id,lane,execution_epoch,cursor_from,cursor_to,payload_sha256,signed_by) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        snapshot.snapshot_id,
                        self.project,
                        run_id,
                        snapshot.lane,
                        snapshot.epoch,
                        snapshot.from_cursor,
                        snapshot.through_cursor,
                        snapshot.snapshot_sha256,
                        user,
                    ),
                )
            return self._consumption(snapshot)

    def materialize(
        self,
        cookie: str,
        run_id: UUID,
        request: SubscriptionAckRequest,
    ) -> MaterializedSourcePage:
        """Trusted in-process read; never registered as an HTTP/model tool endpoint."""
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            snapshot, _ = self._checked_intake(conn, run_id, request)
            if snapshot.lane == "observer":
                raise _error("SOURCE_SUBSCRIPTION_OBSERVER_READ_DENIED", 403)
            _, run = self._runtime(conn, run_id, request.epoch)
            items = []
            used = 0
            for reference in snapshot.items:
                if reference.disposition != "ready":
                    continue
                loaded = self.correlations._input(conn, reference.input.input_id)
                _, subscription = self._record(conn, reference.subscription_id)
                if not loaded.definition.receive.model_input or loaded.definition.mode not in {
                    "auto_investigate",
                    "auto_repair",
                }:
                    raise _error("SOURCE_SUBSCRIPTION_MODEL_INPUT_DENIED", 403)
                if run["execution_mode"] != "fixture":
                    binding = self.engine.source_policies._bound(conn, run_id)
                    if (
                        binding is None
                        or binding["policy_id"] != subscription.anchor.policy_id
                        or binding["identity_id"] != loaded.row["identity_id"]
                    ):
                        raise _error("SOURCE_SUBSCRIPTION_LIVE_BRIDGE_REQUIRED", 403)
                    self.engine.source_policies.check_run(conn, run, "model_call")
                used += len(loaded.text.encode())
                if used > MAX_PAGE_BYTES:
                    raise _error("SOURCE_SUBSCRIPTION_CONTEXT_BOUND", 413)
                items.append(
                    MaterializedSourceItem(
                        delivery_id=reference.delivery_id,
                        input_id=reference.input.input_id,
                        text=loaded.text,
                    )
                )
            return MaterializedSourcePage(
                snapshot=snapshot,
                items=tuple(items),
                materialized_at=self._now(conn),
            )
