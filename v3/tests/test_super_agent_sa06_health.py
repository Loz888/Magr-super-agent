"""Controlled health display regressions with real isolated PG and browser login.

The granted observation is a synthetic internal fact for stale-authority negative
checks; it does not establish live platform scopes. Timestamp fixtures test TTL,
not elapsed long-running operation.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sa06_support import ORIGIN, setup

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError
from mgar_v3.sources.feishu_domain import FeishuConfigure
from mgar_v3.sources.ingress_domain import IngressConfigure


@pytest.fixture
def env(engine):
    return setup(engine)


def browser_health(env):
    with TestClient(
        create_app(
            env.engine.settings,
            engine=env.engine,
            feishu_runtime=env.runtime,
            run_background=False,
        ),
        base_url=ORIGIN,
    ) as client:
        login = client.post(
            "/api/v3/auth/login",
            headers={"Origin": ORIGIN},
            json={"username": "test-owner", "password": "fixture-owner-password"},
        )
        assert login.status_code == 200 and login.json()["authenticated"] is True
        response = client.get("/api/v3/feishu-sources/" + str(env.source_id))
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        return response.json()


def record_health(env, permission):
    if permission == "granted":
        code = "SYNTHETIC_HEALTH_GRANTED"
        env.sources.observe_health(
            env.source_id,
            1,
            "controlled",
            connection="online",
            permissions="granted",
            code=code,
        )
        return code
    status = 403 if permission == "insufficient" else 401
    code = "FEISHU_PERMISSION_DENIED" if status == 403 else "FEISHU_TOKEN_INVALID"
    env.response = lambda request: httpx.Response(status) if request.method == "GET" else None
    with pytest.raises(DomainError) as denied:
        env.broker.probe_permissions()
    assert denied.value.code == code
    return code


@pytest.mark.parametrize("permission", ["insufficient", "revoked"])
def test_fresh_current_negative_health_survives_fence_and_real_login(env, permission):
    code = record_health(env, permission)
    stored = env.sources.inspect(env.cookie, env.source_id)
    assert stored["stale"] is True
    assert stored["health"]["permissions"] == permission
    assert stored["health"]["reason_code"] == code
    status = env.runtime.status()
    body = browser_health(env)
    for value in (status, body):
        assert value["permissions"] == permission
        assert value["connection"] == "degraded"
        assert value["reason_code"] == code
        assert value["live_source_channel_gate"] == "NOT_RUN"


@pytest.mark.parametrize("permission", ["insufficient", "revoked", "granted"])
@pytest.mark.parametrize("offset", [timedelta(minutes=-6), timedelta(minutes=1)])
def test_expired_or_future_health_never_becomes_current(env, permission, offset):
    code = record_health(env, permission)
    with env.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.feishu_source_health SET observed_at=now()+%s WHERE source_instance_id=%s",
            (offset, env.source_id),
        )
    for value in (env.runtime.status(), browser_health(env)):
        assert value["permissions"] == "unknown"
        assert value["connection"] != "online"
        assert value.get("reason_code") != code


@pytest.mark.parametrize("permission", ["insufficient", "revoked", "granted"])
def test_new_config_cannot_inherit_late_old_revision_health(env, permission):
    code = record_health(env, permission)
    result = env.sources.configure(
        env.cookie,
        env.source_id,
        FeishuConfigure(
            expected_revision=1, settings=env.settings, review_ref="new-reviewed-health-config"
        ),
    )
    assert result["revision"] == 2
    assert env.sources.inspect(env.cookie, env.source_id)["health"] is None
    # A late old-runtime observation must not become the new config's health.
    env.sources.observe_health(
        env.source_id,
        1,
        "controlled",
        connection="online" if permission == "granted" else "degraded",
        permissions=permission,
        code=code,
    )
    for value in (env.runtime.status(), browser_health(env)):
        assert value["permissions"] == "unknown"
        assert value["connection"] != "online"
        assert value.get("reason_code") != code


@pytest.mark.parametrize("invalid", ["activation", "owner_generation"])
def test_old_granted_online_fact_does_not_survive_runtime_authority_loss(env, invalid):
    code = record_health(env, "granted")
    assert env.runtime.status()["permissions"] == "granted"
    if invalid == "activation":
        env.runtime.activation = replace(
            env.activation, expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
    else:
        env.engine.source_ingress.configure(
            env.cookie,
            env.source_id,
            IngressConfigure(
                expected_generation=1,
                settings=env.control,
                review_ref="new-owner-generation",
            ),
        )
    for value in (env.runtime.status(), browser_health(env)):
        assert value["permissions"] == "unknown"
        assert value["connection"] != "online"
        assert value.get("reason_code") != code


@pytest.mark.parametrize("permission", ["insufficient", "revoked"])
def test_other_runtime_mode_health_is_not_inherited(env, permission):
    code = record_health(env, permission)
    env.sources.observe_health(
        env.source_id,
        1,
        "live",
        connection="degraded",
        permissions=permission,
        code=code,
    )
    for value in (env.runtime.status(), browser_health(env)):
        assert value["permissions"] == "unknown"
        assert value["connection"] != "online"
        assert value.get("reason_code") != code
