"""Rich-post control regressions using synthetic content and the original Adapter."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from mgar_v3.sources.domain import (
    ActorRef,
    ChannelCapabilities,
    EntityRevision,
    SourceEvent,
    SourceInstance,
    SourceScope,
    SourceThreadRef,
)
from mgar_v3.sources.feishu import CAPABILITIES, FeishuAdapter
from mgar_v3.sources.feishu_domain import (
    RECEIVE,
    VERSION,
    FeishuSecrets,
    FeishuSettings,
)
from mgar_v3.sources.ingress_domain import ItemMetadata
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.super_agent.fast_path_rules import extract_text, parse_intent

TICKET = UUID("11111111-1111-4111-8111-111111111111")
SOURCE = UUID("22222222-2222-4222-8222-222222222222")
APP = "Cli_SA08-Post-Probe"
TENANT = "Tenant_SA08-Post-Probe"
BOT = "Ou_SA08-Post-Probe-Bot"
HUMAN = "Ou_SA08-Post-Probe-Human"
CHAT = "Oc_SA08-Post-Probe-Chat"
SCOPE = SourceScope(
    source_kind="feishu", source_instance_id=SOURCE,
    tenant_id=TENANT, installation_id=APP,
)


def post_content(command):
    return {
        "zh_cn": {
            "title": "",
            "content": [[
                {"tag": "at", "user_id": BOT},
                {"tag": "text", "text": f"/mgar {command} {TICKET}"},
            ]],
        },
    }


def synthetic_item(body):
    now = datetime.now(UTC)
    return ItemMetadata(
        event=SourceEvent(
            scope=SCOPE, project_id="orders-demo", source_revision=1,
            delivery_id="sa08-rich-post-pure", remote_event_id="sa08-rich-post-pure",
            entity_id="Om_SA08-Post-Probe-Message",
            entity_revision=EntityRevision(origin="derived", value="synthetic-version-1"),
            event_kind=RECEIVE,
            source_thread=SourceThreadRef(scope=SCOPE, conversation_id=CHAT),
            sender=ActorRef(
                scope=SCOPE, id_type="open_id", actor_id=HUMAN, actor_kind="human",
            ),
            addressed="mention", occurred_at=now, received_at=now,
            body_ref=hashlib.sha256(body).hexdigest(),
            verification_ref="synthetic-rules-only-not-authority",
            provenance="verified_source",
        ),
        entity_namespace="feishu-message:synthetic-post-probe", operation="create",
    )


def test_verified_mention_post_at_prefix_preserves_pause_intent():
    """A structural leading mention is not ordinary text before the control."""
    body = json.dumps({
        "schema_version": "feishu-content-v1", "message_type": "post",
        "content": post_content("pause"),
    }, ensure_ascii=False, separators=(",", ":")).encode()
    intent = parse_intent(synthetic_item(body), extract_text(body))
    assert intent.control_action == "pause"
    assert intent.ticket_id == TICKET and intent.request_kind is None


def test_original_adapter_verified_post_at_prefix_preserves_cancel_intent():
    """Real parser/normalizer shape with locally authenticated synthetic evidence."""
    now = datetime.now(UTC)
    timestamp = str(int(now.timestamp()))
    milliseconds = str(int(now.timestamp() * 1000))
    secret = FeishuSecrets(
        "synthetic-app-secret-no-platform",
        "synthetic-verification-token-no-platform",
        "synthetic-encrypt-key-no-platform",
    )
    adapter = FeishuAdapter(FeishuSettings(
        app_id=APP, tenant_key=TENANT, bot_open_id=BOT,
        chat_ids=(CHAT,), sender_open_ids=(HUMAN,),
        scopes_review_ref="SA08-synthetic-rich-post-probe", enabled=True,
    ), secret)
    instance = SourceInstance(
        scope=SCOPE, project_id="orders-demo", adapter_version=VERSION,
        capabilities=CAPABILITIES,
        channel_capabilities=ChannelCapabilities(
            send="unsupported", idempotent_send="unsupported",
            query_delivery="unsupported", delete_message="unsupported",
        ),
        credential_ref="vault://synthetic/sa08-post-probe",
    )
    raw = {
        "schema": "2.0",
        "header": {
            "event_id": "sa08-post-original-adapter-probe", "event_type": RECEIVE,
            "create_time": milliseconds, "token": secret.verification_token,
            "app_id": APP, "tenant_key": TENANT,
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": HUMAN}, "sender_type": "user",
                "tenant_key": TENANT,
            },
            "message": {
                "message_id": "Om_SA08-Post-Probe-Message", "chat_id": CHAT,
                "chat_type": "group", "create_time": milliseconds,
                "update_time": milliseconds, "message_type": "post",
                "content": json.dumps(post_content("cancel"), ensure_ascii=False),
                "mentions": [{
                    "key": "@_user_1", "id": {"open_id": BOT},
                    "name": "Synthetic Probe Bot", "tenant_key": TENANT,
                }],
            },
        },
    }
    wire = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    nonce = "synthetic-rich-post-probe-nonce"
    signature = hashlib.sha256((timestamp + nonce + secret.encrypt_key).encode() + wire).hexdigest()
    envelope = TransportEnvelope({
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature,
    }, wire)
    verified = adapter.delivery(instance, envelope, "webhook")
    item, body = verified.metadata.items[0], verified.bodies[0]
    assert verified.metadata.verification_method == "platform_signature"
    assert item.event.addressed == "mention" and item.event.sender.actor_kind == "human"
    assert item.event.sender.actor_id == HUMAN and item.event.scope == SCOPE
    assert item.event.source_thread.conversation_id == CHAT
    assert body is not None
    normalized = json.loads(body)
    assert normalized["schema_version"] == "feishu-content-v1"
    assert normalized["message_type"] == "post"
    assert normalized["content"] == post_content("cancel")
    intent = parse_intent(item, extract_text(body))
    assert intent.control_action == "cancel"
    assert intent.ticket_id == TICKET and intent.request_kind is None
