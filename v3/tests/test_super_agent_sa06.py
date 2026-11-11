"""SA06 controlled engineering: actual crypto and sole-Engine disposable PostgreSQL."""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sa06_support import (
    APP_ID,
    BOT_ID,
    CHAT_ID,
    CONTEXT_BOT_ID,
    ORIGIN,
    SENDER_ID,
    TENANT,
    payload,
    setup,
    signed,
)

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError
from mgar_v3.sources.feishu_domain import (
    ALL_SCOPE,
    AT_SCOPE,
    HISTORY_SCOPE,
    FeishuConfigure,
    FeishuSettings,
)
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.sources.gateway import SourceGateway
from mgar_v3.sources.identity_domain import BindIdentity, RoleChange
from mgar_v3.sources.ingress_domain import IngressConfigure
from mgar_v3.sources.ports import TransportEnvelope, VerifiedEnvelope
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.policy_domain import PolicySave
from mgar_v3.web_access import SESSION_COOKIE, WebAccounts


@pytest.fixture
def env(engine):
    return setup(engine)


def count(env, table):
    with env.engine.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"]


def read(env, receipt):
    return json.loads(env.engine.source_ingress.body(env.cookie, receipt.input_ids[0]))


def test_signed_message_preserves_scope_ids_and_no_fake_issue(env):
    raw = payload()
    verified = env.adapter.verify_ingress(env.instance, signed(env, raw))
    item = verified.metadata.items[0]
    assert item.event.sender.actor_id == SENDER_ID
    assert item.event.sender.id_type == "open_id" and item.event.sender.actor_kind == "human"
    assert item.event.scope.tenant_id == TENANT and item.event.scope.installation_id == APP_ID
    assert item.event.source_thread.conversation_id == CHAT_ID
    assert item.event.source_thread.native_thread_id is None
    assert item.event.repository_hint is None and item.native_order is None
    assert item.event.entity_revision.origin == "derived" and item.event.addressed == "mention"
    receipt = env.runtime.receive(signed(env, raw))
    assert receipt.committed and receipt.disposition == "accepted"
    assert (
        read(env, receipt)["content"]["text"]
        == json.loads(raw["event"]["message"]["content"])["text"]
    )
    assert count(env, "source_inbox") == count(env, "source_ingress_inputs") == 1
    for table in ("trigger_events", "threads", "tickets", "runs", "outbox", "usage_calls"):
        assert count(env, table) == 0
    assert env.calls == []


@pytest.mark.parametrize(
    "failure",
    [
        "signature",
        "token",
        "stale",
        "future",
        "tenant",
        "app",
        "sender_tenant",
        "json_duplicate",
        "header_duplicate",
    ],
)
def test_authentication_failures_never_persist_body_or_identity(env, failure):
    raw = payload(body="DO_NOT_STORE_UNTRUSTED_CONTENT")
    headers = {}
    kwargs = {}
    if failure == "signature":
        headers["X-Lark-Signature"] = "0" * 64
    elif failure == "token":
        raw["header"]["token"] = "wrong"
    elif failure == "stale":
        kwargs["timestamp"] = int(time.time()) - 301
    elif failure == "future":
        kwargs["timestamp"] = int(time.time()) + 301
    elif failure == "tenant":
        raw["header"]["tenant_key"] = TENANT.lower()
    elif failure == "app":
        raw["header"]["app_id"] = APP_ID.lower()
    elif failure == "sender_tenant":
        raw["event"]["sender"]["tenant_key"] = TENANT.lower()
    elif failure == "header_duplicate":
        headers["x-lark-request-nonce"] = "duplicate"
    envelope = signed(env, raw, headers=headers, **kwargs)
    if failure == "json_duplicate":
        data = b'{"schema":"2.0","schema":"2.0"}'
        timestamp = envelope.headers["X-Lark-Request-Timestamp"]
        nonce = envelope.headers["X-Lark-Request-Nonce"]
        altered = dict(envelope.headers)
        altered["X-Lark-Signature"] = hashlib.sha256(
            (timestamp + nonce + env.secrets.encrypt_key).encode() + data
        ).hexdigest()
        envelope = TransportEnvelope(altered, data)
    before = count(env, "source_identity_evidence")
    with pytest.raises(DomainError):
        env.runtime.receive(envelope)
    assert count(env, "source_identity_evidence") == before
    assert count(env, "source_ingress_inputs") == count(env, "source_ingress_bodies") == 0
    assert count(env, "feishu_transport_filters") == 0


@pytest.mark.parametrize(
    "failure,code",
    [
        ("private", "FEISHU_CHAT_FILTERED"),
        ("group", "FEISHU_CHAT_FILTERED"),
        ("sender", "FEISHU_SENDER_FILTERED"),
        ("case", "FEISHU_SENDER_FILTERED"),
        ("own_bot", "FEISHU_SENDER_FILTERED"),
        ("body_mention", "FEISHU_ADDRESS_FILTERED"),
        ("cross_tenant_mention", "FEISHU_ADDRESS_FILTERED"),
        ("other_topic", "FEISHU_ADDRESS_FILTERED"),
        ("attachment", "FEISHU_CONTENT_UNSUPPORTED"),
    ],
)
def test_authentic_out_of_scope_transport_only_records_hashes(env, failure, code):
    raw = payload(body="DO_NOT_RETAIN_FILTERED_BODY")
    message, sender = raw["event"]["message"], raw["event"]["sender"]
    if failure == "private":
        message["chat_type"] = "p2p"
    elif failure == "group":
        message["chat_id"] = "Oc_Unauthorized"
    elif failure in {"sender", "case"}:
        sender["sender_id"]["open_id"] = (
            "Ou_Same-Display-Name" if failure == "sender" else SENDER_ID.lower()
        )
    elif failure == "own_bot":
        sender["sender_id"]["open_id"] = BOT_ID
        sender["sender_type"] = "app"
    elif failure in {"body_mention", "other_topic"}:
        message["mentions"] = []
        message["content"] = json.dumps({"text": "@Synthetic Bot " + BOT_ID})
        if failure == "other_topic":
            message["thread_id"] = "Omt_Unsubscribed"
    elif failure == "cross_tenant_mention":
        message["mentions"][0]["tenant_key"] = "Tenant-Other"
    else:
        message["message_type"] = "image"
        message["content"] = "malformed content must not be parsed"
    before = count(env, "source_identity_evidence")
    first = env.runtime.receive(signed(env, raw))
    second = env.runtime.receive(signed(env, raw))
    assert first.disposition == second.disposition == "filtered"
    assert first.reason_code == code and first.receipt_id == second.receipt_id
    assert first.committed and count(env, "feishu_transport_filters") == 1
    assert count(env, "source_identity_evidence") == before
    assert count(env, "source_ingress_inputs") == count(env, "source_ingress_bodies") == 0
    with env.engine.transaction() as conn:
        row = conn.execute("SELECT * FROM app.feishu_transport_filters").fetchone()
        serialized = json.dumps(row, default=str)
    assert "DO_NOT_RETAIN" not in serialized and "sender_id" not in serialized
    assert raw["header"]["event_id"] not in serialized


def test_real_official_aes_envelope_is_authenticated_and_decrypted(env):
    raw = payload(body="encrypted synthetic message")
    delivery = env.adapter.verify_ingress(env.instance, signed(env, raw, encrypted=True))
    assert json.loads(delivery.bodies[0])["content"]["text"] == "encrypted synthetic message"
    receipt = env.runtime.receive(signed(env, raw, encrypted=True))
    assert read(env, receipt)["content"]["text"] == "encrypted synthetic message"
    assert env.secrets.app_secret not in repr(env.secrets)
    assert env.secrets.verification_token not in repr(env.secrets)
    assert env.secrets.encrypt_key not in repr(env.secrets)


def test_equivalent_duplicate_and_concurrent_delivery_have_one_input(env):
    raw = payload()
    envelope = signed(env, raw)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: env.runtime.receive(envelope), range(12)))
    assert len({r.receipt_id for r in results}) == 1
    assert len({r.input_ids for r in results}) == 1
    assert sum(r.disposition == "accepted" for r in results) == 1
    assert count(env, "source_inbox") == count(env, "source_ingress_inputs") == 1
    # A new authenticated platform event observes the exact same resource version.
    raw["header"]["event_id"] = "second-platform-event"
    alias = env.runtime.receive(signed(env, raw))
    assert alias.input_ids == results[0].input_ids
    assert count(env, "source_ingress_inputs") == 1
    assert read(env, alias)["content"]["text"] == "synthetic message @_user_1"


def test_same_transport_changed_body_is_explicit_conflict(env):
    raw = payload()
    first = env.runtime.receive(signed(env, raw))
    raw["event"]["message"]["content"] = json.dumps({"text": "conflicting new body"})
    with pytest.raises(DomainError) as error:
        env.runtime.receive(signed(env, raw))
    assert error.value.code == "INGRESS_DELIVERY_CONFLICT"
    assert count(env, "source_ingress_inputs") == 1
    assert read(env, first)["content"]["text"] == "synthetic message @_user_1"


def test_filtered_transport_conflict_and_capacity_are_not_success_ack(env):
    raw = payload(sender="Ou_Unselected")
    env.runtime.receive(signed(env, raw))
    raw["event"]["message"]["content"] = json.dumps({"text": "changed filtered body"})
    with pytest.raises(DomainError) as error:
        env.runtime.receive(signed(env, raw))
    assert error.value.code == "FEISHU_FILTER_CONFLICT"
    with env.engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.feishu_transport_filters"
            "(receipt_id,source_instance_id,event_key,payload_sha256,reason_code) "
            "SELECT gen_random_uuid(),%s,md5(g::text)||md5(g::text),'"
            + "a" * 64
            + "','FEISHU_SENDER_FILTERED' FROM generate_series(1,9999) g",
            (env.source_id,),
        )
    with pytest.raises(DomainError) as error:
        env.runtime.receive(signed(env, payload(sender="Ou_Unselected")))
    assert error.value.code == "FEISHU_FILTER_CAPACITY"
    assert count(env, "feishu_transport_filters") == 10000


def test_filter_requires_durable_root_and_immutable_hash_only_fact(env):
    raw = payload(sender="Ou_Unselected")
    with env.engine.transaction():
        with pytest.raises(DomainError) as error:
            env.runtime.receive(signed(env, raw))
        assert error.value.code == "INGRESS_OUTER_TRANSACTION"
    assert count(env, "feishu_transport_filters") == 0
    receipt = env.runtime.receive(signed(env, raw))
    assert env.engine.connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    with psycopg.connect(env.engine.settings.database_url.get_secret_value()) as conn:
        assert conn.execute("SELECT receipt_id FROM app.feishu_transport_filters").fetchone() == (
            receipt.receipt_id,
        )
    for statement in [
        "UPDATE app.feishu_transport_filters SET reason_code='changed'",
        "DELETE FROM app.feishu_transport_filters",
    ]:
        with pytest.raises(psycopg.Error):
            with env.engine.transaction() as conn:
                conn.execute(statement)


def test_commit_before_ack_and_transaction_failure_no_input(env):
    raw = payload()
    adapter = env.runtime.adapter()
    # Identity evidence may exist without a role grant; Alice is already explicitly bound.
    env.identities.authenticate(env.source_id, signed(env, raw))
    acknowledgements = []

    def ack(receipt):
        assert env.engine.connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        with psycopg.connect(env.engine.settings.database_url.get_secret_value()) as conn:
            assert conn.execute(
                "SELECT sealed FROM app.source_ingress_deliveries WHERE receipt_id=%s",
                (receipt.receipt_id,),
            ).fetchone() == (True,)
        acknowledgements.append(receipt.receipt_id)

    gateway = SourceGateway(
        env.engine.source_ingress,
        env.source_id,
        1,
        {("feishu", env.instance.adapter_version): adapter},
        ack_mode="after_handler",
    )
    receipt = gateway.receive(signed(env, raw), ack)
    assert acknowledgements == [receipt.receipt_id]


def test_explicit_followup_topic_without_at_and_reply_link(engine):
    env = setup(
        engine,
        settings_changes={"followup_topics": ({"chat_id": CHAT_ID, "thread_id": "Omt_Existing"},)},
    )
    raw = payload()
    message = raw["event"]["message"]
    message.update(thread_id="Omt_Existing", root_id="Om_Root", parent_id="Om_Parent", mentions=[])
    delivery = env.adapter.verify_ingress(env.instance, signed(env, raw))
    event = delivery.metadata.items[0].event
    assert event.source_thread.native_thread_id == "Omt_Existing"
    assert event.reply_to_entity_id == "Om_Parent" and event.addressed == "ambient"
    receipt = env.runtime.receive(signed(env, raw))
    assert receipt.disposition == "accepted" and count(env, "tickets") == 0


def test_scope_settings_do_not_grant_unbound_sender_or_context_bot(env):
    raw = payload(sender=CONTEXT_BOT_ID)
    raw["event"]["sender"]["sender_type"] = "app"
    receipt = env.runtime.receive(signed(env, raw))
    assert receipt.disposition == "rejected"
    assert count(env, "source_ingress_bodies") == 0
    evidence = env.identities.evidence(env.cookie)
    proof = next(row for row in evidence if row["actor_ref"]["actor_id"] == CONTEXT_BOT_ID)
    with pytest.raises(DomainError):
        env.identities.bind(
            env.cookie,
            BindIdentity(
                evidence_id=proof["evidence_id"],
                expected_identity_revision=proof["identity_revision"],
                roles=("context_provider", "requester"),
                review_ref="must-not-elevate-bot",
            ),
        )
    binding = env.identities.bind(
        env.cookie,
        BindIdentity(
            evidence_id=proof["evidence_id"],
            expected_identity_revision=proof["identity_revision"],
            roles=("context_provider",),
            review_ref="context-only-bot",
        ),
    )
    assert binding
    actor = env.adapter.verify_identity(env.instance, signed(env, payload())).actor
    bot = actor.model_copy(update={"actor_id": CONTEXT_BOT_ID, "actor_kind": "bot"})
    definition = env.policy.model_dump(mode="json")
    definition["selector"]["actors"].append(bot.model_dump(mode="json"))
    env.engine.source_policies.save(
        env.cookie,
        env.policy_id,
        PolicySave(
            expected_revision=1, definition=definition, review_ref="explicit-bot-context-policy"
        ),
    )
    # Saving policy is not an implicit owner-generation upgrade.
    with pytest.raises(DomainError):
        env.runtime.receive(signed(env, payload()))
    changed = env.control.model_copy(update={"policy_revision": 2})
    env.engine.source_ingress.configure(
        env.cookie,
        env.source_id,
        IngressConfigure(
            expected_generation=1, settings=changed, review_ref="switch-reviewed-bot-policy"
        ),
    )
    env.runtime.generation = 2
    env.runtime.activation = replace(env.activation, generation=2)
    raw = payload(sender=CONTEXT_BOT_ID, message_id="Om_Context-After-Bind")
    raw["event"]["sender"]["sender_type"] = "app"
    receipt = env.runtime.receive(signed(env, raw))
    assert receipt.disposition == "accepted"
    with env.engine.transaction() as conn:
        row = conn.execute(
            "SELECT context_only FROM app.source_ingress_inputs WHERE inbox_id=%s",
            (receipt.input_ids[0],),
        ).fetchone()
    assert row["context_only"]
    assert read(env, receipt)["content"]["text"] == "synthetic message @_user_1"
    assert count(env, "runs") == count(env, "outbox") == 0


def test_current_role_revocation_clears_body_and_blocks_new_intake(env):
    first = env.runtime.receive(signed(env, payload()))
    env.identities.roles(
        env.cookie,
        env.binding["binding_id"],
        RoleChange(
            expected_revision=env.binding["revision"],
            roles=("requester",),
            review_ref="remove-context",
        ),
    )
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    fresh = env.runtime.receive(signed(env, payload(message_id="Om_After-Revoke")))
    assert fresh.disposition == "rejected" and count(env, "source_ingress_bodies") == 0


def test_socket_headers_cannot_impersonate_verified_sdk_session(env):
    envelope = signed(env, payload())
    forged = TransportEnvelope(
        {
            "mgar-feishu-session": "chosen",
            "mgar-feishu-delivery": "chosen",
            "mgar-feishu-proof": "0" * 64,
        },
        envelope.payload,
    )
    epoch = env.runtime.socket_connected()
    with pytest.raises(DomainError):
        env.runtime.receive(forged, transport="socket")
    sealed = env.runtime.proof.seal(envelope.payload, "sdk-delivery", epoch)
    receipt = env.runtime.receive(sealed, transport="socket")
    assert receipt.committed and receipt.disposition == "accepted"
    env.runtime.socket_lost()
    with pytest.raises(DomainError):
        env.runtime.receive(sealed, transport="socket")
    assert env.runtime.status()["connection"] == "offline"


def test_default_activation_and_expired_activation_are_fail_closed(env):
    disabled = FeishuRuntime(env.sources, env.source_id, 1, 1, env.secrets)
    with pytest.raises(DomainError):
        disabled.receive(signed(env, payload()))
    assert disabled.status()["connection"] == "disabled"
    env.runtime.activation = replace(
        env.activation, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    with pytest.raises(DomainError):
        env.runtime.receive(signed(env, payload()))
    assert env.calls == []


def test_config_cas_source_revision_fences_runtime_and_old_body(env):
    first = env.runtime.receive(signed(env, payload()))
    request = FeishuConfigure(
        expected_revision=1, settings=env.settings, review_ref="explicit-new-config"
    )
    updated = env.sources.configure(env.cookie, env.source_id, request)
    assert updated["revision"] == 2 and updated["source_revision"] == env.instance.revision + 1
    with pytest.raises(DomainError) as error:
        env.sources.configure(env.cookie, env.source_id, request)
    assert error.value.code == "FEISHU_REVISION_CONFLICT"
    with pytest.raises(DomainError):
        env.runtime.receive(signed(env, payload()))
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    with pytest.raises(psycopg.Error):
        with env.engine.transaction() as conn:
            conn.execute("UPDATE app.feishu_source_versions SET review_ref='changed'")


def test_config_outer_rollback_preserves_revision_source_and_audit(env):
    with pytest.raises(RuntimeError):
        with env.engine.transaction():
            env.sources.configure(
                env.cookie,
                env.source_id,
                FeishuConfigure(
                    expected_revision=1, settings=env.settings, review_ref="rollback-config"
                ),
            )
            raise RuntimeError("rollback fixture")
    current = env.sources.inspect(env.cookie, env.source_id)
    assert current["revision"] == 1 and current["source_revision"] == env.instance.revision
    assert len(current["audit"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"sender_open_ids": (SENDER_ID, SENDER_ID)},
        {"sender_open_ids": (BOT_ID,)},
        {"context_bot_open_ids": (SENDER_ID,)},
        {"receive_mode": "all_selected"},
        {"history_enabled": True},
        {"history_enabled": True, "requested_scopes": (AT_SCOPE, HISTORY_SCOPE)},
        {"followup_topics": ({"chat_id": "Oc_Other", "thread_id": "Omt_Other"},)},
        {"verification_token": "must-not-accept-secrets"},
    ],
)
def test_settings_reject_ambiguous_scope_secret_fields_and_implicit_sensitive_reads(env, change):
    raw = env.settings.model_dump(mode="json")
    raw.update(change)
    with pytest.raises(ValidationError):
        FeishuSettings.model_validate(raw)


def test_explicit_all_group_scope_is_required_separately(engine):
    env = setup(
        engine,
        settings_changes={
            "receive_mode": "all_selected",
            "requested_scopes": (AT_SCOPE, ALL_SCOPE),
        },
    )
    raw = payload()
    raw["event"]["message"]["mentions"] = []
    receipt = env.runtime.receive(signed(env, raw))
    assert receipt.disposition == "accepted"


def test_authenticated_registry_adapter_port_checks_proof_and_scope(env):
    registry = SourceRegistry(env.engine)
    registry.add_adapter(env.adapter)
    normalized = registry.verify_and_normalize(env.source_id, signed(env, payload()))
    assert normalized[0].sender.actor_id == SENDER_ID
    verified = env.adapter.verify(env.instance, signed(env, payload()))
    with pytest.raises(DomainError):
        env.adapter.normalize(
            env.instance, VerifiedEnvelope(env.instance.scope, "fabricated-proof", verified.payload)
        )


def test_management_api_cookie_origin_csrf_no_store_no_secrets_and_default_callback(env):
    with TestClient(
        create_app(env.engine.settings, engine=env.engine, run_background=False), base_url=ORIGIN
    ) as client:
        path = "/api/v3/feishu-sources/" + str(env.source_id)
        assert client.get(path).status_code == 401
        client.cookies.set(SESSION_COOKIE, env.cookie)
        response = client.get(path)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["live_source_channel_gate"] == "NOT_RUN"
        for value in (
            env.secrets.app_secret,
            env.secrets.verification_token,
            env.secrets.encrypt_key,
        ):
            assert value not in response.text
        request = FeishuConfigure(
            expected_revision=1, settings=env.settings, review_ref="api-reviewed"
        ).model_dump(mode="json")
        assert client.put(path, json=request).status_code == 403
        csrf = WebAccounts(env.engine).session(env.cookie).csrf_token
        assert (
            client.put(
                path, json=request, headers={"Origin": "https://evil.invalid", "X-CSRF-Token": csrf}
            ).status_code
            == 403
        )
        assert (
            client.put(
                path, json=request, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
            ).status_code
            == 200
        )
        assert client.post("/api/v3/webhooks/feishu", json=payload()).status_code == 503


def test_explicit_callback_accepts_signed_challenge_without_creating_message(env):
    with TestClient(
        create_app(
            env.engine.settings, engine=env.engine, feishu_runtime=env.runtime, run_background=False
        ),
        base_url=ORIGIN,
    ) as client:
        raw = {
            "type": "url_verification",
            "token": env.secrets.verification_token,
            "challenge": "synthetic-challenge",
        }
        envelope = signed(env, raw)
        response = client.post(
            "/api/v3/webhooks/feishu", content=envelope.payload, headers=dict(envelope.headers)
        )
        assert response.status_code == 200 and response.json() == {
            "challenge": "synthetic-challenge"
        }
        assert response.headers["cache-control"] == "no-store"
        assert count(env, "source_ingress_inputs") == 0


@pytest.mark.parametrize(
    "mutation", ["actor_kind", "opaque_empty", "opaque_numeric", "mention_case"]
)
def test_invalid_or_confused_platform_actor_cannot_inherit_role(env, mutation):
    raw = payload(body="untrusted actor body")
    if mutation == "actor_kind":
        raw["event"]["sender"]["sender_type"] = "app"
    elif mutation == "opaque_empty":
        raw["event"]["sender"]["sender_id"]["open_id"] = ""
    elif mutation == "opaque_numeric":
        raw["event"]["sender"]["sender_id"]["open_id"] = 42
    else:
        raw["event"]["message"]["mentions"][0]["id"]["open_id"] = BOT_ID.lower()
    before = count(env, "source_identity_evidence")
    try:
        receipt = env.runtime.receive(signed(env, raw))
    except DomainError:
        pass
    else:
        assert receipt.disposition in {"filtered", "rejected"}
    assert count(env, "source_identity_evidence") == before
    assert count(env, "source_ingress_inputs") == count(env, "source_ingress_bodies") == 0


def test_recipient_recall_is_bodyless_app_fact_and_irreversible(env):
    first_raw = payload()
    first = env.runtime.receive(signed(env, first_raw))
    recalled = {
        "schema": "2.0",
        "header": {
            **first_raw["header"],
            "event_type": "im.message.recalled_v1",
            "event_id": "recall-platform-event",
        },
        "event": {
            "message_id": first_raw["event"]["message"]["message_id"],
            "chat_id": CHAT_ID,
            "recall_time": str(int(time.time() * 1000)),
        },
    }
    delivery = env.adapter.verify_ingress(env.instance, signed(env, recalled))
    item = delivery.metadata.items[0]
    assert item.operation == "delete" and delivery.bodies == (None,)
    assert item.event.sender.actor_kind == "service"
    assert item.event.sender.actor_id == APP_ID and item.platform_moderation
    with pytest.raises(DomainError) as error:
        env.adapter.verify_identity(env.instance, signed(env, recalled))
    assert error.value.code == "FEISHU_RECALL_NO_HUMAN_PROOF"
    before = count(env, "source_identity_evidence")
    receipt = env.runtime.receive(signed(env, recalled))
    assert receipt.committed
    assert count(env, "source_identity_evidence") == before
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    assert count(env, "source_ingress_bodies") == 0
    first_raw["header"]["event_id"] = "late-original-message"
    env.runtime.receive(signed(env, first_raw))
    assert count(env, "source_ingress_bodies") == 0


def test_changed_snapshot_without_native_order_is_ambiguous_and_huge_body_never_retained(env):
    raw = payload()
    first = env.runtime.receive(signed(env, raw))
    raw["header"]["event_id"] = "observed-large-change"
    raw["event"]["message"]["content"] = json.dumps({"text": "中" * 30000}, ensure_ascii=False)
    delivery = env.adapter.verify_ingress(env.instance, signed(env, raw))
    assert delivery.bodies == (None,)
    result = env.runtime.receive(signed(env, raw))
    assert result.committed
    assert count(env, "source_ingress_bodies") == 0
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    with env.engine.transaction() as conn:
        assert conn.execute("SELECT ambiguous FROM app.source_ingress_entities").fetchone()[
            "ambiguous"
        ]


def test_distinct_chats_keep_equal_message_ids_in_separate_entity_namespaces(engine):
    other_chat = "Oc_Chat-Other"
    env = setup(engine, settings_changes={"chat_ids": (CHAT_ID, other_chat)})
    first = env.runtime.receive(signed(env, payload()))
    other = payload(event_id="other-chat-event")
    other["event"]["message"]["chat_id"] = other_chat
    # The human has no role binding in the second conversation, despite matching OpenID.
    denied = env.runtime.receive(signed(env, other))
    assert denied.disposition == "rejected"
    assert read(env, first)["content"]["text"] == "synthetic message @_user_1"


def test_concurrent_settings_cas_has_one_winner(env):
    request = FeishuConfigure(
        expected_revision=1, settings=env.settings, review_ref="concurrent-settings"
    )

    def update(_):
        try:
            return env.sources.configure(env.cookie, env.source_id, request)
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("FEISHU_REVISION_CONFLICT") == 1
    assert env.sources.inspect(env.cookie, env.source_id)["revision"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("bot_open_id", "Ou_Other-Bot"),
        ("app_id", "Cli_Other"),
        ("tenant_key", "OtherTenant"),
    ],
)
def test_installed_identity_cannot_be_rebound_by_settings(env, field, value):
    candidate = env.settings.model_copy(update={field: value})
    with pytest.raises(DomainError):
        env.sources.configure(
            env.cookie,
            env.source_id,
            FeishuConfigure(expected_revision=1, settings=candidate, review_ref="forbidden-rebind"),
        )
    assert env.sources.inspect(env.cookie, env.source_id)["revision"] == 1
    assert SourceRegistry(env.engine).get_instance(env.source_id) == env.instance


def test_config_revoked_browser_session_cannot_save_even_with_cached_user(env):
    accounts = WebAccounts(env.engine)
    accounts.revoke(env.cookie)
    with pytest.raises(DomainError):
        env.sources.configure(
            env.cookie,
            env.source_id,
            FeishuConfigure(
                expected_revision=1, settings=env.settings, review_ref="revoked-session"
            ),
        )


def test_optional_webhook_actual_signed_message_duplicates_and_bad_headers(env):
    with TestClient(
        create_app(
            env.engine.settings, engine=env.engine, feishu_runtime=env.runtime, run_background=False
        ),
        base_url=ORIGIN,
    ) as client:
        envelope = signed(env, payload())
        for _ in range(2):
            response = client.post(
                "/api/v3/webhooks/feishu", content=envelope.payload, headers=dict(envelope.headers)
            )
            assert response.status_code == 200 and response.json()["code"] == 0
            assert response.headers["cache-control"] == "no-store"
        assert count(env, "source_ingress_inputs") == 1
        headers = list(envelope.headers.items()) + [("x-lark-request-nonce", "conflicting-nonce")]
        response = client.post("/api/v3/webhooks/feishu", content=envelope.payload, headers=headers)
        assert response.status_code == 403
        assert response.json()["code"] == "FEISHU_DUPLICATE_HEADER"


def test_human_context_provider_without_requester_remains_context_only(env):
    first = env.runtime.receive(signed(env, payload()))
    env.identities.roles(
        env.cookie,
        env.binding["binding_id"],
        RoleChange(
            expected_revision=env.binding["revision"],
            roles=("context_provider",),
            review_ref="human-context-only",
        ),
    )
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    receipt = env.runtime.receive(signed(env, payload(message_id="Om_Human-Context-Only")))
    assert receipt.disposition == "accepted" and receipt.committed
    with env.engine.transaction() as conn:
        row = conn.execute(
            "SELECT context_only FROM app.source_ingress_inputs WHERE inbox_id=%s",
            (receipt.input_ids[0],),
        ).fetchone()
    assert row["context_only"]
    assert read(env, receipt)["content"]["text"] == "synthetic message @_user_1"
    assert count(env, "runs") == count(env, "outbox") == 0
