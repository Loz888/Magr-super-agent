"""Hostile bounded platform formats never escape fixed diagnostics or release data."""

import httpx
import pytest
from sa06_support import setup
from test_super_agent_sa06_broker import history

from mgar_v3.domain import DomainError

FORMATS = [
    b"PRIVATE_REMOTE_SECRET-not-json",
    b"[]",
    b'{"code":0,"code":0,"msg":"PRIVATE_REMOTE_SECRET"}',
    b"\xff",
    b"[" * 2000 + b"0" + b"]" * 2000,
    b"{}",
    b'{"code":true}',
    b" " * (2097152 + 1),
]


@pytest.mark.parametrize("where", ["mint", "probe", "history"])
@pytest.mark.parametrize(
    "body",
    FORMATS,
    ids=[
        "malformed",
        "array",
        "duplicate",
        "utf8",
        "recursion",
        "missing_code",
        "boolean_code",
        "oversized",
    ],
)
def test_decoder_fixed_error_health_no_retry_no_secret_or_data(engine, where, body, caplog):
    env = setup(engine, history=True)

    def response(req):
        match = (
            where == "mint"
            and req.method == "POST"
            or where == "probe"
            and req.url.path.endswith("/info")
            or where == "history"
            and req.url.path.endswith("/messages")
        )
        return httpx.Response(200, content=body) if match else None

    env.response = response
    with pytest.raises(DomainError) as error:
        env.broker.probe_permissions() if where == "probe" else history(env)
    assert error.value.code in {"FEISHU_RESPONSE_INVALID", "FEISHU_RESPONSE_BOUND"}
    assert "PRIVATE_REMOTE_SECRET" not in str(error.value) + caplog.text
    assert len(env.calls) == (1 if where == "mint" else 2)
    assert not hasattr(env.broker, "token")
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_inputs").fetchone()["n"] == 0
        )
        row = conn.execute("SELECT * FROM app.feishu_source_health").fetchone()
        assert row["permissions"] == "unknown" and row["reason_code"] == error.value.code
        if where != "probe":
            assert (
                conn.execute("SELECT count(*) AS n FROM app.source_ingress_gaps").fetchone()["n"]
                >= 1
            )


@pytest.mark.parametrize(
    "field,value", [("tenant_access_token", ""), ("expire", None), ("expire", True)]
)
def test_bad_token_shape_is_not_permission_revocation(engine, field, value):
    env = setup(engine, history=True)
    body = {"code": 0, "tenant_access_token": "PRIVATE_REMOTE_SECRET", "expire": 7200}
    body[field] = value
    env.response = lambda req: httpx.Response(200, json=body)
    with pytest.raises(DomainError) as error:
        history(env)
    assert error.value.code == "FEISHU_RESPONSE_INVALID"
    assert len(env.calls) == 1
    assert env.runtime.status()["permissions"] == "unknown"
    assert env.runtime.current("history")  # Not spuriously fenced.


@pytest.mark.parametrize("permission", ["insufficient", "revoked"])
def test_format_health_cannot_clobber_stronger_fresh_permission_fact(engine, permission):
    env = setup(engine, history=True)

    def response(req):
        if req.method == "GET":
            env.runtime.fence("TRUSTED_NEGATIVE", permission)
            return httpx.Response(200, content=b"PRIVATE_REMOTE_SECRET-not-json")
        return None

    env.response = response
    with pytest.raises(DomainError) as error:
        history(env)
    assert error.value.code == "FEISHU_RESPONSE_INVALID"
    stored = env.sources.inspect(env.cookie, env.source_id)["health"]
    assert stored["permissions"] == permission and stored["reason_code"] == "TRUSTED_NEGATIVE"
    assert env.runtime.status()["permissions"] == permission
    assert len(env.calls) == 2


@pytest.mark.parametrize(
    "body",
    [{"code": 0}, {"code": 0, "data": []}, {"code": 0, "data": {"items": {}, "has_more": False}}],
)
def test_history_shape_failure_has_gap_and_fixed_health(engine, body):
    env = setup(engine, history=True)
    env.response = lambda req: httpx.Response(200, json=body) if req.method == "GET" else None
    with pytest.raises(DomainError) as error:
        history(env)
    assert error.value.code == "FEISHU_HISTORY_BOUND"
    assert env.runtime.status()["reason_code"] == error.value.code
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_gaps").fetchone()["n"] >= 1
        )
