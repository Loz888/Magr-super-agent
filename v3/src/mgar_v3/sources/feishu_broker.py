"""Trusted bounded Feishu reads; no send endpoint, public token API or token cache."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import urlencode

import httpx
from psycopg.pq import TransactionStatus

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.domain import ActorRef, SourceInstance
from mgar_v3.sources.feishu import FeishuAdapter, milliseconds, opaque
from mgar_v3.sources.feishu_domain import HISTORY_SCOPE, RECALL, RECEIVE, FeishuSettings
from mgar_v3.sources.feishu_response import decode_response
from mgar_v3.sources.feishu_topics import require_native_topic
from mgar_v3.sources.ingress_domain import (
    DeliveryMetadata,
    GapDeclaration,
    HistoryPage,
    HistoryRequest,
    VerifiedDelivery,
)
from mgar_v3.super_agent.policy_domain import PolicyDefinition

if TYPE_CHECKING:
    from mgar_v3.sources.feishu_runtime import FeishuRuntime

API = "https://open.feishu.cn"
MAX_RESPONSE = 2097152
MAX_PAGES = 100
TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
HISTORY_PATH = "/open-apis/im/v1/messages"
BOT_PATH = "/open-apis/bot/v3/info"


class FeishuReadBroker:
    def __init__(self, runtime: FeishuRuntime, *, transport: httpx.BaseTransport | None = None):
        self.runtime, self.sources = runtime, runtime.sources
        self.source_id, self.revision = runtime.source_id, runtime.revision
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise ValueError("Only explicit MockTransport is accepted for controlled engineering")
        if (
            runtime.activation is not None
            and runtime.activation.mode == "controlled"
            and not isinstance(transport, httpx.MockTransport)
        ):
            raise ValueError(
                "Controlled activation requires MockTransport; real network is disabled"
            )
        self._transport = transport
        self._cursor_key = secrets.token_bytes(32)
        self._blocked_until: datetime | None = None

    def current(self, operation: str) -> FeishuSettings:
        settings = self.runtime.current(operation)
        if self._blocked_until and datetime.now(UTC) < self._blocked_until:
            raise DomainError("FEISHU_RATE_LIMITED", "Wait for the recorded rate window", 429)
        return settings

    def _health(self, code: str, permission: str = "unknown") -> None:
        self.sources.observe_health(
            self.source_id,
            self.revision,
            self.runtime._mode(),
            connection="degraded",
            permissions=permission,
            code=code,
        )

    def _denied(self, code: str, permission: str) -> NoReturn:
        self.runtime.fence(code, permission)
        raise DomainError(code, "Feishu read authority unavailable; no automatic retry", 403)

    def _request(
        self, method: str, path: str, token: str | None = None, body: dict[str, str] | None = None
    ) -> dict[str, Any]:
        base = path.split("?", 1)[0]
        if (
            (method, base) not in {("POST", TOKEN_PATH), ("GET", HISTORY_PATH), ("GET", BOT_PATH)}
            or "://" in path
            or "\\" in path
            or "#" in path
            or ".." in path
        ):
            raise DomainError(
                "FEISHU_ENDPOINT_DENIED", "Only fixed Feishu read endpoints allowed", 403
            )
        if self.sources.engine.connection.info.transaction_status != TransactionStatus.IDLE:
            raise DomainError(
                "FEISHU_NETWORK_IN_TRANSACTION", "Remote reads must be outside SQL", 409
            )
        headers = {"Accept": "application/json", "User-Agent": "MAGR-source-feishu-v1"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        try:
            with httpx.Client(
                transport=self._transport, timeout=10, follow_redirects=False, trust_env=False
            ) as client:
                with client.stream(method, API + path, headers=headers, json=body) as response:
                    status = response.status_code
                    if status == 429:
                        try:
                            delay = int(response.headers.get("Retry-After", "60"))
                        except ValueError:
                            delay = 60
                        self._blocked_until = datetime.now(UTC) + timedelta(
                            seconds=max(1, min(delay, 86400))
                        )
                        self._health("FEISHU_RATE_LIMITED")
                        raise DomainError(
                            "FEISHU_RATE_LIMITED", "Rate limited; no automatic retry", 429
                        )
                    if status == 401:
                        self._denied("FEISHU_TOKEN_INVALID", "revoked")
                    if status == 403:
                        self._denied("FEISHU_PERMISSION_DENIED", "insufficient")
                    if status not in {200, 400}:
                        self._health("FEISHU_REMOTE_UNAVAILABLE")
                        raise DomainError(
                            "FEISHU_REMOTE_UNAVAILABLE", "Read unavailable; no automatic retry", 503
                        )
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE:
                            raise DomainError(
                                "FEISHU_RESPONSE_BOUND", "Feishu response exceeds 2 MiB", 413
                            )
                        chunks.append(chunk)
            value = decode_response(b"".join(chunks))
            if not isinstance(value, dict) or type(value.get("code")) is not int:
                raise DomainError("FEISHU_RESPONSE_INVALID", "Malformed Feishu response", 503)
            code = value["code"]
            if code != 0:
                # Official generic codes identify invalid credentials and denied scopes.
                # Remote error text is neither persisted nor exposed.
                if code in {99991661, 99991663, 99991664, 99991665, 99991668, 99991671, 99991677}:
                    self._denied("FEISHU_TOKEN_INVALID", "revoked")
                if code in {99991672, 99991676, 99991679} or (
                    base == HISTORY_PATH and code == 230027
                ):
                    self._denied("FEISHU_PERMISSION_DENIED", "insufficient")
                if base == HISTORY_PATH and code == 230073:
                    # There is no reviewed per-topic retained-body revocation primitive.
                    # Fence this source conservatively, without claiming global app revocation.
                    self._denied("FEISHU_THREAD_INVISIBLE", "insufficient")
                self._health("FEISHU_REMOTE_DENIED")
                raise DomainError(
                    "FEISHU_REMOTE_DENIED", "Platform rejected read; no automatic retry", 503
                )
            if status != 200:
                # An error HTTP status is never accepted because its body claims code=0.
                self._health("FEISHU_REMOTE_UNAVAILABLE")
                raise DomainError("FEISHU_REMOTE_UNAVAILABLE", "Contradictory Feishu response", 503)
            return value
        except DomainError as error:
            if error.code in {"FEISHU_RESPONSE_BOUND", "FEISHU_RESPONSE_INVALID"}:
                self._health(error.code)
            raise error from None
        except (httpx.HTTPError, OSError):
            self._health("FEISHU_REQUEST_UNKNOWN")
            raise DomainError(
                "FEISHU_REQUEST_UNKNOWN", "No usable read response; no automatic retry", 503
            ) from None

    def _token(self, operation: str, authority: Callable[[], object]) -> str:
        if operation not in {"history", "health"}:
            raise DomainError(
                "FEISHU_TOKEN_OPERATION", "Only explicit bounded read operations", 403
            )
        authority()
        settings = self.current(operation)
        value = self._request(
            "POST",
            TOKEN_PATH,
            body={"app_id": settings.app_id, "app_secret": self.runtime.secrets.app_secret},
        )
        self.current(operation)
        authority()
        token, expiry = value.get("tenant_access_token"), value.get("expire")
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 16384
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
            or type(expiry) is not int
            or not 5 < expiry <= 7200
        ):
            self._health("FEISHU_RESPONSE_INVALID")
            raise DomainError("FEISHU_RESPONSE_INVALID", "Malformed token response; no retry", 503)
        return token

    def _read(self, operation: str, path: str, authority: Callable[[], object]) -> dict[str, Any]:
        authority()
        self.current(operation)
        token = self._token(operation, authority)
        self.current(operation)
        authority()
        value = self._request("GET", path, token)
        self.current(operation)
        authority()
        return value

    def probe_permissions(self) -> dict[str, Any]:
        """Explicit identity probe; bot identity is not proof that all scopes are granted."""
        settings = self.current("health")
        value = self._read("health", BOT_PATH, lambda: self.current("health"))
        data = value.get("data", value)
        bot = data.get("bot") if isinstance(data, dict) else None
        if bot is None and isinstance(data, dict):
            bot = data
        if not isinstance(bot, dict) or not all(
            isinstance(bot.get(k), str) and bot[k] for k in ("open_id", "app_id")
        ):
            self._health("FEISHU_RESPONSE_INVALID")
            raise DomainError("FEISHU_RESPONSE_INVALID", "Malformed bot response; no retry", 503)
        if (
            bot.get("open_id") != settings.bot_open_id
            or bot.get("app_id") != settings.app_id
            or bot.get("tenant_key", settings.tenant_key) != settings.tenant_key
        ):
            self._denied("FEISHU_BOT_IDENTITY_MISMATCH", "revoked")
        self.sources.observe_health(
            self.source_id,
            self.revision,
            self.runtime._mode(),
            connection="unknown",
            permissions="unknown",
            code="FEISHU_BOT_IDENTITY_VERIFIED",
        )
        return {
            "observed_at": datetime.now(UTC),
            "connection": "unknown",
            "permissions": "unknown",
            "bot_identity_verified": True,
            "tenant_identity_verified": bot.get("tenant_key") == settings.tenant_key,
            "requested_scopes": settings.requested_scopes,
            "scopes_verified": False,
            "mode": self.runtime._mode(),
            "config_revision": self.revision,
            "live_source_channel_gate": "NOT_RUN",
        }


class FeishuHistory:
    """Bounded current group snapshots, never a claim of complete delivery/edit replay."""

    def __init__(self, broker: FeishuReadBroker):
        self.broker = broker

    def _authorized(self, instance: SourceInstance, request: HistoryRequest) -> None:
        settings = self.broker.current("history")
        if not settings.history_enabled or HISTORY_SCOPE not in settings.requested_scopes:
            raise DomainError(
                "FEISHU_HISTORY_RECEIVE_DENIED", "History is not separately enabled", 403
            )
        sources = self.broker.sources
        with sources.engine.transaction() as conn:
            _, control = sources.engine.source_ingress._control(conn, self.broker.source_id)
            if control.owner != "gateway" or control.policy_id is None:
                raise DomainError(
                    "FEISHU_HISTORY_RECEIVE_DENIED", "History owner/Receive changed", 403
                )
            row = sources.engine.source_policies._current(conn, control.policy_id)
            definition = PolicyDefinition.model_validate(row["definition"])
            sources.engine.source_policies._validate_source(conn, definition)
            thread, selector = request.source_thread, definition.selector
            now = sources.engine.source_ingress.now(conn)
            current_instance = sources.identities.registry.get_instance(self.broker.source_id)
            if (
                row["revoked"]
                or row["current_revision"] != control.policy_revision
                or not definition.valid_from <= now < definition.valid_until
                or not definition.receive.enabled
                or not definition.receive.allow_history_context
                or instance != current_instance
                or thread.scope != instance.scope
                or thread.scope != selector.scope
                or instance.scope.source_instance_id != self.broker.source_id
                or thread.conversation_id not in settings.chat_ids
                or thread.conversation_id not in selector.conversations
                or selector.native_threads
                and thread.native_thread_id not in selector.native_threads
            ):
                raise DomainError(
                    "FEISHU_HISTORY_RECEIVE_DENIED", "History scope/Receive changed", 403
                )
            require_native_topic(self.broker.runtime, instance, request, conn)
            for actor in selector.actors:
                identity = conn.execute(
                    "SELECT actor_ref FROM app.source_identities WHERE identity_key=%s "
                    "AND revoked_at IS NULL AND project_id=%s",
                    (actor.key, sources.engine.settings.project_id),
                ).fetchone()
                roles, _ = sources.engine.source_policies._roles(conn, actor, thread, None)
                if (
                    identity
                    and ActorRef.model_validate(identity["actor_ref"]) == actor
                    and "context_provider" in roles
                ):
                    return
            raise DomainError("FEISHU_HISTORY_RECEIVE_DENIED", "No current context identity", 403)

    def _cursor_tag(self, page: int, token: str, request: HistoryRequest) -> str:
        value = digest(
            [
                self.broker.revision,
                request.source_thread.model_dump(mode="json"),
                page,
                token,
            ]
        )
        mac = hmac.new(self.broker._cursor_key, value.encode(), hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def _encode_cursor(self, page: int, token: str, request: HistoryRequest) -> str:
        encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
        return f"{page}:{encoded}:{self._cursor_tag(page, token, request)}"

    def _cursor(self, value: str | None, request: HistoryRequest) -> tuple[int, str | None]:
        if value is None:
            return 1, None
        try:
            page, encoded, tag = value.split(":")
            token = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("ascii")
            if (
                not page.isascii()
                or not page.isdigit()
                or not 2 <= int(page) <= MAX_PAGES
                or not 1 <= len(token) <= 160
                or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or base64.urlsafe_b64encode(token.encode()).decode().rstrip("=") != encoded
                or not hmac.compare_digest(tag, self._cursor_tag(int(page), token, request))
            ):
                raise ValueError
            return int(page), token
        except (ValueError, UnicodeError, TypeError):
            raise DomainError(
                "FEISHU_HISTORY_CURSOR", "Invalid or cross-scope history cursor", 422
            ) from None

    def fetch(self, instance: SourceInstance, request: HistoryRequest) -> HistoryPage:
        if self.broker.sources.engine.connection.info.transaction_status != TransactionStatus.IDLE:
            raise DomainError(
                "FEISHU_NETWORK_IN_TRANSACTION", "History must start outside SQL", 409
            )
        try:
            return self._fetch(instance, request)
        except DomainError as error:
            if error.code in {
                # The outer response may be valid while a row's content JSON is not.
                "FEISHU_INVALID_EVENT",
                "FEISHU_HISTORY_BOUND",
                "FEISHU_HISTORY_INVALID",
                "FEISHU_HISTORY_CURSOR",
                "FEISHU_HISTORY_SCOPE",
                "FEISHU_HISTORY_THREAD_UNPROVEN",
            }:
                self.broker._health(error.code)
            reason = (
                "permission"
                if error.code
                in {
                    "FEISHU_HISTORY_RECEIVE_DENIED",
                    "FEISHU_PERMISSION_DENIED",
                    "FEISHU_THREAD_INVISIBLE",
                    "FEISHU_TOKEN_INVALID",
                    "FEISHU_SOURCE_STALE",
                    "FEISHU_CONFIG_FENCED",
                    "FEISHU_ACTIVATION_FENCED",
                }
                else "no_replay"
            )
            self.broker.sources.engine.source_ingress.gap(
                self.broker.source_id,
                GapDeclaration(
                    stream_id="feishu-history",
                    epoch="bounded-api",
                    reason=reason,
                    started_at=datetime.now(UTC),
                ),
            )
            raise

    def _fetch(self, instance: SourceInstance, request: HistoryRequest) -> HistoryPage:
        self._authorized(instance, request)
        if request.limit > 50:
            raise DomainError("FEISHU_HISTORY_BOUND", "At most 50 messages per page", 413)
        page, token = self._cursor(request.cursor, request)
        query: dict[str, str | int] = {
            "container_id_type": "thread" if request.source_thread.native_thread_id else "chat",
            "container_id": request.source_thread.native_thread_id
            or request.source_thread.conversation_id,
            "page_size": request.limit,
        }
        if token is not None:
            query["page_token"] = token
        result = self.broker._read(
            "history",
            HISTORY_PATH + "?" + urlencode(query),
            lambda: self._authorized(instance, request),
        )
        data = result.get("data")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("items"), list)
            or len(data["items"]) > request.limit
            or type(data.get("has_more")) is not bool
        ):
            raise DomainError("FEISHU_HISTORY_BOUND", "Invalid/beyond-bound history page", 413)
        settings = self.broker.current("history")
        adapter = FeishuAdapter(settings, self.broker.runtime.secrets)
        deliveries = []
        for row in data["items"]:
            deliveries.append(self._row(adapter, instance, request, row))
        has_more, next_token = data["has_more"], data.get("page_token")
        next_cursor = None
        if has_more:
            if (
                not isinstance(next_token, str)
                or not 1 <= len(next_token) <= 160
                or not next_token.isascii()
                or any(ord(c) < 33 or ord(c) > 126 for c in next_token)
                or next_token == token
            ):
                raise DomainError("FEISHU_HISTORY_CURSOR", "Unusable/repeated platform cursor", 409)
            if page >= MAX_PAGES:
                raise DomainError("FEISHU_HISTORY_BOUND", "History limit reached; gap remains", 413)
            next_cursor = self._encode_cursor(page + 1, next_token, request)
        self._authorized(instance, request)
        # Chat listing omits native-thread replies; exhaustion is not full chat coverage.
        complete = not has_more and request.source_thread.native_thread_id is not None
        return HistoryPage(tuple(deliveries), next_cursor, complete=complete)

    def _row(
        self, adapter: FeishuAdapter, instance: SourceInstance, request: HistoryRequest, row: Any
    ) -> VerifiedDelivery:
        try:
            if (
                not isinstance(row, dict)
                or row.get("chat_id") != request.source_thread.conversation_id
                or type(row.get("deleted", False)) is not bool
            ):
                raise ValueError
            message_id = opaque(row["message_id"])
            created = row["create_time"]
            milliseconds(created)
            updated = row.get("update_time")
            if updated in {None, 0, "0", ""}:
                updated = created
            milliseconds(updated)
            if row.get("deleted") is True:
                # Current API tombstone is an app observation, not an invented human recall actor.
                raw = {
                    "schema": "2.0",
                    "header": {
                        "event_type": RECALL,
                        "event_id": "history:" + digest([message_id, "deleted"]),
                        "app_id": adapter.settings.app_id,
                        "tenant_key": adapter.settings.tenant_key,
                        "create_time": updated,
                    },
                    "event": {
                        "chat_id": row["chat_id"],
                        "message_id": message_id,
                        "recall_time": updated,
                        "thread_id": row.get("thread_id"),
                    },
                }
            else:
                sender = row["sender"]
                identity_type = sender["id_type"]
                if (
                    identity_type not in {"open_id", "app_id"}
                    or sender.get("tenant_key") != adapter.settings.tenant_key
                ):
                    raise ValueError
                mentions = row.get("mentions") or []
                if not isinstance(mentions, list) or len(mentions) > 100:
                    raise ValueError
                mapped_mentions = []
                for mention in mentions:
                    if (
                        mention["id_type"] not in {"open_id", "user_id", "union_id", "app_id"}
                        or mention.get("tenant_key") != adapter.settings.tenant_key
                    ):
                        raise ValueError
                    mapped_mentions.append(
                        {
                            "id": {mention["id_type"]: opaque(mention["id"])},
                            "tenant_key": mention["tenant_key"],
                        }
                    )
                raw = {
                    "schema": "2.0",
                    "header": {
                        "event_type": RECEIVE,
                        "event_id": "history:"
                        + digest([message_id, updated, row.get("body"), sender, mapped_mentions]),
                        "app_id": adapter.settings.app_id,
                        "tenant_key": adapter.settings.tenant_key,
                        "create_time": updated,
                    },
                    "event": {
                        "sender": {
                            "sender_id": {identity_type: opaque(sender["id"])},
                            "id_type": identity_type,
                            "sender_type": sender["sender_type"],
                            "tenant_key": sender["tenant_key"],
                        },
                        "message": {
                            "chat_id": row["chat_id"],
                            "chat_type": "group",
                            "message_id": message_id,
                            "create_time": created,
                            "update_time": updated,
                            "root_id": row.get("root_id"),
                            "parent_id": row.get("parent_id"),
                            "thread_id": row.get("thread_id"),
                            "message_type": row["msg_type"],
                            "content": row["body"]["content"],
                            "mentions": mapped_mentions,
                        },
                    },
                }
            identifier = "history:" + digest(raw)
            item, body = adapter.normalize_raw(
                instance, raw, identifier, "installation_proof", historical=True
            )
            if row.get("deleted") is not True and row.get("msg_type") not in {"text", "post"}:
                # The trusted resource version still invalidates stale content. No unsupported
                # body is retained or replayed as if its semantics were understood.
                self.broker.sources.engine.source_ingress.gap(
                    self.broker.source_id,
                    GapDeclaration(
                        stream_id="feishu-history",
                        epoch="bounded-api",
                        reason="no_replay",
                        started_at=datetime.now(UTC),
                    ),
                )
            if (
                request.source_thread.native_thread_id is not None
                and item.event.source_thread.native_thread_id
                != request.source_thread.native_thread_id
            ):
                raise DomainError(
                    "FEISHU_HISTORY_SCOPE", "History row escapes requested topic", 403
                )
            return VerifiedDelivery(
                DeliveryMetadata(
                    source_instance_id=instance.scope.source_instance_id,
                    source_revision=instance.revision,
                    delivery_id=identifier,
                    payload_sha256=digest(raw),
                    verified_at=datetime.now(UTC),
                    verification_method="installation_proof",
                    items=(item,),
                ),
                (body,),
            )
        except DomainError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise DomainError(
                "FEISHU_HISTORY_INVALID", "History provenance is invalid", 403
            ) from None
