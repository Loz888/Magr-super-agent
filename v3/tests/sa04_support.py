"""Synthetic signed platform fixtures. This is not a production adapter or SDK."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from cryptography.fernet import Fernet
from pydantic import SecretStr
from test_super_agent_sa02 import ControlledPlatform, bind
from test_super_agent_sa03 import definition

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import (
    ActorRef,
    ChannelCapabilities,
    SourceCapabilities,
    SourceEvent,
    SourceInstance,
    SourceScope,
    SourceThreadRef,
)
from mgar_v3.sources.gateway import SourceGateway
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.sources.ingress_domain import (
    DeliveryMetadata,
    IngressConfigure,
    IngressSettings,
    ItemMetadata,
    VerifiedDelivery,
)
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.web_access import LoginInput, WebAccounts

KEY = b"SAV1-04-synthetic-signed-platform-only"


class SignedIngress:
    def __init__(self, engine):
        self.engine = engine

    def verify_ingress(self, instance, envelope):
        assert self.engine.connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        signature = hmac.new(KEY, envelope.payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, envelope.headers.get("signature", "")):
            raise DomainError("FIXTURE_BAD_SIGNATURE", "Signed fixture rejected", 401)
        raw = json.loads(envelope.payload)
        scope = SourceScope.model_validate(raw["scope"])
        at = datetime.now(UTC)
        items, bodies = [], []
        for item in raw["items"]:
            body = item["body"].encode() if item["body"] is not None else None
            event = SourceEvent(
                scope=scope,
                project_id=instance.project_id,
                source_revision=raw["source_revision"],
                delivery_id=raw["delivery"],
                remote_event_id=item["event"],
                entity_id=item["entity"],
                entity_revision={
                    "origin": "native" if item["order"] is not None else "derived",
                    "value": item["revision"],
                },
                event_kind=item["kind"],
                source_thread=SourceThreadRef(
                    scope=scope,
                    conversation_id=item["conversation"],
                    native_thread_id=item.get("topic"),
                ),
                sender=ActorRef(
                    scope=scope,
                    id_type="open_id",
                    actor_id=item["sender"],
                    actor_kind=item["actor_kind"],
                ),
                addressed="mention",
                occurred_at=item["occurred_at"],
                received_at=at,
                body_ref=hashlib.sha256(body).hexdigest() if body is not None else None,
                verification_ref="synthetic-platform-signature",
                provenance="verified_source",
                historical_backfill=item.get("historical", False),
            )
            items.append(
                ItemMetadata(
                    event=event,
                    entity_namespace="messages",
                    operation=item["operation"],
                    native_order=item["order"],
                    platform_moderation=item.get("moderation", False),
                )
            )
            bodies.append(body)
        return VerifiedDelivery(
            DeliveryMetadata(
                source_instance_id=instance.scope.source_instance_id,
                source_revision=raw["source_revision"],
                delivery_id=raw["delivery"],
                payload_sha256=hashlib.sha256(envelope.payload).hexdigest(),
                verified_at=at,
                verification_method="platform_signature",
                items=tuple(items),
                cursor=raw.get("cursor"),
            ),
            tuple(bodies),
        )


def envelope(
    env,
    delivery="d1",
    entity="m1",
    version=1,
    body="synthetic private content",
    operation="create",
    **changes,
):
    item = {
        "event": changes.pop("event", f"{entity}-{version}"),
        "entity": entity,
        "revision": str(version),
        "order": version,
        "kind": "message."
        + {"create": "created", "edit": "edited", "delete": "deleted"}[operation],
        "conversation": "Chat-A",
        "sender": "Alice-AbC",
        "actor_kind": "human",
        "occurred_at": env.occurred_at,
        "body": None if operation == "delete" else body,
        "operation": operation,
        **changes,
    }
    raw = {
        "scope": env.installation.scope.model_dump(mode="json"),
        "source_revision": env.installation.revision,
        "delivery": delivery,
        "items": [item],
    }
    if "cursor" in item:
        raw["cursor"] = item.pop("cursor")
    return signed(raw)


def signed(raw):
    payload = json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()
    return TransportEnvelope(
        payload=payload, headers={"signature": hmac.new(KEY, payload, hashlib.sha256).hexdigest()}
    )


def setup(
    engine, *, kind="feishu", receive=None, limits=None, capabilities=None, mode="auto_investigate"
):
    accounts = WebAccounts(engine)
    owner = accounts.create_owner(
        LoginInput(username="test-owner", password=SecretStr("fixture-owner-password"))
    )
    cookie = accounts.issue_session(owner)
    installation = SourceInstance(
        scope=SourceScope(
            source_kind=kind,
            source_instance_id=uuid4(),
            tenant_id="Tenant-A",
            installation_id="Install-A",
        ),
        project_id=engine.settings.project_id,
        adapter_version="controlled-v1",
        capabilities=SourceCapabilities.model_validate(
            capabilities or {key: "supported" for key in SourceCapabilities.model_fields}
        ),
        channel_capabilities=ChannelCapabilities.model_validate(
            {key: "unsupported" for key in ChannelCapabilities.model_fields}
        ),
    )
    SourceRegistry(engine).register_instance(installation)
    identities = SourceIdentities(engine, {(kind, "controlled-v1"): ControlledPlatform(engine)})
    binding, credential = bind(
        identities, cookie, installation, roles=("context_provider", "requester")
    )
    policy_id = uuid4()
    raw = definition(
        installation,
        mode=mode,
        receive=receive
        or {
            "enabled": True,
            "retain_body": True,
            "body_ttl_seconds": 300,
            "allow_history_context": True,
        },
    ).model_dump(mode="json")
    raw["selector"]["event_kinds"] = ["message.created", "message.edited", "message.deleted"]
    value = PolicyDefinition.model_validate(raw)
    engine.source_policies.save(
        cookie,
        policy_id,
        PolicySave(expected_revision=0, definition=value, review_ref="SA04-fixture-policy"),
    )
    settings = IngressSettings(
        owner="gateway",
        policy_id=policy_id,
        policy_revision=1,
        legacy_repository=engine.settings.repository if kind == "github" else None,
        **(limits or {}),
    )
    key = Fernet.generate_key()
    engine.source_ingress.cipher = BodyCipher(key)
    engine.source_ingress.configure(
        cookie,
        installation.scope.source_instance_id,
        IngressConfigure(expected_generation=0, settings=settings, review_ref="SA04-owner-cutover"),
    )
    env = SimpleNamespace(
        engine=engine,
        installation=installation,
        identities=identities,
        binding=binding,
        credential=credential,
        cookie=cookie,
        policy_id=policy_id,
        definition=value,
        settings=settings,
        key=key,
        occurred_at=datetime.now(UTC).isoformat(),
    )
    env.gateway = gateway(env)
    return env


def gateway(env, **options):
    return SourceGateway(
        env.engine.source_ingress,
        env.installation.scope.source_instance_id,
        options.pop("generation", 1),
        {(env.installation.scope.source_kind, "controlled-v1"): SignedIngress(env.engine)},
        **options,
    )


def rows(env, table):
    assert table in {
        "inputs",
        "bodies",
        "entities",
        "deliveries",
        "cursors",
        "gaps",
        "aliases",
        "conflicts",
    }
    with env.engine.transaction() as conn:
        return conn.execute("SELECT * FROM app.source_ingress_" + table).fetchall()
