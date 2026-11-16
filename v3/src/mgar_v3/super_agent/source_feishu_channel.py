"""Explicit Feishu replies for the original Dispatcher; no model or raw-source egress."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from psycopg.pq import TransactionStatus

from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import DefinitiveFailure
from mgar_v3.sources.domain import SourceInstance, SourceScope
from mgar_v3.sources.feishu_domain import FeishuSecrets
from mgar_v3.sources.ports import ChannelMessage, DeliveryLookup, DeliveryReceipt
from mgar_v3.super_agent.source_notification_store import denied
from mgar_v3.super_agent.source_notification_transport import NotificationTransport

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine

API = "https://open.feishu.cn"
TOKEN = "/open-apis/auth/v3/tenant_access_token/internal"


class FeishuNotificationTransport(NotificationTransport):
    mode = "live"


class FeishuNotificationChannel:
    def __init__(
        self, engine: Engine, scope: SourceScope, credentials: Callable[[], FeishuSecrets], *,
        mode: str = "live", proxy: str | None = None,
        transport: httpx.MockTransport | None = None,
    ):
        if scope.source_kind != "feishu" or mode not in {"fixture", "live"}:
            raise ValueError("Explicit Feishu installation and mode required")
        if (mode == "fixture") != isinstance(transport, httpx.MockTransport):
            raise ValueError("Controlled HTTP must be explicit; live HTTP cannot use a mock")
        if proxy is not None:
            url = urlsplit(proxy)
            if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
                    or url.username or url.password or url.path or url.query or url.fragment
                    or url.port is None):
                raise ValueError("Only an explicit trusted local HTTP proxy is supported")
        self.engine, self.scope, self.credentials = engine, scope, credentials
        self.mode, self.proxy, self.transport = mode, proxy, transport

    def _current(self, instance: SourceInstance, message: ChannelMessage, body: str) -> str:
        core = self.engine.source_notifications
        with self.engine.transaction() as conn:
            action, run = self.engine._action(conn, message.action_id)
            core.validate_action(conn, action, run)
            intent = core._intent(conn, message.action_id)
            profile = core._profiles.get(intent["profile_sha256"])
            current = core.engine.source_correlations.identities.registry._instance(
                conn, self.scope.source_instance_id
            )
            if (action["status"] != "dispatching" or instance != current
                    or instance.scope != self.scope or message.destination.scope != self.scope
                    or action["payload_hash"] != message.payload_hash
                    or action["payload"]["body"] != body
                    or hashlib.sha256(body.encode()).hexdigest() != message.body_ref
                    or action["payload"]["idempotency_key"] != message.idempotency_key
                    or profile is None or profile.channel is not self or profile.mode != self.mode
                    or intent["destination"]["thread"] != message.destination.model_dump(mode="json")):
                raise denied("LIVE_CHANNEL_FENCED")
            from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef

            ref = CorrelationInputRef.model_validate_json(json.dumps(intent["source_ref"]))
            _, settings = core._config(conn, intent["source_instance_id"])
            source = core._source(conn, run, ref, settings)
            if source.item.event.scope != self.scope:
                raise denied("LIVE_CHANNEL_FENCED")
            return str(source.item.event.entity_id)

    @staticmethod
    def _post(
        client: httpx.Client, path: str, body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        raw = bytearray()
        with client.stream("POST", path, json=body, headers=headers) as response:
            status = response.status_code
            for block in response.iter_bytes():
                raw.extend(block)
                if len(raw) > 2097152:
                    raise OSError("Feishu response exceeded the bound; outcome unknown")
        try:
            from mgar_v3.sources.feishu_response import decode_response

            value = decode_response(bytes(raw))
            if not isinstance(value, dict) or type(value.get("code")) is not int:
                raise ValueError
            return status, value
        except (ValueError, UnicodeError, DomainError):
            raise OSError("Unproven Feishu response; outcome unknown") from None

    def send(self, instance: SourceInstance, message: ChannelMessage, body: str) -> DeliveryReceipt:
        try:
            remote_input = self._current(instance, message, body)
            secret = self.credentials()
        except DomainError:
            raise DefinitiveFailure("Current notification authority denied before send") from None
        if self.engine.connection.info.transaction_status != TransactionStatus.IDLE:
            raise DefinitiveFailure("Platform IO cannot run in a business transaction")
        if self.mode == "live" and not self.engine.settings.allow_external_write:
            raise DefinitiveFailure("External notification deployment is disabled")
        with httpx.Client(base_url=API, transport=self.transport, proxy=self.proxy,
                          timeout=10, trust_env=False, follow_redirects=False) as client:
            try:
                status, token = self._post(client, TOKEN, {
                    "app_id": self.scope.installation_id, "app_secret": secret.app_secret,
                })
                if status != 200 or token["code"] != 0:
                    raise ValueError
                bearer = token["tenant_access_token"]
                if not isinstance(bearer, str) or not bearer or len(bearer) > 4096:
                    raise ValueError
            except (httpx.HTTPError, OSError, KeyError, ValueError):
                # No message endpoint was invoked, even if token minting is unknown.
                raise DefinitiveFailure("Feishu token unavailable; message was not sent") from None
            try:
                if self._current(instance, message, body) != remote_input:
                    raise denied("LIVE_CHANNEL_FENCED")
                self.credentials()  # Credential revocation during token IO fences the send too.
            except DomainError:
                raise DefinitiveFailure("Notification authority changed during token IO") from None
            if self.engine.connection.info.transaction_status != TransactionStatus.IDLE:
                raise DefinitiveFailure("Platform IO cannot run in a business transaction")
            status, value = self._post(
                client, "/open-apis/im/v1/messages/" + quote(remote_input, safe="") + "/reply",
                {"msg_type": "text", "content": json.dumps({"text": body}, ensure_ascii=False),
                      "reply_in_thread": message.destination.native_thread_id is not None,
                      "uuid": str(uuid5(NAMESPACE_URL, self.scope.key + message.idempotency_key))},
                {"Authorization": "Bearer " + bearer},
            )
            if status in {200, 400, 401, 403, 429} and value["code"] != 0:
                return DeliveryReceipt(scope=self.scope, idempotency_key=message.idempotency_key,
                                       outcome="rejected")
            data = value.get("data")
            if (status != 200 or value["code"] != 0 or not isinstance(data, dict)
                    or not isinstance(data.get("message_id"), str) or not data["message_id"]
                    or len(data["message_id"]) > 256
                    or data.get("chat_id", message.destination.conversation_id)
                    != message.destination.conversation_id):
                raise OSError("Feishu did not prove exact delivery; no retry")
            return DeliveryReceipt(scope=self.scope, idempotency_key=message.idempotency_key,
                                   outcome="accepted", remote_message_id=data["message_id"])

    def query(self, instance: SourceInstance, lookup: DeliveryLookup) -> DeliveryReceipt:
        # No granted API can reliably recover an unknown message by client UUID.
        # Never expand to group-history access or turn absence into permission to resend.
        return DeliveryReceipt(scope=self.scope, idempotency_key=lookup.idempotency_key,
                               outcome="unknown")
