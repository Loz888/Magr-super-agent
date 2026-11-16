"""Controlled HTTP for the paid-protocol branches; never real supplier/platform IO."""

import json
import shutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
from sa06_support import BOT_ID, CHAT_ID, payload, signed
from sa07_support import PRICE, definition, save
from sa22_reply_support import compose

from mgar_v3.domain import digest
from mgar_v3.settings import ROOT
from mgar_v3.sources.domain import ActorRef
from mgar_v3.sources.feishu_domain import FeishuActivation
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.sources.ingress_domain import IngressConfigure, IngressSettings
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_conversation_crypto import ReplyKeys
from mgar_v3.super_agent.source_conversations import ConversationProfile, SourceConversations
from mgar_v3.super_agent.source_feishu_channel import FeishuNotificationChannel
from mgar_v3.super_agent.source_notification_domain import NotificationConfigure
from mgar_v3.super_agent.source_notification_ports import NotificationProfile


def model_environment(
    engine,
    *,
    handler=None,
    token_hook=None,
    repository=None,
    question="请解释这段授权仓库代码 @_user_1",
    history_turns=0,
):
    engine.settings = engine.settings.model_copy(
        update={"allow_paid": True, "allow_external_write": True}
    )
    old = compose(engine)
    source = old.source
    environment = SimpleNamespace(
        engine=engine, service=engine.project_budgets, cookie=source.cookie
    )
    caps = {"total_micros": 2000000, "hour_micros": 2000000, "day_micros": 2000000}
    save(environment, "organization", "sa22-organization", definition(caps=caps))
    save(
        environment,
        "project",
        engine.settings.project_id,
        definition(caps=caps, organization_id="sa22-organization"),
    )
    base = repository.commit if repository is not None else "a" * 40
    current = engine.source_policies.read(source.cookie, source.policy_id)
    raw = current["definition"]
    raw["receive"]["model_input"] = True
    raw["admission"] = {"enabled": True, "request_kinds": ["explicit_request"]}
    raw["execution"] = {
        "enabled": True,
        "capabilities": ["model_call", "read_repository"],
        "repositories": [
            {"repository": engine.settings.repository, "base_sha": base, "paths": ["v3/src"]}
        ],
    }
    raw["budget"] = {
        "total_limit_micros": 2000000,
        "router_limit_micros": 2000000,
        "accounting_ref": "sa22-new-model-slice",
    }
    raw["notification"]["data_classes"] = ["public_summary", "private_summary"]
    raw["notification"]["content_classes"] = ["necessary_question", "verified_result"]
    raw["notification"]["max_bytes"] = 4096
    saved = engine.source_policies.save(
        source.cookie,
        source.policy_id,
        PolicySave(
            expected_revision=2,
            definition=PolicyDefinition.model_validate(raw),
            review_ref="SA22-explicit-controlled-model-protocol",
        ),
    )
    engine.source_ingress.configure(
        source.cookie,
        source.source_id,
        IngressConfigure(
            expected_generation=2,
            settings=IngressSettings(
                owner="gateway", policy_id=source.policy_id, policy_revision=3
            ),
            review_ref="SA22-model-policy-cutover",
        ),
    )
    model_calls, platform_calls = [], []

    class ObservedChannel(FeishuNotificationChannel):
        # A controlled override of physical HTTP only. Logical live branches are
        # exercised; this is explicitly not actual live-source evidence.
        @staticmethod
        def _post(client, path, body, headers=None):
            platform_calls.append({"path": path, "body": body})
            if path.endswith("tenant_access_token/internal"):
                if token_hook:
                    token_hook(source)
                return 200, {"code": 0, "tenant_access_token": "controlled-tenant-token"}
            return 200, {"code": 0, "data": {"message_id": "om_model_reply", "chat_id": CHAT_ID}}

    holder = {}
    channel = ObservedChannel(
        engine,
        source.instance.scope,
        lambda: source.secrets,
        body_resolver=lambda aid, ref: holder["service"].resolve_body(aid, ref),
    )
    profile = NotificationProfile(
        source.instance.scope,
        source.instance.revision,
        ActorRef(scope=source.instance.scope, id_type="open_id", actor_id=BOT_ID, actor_kind="bot"),
        source.instance.channel_capabilities,
        channel,
        mode="live",
    )
    engine.source_notifications.register_profile(profile)
    notification = old.config.model_copy(
        update={
            "policy_revision": 3,
            "policy_sha256": saved["definition_sha256"],
            "profile_sha256": profile.profile_sha256,
            "content_classes": ("necessary_question", "verified_result"),
        }
    )
    engine.source_notifications.configure(
        source.cookie,
        source.source_id,
        NotificationConfigure(
            expected_revision=1,
            settings=notification,
            review_ref="SA22-controlled-answer-delivery",
        ),
    )
    runtime = FeishuRuntime(
        source.sources,
        source.source_id,
        2,
        3,
        source.secrets,
        FeishuActivation(
            source.source_id,
            2,
            3,
            datetime.now(UTC) + timedelta(minutes=10),
            frozenset({"webhook", "socket", "health"}),
            "controlled-model-ingress",
        ),
        transport=httpx.MockTransport(source.handler),
    )
    inbound = runtime.receive(
        signed(
            source,
            payload(message_id="Om_QA-" + uuid4().hex, body=question),
        )
    )
    assert inbound.disposition == "accepted"

    def model(request):
        model_calls.append(request)
        if handler:
            return handler(request)
        return httpx.Response(
            200,
            json={
                "id": "controlled-model-receipt",
                "model": PRICE.model_id,
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"intent": "answer", "answer": "授权代码说明。"}),
                        },
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(model), trust_env=False)
    keys = ReplyKeys(ROOT / ".local/sa22-controlled-reply-keys" / uuid4().hex)
    service = SourceConversations(
        engine,
        source.cookie,
        ConversationProfile(
            project_id=engine.settings.project_id,
            repository=engine.settings.repository,
            source_id=source.source_id,
            price=PRICE,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            manifest_sha256=digest("explicit-controlled-model-slice"),
            repository_base_sha=base,
            repository_tools=repository is not None,
            history_turns=history_turns,
            context_tokens=32768 if repository is not None else 16384,
        ),
        keys,
        client=client,
        api_key="controlled-not-a-credential",
        controlled_transport=True,
        repository=repository,
    )
    holder["service"] = service
    return SimpleNamespace(
        source=source,
        service=service,
        input_id=inbound.input_ids[0],
        model_calls=model_calls,
        platform_calls=platform_calls,
        client=client,
        keys=keys,
        runtime=runtime,
        close=lambda: (client.close(), shutil.rmtree(keys.root, ignore_errors=True)),
    )
