"""Hostile bounded platform formats never escape fixed diagnostics or release data."""

import json

import httpx
import pytest
from sa06_support import CHAT_ID, payload, setup, signed
from test_super_agent_sa06_broker import api_row, history, serve

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.domain import SourceThreadRef
from mgar_v3.sources.feishu import FeishuAdapter
from mgar_v3.sources.ingress_domain import HistoryRequest

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


I04_SECRET = "PRIVATE_I04_REPLACEMENT"
I04_INVALID_CONTENT = [
    pytest.param(I04_SECRET + "-not-json", id="malformed"),
    pytest.param('{"text":"' + I04_SECRET + '","text":"duplicate"}', id="duplicate"),
]


def i04_business_counts(env):
    with env.engine.transaction() as conn:
        return {
            table: conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"]
            for table in (
                "source_ingress_inputs",
                "source_inbox",
                "source_ingress_deliveries",
                "source_identities",
                "source_identity_evidence",
                "source_ingress_entities",
            )
        }


def i04_prior(env, name, *, message_type="text", content=None, topic=None):
    raw = payload(message_id=name, body="previous authorized " + name)
    raw["event"]["message"]["message_type"] = message_type
    if content is not None:
        raw["event"]["message"]["content"] = content
    if topic is not None:
        raw["event"]["message"]["thread_id"] = topic
    receipt = env.runtime.receive(signed(env, raw))
    assert receipt.committed and receipt.input_ids
    input_id = receipt.input_ids[0]
    body = env.engine.source_ingress.body(env.cookie, input_id)
    assert body
    return raw, input_id, body


def i04_changed(raw, content, *, advance=True):
    row = api_row(raw)
    row["body"] = {"content": content}
    if advance:
        row["update_time"] = str(int(row["create_time"]) + 1000)
    return row


def i04_rejected(env, caplog, *, request=None, codes=("FEISHU_INVALID_EVENT",)):
    before_calls = len(env.calls)
    with pytest.raises(DomainError) as error:
        history(env, request)
    assert error.value.code in codes
    assert I04_SECRET not in str(error.value) + caplog.text
    assert [call.method for call in env.calls[before_calls:]] == ["POST", "GET"]
    with env.engine.transaction() as conn:
        gaps = conn.execute("SELECT declaration FROM app.source_ingress_gaps").fetchall()
        health = conn.execute("SELECT * FROM app.feishu_source_health").fetchone()
    assert any(gap["declaration"]["reason"] == "no_replay" for gap in gaps)
    assert health["reason_code"] == error.value.code
    assert health["connection"] == "degraded" and health["permissions"] == "unknown"
    projected = env.runtime.status()
    assert projected["reason_code"] == error.value.code
    assert projected["permissions"] == "unknown"
    assert env.runtime.current("history")  # Bad content never manufactures revocation.


def i04_unreadable(env, input_id):
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, input_id)
    with env.engine.transaction() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM app.source_ingress_bodies WHERE input_id=%s",
            (input_id,),
        ).fetchone()["n"] == 0


@pytest.mark.parametrize("content", I04_INVALID_CONTENT)
@pytest.mark.parametrize("message_type", ["text", "post"])
@pytest.mark.parametrize("advance", [False, True], ids=["same-time", "changed-time"])
def test_known_changed_invalid_snapshot(engine, content, message_type, advance, caplog):
    env = setup(engine, history=True)
    raw, input_id, _ = i04_prior(env, "known-invalid", message_type=message_type)
    before = i04_business_counts(env)
    serve(env, [i04_changed(raw, content, advance=advance)])
    i04_rejected(env, caplog)
    assert i04_business_counts(env) == before
    i04_unreadable(env, input_id)


def test_valid_changed_snapshot_control(engine):
    env = setup(engine, history=True)
    raw, input_id, _ = i04_prior(env, "valid-change-control")
    serve(env, [i04_changed(raw, json.dumps({"text": "new authorized content"}))])
    page = history(env)
    assert len(page.deliveries) == 1
    assert page.deliveries[0].metadata.items[0].event.historical_backfill
    assert [call.method for call in env.calls] == ["POST", "GET"]
    i04_unreadable(env, input_id)


def test_invalid_snapshot_domain_separated_from_valid_hash_object(engine, caplog):
    env = setup(engine, history=True)
    content = I04_SECRET + "-not-json"
    raw, input_id, _ = i04_prior(
        env,
        "hash-domain-control",
        content=json.dumps({"unsupported_content_sha256": digest(content)}),
    )
    before = i04_business_counts(env)
    serve(env, [i04_changed(raw, content, advance=False)])
    i04_rejected(env, caplog)
    assert i04_business_counts(env) == before
    i04_unreadable(env, input_id)


@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize("other_state", ["unchanged", "changed", "deleted"])
def test_invalid_snapshot_mixed_known_resource_changes(engine, bad_first, other_state, caplog):
    env = setup(engine, history=True)
    raw, input_id, _ = i04_prior(env, "bad-known-in-mixed-page")
    other, other_id, other_body = i04_prior(env, "other-known-in-mixed-page")
    bad = i04_changed(raw, I04_SECRET + "-not-json")
    other_row = api_row(other)
    if other_state == "changed":
        other_row = i04_changed(other, json.dumps({"text": "changed normal resource"}))
    elif other_state == "deleted":
        other_row.update(deleted=True, update_time=str(int(other_row["create_time"]) + 1000))
    before = i04_business_counts(env)
    serve(env, [bad, other_row] if bad_first else [other_row, bad])
    i04_rejected(env, caplog)
    assert i04_business_counts(env) == before
    i04_unreadable(env, input_id)
    if other_state == "unchanged":
        assert env.engine.source_ingress.body(env.cookie, other_id) == other_body
    else:
        i04_unreadable(env, other_id)


@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize("scope_error", ["chat", "thread", "tenant", "sender"])
def test_invalid_snapshot_page_scope_failure_keeps_prior_body(
    engine, bad_first, scope_error, caplog
):
    env = setup(engine, history=True)
    topic = "I04-Allowed-Native-Thread"
    raw, input_id, old_body = i04_prior(env, "scope-protected-known", topic=topic)
    bad = i04_changed(raw, I04_SECRET + "-not-json")
    outside = api_row(payload(message_id="outside-provenance"))
    outside["thread_id"] = topic
    if scope_error == "chat":
        outside["chat_id"] = "Other-Chat"
    elif scope_error == "thread":
        outside["thread_id"] = "Other-Native-Thread"
    elif scope_error == "tenant":
        outside["sender"]["tenant_key"] = "Other-Tenant"
    else:
        outside["sender"]["sender_type"] = "unsupported-sender-kind"
    request = HistoryRequest(
        source_thread=SourceThreadRef(
            scope=env.instance.scope, conversation_id=CHAT_ID, native_thread_id=topic
        ),
        limit=10,
    )
    before = i04_business_counts(env)
    serve(env, [bad, outside] if bad_first else [outside, bad])
    i04_rejected(
        env,
        caplog,
        request=request,
        codes=("FEISHU_INVALID_EVENT", "FEISHU_HISTORY_INVALID", "FEISHU_HISTORY_SCOPE"),
    )
    assert i04_business_counts(env) == before
    assert env.engine.source_ingress.body(env.cookie, input_id) == old_body


@pytest.mark.parametrize("page_token", [None, "X" * 161], ids=["missing", "oversized"])
def test_invalid_snapshot_unusable_next_token_still_invalidates(engine, page_token, caplog):
    env = setup(engine, history=True)
    raw, input_id, _ = i04_prior(env, "known-before-bad-page-token")
    before = i04_business_counts(env)
    serve(env, [i04_changed(raw, I04_SECRET + "-not-json")], has_more=True, page_token=page_token)
    i04_rejected(env, caplog)  # No page/cursor is released; the resource change is still real.
    assert i04_business_counts(env) == before
    i04_unreadable(env, input_id)


@pytest.mark.parametrize("content", I04_INVALID_CONTENT)
def test_unknown_invalid_snapshot_has_no_new_entity(engine, content, caplog):
    env = setup(engine, history=True)
    before = i04_business_counts(env)
    assert before["source_ingress_entities"] == 0
    serve(env, [i04_changed(payload(message_id="unseen-invalid-resource"), content)])
    i04_rejected(env, caplog)
    assert i04_business_counts(env) == before
    with engine.transaction() as conn:
        remaining = conn.execute("SELECT count(*) AS n FROM app.source_ingress_bodies").fetchone()
        assert remaining["n"] == 0


def test_invalid_snapshot_replay_never_revives_prior_body(engine, caplog):
    env = setup(engine, history=True)
    raw, input_id, _ = i04_prior(env, "known-before-invalid-replay")
    before = i04_business_counts(env)
    serve(env, [i04_changed(raw, I04_SECRET + "-not-json")])
    for _ in range(2):
        i04_rejected(env, caplog)
        i04_unreadable(env, input_id)
        assert i04_business_counts(env) == before
    duplicate = env.runtime.receive(signed(env, raw))
    assert duplicate.committed and duplicate.input_ids == (input_id,)
    i04_unreadable(env, input_id)
    assert i04_business_counts(env) == before
    assert [call.method for call in env.calls] == ["POST", "GET", "POST", "GET"]


def test_invalid_snapshot_invalidation_transaction_rolls_back(engine, caplog, monkeypatch):
    env = setup(engine, history=True)
    raw, input_id, old_body = i04_prior(env, "bad-in-rollback-page")
    other, other_id, other_body = i04_prior(env, "normal-change-in-rollback-page")
    before = i04_business_counts(env)
    serve(
        env,
        [
            i04_changed(raw, I04_SECRET + "-not-json"),
            i04_changed(other, json.dumps({"text": "new normal body before rollback"})),
        ],
    )
    original = engine.source_ingress._privacy_delete
    attempted = []

    def fail_after_actual_invalidation(conn, item, *args, **kwargs):
        original(conn, item, *args, **kwargs)
        attempted.append(item.entity_key)
        assert conn.execute(
            "SELECT count(*) AS n FROM app.source_ingress_bodies b "
            "JOIN app.source_ingress_inputs i USING(input_id) WHERE i.entity_key=%s",
            (item.entity_key,),
        ).fetchone()["n"] == 0
        raise DomainError(
            "FEISHU_HISTORY_INVALID", "Controlled rollback after SQL invalidation", 409,
        )

    with monkeypatch.context() as patch:
        patch.setattr(engine.source_ingress, "_privacy_delete", fail_after_actual_invalidation)
        i04_rejected(env, caplog, codes=("FEISHU_HISTORY_INVALID",))
    assert len(attempted) == 1
    assert i04_business_counts(env) == before
    assert engine.source_ingress.body(env.cookie, input_id) == old_body
    assert engine.source_ingress.body(env.cookie, other_id) == other_body
    i04_rejected(env, caplog)
    assert i04_business_counts(env) == before
    i04_unreadable(env, input_id)
    i04_unreadable(env, other_id)
