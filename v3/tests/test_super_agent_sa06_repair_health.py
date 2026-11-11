"""SDK cleanup and fixed diagnostics against real PG, no live platform."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sa06_support import payload, setup
from test_super_agent_sa06_health import browser_health
from test_super_agent_sa06_sdk import Socket, frame

from mgar_v3.domain import DomainError
from mgar_v3.sources.feishu_sdk import FeishuSocketClient


@pytest.mark.parametrize(
    "permission,status,code",
    [
        ("insufficient", 403, "FEISHU_PERMISSION_DENIED"),
        ("revoked", 401, "FEISHU_TOKEN_INVALID"),
    ],
)
@pytest.mark.parametrize("cleanup", ["automatic", "explicit"])
def test_disconnect_keeps_permission_diagnosis_in_db_runtime_and_browser(
    engine,
    permission,
    status,
    code,
    cleanup,
):
    env = setup(engine)

    async def scenario():
        class Receiver(Socket):
            async def recv(self):
                return frame(json.dumps(payload()).encode())

        client = FeishuSocketClient(env.runtime)
        client.connect_controlled(Receiver())
        env.response = lambda req: httpx.Response(status) if req.method == "GET" else None
        with pytest.raises(DomainError):
            env.broker.probe_permissions()
        before = env.sources.inspect(env.cookie, env.source_id)["health"]
        assert before["permissions"] == permission
        if cleanup == "automatic":
            await client._receive_message_loop()
        else:
            await client.close_controlled()
        stored = env.sources.inspect(env.cookie, env.source_id)["health"]
        assert stored["connection"] == "offline"
        assert stored["permissions"] == permission and stored["reason_code"] == code
        assert stored["observed_at"] == before["observed_at"]  # Cleanup cannot renew TTL.
        for result in (env.runtime.status(), browser_health(env)):
            assert result["permissions"] == permission and result["reason_code"] == code
            assert result["connection"] == "offline" and result["runtime_current"] is False
        with engine.transaction() as conn:
            audit = conn.execute(
                "SELECT details FROM app.feishu_source_audit WHERE "
                "event='health' AND reason_code='FEISHU_DISCONNECTED' ORDER BY audit_id DESC"
            ).fetchone()
        assert audit["details"]["connection"] == "offline"
        assert audit["details"]["preserved_permission_diagnosis"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize("seconds", [-600, 60])
def test_cleanup_cannot_renew_stale_or_future_permission_diagnosis(engine, seconds):
    env = setup(engine)
    env.sources.observe_health(
        env.source_id,
        1,
        "controlled",
        connection="degraded",
        permissions="revoked",
        code="FIXTURE_NEGATIVE",
    )
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.feishu_source_health SET observed_at=%s",
            (datetime.now(UTC) + timedelta(seconds=seconds),),
        )
    env.runtime.socket_lost()
    assert env.runtime.status()["permissions"] == "unknown"
    assert env.sources.inspect(env.cookie, env.source_id)["health"]["permissions"] == "unknown"


def test_expired_runtime_cannot_reuse_old_granted_after_close(engine):
    env = setup(engine)

    async def scenario():
        client = FeishuSocketClient(env.runtime)
        client.connect_controlled(Socket())
        env.sources.observe_health(
            env.source_id,
            1,
            "controlled",
            connection="online",
            permissions="granted",
            code="FIXTURE_GRANTED",
        )
        env.runtime.activation = replace(
            env.activation, expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
        await client.close_controlled()
        for result in (env.runtime.status(), browser_health(env)):
            assert result["permissions"] == "unknown" and result["connection"] != "online"

    asyncio.run(scenario())
