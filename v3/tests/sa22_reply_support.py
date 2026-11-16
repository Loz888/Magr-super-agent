"""Fresh controlled-platform composition of the real reply services and PostgreSQL."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from sa06_support import BOT_ID, CHAT_ID, payload, setup, signed

from mgar_v3.sources.domain import ActorRef, ChannelCapabilities, SourceThreadRef
from mgar_v3.sources.feishu_domain import FeishuActivation, FeishuConfigure
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.sources.ingress_domain import IngressConfigure, IngressSettings
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.policy_domain import NotificationDestination, PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_feishu_channel import FeishuNotificationChannel
from mgar_v3.super_agent.source_notification_domain import NotificationConfigure, NotificationSettings
from mgar_v3.super_agent.source_notification_ports import NotificationProfile
from mgar_v3.super_agent.source_notification_transport import NotificationTransport


def compose(engine, *, hook=None, outcome="accepted"):
    env = setup(engine)
    registry = SourceRegistry(engine)
    old = registry.get_instance(env.source_id)
    caps = ChannelCapabilities(send="supported", idempotent_send="unknown",
                               query_delivery="unsupported", delete_message="unsupported")
    registry.register_instance(old.model_copy(update={"revision": old.revision + 1,
                                                    "channel_capabilities": caps}))
    env.sources.configure(env.cookie, env.source_id, FeishuConfigure(
        expected_revision=1, settings=env.settings, review_ref="SA22-controlled-channel-install"))
    env.instance = registry.get_instance(env.source_id)
    destination = NotificationDestination(thread=SourceThreadRef(
        scope=env.instance.scope, conversation_id=CHAT_ID), target="feishu:reply:"+CHAT_ID,
        visibility="private")
    raw = env.policy.model_dump(mode="json")
    raw.update(mode="auto_investigate", execution={"enabled": False},
               admission={"enabled": False}, budget={})
    raw["selector"]["source_revision"] = env.instance.revision
    raw["notification"] = {"enabled": True, "destinations": [destination.model_dump(mode="json")],
        "content_classes": ["verified_result"], "data_classes": ["public_summary"],
        "max_bytes": 512, "max_per_hour": 20}
    definition = PolicyDefinition.model_validate(raw)
    saved = engine.source_policies.save(env.cookie, env.policy_id, PolicySave(
        expected_revision=1, definition=definition, review_ref="SA22-owner-receipt-replies-only"))
    engine.source_ingress.configure(env.cookie, env.source_id, IngressConfigure(
        expected_generation=1, settings=IngressSettings(owner="gateway", policy_id=env.policy_id,
        policy_revision=2), review_ref="SA22-current-policy"))
    activation = FeishuActivation(env.source_id, 2, 2, datetime.now(UTC)+timedelta(minutes=20),
        frozenset({"webhook", "socket", "health"}), "SA22-controlled-no-network")
    env.runtime = FeishuRuntime(env.sources, env.source_id, 2, 2, env.secrets, activation,
                               transport=httpx.MockTransport(env.handler))
    calls = []

    def transport(request):
        calls.append(request)
        if hook:
            hook(env, request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "controlled-token"})
        if outcome == "lost":
            raise httpx.ReadTimeout("controlled lost response", request=request)
        if outcome == "malformed":
            return httpx.Response(200, json={"code": 0, "data": {}})
        if outcome == "rejected":
            return httpx.Response(400, json={"code": 230002})
        return httpx.Response(200, json={"code": 0, "data": {
            "message_id": "om_controlled_reply", "chat_id": CHAT_ID}})

    channel = FeishuNotificationChannel(engine, env.instance.scope, lambda: env.secrets,
        mode="fixture", transport=httpx.MockTransport(transport))
    profile = NotificationProfile(env.instance.scope, env.instance.revision, ActorRef(
        scope=env.instance.scope, id_type="open_id", actor_id=BOT_ID, actor_kind="bot"), caps, channel)
    engine.source_notifications.register_profile(profile)
    now = datetime.now(UTC)
    config = NotificationSettings(enabled=True, policy_id=env.policy_id, policy_revision=2,
        policy_sha256=saved["definition_sha256"], profile_sha256=profile.profile_sha256,
        destination=destination, content_classes=("verified_result",), cooldown_seconds=1,
        aggregation_seconds=0, ttl_seconds=120, valid_from=now-timedelta(seconds=2),
        valid_until=min(definition.valid_until, now+timedelta(minutes=10)))
    engine.source_notifications.configure(env.cookie, env.source_id, NotificationConfigure(
        expected_revision=0, settings=config, review_ref="SA22-controlled-notification-authorization"))
    receipt = env.runtime.receive(signed(env, payload(body="PRIVATE-SOURCE-DO-NOT-ECHO @_user_1")))
    assert receipt.disposition == "accepted"
    return SimpleNamespace(source=env, calls=calls, channel=channel, profile=profile,
        input_id=receipt.input_ids[0], transport=NotificationTransport(engine.source_notifications),
        config=config, request_payloads=lambda:[json.loads(r.content) for r in calls])
