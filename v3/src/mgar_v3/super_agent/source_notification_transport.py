"""Original Dispatcher transport: final committed authority, one send, query-only unknown."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import DefinitiveFailure
from mgar_v3.sources.domain import SourceInstance, SourceThreadRef
from mgar_v3.sources.ports import ChannelMessage, DeliveryLookup, DeliveryReceipt
from mgar_v3.super_agent.source_notification_domain import ACTION_TYPE
from mgar_v3.super_agent.source_notification_ports import NotificationProfile
from mgar_v3.super_agent.source_notification_store import Row, denied

if TYPE_CHECKING:
    from mgar_v3.super_agent.source_notifications import SourceNotifications


class NotificationTransport:
    mode = "fixture"
    action_types = (ACTION_TYPE,)

    def __init__(self, service: SourceNotifications):
        self.service = service

    def _prepare(
        self, claimed: Row
    ) -> tuple[SourceInstance, NotificationProfile, ChannelMessage, str]:
        core, engine = self.service, self.service.engine
        # Quota reservation commits BEFORE the final authority read. A role/policy
        # revocation committed at this boundary must prevent the actual request.
        with engine.transaction() as conn:
            action, run = engine._action(conn, claimed["action_id"])
            if (
                not core.handles(action)
                or action["status"] != "dispatching"
                or action["lease_token"] != claimed["lease_token"]
            ):
                raise denied("FENCED")
            core.validate_action(conn, action, run)
            engine.project_budgets.notification_quota(conn, str(action["action_id"]))
        with engine.transaction() as conn:
            action, run = engine._action(conn, claimed["action_id"])
            if action["status"] != "dispatching" or action["lease_token"] != claimed["lease_token"]:
                raise denied("FENCED")
            core.validate_action(conn, action, run)
            intent = core._intent(conn, action["action_id"])
            config, settings = core._config(conn, intent["source_instance_id"])
            instance, _, profile = core._policy(
                conn, settings, intent["source_instance_id"], config["signed_by"]
            )
            message = ChannelMessage(
                action_id=action["action_id"],
                destination=SourceThreadRef.model_validate(intent["destination"]["thread"]),
                payload_hash=action["payload_hash"],
                body_ref=intent["body_sha256"],
                idempotency_key=intent["idempotency_key"],
            )
            body = str(action["payload"]["body"])
            core._event(
                conn,
                intent["source_instance_id"],
                "send_authorized",
                "FINAL_CURRENT_CHECK",
                action["action_id"],
            )
        return instance, profile, message, body

    def send(self, action: Row) -> str:
        try:
            instance, profile, message, body = self._prepare(action)
        except DomainError:
            with self.service.engine.transaction() as conn:
                intent = self.service._intent(conn, action["action_id"])
                self.service._event(
                    conn,
                    intent["source_instance_id"],
                    "blocked",
                    "FINAL_CHECK_BLOCKED",
                    action["action_id"],
                )
            raise DefinitiveFailure("Notification was blocked before channel IO") from None
        # No business SQL transaction is held while the registered channel runs.
        receipt = profile.channel.send(instance, message, body)
        self._check(receipt, profile, message.idempotency_key)
        if receipt.outcome == "accepted" and receipt.remote_message_id is not None:
            return str(receipt.remote_message_id)
        if receipt.outcome == "rejected" and receipt.remote_message_id is None:
            raise DefinitiveFailure("Channel definitively rejected the notification")
        raise OSError("Notification delivery is unknown; do not resend")

    @staticmethod
    def _check(receipt: DeliveryReceipt, profile: NotificationProfile, key: str) -> None:
        if (
            not isinstance(receipt, DeliveryReceipt)
            or receipt.scope != profile.scope
            or receipt.idempotency_key != key
            or (receipt.outcome == "accepted") != (receipt.remote_message_id is not None)
        ):
            raise OSError("Channel receipt does not prove this exact delivery")

    def find(self, action: Row) -> str | None:
        core, engine = self.service, self.service.engine
        with engine.transaction() as conn:
            current, _ = engine._action(conn, action["action_id"])
            if current["status"] != "outcome_unknown" or not core.handles(current):
                raise denied("NOT_UNKNOWN", 409)
            intent = core._intent(conn, action["action_id"])
            profile = core._profiles.get(intent["profile_sha256"])
            try:
                config, settings = core._config(conn, intent["source_instance_id"])
                instance, _, checked = core._policy(
                    conn, settings, intent["source_instance_id"], config["signed_by"]
                )
                available = (
                    profile is not None
                    and profile == checked
                    and profile.capabilities.query_delivery == "supported"
                )
            except DomainError:
                available = False
            if not available or profile is None:
                core._receipt(conn, intent, "query", "unqueryable")
                core._event(
                    conn,
                    intent["source_instance_id"],
                    "query",
                    "WAIT_MANUAL_NO_QUERY",
                    action["action_id"],
                )
                return None
            lookup = DeliveryLookup(
                scope=profile.scope,
                idempotency_key=intent["idempotency_key"],
                remote_message_id=current["external_id"],
            )
        # A bounded negative query is never evidence permitting another send.
        try:
            receipt = profile.channel.query(instance, lookup)
            self._check(receipt, profile, lookup.idempotency_key)
        except (OSError, TimeoutError):
            receipt = DeliveryReceipt(
                scope=profile.scope, idempotency_key=lookup.idempotency_key, outcome="unknown"
            )
        with engine.transaction() as conn:
            core._receipt(conn, intent, "query", receipt.outcome, receipt.remote_message_id)
            core._event(
                conn, intent["source_instance_id"], "query", "READ_ONLY_LOOKUP", action["action_id"]
            )
        return receipt.remote_message_id if receipt.outcome == "accepted" else None
