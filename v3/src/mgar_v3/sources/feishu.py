"""Strict platform proof and body normalization. No socket, model or business writer."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from lark_oapi.core.utils import AESCipher
from pydantic import TypeAdapter

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.domain import (
    ActorRef,
    EntityRevision,
    OpaqueId,
    SourceCapabilities,
    SourceEvent,
    SourceHealth,
    SourceInstance,
    SourceThreadRef,
)
from mgar_v3.sources.feishu_domain import (
    EVENTS,
    RECALL,
    VERSION,
    FeishuSecrets,
    FeishuSettings,
)
from mgar_v3.sources.identity_domain import VerifiedSubject
from mgar_v3.sources.ingress_domain import DeliveryMetadata, ItemMetadata, VerifiedDelivery
from mgar_v3.sources.ports import ContextPage, ContextRequest, TransportEnvelope, VerifiedEnvelope

CAPABILITIES = SourceCapabilities(
    history_read="supported",
    incremental_cursor="unsupported",
    reconnect_replay="unsupported",
    edit_events="unsupported",
    delete_events="supported",
    native_entity_revision="unsupported",
)
_id_adapter = TypeAdapter(OpaqueId)


def opaque(value: Any) -> str:
    return _id_adapter.validate_python(value)


def parsed(data: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(
        data, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
    )
    if not isinstance(value, dict):
        raise ValueError("Object required")
    return value


def milliseconds(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Invalid time")
    number = str(value)
    if not number.isascii() or not number.isdigit() or not 0 < int(number) < 2**53:
        raise ValueError("Invalid time")
    return datetime.fromtimestamp(int(number) / 1000, UTC)


class Filtered(DomainError):
    def __init__(self, code: str, event_id: str):
        super().__init__(
            code, "Authenticated Feishu transport is outside selected receive scope", 403
        )
        self.event_id = event_id  # No content, sender or raw event in the exception.


class SocketProof:
    """Private per-runtime session MAC. Header strings alone cannot assert SDK authentication."""

    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)
        self._epoch: str | None = None

    def connected(self) -> str:
        self._epoch = secrets.token_hex(16)
        return self._epoch

    def disconnected(self) -> None:
        self._epoch = None

    def seal(self, payload: bytes, delivery_id: str, epoch: str) -> TransportEnvelope:
        if epoch != self._epoch or self._epoch is None:
            raise DomainError("FEISHU_SOCKET_FENCED", "Socket session is not current", 403)
        mac = hmac.new(
            self._key,
            epoch.encode() + b"\0" + delivery_id.encode() + b"\0" + payload,
            hashlib.sha256,
        ).hexdigest()
        return TransportEnvelope(
            {
                "mgar-feishu-session": epoch,
                "mgar-feishu-delivery": delivery_id,
                "mgar-feishu-proof": mac,
            },
            payload,
        )

    def verify(self, envelope: TransportEnvelope) -> str:
        epoch = envelope.headers.get("mgar-feishu-session", "")
        delivery = opaque(envelope.headers.get("mgar-feishu-delivery", ""))
        wanted = hmac.new(
            self._key,
            epoch.encode() + b"\0" + delivery.encode() + b"\0" + envelope.payload,
            hashlib.sha256,
        ).hexdigest()
        if (
            self._epoch is None
            or epoch != self._epoch
            or not hmac.compare_digest(envelope.headers.get("mgar-feishu-proof", ""), wanted)
        ):
            raise DomainError("FEISHU_SOCKET_UNTRUSTED", "No current SDK connection proof", 403)
        return delivery


class FeishuAdapter:
    source_kind = "feishu"
    adapter_version = VERSION
    verifier_id = "feishu-bot-proof-v1"
    capabilities = CAPABILITIES

    def __init__(
        self,
        settings: FeishuSettings,
        secret: FeishuSecrets,
        *,
        socket_proof: SocketProof | None = None,
        check_current: Callable[[], object] | None = None,
    ):
        self.settings, self._secret = settings, secret
        self._socket, self._check = socket_proof, check_current

    def configuration_schema(self) -> dict[str, Any]:
        return FeishuSettings.model_json_schema()

    def authenticated(
        self, instance: SourceInstance, envelope: TransportEnvelope, *, transport: str = "webhook"
    ) -> tuple[dict[str, Any], str, str]:
        if self._check:
            self._check()
        s = self.settings
        if (
            not s.enabled
            or instance.adapter_version != VERSION
            or instance.scope.source_kind != "feishu"
            or instance.scope.tenant_id != s.tenant_key
            or instance.scope.installation_id != s.app_id
        ):
            raise DomainError(
                "FEISHU_INSTALLATION_DENIED", "Feishu installation scope mismatch", 403
            )
        if len(envelope.payload) > 262144:
            raise DomainError("INGRESS_ENVELOPE_BOUND", "Feishu transport exceeded byte limit", 413)
        try:
            if transport == "socket":
                if self._socket is None:
                    raise DomainError("FEISHU_SOCKET_UNTRUSTED", "No SDK verifier", 403)
                delivery = self._socket.verify(envelope)
                raw = parsed(envelope.payload)
                method = "installation_proof"
            elif transport == "webhook":
                headers: dict[str, str] = {}
                for key, value in envelope.headers.items():
                    key = key.lower()
                    if key in headers:
                        raise ValueError("Duplicate header")
                    headers[key] = value
                timestamp, nonce = (
                    headers["x-lark-request-timestamp"],
                    headers["x-lark-request-nonce"],
                )
                if (
                    not timestamp.isascii()
                    or not timestamp.isdigit()
                    or len(nonce) > 200
                    or not nonce
                    or any(ord(c) < 33 or ord(c) > 126 for c in nonce)
                    or abs(datetime.now(UTC).timestamp() - int(timestamp)) > 300
                ):
                    raise ValueError("Stale callback")
                expected = hashlib.sha256(
                    (timestamp + nonce + self._secret.encrypt_key).encode() + envelope.payload
                ).hexdigest()
                if not hmac.compare_digest(headers.get("x-lark-signature", ""), expected):
                    raise DomainError("FEISHU_BAD_SIGNATURE", "Feishu signature rejected", 401)
                raw = parsed(envelope.payload)
                if "encrypt" in raw:
                    encrypted = raw["encrypt"]
                    if not isinstance(encrypted, str):
                        raise ValueError("Invalid encrypted envelope")
                    plain = AESCipher(self._secret.encrypt_key).decrypt_str(encrypted).encode()
                    if len(plain) > 262144:
                        raise ValueError("Plaintext bound")
                    raw = parsed(plain)
                token = (
                    raw.get("header", {}).get("token") if raw.get("schema") else raw.get("token")
                )
                if not isinstance(token, str) or not hmac.compare_digest(
                    token.encode(), self._secret.verification_token.encode()
                ):
                    raise DomainError("FEISHU_BAD_TOKEN", "Feishu verification token rejected", 401)
                if raw.get("type") == "url_verification":
                    return raw, opaque(raw["challenge"]), "challenge"
                delivery = opaque(raw["header"]["event_id"])
                method = "platform_signature"
            else:
                raise ValueError("Unknown transport")
            header = raw["header"]
            if (
                raw.get("schema") != "2.0"
                or header["app_id"] != s.app_id
                or header["tenant_key"] != s.tenant_key
                or header["event_type"] not in s.events
                or header["event_type"] not in EVENTS
            ):
                raise DomainError("FEISHU_INSTALLATION_DENIED", "Unapproved app/tenant/event", 403)
            opaque(header["event_id"])
            milliseconds(header["create_time"])
            return raw, delivery, method
        except DomainError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
            raise DomainError(
                "FEISHU_INVALID_ENVELOPE", "Malformed Feishu proof or event", 403
            ) from None

    def normalize_raw(
        self,
        instance: SourceInstance,
        raw: dict[str, Any],
        delivery: str,
        method: str,
        *,
        historical: bool = False,
        retention_selected: bool = True,
        observation_only: bool = False,
    ) -> tuple[ItemMetadata, bytes | None]:
        s, now = self.settings, datetime.now(UTC)
        try:
            header, event = raw["header"], raw["event"]
            kind, remote = header["event_type"], opaque(header["event_id"])
            if kind == RECALL:
                chat, message_id = opaque(event["chat_id"]), opaque(event["message_id"])
                if chat not in s.chat_ids:
                    raise Filtered("FEISHU_CHAT_FILTERED", remote)
                actor = ActorRef(
                    scope=instance.scope, id_type="app_id", actor_id=s.app_id, actor_kind="service"
                )
                thread = SourceThreadRef(
                    scope=instance.scope,
                    conversation_id=chat,
                    native_thread_id=opaque(event["thread_id"]) if event.get("thread_id") else None,
                )
                occurred = milliseconds(event["recall_time"])
                operation, body, version, addressed, parent = (
                    "delete",
                    None,
                    digest(["recalled", message_id]),
                    "ambient",
                    None,
                )
            else:
                message, sender = event["message"], event["sender"]
                chat, message_id = opaque(message["chat_id"]), opaque(message["message_id"])
                sender_type = sender["sender_type"]
                actor_type = sender.get("id_type", "open_id")
                actor_id = opaque(sender["sender_id"][actor_type])
                if (
                    sender_type not in {"user", "app"}
                    or actor_type not in {"open_id", "app_id"}
                    or sender_type == "user"
                    and actor_type != "open_id"
                    or sender.get("tenant_key") != s.tenant_key
                ):
                    raise ValueError("Sender provenance")
                actor = ActorRef(
                    scope=instance.scope,
                    id_type=actor_type,
                    actor_id=actor_id,
                    actor_kind="human" if sender_type == "user" else "bot",
                )
                topic = message.get("thread_id") or None
                topic = opaque(topic) if topic else None
                thread = SourceThreadRef(
                    scope=instance.scope, conversation_id=chat, native_thread_id=topic
                )
                parent = message.get("parent_id") or message.get("root_id") or None
                parent = opaque(parent) if parent else None
                mentions = message.get("mentions") or []
                if not isinstance(mentions, list) or len(mentions) > 100:
                    raise ValueError("Mentions bound")
                mentioned = any(
                    isinstance(m, dict)
                    and isinstance(m.get("id"), dict)
                    and m["id"].get("open_id") == s.bot_open_id
                    and m.get("tenant_key", s.tenant_key) == s.tenant_key
                    for m in mentions
                )
                followed = any(
                    t.chat_id == chat and t.thread_id == topic for t in s.followup_topics
                )
                if message.get("chat_type") != "group" or chat not in s.chat_ids:
                    raise Filtered("FEISHU_CHAT_FILTERED", remote)
                own = actor_id in {s.bot_open_id, s.app_id}
                selected = (actor.actor_kind == "human" and actor_id in s.sender_open_ids) or (
                    actor.actor_kind == "bot" and actor_id in s.context_bot_open_ids
                )
                if not (historical or observation_only):
                    if own or not selected:
                        raise Filtered("FEISHU_SENDER_FILTERED", remote)
                    if s.receive_mode != "all_selected" and not mentioned and not followed:
                        raise Filtered("FEISHU_ADDRESS_FILTERED", remote)
                supported = message["message_type"] in {"text", "post"}
                if not supported and not (historical or observation_only):
                    raise Filtered("FEISHU_CONTENT_UNSUPPORTED", remote)
                # Current authenticated history may reveal a changed unsupported resource.
                # Preserve only its type/hash as a version fact, never attachment contents.
                content = (
                    parsed(message["content"].encode())
                    if supported and not observation_only
                    else {"unsupported_content_sha256": digest(message["content"])}
                )
                created = milliseconds(message["create_time"])
                updated = message.get("update_time")
                occurred = milliseconds(
                    message["create_time"] if updated in (None, "", 0, "0") else updated
                )
                addressed = "mention" if mentioned else "ambient"
                version = digest(
                    [
                        chat,
                        message_id,
                        actor.model_dump(mode="json"),
                        topic,
                        parent,
                        message["message_type"],
                        content,
                        created.isoformat(),
                        occurred.isoformat(),
                        addressed,
                    ]
                )
                encoded = json.dumps(
                    {
                        "schema_version": "feishu-content-v1",
                        "message_type": message["message_type"],
                        "content": content,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                body = (
                    encoded
                    if (
                        not observation_only
                        and supported
                        and selected
                        and not own
                        and retention_selected
                        and len(encoded) <= 65536
                    )
                    else None
                )
                operation = "create"
            ev = SourceEvent(
                scope=instance.scope,
                project_id=instance.project_id,
                source_revision=instance.revision,
                delivery_id=delivery,
                remote_event_id=remote,
                entity_id=message_id,
                entity_revision=EntityRevision(origin="derived", value=version),
                event_kind=kind,
                source_thread=thread,
                reply_to_entity_id=parent,
                sender=actor,
                addressed=addressed,
                occurred_at=occurred,
                received_at=now,
                body_ref=hashlib.sha256(body).hexdigest() if body is not None else None,
                verification_ref="feishu-installation-proof"
                if method == "installation_proof"
                else "feishu-webhook-signature",
                historical_backfill=historical,
                provenance="verified_source",
            )
            return ItemMetadata(
                event=ev,
                entity_namespace="feishu-message:" + digest(chat),
                operation=operation,
                native_order=None,
                platform_moderation=True,
            ), body
        except DomainError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
            raise DomainError(
                "FEISHU_INVALID_EVENT", "Feishu event metadata/content is invalid", 403
            ) from None

    def delivery(
        self,
        instance: SourceInstance,
        envelope: TransportEnvelope,
        transport: str,
        *,
        observation_only: bool = False,
    ) -> VerifiedDelivery:
        raw, delivery, method = self.authenticated(instance, envelope, transport=transport)
        if method == "challenge":
            raise DomainError("FEISHU_CHALLENGE_NOT_EVENT", "Challenge is not a source input", 409)
        item, body = self.normalize_raw(
            instance, raw, delivery, method, observation_only=observation_only
        )
        return VerifiedDelivery(
            DeliveryMetadata(
                source_instance_id=instance.scope.source_instance_id,
                source_revision=instance.revision,
                delivery_id=delivery,
                payload_sha256=hashlib.sha256(envelope.payload).hexdigest(),
                verified_at=datetime.now(UTC),
                verification_method=method,
                items=(item,),
            ),
            (body,),
        )

    def verify_ingress(
        self, instance: SourceInstance, envelope: TransportEnvelope
    ) -> VerifiedDelivery:
        return self.delivery(instance, envelope, "socket" if self._socket else "webhook")

    def verify_identity(
        self, instance: SourceInstance, envelope: TransportEnvelope
    ) -> VerifiedSubject:
        d = self.verify_ingress(instance, envelope)
        item = d.metadata.items[0]
        if item.operation == "delete":
            raise DomainError(
                "FEISHU_RECALL_NO_HUMAN_PROOF", "Recall has no verified human operator", 403
            )
        return VerifiedSubject(
            actor=item.event.sender,
            source_thread=item.event.source_thread,
            remote_event_id=item.event.remote_event_id,
            payload_sha256=d.metadata.payload_sha256,
            occurred_at=item.event.occurred_at,
            verification_method=d.metadata.verification_method,
        )

    def verify(self, instance: SourceInstance, envelope: TransportEnvelope) -> VerifiedEnvelope:
        delivery = self.verify_ingress(instance, envelope)
        capsule = json.dumps(delivery.metadata.items[0].event.model_dump(mode="json")).encode()
        return VerifiedEnvelope(
            instance.scope, delivery.metadata.items[0].event.verification_ref, capsule
        )

    def normalize(
        self, instance: SourceInstance, envelope: VerifiedEnvelope
    ) -> tuple[SourceEvent, ...]:
        if envelope.scope != instance.scope or envelope.verification_ref not in {
            "feishu-installation-proof",
            "feishu-webhook-signature",
        }:
            raise DomainError("FEISHU_VERIFIED_SCOPE", "Adapter capsule scope mismatch", 403)
        event = SourceEvent.model_validate(parsed(envelope.payload))
        if event.verification_ref != envelope.verification_ref:
            raise DomainError("FEISHU_VERIFIED_SCOPE", "Adapter capsule proof mismatch", 403)
        return (event,)

    def fetch_context(self, instance: SourceInstance, request: ContextRequest) -> ContextPage:
        raise DomainError(
            "FEISHU_HISTORY_GATEWAY_REQUIRED", "Use current-authorized Gateway.history", 409
        )

    def health(self, instance: SourceInstance) -> SourceHealth:
        return SourceHealth(
            scope=instance.scope,
            observed_at=datetime.now(UTC),
            connection="disabled",
            permissions="unknown",
        )
