"""Official native protobuf/dispatcher tests; no real platform connection."""
import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest
from lark_oapi.ws import Client
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

from mgar_v3.domain import DomainError
from mgar_v3.sources.feishu_sdk import (
    MAX_BUFFER,
    MAX_FRAGMENTS,
    MAX_MESSAGE,
    MAX_PENDING,
    FeishuSocketClient,
    sdk_facts,
)


class Socket:
    def __init__(self, *, lose_ack: bool = False):
        self.frames: list[bytes] = []
        self.lose_ack = lose_ack

    async def send(self, value: bytes) -> None:
        if self.lose_ack:
            self.lose_ack = False
            raise ConnectionError("ACK disappeared")
        self.frames.append(value)

    async def close(self) -> None:
        pass


def frame(raw: bytes, *, delivery: str = "native-delivery", count: int = 1, seq: int = 0,
          extra: tuple[tuple[str, str], ...] = ()) -> bytes:
    value = Frame(SeqID=1, LogID=1, service=1, method=1)
    value.payload = raw
    for key, text in (("message_id", delivery), ("trace_id", "controlled-trace"),
                      ("sum", str(count)), ("seq", str(seq)), ("type", "event"), *extra):
        item = value.headers.add()
        item.key, item.value = key, text
    return value.SerializeToString()


def codes(socket: Socket) -> list[int]:
    values = []
    for raw in socket.frames:
        value = Frame()
        value.ParseFromString(raw)
        values.append(json.loads(value.payload)["code"])
    return values


def protocol_payload() -> bytes:
    return json.dumps({"schema": "2.0",
        "header": {"event_id": "native-event", "event_type": "im.message.receive_v1",
                   "create_time": "0", "tenant_key": "TenantCase", "app_id": "AppCase"},
        "event": {"sender": {"sender_id": {"open_id": "OpenCase"}, "sender_type": "user",
                            "tenant_key": "TenantCase"},
                  "message": {"message_id": "MsgCase", "chat_id": "ChatCase",
                              "chat_type": "group", "message_type": "text",
                              "content": json.dumps({"text": "protocol-only secret"})}}}).encode()


class ProtocolRuntime:
    """Protocol-only double; PG durability is separately tested below."""
    def __init__(self):
        self.sources = SimpleNamespace(current=lambda _: (None, SimpleNamespace(app_id="app")))
        self.source_id = "protocol-only"
        self.secrets = SimpleNamespace(app_secret="never-log-app-secret")
        self.activation = SimpleNamespace(mode="controlled")
        self.epoch = ""
        self.counter = 0
        self.revision = 1
        self.calls: list[tuple[bytes, str, str]] = []
        self.gaps: list[str] = []
        self.fail: Exception | None = None
        self.before_commit = None

    def current(self, operation: str) -> Any:
        if self.revision != 1:
            raise DomainError("FEISHU_CONFIG_FENCED", "Changed", 403)
        return SimpleNamespace(app_id="app")

    def socket_connected(self) -> str:
        self.counter += 1
        self.epoch = str(self.counter)
        return self.epoch

    def socket_lost(self, code: str = "FEISHU_DISCONNECTED") -> None:
        self.gaps.append(code)
        self.epoch = ""

    def receive_socket(self, raw: bytes, delivery: str, epoch: str) -> Any:
        if self.fail:
            raise self.fail
        if self.before_commit:
            self.before_commit()
        assert epoch == self.epoch
        self.calls.append((raw, delivery, epoch))
        return SimpleNamespace(committed=True)


def test_native_sdk_dispatch_before_ack_and_raw_bytes():
    async def scenario():
        runtime, socket = ProtocolRuntime(), Socket()
        client = FeishuSocketClient(runtime)
        assert isinstance(client, Client)
        client.connect_controlled(socket)
        def before_commit():
            assert not socket.frames
        runtime.before_commit = before_commit
        await client.feed_frame(frame(protocol_payload()))
        assert runtime.calls[0][0] == protocol_payload()
        assert runtime.calls[0][1] == "native-delivery"
        assert codes(socket) == [200]
        await client.close_controlled()
    asyncio.run(scenario())


def test_native_callback_failure_ack500_and_no_dynamic_logs(caplog):
    async def scenario():
        runtime, socket = ProtocolRuntime(), Socket()
        client = FeishuSocketClient(runtime)
        client.connect_controlled(socket)
        runtime.fail = RuntimeError("secret-body-token-url https://secret.invalid/token")
        with caplog.at_level(logging.DEBUG, logger="Lark"):
            await client.feed_frame(frame(protocol_payload()))
        assert codes(socket) == [500]
        assert not runtime.calls
        assert "secret-body-token-url" not in caplog.text
        assert "protocol-only secret" not in caplog.text
        assert "https://secret.invalid" not in caplog.text
        assert client._fmt_log("{}", "sensitive") == "Feishu SDK transport event"
        await client.close_controlled()
    asyncio.run(scenario())


@pytest.mark.parametrize("raw", [
    frame(protocol_payload(), extra=(("message_id", "duplicate"),)),
    frame(bytes([255])),
    frame(protocol_payload(), count=0),
    frame(protocol_payload(), count=MAX_FRAGMENTS + 1),
    frame(protocol_payload(), count=2, seq=2),
    frame(b"x" * (MAX_MESSAGE + 1)),
])
def test_invalid_native_frame_never_ack_or_dispatch(raw):
    async def scenario():
        runtime, socket = ProtocolRuntime(), Socket()
        client = FeishuSocketClient(runtime)
        client.connect_controlled(socket)
        with pytest.raises(DomainError):
            await client.feed_frame(raw)
        assert not socket.frames and not runtime.calls
        await client.close_controlled()
    asyncio.run(scenario())


def test_official_fragment_assembly_and_exact_retransmission():
    async def scenario():
        runtime, socket = ProtocolRuntime(), Socket()
        client = FeishuSocketClient(runtime)
        client.connect_controlled(socket)
        raw = protocol_payload()
        split = len(raw) // 2
        first = frame(raw[:split], count=2)
        await client.feed_frame(first)
        await client.feed_frame(first)
        assert not socket.frames and not runtime.calls
        await client.feed_frame(frame(raw[split:], count=2, seq=1))
        assert codes(socket) == [200]
        assert runtime.calls[0][0] == raw
        assert client._buffered == 0 and not client._fragments
        await client.close_controlled()
    asyncio.run(scenario())


@pytest.mark.parametrize("complete_next", [False, True])
def test_fragment_expiry_fences_connection_and_records_gap(complete_next):
    async def scenario():
        now = [0.0]
        runtime, socket = ProtocolRuntime(), Socket()
        client = FeishuSocketClient(runtime, clock=lambda: now[0])
        old_epoch = client.connect_controlled(socket)
        await client.feed_frame(frame(b'{"partial":', count=2))
        now[0] = 5.0
        next_frame = (frame(protocol_payload()) if complete_next
                      else frame(b"null}", count=2, seq=1))
        with pytest.raises(DomainError, match="expired"):
            await client.feed_frame(next_frame)
        assert runtime.gaps == ["FEISHU_FRAGMENT_EXPIRED"]
        assert not socket.frames and not runtime.calls
        with pytest.raises(DomainError, match="Stale"):
            await client.feed_frame(frame(protocol_payload()), epoch=old_epoch)
        await client.close_controlled()
    asyncio.run(scenario())


def test_partial_count_and_bytes_bounded():
    async def scenario():
        runtime, socket = ProtocolRuntime(), Socket()
        client = FeishuSocketClient(runtime)
        client.connect_controlled(socket)
        for index in range(MAX_PENDING + 5):
            await client.feed_frame(frame(b"x", delivery=f"pending-{index}", count=2))
        assert len(client._fragments) == MAX_PENDING
        assert not socket.frames and not runtime.calls
        assert client._buffered <= MAX_BUFFER
        await client.close_controlled()
        runtime, socket = ProtocolRuntime(), Socket()
        client = FeishuSocketClient(runtime)
        client.connect_controlled(socket)
        for index in range(10):
            await client.feed_frame(frame(b"x" * (MAX_MESSAGE // 2),
                                         delivery=f"bytes-{index}", count=2))
        assert client._buffered == MAX_BUFFER
        assert not socket.frames and not runtime.calls
        await client.close_controlled()
    asyncio.run(scenario())


def test_controlled_network_and_stale_epoch_fenced():
    async def scenario():
        runtime, socket = ProtocolRuntime(), Socket()
        client = FeishuSocketClient(runtime)
        old = client.connect_controlled(socket)
        with pytest.raises(DomainError, match="Live socket"):
            client.start()
        with pytest.raises(DomainError, match="Live socket"):
            await client._connect()
        with pytest.raises(DomainError, match="Live socket"):
            client._get_conn_url()
        await client.close_controlled()
        client.connect_controlled(Socket())
        with pytest.raises(DomainError, match="Stale"):
            await client.feed_frame(frame(protocol_payload()), epoch=old)
        runtime.revision = 2
        with pytest.raises(DomainError, match="Changed"):
            await client.feed_frame(frame(protocol_payload()))
        assert not runtime.calls
        runtime.revision = 1
        await client.close_controlled()
    asyncio.run(scenario())


def test_pinned_official_sdk_ack_implementation():
    facts = sdk_facts()
    assert facts["version"] == "1.7.5"
    assert facts["ack"] == "after_durable_handler"
    assert facts["automatic_replay"] == "unknown"
    assert len(facts["sha256"]) == 2


def _pg_inputs(engine, source_id):
    with engine.transaction() as conn:
        return conn.execute("SELECT input_id FROM app.source_ingress_inputs "
                            "WHERE source_instance_id=%s", (source_id,)).fetchall()


def test_native_ack_real_pg_commit_duplicate_and_lost_ack(engine):
    from sa06_support import payload, setup
    env = setup(engine)
    raw = json.dumps(payload(event_id="pg-native-event")).encode()
    async def scenario():
        socket = Socket(lose_ack=True)
        client = FeishuSocketClient(env.runtime)
        client.connect_controlled(socket)
        await client.feed_frame(frame(raw))
        # SDK send failed, but the original Engine already durably committed.
        assert not socket.frames
        first = _pg_inputs(engine, env.source_id)
        assert len(first) == 1
        assert engine.source_ingress.body(env.cookie, first[0]["input_id"])
        await client.close_controlled()
        # Reconnection has a new authenticated epoch; no replay is assumed.
        client.connect_controlled(socket)
        await client.feed_frame(frame(raw))
        await client.feed_frame(frame(raw, delivery="native-redelivery"))
        assert codes(socket) == [200, 200]
        assert _pg_inputs(engine, env.source_id) == first
        assert not env.calls
        await client.close_controlled()
    asyncio.run(scenario())


def test_native_real_pg_cross_tenant_and_current_fencing(engine):
    from sa06_support import payload, setup
    env = setup(engine)
    async def scenario():
        socket = Socket()
        client = FeishuSocketClient(env.runtime)
        client.connect_controlled(socket)
        value = payload()
        value["header"]["tenant_key"] = "other-tenant"
        await client.feed_frame(frame(json.dumps(value).encode()))
        assert codes(socket) == [500]
        assert not _pg_inputs(engine, env.source_id)
        from mgar_v3.sources.feishu_domain import FeishuConfigure
        env.sources.configure(env.cookie, env.source_id, FeishuConfigure(
            expected_revision=1, settings=env.settings.model_copy(update={"enabled": False}),
            review_ref="SDK-config-revoked"))
        with pytest.raises(DomainError):
            await client.feed_frame(frame(json.dumps(payload()).encode()))
        assert codes(socket) == [500]
        assert not _pg_inputs(engine, env.source_id)
        await client.close_controlled()
    asyncio.run(scenario())


def test_native_real_pg_unselected_chat_is_durable_filter(engine):
    from sa06_support import payload, setup
    env = setup(engine)
    async def scenario():
        socket = Socket()
        client = FeishuSocketClient(env.runtime)
        client.connect_controlled(socket)
        value = payload()
        value["event"]["message"]["chat_id"] = "UnselectedChat"
        raw = json.dumps(value).encode()
        await client.feed_frame(frame(raw))
        assert codes(socket) == [200]
        assert not _pg_inputs(engine, env.source_id)
        with engine.transaction() as conn:
            rows = conn.execute("SELECT * FROM app.feishu_transport_filters "
                                "WHERE source_instance_id=%s", (env.source_id,)).fetchall()
        assert len(rows) == 1
        assert raw.decode() not in str(rows)
        await client.close_controlled()
    asyncio.run(scenario())


def test_native_pg_callback_connection_failure_ack500_then_restart(engine):
    from sa06_support import payload, setup

    from mgar_v3.coordination.engine import Engine
    from mgar_v3.sources.feishu_runtime import FeishuRuntime
    from mgar_v3.sources.feishu_store import FeishuSources
    env = setup(engine)
    cipher = engine.source_ingress.cipher
    raw = json.dumps(payload(event_id="pg-failure-event")).encode()
    async def scenario():
        socket = Socket()
        client = FeishuSocketClient(env.runtime)
        client.connect_controlled(socket)
        original = env.runtime.receive_socket
        def broken(*args, **kwargs):
            engine.close()  # Real PG session loss at the official dispatcher boundary.
            return original(*args, **kwargs)
        env.runtime.receive_socket = broken
        await client.feed_frame(frame(raw))
        assert codes(socket) == [500]
        with pytest.raises(psycopg.OperationalError):
            await client.close_controlled()
        recovered = Engine(engine.settings, engine.artifacts)
        try:
            recovered.source_ingress.cipher = cipher
            sources = FeishuSources(recovered)
            runtime = FeishuRuntime(sources, env.source_id, 1, 1, env.secrets, env.activation,
                                   transport=httpx.MockTransport(env.handler))
            restarted = FeishuSocketClient(runtime)
            restarted.connect_controlled(socket)
            assert not _pg_inputs(recovered, env.source_id)
            await restarted.feed_frame(frame(raw))
            await restarted.feed_frame(frame(raw))
            assert codes(socket) == [500, 200, 200]
            assert len(_pg_inputs(recovered, env.source_id)) == 1
            await restarted.close_controlled()
        finally:
            recovered.close()
    asyncio.run(scenario())


def test_sdk_pending_feed_queue_is_bounded():
    async def scenario():
        runtime = ProtocolRuntime()
        release = asyncio.Event()
        class BlockingSocket(Socket):
            async def send(self, value):
                await release.wait()
                await super().send(value)
        socket = BlockingSocket()
        client = FeishuSocketClient(runtime)
        client.connect_controlled(socket)
        tasks = [asyncio.create_task(client.feed_frame(frame(protocol_payload())))
                 for _ in range(MAX_PENDING + 1)]
        await asyncio.sleep(0)
        assert client._pending == MAX_PENDING
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(isinstance(x, DomainError) for x in results) == 1
        assert len(socket.frames) == MAX_PENDING
        assert client._pending == 0
        await client.close_controlled()
    asyncio.run(scenario())


def test_late_old_receive_loop_cannot_disconnect_new_session():
    async def scenario():
        runtime = ProtocolRuntime()
        pending = asyncio.Future()
        class DelayedSocket(Socket):
            async def recv(self):
                return await pending
        old_socket, new_socket = DelayedSocket(), Socket()
        client = FeishuSocketClient(runtime)
        client.connect_controlled(old_socket)
        loop = asyncio.create_task(client._receive_message_loop())
        await asyncio.sleep(0)
        await client.close_controlled()
        current = client.connect_controlled(new_socket)
        pending.set_result(frame(protocol_payload()))
        await loop
        assert client._conn is new_socket
        assert client._epoch == current
        assert not old_socket.frames and not new_socket.frames and not runtime.calls
        await client.feed_frame(frame(protocol_payload()))
        assert codes(new_socket) == [200]
        await client.close_controlled()
    asyncio.run(scenario())


def test_missing_required_native_proto_fields_rejected():
    async def scenario():
        runtime, socket = ProtocolRuntime(), Socket()
        client = FeishuSocketClient(runtime)
        client.connect_controlled(socket)
        malformed = Frame(method=1).SerializePartialToString()
        with pytest.raises(DomainError):
            await client.feed_frame(malformed)
        assert not socket.frames and not runtime.calls
        await client.close_controlled()
    asyncio.run(scenario())


def test_lost_ack_durable_after_new_engine_pg_connection(engine):
    from sa06_support import payload, setup

    from mgar_v3.coordination.engine import Engine
    from mgar_v3.sources.feishu_runtime import FeishuRuntime
    from mgar_v3.sources.feishu_store import FeishuSources
    env = setup(engine)
    cipher = engine.source_ingress.cipher
    raw = json.dumps(payload(event_id="restart-lost-ack")).encode()
    async def scenario():
        socket = Socket(lose_ack=True)
        first_client = FeishuSocketClient(env.runtime)
        first_client.connect_controlled(socket)
        await first_client.feed_frame(frame(raw))
        original = _pg_inputs(engine, env.source_id)
        assert len(original) == 1 and not socket.frames
        await first_client.close_controlled()
        engine.close()
        recovered = Engine(engine.settings, engine.artifacts)
        try:
            recovered.source_ingress.cipher = cipher
            runtime = FeishuRuntime(FeishuSources(recovered), env.source_id, 1, 1,
                env.secrets, env.activation, transport=httpx.MockTransport(env.handler))
            second_client = FeishuSocketClient(runtime)
            second_client.connect_controlled(socket)
            await second_client.feed_frame(frame(raw))
            assert codes(socket) == [200]
            assert _pg_inputs(recovered, env.source_id) == original
            await second_client.close_controlled()
        finally:
            recovered.close()
    asyncio.run(scenario())


def test_queued_native_ack_cannot_cross_replacement_connection():
    async def scenario():
        runtime = ProtocolRuntime()
        old, new = Socket(), Socket()
        client = FeishuSocketClient(runtime)
        client.connect_controlled(old)
        await client._lock.acquire()
        async def reconnect():
            await client.close_controlled()
            client.connect_controlled(new)
        reconnecting = asyncio.create_task(reconnect())
        await asyncio.sleep(0)
        native_ack = frame(b'{"code":200}')
        writer = asyncio.create_task(client._write_message(native_ack))
        await asyncio.sleep(0)
        client._lock.release()
        await reconnecting
        outcomes = await asyncio.gather(writer, return_exceptions=True)
        assert isinstance(outcomes[0], DomainError)
        assert outcomes[0].code == "FEISHU_SOCKET_FENCED"
        assert not old.frames and not new.frames
        # New-session dispatch remains functional and sends its own ACK only.
        await client.feed_frame(frame(protocol_payload()))
        assert codes(new) == [200] and not old.frames
        await client.close_controlled()
    asyncio.run(scenario())
