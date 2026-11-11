"""Hostile bounded platform formats never escape fixed diagnostics or release data."""

import httpx
import pytest
from sa06_support import payload, setup
from test_super_agent_sa06_broker import api_row, history, serve

from mgar_v3.domain import DomainError
from mgar_v3.sources.feishu import FeishuAdapter

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


INVALID_CONTENT = [
    pytest.param("PRIVATE_NESTED_SECRET-not-json", id="malformed"),
    pytest.param('{"text":"PRIVATE_NESTED_SECRET","text":"duplicate"}', id="duplicate"),
]


def assert_content_rejection(env, caplog):
    with pytest.raises(DomainError) as error:
        history(env)
    assert error.value.code == "FEISHU_INVALID_EVENT"
    assert "PRIVATE_NESTED_SECRET" not in str(error.value) + caplog.text
    assert [call.method for call in env.calls] == ["POST", "GET"]  # No retry.
    with env.engine.transaction() as conn:
        for table in ("source_ingress_inputs", "source_ingress_bodies", "source_inbox"):
            assert conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"] == 0
        gaps = conn.execute("SELECT declaration FROM app.source_ingress_gaps").fetchall()
    assert any(gap["declaration"]["reason"] == "no_replay" for gap in gaps)


@pytest.mark.parametrize("content", INVALID_CONTENT)
@pytest.mark.parametrize("initial_health", ["absent", "online"])
@pytest.mark.parametrize("message_type", ["text", "post"])
def test_history_content_rejection_records_health(
    engine, content, initial_health, message_type, caplog
):
    env = setup(engine, history=True)
    before = None
    if initial_health == "online":
        env.runtime.socket_connected()
        before = env.sources.inspect(env.cookie, env.source_id)["health"]
        assert before["connection"] == "online"
    else:
        assert env.sources.inspect(env.cookie, env.source_id)["health"] is None
    valid = api_row(payload(message_id="valid-before-invalid"))
    bad = api_row(payload(message_id="invalid-content"))
    bad.update(msg_type=message_type, body={"content": content})
    serve(env, [valid, bad])  # Even the earlier valid row must not be submitted.
    assert_content_rejection(env, caplog)
    stored = env.sources.inspect(env.cookie, env.source_id)["health"]
    assert stored is not None
    assert stored["connection"] == "degraded" and stored["permissions"] == "unknown"
    assert stored["reason_code"] == "FEISHU_INVALID_EVENT"
    if before:
        assert stored["observed_at"] > before["observed_at"]
    projected = env.runtime.status()
    assert projected["reason_code"] == "FEISHU_INVALID_EVENT"
    assert projected["connection"] == "degraded" and projected["permissions"] == "unknown"
    assert env.runtime.current("history")  # Malformed content never infers revocation.


@pytest.mark.parametrize("content", INVALID_CONTENT)
@pytest.mark.parametrize(
    "permission,code",
    [("insufficient", "FEISHU_PERMISSION_DENIED"), ("revoked", "FEISHU_TOKEN_INVALID")],
)
def test_history_content_health_preserves_newer_permission_diagnosis(
    engine, content, permission, code, caplog, monkeypatch
):
    env = setup(engine, history=True)
    env.runtime.socket_connected()
    original = FeishuAdapter.normalize_raw
    negative = {}

    def revoked_before_parse(adapter, *args, **kwargs):
        # The GET passed its authority checks. A trusted permission loss then commits
        # before content decoding; exercise the real fence, not a direct SQL status edit.
        env.runtime.fence(code, permission)
        negative.update(env.sources.inspect(env.cookie, env.source_id)["health"])
        return original(adapter, *args, **kwargs)

    monkeypatch.setattr(FeishuAdapter, "normalize_raw", revoked_before_parse)
    bad = api_row(payload())
    bad["body"]["content"] = content
    serve(env, [bad])
    assert_content_rejection(env, caplog)
    stored = env.sources.inspect(env.cookie, env.source_id)["health"]
    for result in (stored, env.runtime.status()):
        assert result["permissions"] == permission and result["reason_code"] == code
        assert result["observed_at"] == negative["observed_at"]  # Never renew its TTL.
    assert env.runtime.status()["runtime_current"] is False
    with engine.transaction() as conn:
        diagnostic = conn.execute(
            "SELECT details FROM app.feishu_source_audit "
            "WHERE event='health' AND reason_code='FEISHU_INVALID_EVENT'"
        ).fetchone()
    assert diagnostic is not None
    assert diagnostic["details"]["preserved_permission_diagnosis"] is True
