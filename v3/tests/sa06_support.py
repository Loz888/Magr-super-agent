"""Synthetic Feishu official envelopes and controlled HTTP, never a live platform."""

import base64
import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import SecretStr
from test_super_agent_sa03 import definition

from mgar_v3.sources.domain import ChannelCapabilities, SourceInstance, SourceScope
from mgar_v3.sources.feishu import CAPABILITIES, FeishuAdapter
from mgar_v3.sources.feishu_domain import (
    ALL_SCOPE,
    AT_SCOPE,
    EVENTS,
    HISTORY_SCOPE,
    RECEIVE,
    VERSION,
    FeishuActivation,
    FeishuConfigure,
    FeishuSecrets,
    FeishuSettings,
)
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.sources.feishu_store import FeishuSources
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.identity_domain import BindIdentity
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.sources.ingress_domain import IngressConfigure, IngressSettings
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.web_access import LoginInput, WebAccounts

APP_ID = "Cli_App-AbC"
TENANT = "Tenant-AbC"
BOT_ID = "Ou_Bot-AbC"
SENDER_ID = "Ou_Alice-AbC"
CHAT_ID = "Oc_Chat-AbC"
CONTEXT_BOT_ID = "Ou_Context-Bot"
TOKEN = "SAV1-06-synthetic-verification-token"
ORIGIN = "http://127.0.0.1:18080"


def payload(
    *,
    event_id=None,
    message_id="Om_Message-AbC",
    sender=SENDER_ID,
    body="synthetic message @_user_1",
    event_type=RECEIVE,
    **changes,
):
    now = str(int(time.time() * 1000))
    value = {
        "schema": "2.0",
        "header": {
            "event_id": event_id or str(uuid4()),
            "event_type": event_type,
            "create_time": now,
            "token": TOKEN,
            "app_id": APP_ID,
            "tenant_key": TENANT,
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": sender},
                "sender_type": "user",
                "tenant_key": TENANT,
            },
            "message": {
                "message_id": message_id,
                "chat_id": CHAT_ID,
                "chat_type": "group",
                "create_time": now,
                "update_time": now,
                "message_type": "text",
                "content": json.dumps({"text": body}, ensure_ascii=False),
                "mentions": [
                    {
                        "key": "@_user_1",
                        "id": {"open_id": BOT_ID},
                        "name": "Synthetic Bot",
                        "tenant_key": TENANT,
                    }
                ],
            },
        },
    }
    value.update(changes)
    return value


def signed(env, raw, *, headers=None, timestamp=None, nonce=None, encrypted=False):
    data = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if encrypted:
        # Match the official SDK's AES-CBC envelope format: IV + PKCS7-padded plaintext.
        iv = os.urandom(16)
        size = 16 - len(data) % 16
        pad = data + bytes([size]) * size
        encryptor = Cipher(
            algorithms.AES(hashlib.sha256(env.secrets.encrypt_key.encode()).digest()), modes.CBC(iv)
        ).encryptor()
        encoded = base64.b64encode(iv + encryptor.update(pad) + encryptor.finalize()).decode()
        data = json.dumps({"encrypt": encoded}, separators=(",", ":")).encode()
    ts = str(int(time.time())) if timestamp is None else str(timestamp)
    salt = nonce or uuid4().hex
    signature = hashlib.sha256((ts + salt + env.secrets.encrypt_key).encode() + data).hexdigest()
    values = {
        "X-Lark-Request-Timestamp": ts,
        "X-Lark-Request-Nonce": salt,
        "X-Lark-Signature": signature,
    }
    values.update(headers or {})
    return TransportEnvelope(values, data)


def setup(engine, *, history=False, settings_changes=None, receive=None, mode="auto_investigate"):
    accounts = WebAccounts(engine)
    owner = accounts.create_owner(
        LoginInput(username="test-owner", password=SecretStr("fixture-owner-password"))
    )
    cookie = accounts.issue_session(owner)
    source_id = uuid4()
    registry = SourceRegistry(engine)
    instance = SourceInstance(
        scope=SourceScope(
            source_kind="feishu",
            source_instance_id=source_id,
            tenant_id=TENANT,
            installation_id=APP_ID,
        ),
        project_id=engine.settings.project_id,
        adapter_version=VERSION,
        capabilities=CAPABILITIES,
        channel_capabilities=ChannelCapabilities(
            send="unsupported",
            idempotent_send="unsupported",
            query_delivery="unsupported",
            delete_message="unsupported",
        ),
        credential_ref="vault://synthetic/feishu-bot",
    )
    registry.register_instance(instance)
    config = {
        "app_id": APP_ID,
        "tenant_key": TENANT,
        "bot_open_id": BOT_ID,
        "chat_ids": (CHAT_ID,),
        "sender_open_ids": (SENDER_ID,),
        "context_bot_open_ids": (CONTEXT_BOT_ID,),
        "events": EVENTS,
        "requested_scopes": (AT_SCOPE, ALL_SCOPE, HISTORY_SCOPE) if history else (AT_SCOPE,),
        "scopes_review_ref": "SA06-controlled-scope-review",
        "history_enabled": history,
        "enabled": True,
    }
    config.update(settings_changes or {})
    settings = FeishuSettings.model_validate(config)
    sources = FeishuSources(engine)
    sources.configure(
        cookie,
        source_id,
        FeishuConfigure(expected_revision=0, settings=settings, review_ref="SA06-controlled-owner"),
    )
    instance = registry.get_instance(source_id)
    secrets = FeishuSecrets("SAV1-06-synthetic-app-secret", TOKEN, "SAV1-06-synthetic-encrypt-key")
    adapter = FeishuAdapter(settings, secrets)
    identities = SourceIdentities(engine, {("feishu", VERSION): adapter})
    provisional = SimpleNamespace(secrets=secrets)
    boot = payload(event_id="identity-bootstrap", message_id="identity-bootstrap-message")
    credential = identities.authenticate(source_id, signed(provisional, boot))
    record = next(
        row for row in identities.evidence(cookie) if row["evidence_id"] == credential.evidence_id
    )
    binding = identities.bind(
        cookie,
        BindIdentity(
            evidence_id=credential.evidence_id,
            expected_identity_revision=record["identity_revision"],
            roles=("context_provider", "requester"),
            review_ref="SA06-identity-reviewed",
        ),
    )
    actor = adapter.verify_identity(instance, signed(provisional, payload())).actor
    raw = definition(instance, mode=mode).model_dump(mode="json")
    raw["selector"].update(
        actors=[actor.model_dump(mode="json")],
        conversations=list(settings.chat_ids),
        event_kinds=list(settings.events),
    )
    raw["receive"] = receive or {
        "enabled": True,
        "retain_body": True,
        "body_ttl_seconds": 300,
        "allow_history_context": history,
    }
    policy = PolicyDefinition.model_validate(raw)
    policy_id = uuid4()
    engine.source_policies.save(
        cookie,
        policy_id,
        PolicySave(expected_revision=0, definition=policy, review_ref="SA06-signed-policy"),
    )
    control = IngressSettings(owner="gateway", policy_id=policy_id, policy_revision=1)
    engine.source_ingress.configure(
        cookie,
        source_id,
        IngressConfigure(expected_generation=0, settings=control, review_ref="SA06-single-owner"),
    )
    engine.source_ingress.cipher = BodyCipher(Fernet.generate_key())
    activation = FeishuActivation(
        source_id,
        1,
        1,
        datetime.now(UTC) + timedelta(minutes=30),
        frozenset({"webhook", "socket", "history", "health"}),
        "SA06-controlled-no-network",
    )
    env = SimpleNamespace(
        engine=engine,
        cookie=cookie,
        sources=sources,
        settings=settings,
        instance=instance,
        source_id=source_id,
        adapter=adapter,
        identities=identities,
        credential=credential,
        binding=binding,
        policy_id=policy_id,
        policy=policy,
        control=control,
        secrets=secrets,
        activation=activation,
        calls=[],
        response=None,
    )

    def handler(request):
        env.calls.append(request)
        if env.response:
            changed = env.response(request)
            if changed is not None:
                return changed
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "controlled-token", "expire": 7200}
            )
        if request.url.path.endswith("/bot/v3/info"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "bot": {
                        "app_id": settings.app_id,
                        "open_id": settings.bot_open_id,
                        "tenant_key": settings.tenant_key,
                    },
                },
            )
        if request.url.path.endswith("/im/v1/messages"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        return httpx.Response(404)

    env.handler = handler
    env.runtime = FeishuRuntime(
        sources, source_id, 1, 1, secrets, activation, transport=httpx.MockTransport(handler)
    )
    env.broker = env.runtime.broker
    return env
