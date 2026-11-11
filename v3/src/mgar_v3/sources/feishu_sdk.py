"""Bounded binding of the pinned official Feishu websocket SDK.

Only a server-composed Runtime can commit incoming data. Controlled sockets are
explicitly injected and never bootstrap a network connection. Native SDK dispatch
and ACK serialization remain in lark-oapi; the typed event is not identity proof.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lark_oapi import EventDispatcherHandler, LogLevel
from lark_oapi.ws import Client
from lark_oapi.ws.enum import FrameType, MessageType
from lark_oapi.ws.model import ClientConfig
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

from mgar_v3.domain import DomainError

if TYPE_CHECKING:
    from mgar_v3.sources.feishu_runtime import FeishuRuntime

SDK_VERSION = "1.7.5"
SDK_IMPLEMENTATIONS = {
    "client": "ce05f37d012929721b6484e419340c2ae9ab707510e2cb414123984816df1614",
    "dispatcher": "98695075d462f9725e80545463503846905792a944494d343c3687ae2e6e0dca",
}
MAX_MESSAGE = 256 * 1024
MAX_FRAGMENTS = 16
MAX_PENDING = 32
MAX_BUFFER = 1024 * 1024
FRAGMENT_TTL = 5.0


def sdk_facts() -> dict[str, Any]:
    """The ACK assumption is checked against these exact installed sources."""
    objects = {"client": Client, "dispatcher": EventDispatcherHandler}
    hashes = {}
    for name, obj in objects.items():
        filename = inspect.getsourcefile(obj)
        if filename is None:
            raise DomainError("FEISHU_SDK_VERSION", "SDK source cannot be checked", 503)
        hashes[name] = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
    version = importlib.metadata.version("lark-oapi")
    if version != SDK_VERSION or hashes != SDK_IMPLEMENTATIONS:
        raise DomainError("FEISHU_SDK_VERSION", "Unreviewed SDK implementation", 503)
    return {"version": version, "sha256": hashes, "ack": "after_durable_handler",
            "automatic_replay": "unknown"}


@dataclass
class _Fragments:
    count: int
    started: float
    parts: dict[int, bytes] = field(default_factory=dict)


@dataclass
class _Dispatch:
    payload: bytes
    delivery_id: str
    epoch: str
    committed: bool = False


class FeishuSocketClient(Client):  # type: ignore[misc]  # Pinned SDK has no type stubs.
    """Official ws.Client with bounded intake and server-only activation fencing."""

    _conn: Any

    def __init__(self, runtime: FeishuRuntime, *,
                 live_gate: Callable[[], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.runtime = runtime
        self._live_gate, self._clock = live_gate, clock
        self._epoch = ""
        self._fragments: dict[str, _Fragments] = {}
        self._buffered = 0
        self._pending = 0
        self._active: _Dispatch | None = None
        self._intake_lock = asyncio.Lock()
        self.last_error: str | None = None
        sdk_facts()
        handler = (EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self._dispatch)
                   .register_p2_im_message_recalled_v1(self._dispatch).build())
        # Do not ask Runtime for permission at construction: a disabled client is
        # safe to construct. Every activation and incoming frame checks it anew.
        settings = runtime.sources.current(runtime.source_id)[1]
        super().__init__(settings.app_id, runtime.secrets.app_secret,
                         log_level=LogLevel.ERROR, event_handler=handler,
                         auto_reconnect=False)
        # The upstream unbounded cache has an otherwise idle cron task. We use
        # our own bounded, epoch-local fragment map, not that cache.
        self._cache._cron.cancel()
        self._reconnect_count, self._reconnect_interval = 3, 30
        self._reconnect_nonce, self._ping_interval = 0, 120

    def _fmt_log(self, fmt: str, *args: Any) -> str:
        # Upstream passes raw payloads, connection URLs and arbitrary exception
        # text through this function, including at DEBUG. Drop every argument.
        return "Feishu SDK transport event"

    def _require_live(self) -> None:
        self.runtime.current("socket")
        activation = self.runtime.activation
        if (activation is None or activation.mode != "live"
                or self._live_gate is None or self._live_gate() is not True):
            raise DomainError("FEISHU_LIVE_NOT_AUTHORIZED",
                              "Live socket requires a current explicit platform gate", 403)

    def start(self) -> None:
        self._require_live()
        super().start()

    def _get_conn_url(self) -> str:
        self._require_live()
        return str(super()._get_conn_url())

    async def _connect(self) -> None:
        self._require_live()
        if self._conn is not None:
            return
        await super()._connect()
        self._epoch = self.runtime.socket_connected()

    async def _reconnect(self) -> None:
        self._require_live()
        # Server-provided configuration cannot request infinite retry.
        self._reconnect_count = min(max(int(self._reconnect_count), 0), 3)
        await super()._reconnect()

    def connect_controlled(self, socket: Any) -> str:
        """Attach a fake transport from explicit server composition, never HTTP."""
        self.runtime.current("socket")
        activation = self.runtime.activation
        if activation is None or activation.mode != "controlled":
            raise DomainError("FEISHU_CONTROLLED_REQUIRED", "Controlled activation required", 403)
        if self._conn is not None or not callable(getattr(socket, "send", None)):
            raise DomainError("FEISHU_SOCKET_STATE", "Invalid controlled socket state", 409)
        self._epoch = self.runtime.socket_connected()
        self._conn = socket
        self._conn_id, self._service_id = "controlled", "controlled"
        return self._epoch

    async def _disconnect(self) -> None:
        self._epoch = ""
        self._fragments.clear()
        self._buffered = 0
        try:
            self.runtime.socket_lost()
        finally:
            await super()._disconnect()

    async def close_controlled(self) -> None:
        if self.runtime.activation is None or self.runtime.activation.mode != "controlled":
            raise DomainError("FEISHU_CONTROLLED_REQUIRED", "Controlled activation required", 403)
        await self._disconnect()

    async def _receive_message_loop(self) -> None:
        # The official loop creates one task per incoming message without a
        # bound. Serial awaiting here provides actual socket backpressure.
        connection, epoch = self._conn, self._epoch
        try:
            while connection is not None and connection is self._conn and epoch == self._epoch:
                raw = await connection.recv()
                await self.feed_frame(raw, epoch=epoch)
        except Exception:
            # A late failure from an older recv loop must not disconnect a
            # newer authenticated session installed while that recv awaited.
            if connection is self._conn and epoch == self._epoch:
                self.last_error = "FEISHU_SOCKET_LOST"
                await self._disconnect()
                if self._auto_reconnect:
                    await self._reconnect()

    def _frame_headers(self, frame: Any) -> dict[str, str]:
        if len(frame.headers) > 32:
            raise DomainError("FEISHU_FRAME_HEADERS", "Too many SDK headers", 400)
        result: dict[str, str] = {}
        for item in frame.headers:
            if (not item.key or item.key in result or len(item.key) > 128
                    or len(item.value) > 1024):
                raise DomainError("FEISHU_FRAME_HEADERS", "Ambiguous SDK headers", 400)
            result[item.key] = item.value
        return result

    def _validate(self, raw: bytes) -> Any:
        if not isinstance(raw, bytes) or len(raw) > MAX_MESSAGE + 8192:
            raise DomainError("FEISHU_FRAME_BOUND", "SDK frame is too large", 413)
        frame = Frame()
        try:
            frame.ParseFromString(raw)
            if not frame.IsInitialized():
                raise ValueError
            headers = self._frame_headers(frame)
            if frame.method == FrameType.DATA.value:
                if (headers.get("type") != MessageType.EVENT.value
                        or not headers.get("message_id")
                        or not headers.get("sum", "").isascii()
                        or not headers.get("sum", "").isdecimal()
                        or not headers.get("seq", "").isascii()
                        or not headers.get("seq", "").isdecimal()):
                    raise ValueError
                count, seq = int(headers["sum"]), int(headers["seq"])
                if not 1 <= count <= MAX_FRAGMENTS or not 0 <= seq < count:
                    raise ValueError
                if len(frame.payload) > MAX_MESSAGE:
                    raise ValueError
                if count == 1:
                    frame.payload.decode("utf-8", errors="strict")
            elif frame.method == FrameType.CONTROL.value:
                if headers.get("type") not in {"ping", "pong"} or len(frame.payload) > 4096:
                    raise ValueError
            else:
                raise ValueError
        except DomainError:
            raise
        except Exception:
            raise DomainError("FEISHU_FRAME_INVALID", "Invalid native SDK frame", 400) from None
        return frame

    async def feed_frame(self, raw: bytes, *, epoch: str | None = None) -> None:
        """Controlled and live inputs both invoke the actual official SDK path."""
        self.runtime.current("socket")
        expected = self._epoch if epoch is None else epoch
        if not expected or expected != self._epoch or self._conn is None:
            raise DomainError("FEISHU_SOCKET_FENCED", "Stale socket session", 403)
        frame = self._validate(raw)
        if self._pending >= MAX_PENDING:
            raise DomainError("FEISHU_FRAME_CAPACITY", "Bounded SDK intake is full", 503)
        self._pending += 1
        try:
            async with self._intake_lock:
                self.runtime.current("socket")
                if expected != self._epoch or self._conn is None:
                    raise DomainError("FEISHU_SOCKET_FENCED", "Stale socket session", 403)
                headers = self._frame_headers(frame)
                self._active = _Dispatch(frame.payload, headers.get("message_id", ""), expected)
                try:
                    # Parse, frame dispatch, typed EventDispatcherHandler callback,
                    # ACK creation and send all use the official SDK implementation.
                    self._expire_fragments()
                    await super()._handle_message(raw)
                finally:
                    self._active = None
                    if not self._epoch:
                        # Fragment expiry already persisted the gap and fenced
                        # proof. Close its transport too; no orphan recv socket.
                        await super()._disconnect()
        finally:
            self._pending -= 1

    def _dispatch(self, typed_event: Any) -> None:
        active = self._active
        if active is None or active.epoch != self._epoch:
            raise DomainError("FEISHU_SOCKET_FENCED", "No active SDK frame", 403)
        # The typed event exists to exercise SDK dispatch; authorization must use
        # the unchanged original bytes and authenticated connection capsule.
        receipt = self.runtime.receive_socket(active.payload, active.delivery_id, active.epoch)
        if not receipt.committed:
            raise DomainError("FEISHU_NOT_COMMITTED", "Durable commit is required", 503)
        active.committed = True

    async def _write_message(self, data: bytes) -> None:
        # Upstream resolves self._conn only AFTER awaiting its send lock. A
        # queued ACK must remain bound to the exact authenticated frame session.
        connection = self._conn
        epoch = self._active.epoch if self._active is not None else self._epoch
        if connection is None or not epoch or epoch != self._epoch:
            raise DomainError("FEISHU_SOCKET_FENCED", "Stale SDK send session", 403)
        async with self._lock:
            if connection is not self._conn or epoch != self._epoch:
                raise DomainError("FEISHU_SOCKET_FENCED", "SDK send session changed", 403)
            if self._active is None or self._active.committed:
                self.runtime.current("socket")
            # A callback failure may be the PG outage itself. A negative SDK
            # response on the same authenticated socket does not require a new
            # business transaction; successful ACKs still require commit/current.
            # Keep the captured object even while send awaits: a reconnect must
            # never redirect an older session's acknowledgement to a new socket.
            await connection.send(data)

    def _expire_fragments(self) -> None:
        now = self._clock()
        if any(now - value.started >= FRAGMENT_TTL for value in self._fragments.values()):
            # Check on every incoming frame, including complete messages: an
            # unrelated full frame must not bypass an already known lost fragment.
            self._fragments.clear()
            self._buffered = 0
            self._epoch = ""
            self.runtime.socket_lost("FEISHU_FRAGMENT_EXPIRED")
            raise DomainError("FEISHU_FRAGMENT_EXPIRED", "Incomplete message expired", 503)

    def _combine(self, msg_id: str, sum_: int, seq: int, bs: bytes) -> bytes | None:
        self._expire_fragments()
        now = self._clock()
        current = self._fragments.get(msg_id)
        if current is None:
            if len(self._fragments) >= MAX_PENDING:
                raise DomainError("FEISHU_FRAME_CAPACITY", "Too many partial SDK messages", 503)
            current = _Fragments(sum_, now)
            self._fragments[msg_id] = current
        if current.count != sum_:
            raise DomainError("FEISHU_FRAGMENT_CONFLICT", "Fragment count changed", 409)
        old = current.parts.get(seq)
        if old is not None:
            if old != bs:
                raise DomainError("FEISHU_FRAGMENT_CONFLICT", "Fragment content changed", 409)
            return None
        if (sum(len(x) for x in current.parts.values()) + len(bs) > MAX_MESSAGE
                or self._buffered + len(bs) > MAX_BUFFER):
            raise DomainError("FEISHU_FRAME_CAPACITY", "SDK fragment byte limit reached", 503)
        current.parts[seq] = bs
        self._buffered += len(bs)
        if len(current.parts) != sum_:
            return None
        payload = b"".join(current.parts[i] for i in range(sum_))
        del self._fragments[msg_id]
        self._buffered -= len(payload)
        payload.decode("utf-8", errors="strict")
        if self._active is None:
            raise DomainError("FEISHU_SOCKET_FENCED", "No active SDK fragment", 403)
        self._active.payload = payload
        return payload

    def _configure(self, conf: ClientConfig) -> None:
        # These are observed protocol knobs, not evidence that anything missing
        # is replayable. Invalid or unbounded server knobs never change limits.
        values = (conf.ReconnectCount, conf.ReconnectInterval,
                  conf.ReconnectNonce, conf.PingInterval)
        if any(type(x) is not int for x in values):
            raise DomainError("FEISHU_SDK_CONFIG", "Invalid SDK heartbeat configuration", 400)
        count, interval, nonce, ping = values
        self._reconnect_count = min(max(count, 0), 3)
        self._reconnect_interval = min(max(interval, 1), 120)
        self._reconnect_nonce = min(max(nonce, 0), 30)
        self._ping_interval = min(max(ping, 10), 300)

    def protocol_facts(self) -> dict[str, Any]:
        return {**sdk_facts(), "pending_limit": MAX_PENDING, "message_bytes": MAX_MESSAGE,
                "fragment_limit": MAX_FRAGMENTS, "buffer_bytes": MAX_BUFFER,
                "fragment_ttl_seconds": FRAGMENT_TTL,
                "heartbeat_seconds": self._ping_interval,
                "reconnect_attempts": self._reconnect_count,
                "reconnect_seconds": self._reconnect_interval}
