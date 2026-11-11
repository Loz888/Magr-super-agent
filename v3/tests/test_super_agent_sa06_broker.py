"""SA06 bounded real-PG/MockTransport history reads; no live Feishu calls."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from psycopg.pq import TransactionStatus
from sa06_support import CHAT_ID, TENANT, payload, setup, signed

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import SourceThreadRef
from mgar_v3.sources.feishu_broker import MAX_RESPONSE, FeishuHistory, FeishuReadBroker
from mgar_v3.sources.feishu_domain import FeishuConfigure
from mgar_v3.sources.gateway import SourceGateway
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.sources.ingress_domain import HistoryRequest
from mgar_v3.super_agent.policy_domain import PolicySave


def api_row(raw):
    message, sender = raw["event"]["message"], raw["event"]["sender"]
    id_type = sender.get("id_type", "open_id")
    return {
        "message_id": message["message_id"],
        "chat_id": message["chat_id"],
        "root_id": message.get("root_id"),
        "parent_id": message.get("parent_id"),
        "thread_id": message.get("thread_id"),
        "msg_type": message["message_type"],
        "create_time": message["create_time"],
        "update_time": message.get("update_time", "0"),
        "deleted": False,
        "updated": False,
        "sender": {
            "id": sender["sender_id"][id_type],
            "id_type": id_type,
            "sender_type": sender["sender_type"],
            "tenant_key": sender["tenant_key"],
        },
        "body": {"content": message["content"]},
        "mentions": [
            {
                "id": x["id"]["open_id"],
                "id_type": "open_id",
                "tenant_key": x["tenant_key"],
                "key": x["key"],
            }
            for x in message.get("mentions", [])
        ],
    }


def serve(env, rows, *, has_more=False, page_token=None):
    def response(request):
        assert env.engine.connection.info.transaction_status == TransactionStatus.IDLE
        if request.method == "GET" and request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": rows, "has_more": has_more, "page_token": page_token},
                },
            )
        return None

    env.response = response


def request_for(env, **changes):
    return HistoryRequest(
        source_thread=SourceThreadRef(scope=env.instance.scope, conversation_id=CHAT_ID),
        limit=10,
        **changes,
    )


def history(env, request=None):
    return SourceGateway(env.engine.source_ingress, env.source_id, 1).history(
        FeishuHistory(env.broker), request or request_for(env)
    )


def counts(env):
    with env.engine.transaction() as conn:
        return {
            table: conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"]
            for table in (
                "source_ingress_inputs",
                "source_ingress_conflicts",
                "source_ingress_gaps",
                "source_identities",
            )
        }


def test_history_is_bounded_context_only_and_outside_sql(engine):
    env = setup(engine, history=True)
    raw = payload()
    serve(env, [api_row(raw)], has_more=True, page_token="opaque/+Token")
    page = history(env)
    assert len(page.deliveries) == 1 and not page.complete and page.next_cursor
    assert [req.method for req in env.calls] == ["POST", "GET"]
    assert all(req.url.host == "open.feishu.cn" for req in env.calls)
    query = dict(env.calls[-1].url.params)
    assert query == {"container_id_type": "chat", "container_id": CHAT_ID, "page_size": "10"}
    with engine.transaction() as conn:
        row = conn.execute("SELECT context_only,metadata FROM app.source_ingress_inputs").fetchone()
    assert row["context_only"] and row["metadata"]["event"]["historical_backfill"]
    assert "controlled-token" not in json.dumps(row, default=str)


@pytest.mark.parametrize("history_first", [False, True])
@pytest.mark.parametrize("zero_update", [False, True])
def test_live_and_equivalent_history_share_canonical_input(engine, history_first, zero_update):
    env = setup(engine, history=True)
    raw = payload()
    row = api_row(raw)
    if zero_update:
        row["update_time"] = "0"
        raw["event"]["message"].pop("update_time")
    serve(env, [row])
    if history_first:
        history(env)
        receipt = env.runtime.receive(signed(env, raw))
    else:
        receipt = env.runtime.receive(signed(env, raw))
        history(env)
    state = counts(env)
    assert state["source_ingress_inputs"] == 1 and state["source_ingress_conflicts"] == 0
    assert engine.source_ingress.body(env.cookie, receipt.input_ids[0])
    assert len(env.calls) == 2


@pytest.mark.parametrize("where", ["mint", "read"])
def test_role_revocation_during_remote_call_never_releases_history(engine, where):
    env = setup(engine, history=True)
    raw = payload()

    def response(request):
        assert engine.connection.info.transaction_status == TransactionStatus.IDLE
        if (
            where == "mint"
            and request.method == "POST"
            or where == "read"
            and request.method == "GET"
        ):
            env.identities.roles(
                env.cookie,
                env.binding["binding_id"],
                RoleChange(expected_revision=1, roles=("requester",), review_ref="SA06-revoke"),
            )
        if request.method == "GET":
            return httpx.Response(
                200, json={"code": 0, "data": {"items": [api_row(raw)], "has_more": False}}
            )
        return None

    env.response = response
    with pytest.raises(DomainError, match="context identity"):
        history(env)
    assert [req.method for req in env.calls] == (["POST"] if where == "mint" else ["POST", "GET"])
    assert counts(env)["source_ingress_inputs"] == 0
    assert counts(env)["source_ingress_gaps"] >= 1


@pytest.mark.parametrize("what", ["role", "receive", "settings", "activation", "history"])
def test_no_current_history_authority_means_no_network(engine, what):
    env = setup(engine, history=what != "history")
    if what == "role":
        env.identities.roles(
            env.cookie,
            env.binding["binding_id"],
            RoleChange(expected_revision=1, roles=("requester",), review_ref="SA06-no-context"),
        )
    elif what == "receive":
        definition = env.policy.model_copy(
            update={
                "receive": env.policy.receive.model_copy(update={"allow_history_context": False})
            }
        )
        engine.source_policies.save(
            env.cookie,
            env.policy_id,
            PolicySave(expected_revision=1, definition=definition, review_ref="SA06-no-history"),
        )
    elif what == "settings":
        env.sources.configure(
            env.cookie,
            env.source_id,
            FeishuConfigure(
                expected_revision=1, settings=env.settings, review_ref="SA06-settings-fence"
            ),
        )
    elif what == "activation":
        env.runtime.activation = replace(
            env.activation, expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
    with pytest.raises(DomainError):
        FeishuHistory(env.broker).fetch(env.instance, request_for(env))
    assert not env.calls and counts(env)["source_ingress_inputs"] == 0


@pytest.mark.parametrize(
    "status,code,permission",
    [
        (401, "FEISHU_TOKEN_INVALID", "revoked"),
        (403, "FEISHU_PERMISSION_DENIED", "insufficient"),
        (200, "FEISHU_TOKEN_INVALID", "revoked"),
    ],
)
def test_denied_tokens_or_permissions_fence_previous_contents(engine, status, code, permission):
    env = setup(engine, history=True)
    first = env.runtime.receive(signed(env, payload()))
    assert engine.source_ingress.body(env.cookie, first.input_ids[0])

    def response(request):
        if request.method == "GET":
            return httpx.Response(status, json={"code": 99991663})
        return None

    env.response = response
    with pytest.raises(DomainError) as error:
        history(env)
    assert error.value.code == code and len(env.calls) == 2
    with engine.transaction() as conn:
        row = conn.execute("SELECT * FROM app.feishu_source_health").fetchone()
    assert row["permissions"] == permission and row["connection"] == "degraded"
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, first.input_ids[0])
    with pytest.raises(DomainError):
        history(env)
    assert len(env.calls) == 2


def test_rate_limit_has_bounded_wait_and_no_automatic_retry(engine):
    env = setup(engine, history=True)
    env.response = lambda req: httpx.Response(429, headers={"Retry-After": "999999999"})
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            history(env)
        assert error.value.code == "FEISHU_RATE_LIMITED"
    assert len(env.calls) == 1 and counts(env)["source_ingress_inputs"] == 0
    assert env.broker._blocked_until <= datetime.now(UTC) + timedelta(days=1)


@pytest.mark.parametrize(
    "case",
    [
        "size",
        "page",
        "has_more",
        "cursor",
        "repeat",
        "chat",
        "tenant",
        "sender_type",
        "id_type",
        "url",
    ],
)
def test_history_boundaries_preserve_gap_without_business_input(engine, case):
    env = setup(engine, history=True)
    row = api_row(payload())
    if case == "page":
        serve(env, [row] * 11)
    elif case == "has_more":
        serve(env, [row], has_more="true")
    elif case == "cursor":
        serve(env, [row], has_more=True, page_token="X" * 161)
    elif case == "repeat":
        serve(env, [row], has_more=True, page_token="again")
    elif case == "chat":
        row["chat_id"] = "Oc_Another"
        serve(env, [row])
    elif case == "tenant":
        row["sender"]["tenant_key"] = TENANT.lower()
        serve(env, [row])
    elif case == "sender_type":
        row["sender"]["sender_type"] = "human"
        serve(env, [row])
    elif case == "id_type":
        row["sender"]["id_type"] = "user_id"
        serve(env, [row])
    elif case == "url":
        env.response = lambda req: httpx.Response(
            302, headers={"Location": "https://example.invalid"}
        )
    else:
        env.response = lambda req: (
            httpx.Response(200, content=b"X" * (MAX_RESPONSE + 1)) if req.method == "GET" else None
        )
    req = request_for(env)
    if case == "repeat":
        req = req.model_copy(update={"cursor": "2:YWdhaW4"})
    with pytest.raises(DomainError):
        history(env, req)
    assert counts(env)["source_ingress_inputs"] == 0
    assert counts(env)["source_ingress_gaps"] >= 1
    assert all(req.url.host == "open.feishu.cn" for req in env.calls)
    assert len(env.calls) <= 2


@pytest.mark.parametrize("cursor", ["https://evil.invalid", "101:YWJj", "2:", "2:***", "1:YWJj"])
def test_invalid_cursor_does_not_mint_token(engine, cursor):
    env = setup(engine, history=True)
    with pytest.raises(DomainError) as error:
        history(env, request_for(env, cursor=cursor))
    assert error.value.code == "FEISHU_HISTORY_CURSOR"
    assert not env.calls


def test_history_selected_limit_fifty_and_explicit_scope_fail_before_get(engine):
    env = setup(engine, history=True)
    with pytest.raises(DomainError) as error:
        history(env, HistoryRequest(source_thread=request_for(env).source_thread, limit=51))
    assert error.value.code == "FEISHU_HISTORY_BOUND"
    assert not env.calls
    wrong = request_for(env).source_thread.model_copy(update={"conversation_id": "Oc_other"})
    with pytest.raises(DomainError):
        FeishuHistory(env.broker).fetch(env.instance, HistoryRequest(source_thread=wrong, limit=1))
    assert not env.calls


def test_unselected_author_snapshot_is_bodyless_and_does_not_create_identity(engine):
    env = setup(engine, history=True)
    raw = payload(sender="Ou_Unselected", body="never persist this unselected content")
    serve(env, [api_row(raw)])
    before = counts(env)
    page = history(env)
    assert len(page.deliveries) == 1
    delivery = page.deliveries[0]
    assert delivery.bodies == (None,)
    assert delivery.metadata.items[0].event.body_ref is None
    assert delivery.metadata.items[0].event.sender.actor_id == "Ou_Unselected"
    after = counts(env)
    assert after["source_identities"] == before["source_identities"]
    assert after["source_ingress_inputs"] == 0


def test_changed_snapshot_uses_original_author_and_invalidates_previous_content(engine):
    env = setup(engine, history=True)
    raw = payload(body="original retained body")
    first = env.runtime.receive(signed(env, raw))
    row = api_row(raw)
    row["body"] = {"content": json.dumps({"text": "new current snapshot"})}
    row["update_time"] = str(int(row["create_time"]) + 1)
    serve(env, [row])
    page = history(env)
    assert (
        page.deliveries[0].metadata.items[0].event.sender
        == env.adapter.verify_identity(env.instance, signed(env, raw)).actor
    )
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, first.input_ids[0])
    # Feishu snapshots lack a proven native edit sequence. Changed versions stay ambiguous.
    with engine.transaction() as conn:
        assert conn.execute("SELECT ambiguous FROM app.source_ingress_entities").fetchone()[
            "ambiguous"
        ]


def test_current_snapshot_tombstone_is_service_observation_and_no_new_body(engine):
    env = setup(engine, history=True)
    raw = payload()
    first = env.runtime.receive(signed(env, raw))
    row = api_row(raw)
    row.update(deleted=True, sender=None, body=None)
    serve(env, [row])
    page = history(env)
    item = page.deliveries[0].metadata.items[0]
    assert item.operation == "delete" and item.event.sender.actor_kind == "service"
    assert item.event.sender.actor_id == env.settings.app_id
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, first.input_ids[0])


def test_bot_probe_is_explicit_identity_only_and_not_scope_or_live_acceptance(engine):
    env = setup(engine, history=True)
    assert env.runtime.status()["permissions"] == "unknown" and not env.calls
    env.sources.inspect(env.cookie, env.source_id)
    assert not env.calls
    result = env.broker.probe_permissions()
    assert result["bot_identity_verified"] and result["permissions"] == "unknown"
    assert result["scopes_verified"] is False and result["mode"] == "controlled"
    assert result["live_source_channel_gate"] == "NOT_RUN" and len(env.calls) == 2


@pytest.mark.parametrize("change", ["open_id", "app_id", "tenant_key"])
def test_bot_probe_mismatched_installation_fences_runtime(engine, change):
    env = setup(engine, history=True)
    bot = {
        "app_id": env.settings.app_id,
        "open_id": env.settings.bot_open_id,
        "tenant_key": env.settings.tenant_key,
    }
    bot[change] = bot[change].lower()
    env.response = lambda req: (
        httpx.Response(200, json={"code": 0, "bot": bot}) if req.method == "GET" else None
    )
    with pytest.raises(DomainError) as error:
        env.broker.probe_permissions()
    assert error.value.code == "FEISHU_BOT_IDENTITY_MISMATCH"
    assert len(env.calls) == 2


def test_controlled_broker_refuses_real_transport_send_endpoints_and_nested_sql(engine):
    env = setup(engine, history=True)
    with pytest.raises(ValueError):
        FeishuReadBroker(env.runtime)
    with pytest.raises(ValueError):
        FeishuReadBroker(env.runtime, transport=httpx.HTTPTransport())
    with pytest.raises(DomainError) as error:
        env.broker._request("POST", "/open-apis/im/v1/messages", "never-a-real-token")
    assert error.value.code == "FEISHU_ENDPOINT_DENIED" and not env.calls
    with engine.transaction():
        with pytest.raises(DomainError) as error:
            env.broker._request("GET", "/open-apis/bot/v3/info", "never-a-real-token")
        assert error.value.code == "FEISHU_NETWORK_IN_TRANSACTION"
    assert not env.calls


def test_arbitrary_payload_links_are_not_followed_and_token_is_not_cached(engine):
    env = setup(engine, history=True)
    row = api_row(payload())
    row["message_app_link"] = "https://example.invalid/steal"
    serve(env, [row])
    for _ in range(2):
        history(env)
    assert [req.method for req in env.calls] == ["POST", "GET", "POST", "GET"]
    assert all(req.url.host == "open.feishu.cn" for req in env.calls)
    assert not hasattr(env.broker, "token")


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_access_token", ""),
        ("tenant_access_token", "contains space"),
        ("tenant_access_token", 3),
        ("expire", 7201),
        ("expire", True),
        ("expire", 5),
    ],
)
def test_bad_mint_response_is_not_released_or_retried(engine, field, value):
    env = setup(engine, history=True)
    body = {"code": 0, "tenant_access_token": "controlled-token", "expire": 7200}
    body[field] = value
    env.response = lambda req: httpx.Response(200, json=body)
    with pytest.raises(DomainError) as error:
        history(env)
    assert error.value.code == "FEISHU_TOKEN_INVALID"
    assert [req.method for req in env.calls] == ["POST"]
    assert counts(env)["source_ingress_inputs"] == 0
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT permissions FROM app.feishu_source_health").fetchone()[
                "permissions"
            ]
            == "revoked"
        )


def test_bot_probe_does_not_invent_tenant_proof_when_platform_omits_it(engine):
    env = setup(engine, history=True)
    env.response = lambda req: (
        httpx.Response(
            200,
            json={
                "code": 0,
                "bot": {"app_id": env.settings.app_id, "open_id": env.settings.bot_open_id},
            },
        )
        if req.method == "GET"
        else None
    )
    result = env.broker.probe_permissions()
    assert result["bot_identity_verified"] and not result["tenant_identity_verified"]
    assert result["permissions"] == "unknown" and not result["scopes_verified"]


@pytest.mark.parametrize("deleted", [False, True])
def test_oversized_history_change_invalidates_old_body_without_retaining_new(engine, deleted):
    env = setup(engine, history=True)
    raw = payload(body="short previous content")
    first = env.runtime.receive(signed(env, raw))
    row = api_row(raw)
    row["body"] = {"content": json.dumps({"text": "中" * 30000}, ensure_ascii=False)}
    row["update_time"] = str(int(row["create_time"]) + 1)
    row["deleted"] = deleted
    serve(env, [row])
    page = history(env)
    assert page.deliveries[0].bodies == (None,)
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, first.input_ids[0])
    with engine.transaction() as conn:
        # PG never retains the rejected oversized replacement body.
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_bodies").fetchone()["n"] == 0
        )


def test_topic_escape_does_not_commit_or_retain_another_thread(engine):
    env = setup(engine, history=True)
    row = api_row(payload())
    row["thread_id"] = "Om_Another-Thread"
    serve(env, [row])
    thread = request_for(env).source_thread.model_copy(update={"native_thread_id": "Om_Expected"})
    with pytest.raises(DomainError) as error:
        history(env, HistoryRequest(source_thread=thread, limit=10))
    assert error.value.code == "FEISHU_HISTORY_SCOPE"
    assert counts(env)["source_ingress_inputs"] == 0


@pytest.mark.parametrize("message_type", ["image", "file"])
def test_unsupported_current_version_invalidates_old_text_and_records_gap(engine, message_type):
    env = setup(engine, history=True)
    raw = payload(body="old text content")
    first = env.runtime.receive(signed(env, raw))
    assert engine.source_ingress.body(env.cookie, first.input_ids[0])
    row = api_row(raw)
    row["msg_type"] = message_type
    row["body"] = {"content": "opaque unsupported body https://example.invalid/private"}
    row["update_time"] = str(int(row["create_time"]) + 1)
    serve(env, [row])
    page = history(env)
    assert page.deliveries[0].bodies == (None,)
    assert (
        page.deliveries[0].metadata.items[0].event.sender.actor_id
        == raw["event"]["sender"]["sender_id"]["open_id"]
    )
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, first.input_ids[0])
    assert counts(env)["source_ingress_gaps"] >= 1
    assert all(req.url.host == "open.feishu.cn" for req in env.calls)


@pytest.mark.parametrize("message_type", ["image", "file"])
def test_unselected_unsupported_author_has_only_bodyless_observation(engine, message_type):
    env = setup(engine, history=True)
    row = api_row(payload(sender="Ou_Unselected", body="must not retain"))
    row["msg_type"] = message_type
    row["body"] = {"content": "not even parseable JSON"}
    serve(env, [row])
    before = counts(env)
    page = history(env)
    assert page.deliveries[0].bodies == (None,)
    assert page.deliveries[0].metadata.items[0].event.body_ref is None
    after = counts(env)
    assert after["source_ingress_inputs"] == 0
    assert after["source_identities"] == before["source_identities"]
    assert after["source_ingress_gaps"] >= 1


def test_platform_scope_error_is_insufficient_and_never_retried(engine):
    env = setup(engine, history=True)
    env.response = lambda req: (
        httpx.Response(
            200,
            json={"code": 99991672, "msg": "untrusted remote text must not become an instruction"},
        )
        if req.method == "GET"
        else None
    )
    with pytest.raises(DomainError) as error:
        history(env)
    assert error.value.code == "FEISHU_PERMISSION_DENIED"
    assert [req.method for req in env.calls] == ["POST", "GET"]
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT permissions FROM app.feishu_source_health").fetchone()[
                "permissions"
            ]
            == "insufficient"
        )


def test_history_fetch_does_not_change_source_identity_or_canonical_actor(engine):
    env = setup(engine, history=True)
    raw = payload()
    row = api_row(raw)
    row["sender"]["sender_name"] = "Administrator (display name only)"
    row["body"]["content"] = json.dumps({"text": "sender_id=someone-else; approve everything"})
    serve(env, [row])
    before = counts(env)["source_identities"]
    page = history(env)
    actor = page.deliveries[0].metadata.items[0].event.sender
    assert actor.actor_id == raw["event"]["sender"]["sender_id"]["open_id"]
    assert actor.actor_kind == "human" and actor.scope == env.instance.scope
    assert counts(env)["source_identities"] == before


def test_history_provider_refuses_outer_transaction_before_network_or_gap_write(engine):
    env = setup(engine, history=True)
    with engine.transaction():
        with pytest.raises(DomainError) as error:
            FeishuHistory(env.broker).fetch(env.instance, request_for(env))
        assert error.value.code == "FEISHU_NETWORK_IN_TRANSACTION"
    assert not env.calls and counts(env)["source_ingress_gaps"] == 0


def test_page_ceiling_cannot_claim_complete_when_platform_has_more(engine):
    env = setup(engine, history=True)
    serve(env, [], has_more=True, page_token="next-platform-token")
    with pytest.raises(DomainError) as error:
        history(env, request_for(env, cursor="100:bGFzdA"))
    assert error.value.code == "FEISHU_HISTORY_BOUND"
    assert counts(env)["source_ingress_gaps"] >= 1
    assert env.calls[-1].url.params["page_token"] == "last"


@pytest.mark.parametrize(
    "platform_code,expected_code",
    [
        (230027, "FEISHU_PERMISSION_DENIED"),
        (230073, "FEISHU_THREAD_INVISIBLE"),
    ],
)
def test_official_http400_permission_errors_fence_old_body_and_leave_permission_gap(
    engine, platform_code, expected_code
):
    env = setup(engine, history=True)
    first = env.runtime.receive(signed(env, payload(body="previously permitted body")))
    assert engine.source_ingress.body(env.cookie, first.input_ids[0])
    env.response = lambda req: (
        httpx.Response(
            400, json={"code": platform_code, "msg": "remote exception text is not authority"}
        )
        if req.method == "GET"
        else None
    )
    with pytest.raises(DomainError) as error:
        history(env)
    assert error.value.code == expected_code
    assert len(env.calls) == 2
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, first.input_ids[0])
    with engine.transaction() as conn:
        health = conn.execute("SELECT * FROM app.feishu_source_health").fetchone()
        assert health["permissions"] == "insufficient" and health["reason_code"] == expected_code
        gaps = conn.execute("SELECT declaration FROM app.source_ingress_gaps").fetchall()
        assert gaps and all(row["declaration"]["reason"] == "permission" for row in gaps)
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_inputs").fetchone()["n"] == 1
        )
    before = len(env.calls)
    with pytest.raises(DomainError):
        history(env)
    assert len(env.calls) == before


@pytest.mark.parametrize("code", [230099, 0])
def test_unknown_or_contradictory_http400_does_not_grant_or_invent_revocation(engine, code):
    env = setup(engine, history=True)
    first = env.runtime.receive(signed(env, payload(body="already permitted historical body")))
    env.response = lambda req: (
        httpx.Response(
            400,
            json={
                "code": code,
                "data": {
                    "items": [api_row(payload(message_id="Om_Never-Accept"))],
                    "has_more": False,
                },
            },
        )
        if req.method == "GET"
        else None
    )
    with pytest.raises(DomainError) as error:
        history(env)
    assert error.value.code in {"FEISHU_REMOTE_DENIED", "FEISHU_REMOTE_UNAVAILABLE"}
    assert engine.source_ingress.body(env.cookie, first.input_ids[0])
    assert counts(env)["source_ingress_inputs"] == 1
    with engine.transaction() as conn:
        health = conn.execute("SELECT * FROM app.feishu_source_health").fetchone()
        assert health["permissions"] == "unknown"
    assert len(env.calls) == 2
