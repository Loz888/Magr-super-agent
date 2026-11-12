"""Signed synthetic SA08 inputs and independent original-ledger safety oracles."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sa04_support import SignedIngress, signed
from sa04_support import envelope as source_envelope
from sa04_support import setup as source_setup
from sa07_support import ORGANIZATION, configure, definition
from test_super_agent_sa02 import bind

from mgar_v3.sources.domain import ActorRef
from mgar_v3.sources.gateway import SourceGateway
from mgar_v3.sources.ingress_domain import IngressConfigure, ItemMetadata, VerifiedDelivery
from mgar_v3.super_agent.fast_path_domain import (
    FastPathConfigure,
    FastPathSettings,
    ObserveRequest,
)
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave


class SignedFastPathIngress(SignedIngress):
    """The address fact comes from the authenticated capsule, never a public override."""

    def verify_ingress(self, instance, envelope):
        delivery = super().verify_ingress(instance, envelope)
        raw = json.loads(envelope.payload)
        items = tuple(
            ItemMetadata.model_validate({
                **item.model_dump(mode="json"),
                "event": {
                    **item.event.model_dump(mode="json"),
                    "addressed": source.get("addressed", "mention"),
                },
            })
            for item, source in zip(delivery.metadata.items, raw["items"], strict=True)
        )
        return VerifiedDelivery(
            delivery.metadata.model_copy(update={"items": items}), delivery.bodies,
        )


def gateway(env):
    return SourceGateway(
        env.engine.source_ingress,
        env.installation.scope.source_instance_id,
        env.generation,
        {(env.installation.scope.source_kind, "controlled-v1"): SignedFastPathIngress(env.engine)},
    )


def setup(engine, *, mode="shadow", fast_settings=None, enabled=True, quota=100, receive=None):
    env = source_setup(
        engine,
        mode=mode,
        receive=receive or {
            "enabled": True,
            "retain_body": True,
            "model_input": True,
            "body_ttl_seconds": 300,
            "allow_history_context": True,
        },
    )
    env.generation = 1
    env.policy_revision = 1
    env.fast_revision = 0
    env.gateway = gateway(env)
    env.budget = SimpleNamespace(
        engine=engine, cookie=env.cookie, service=engine.project_budgets,
    )
    configure(env.budget, project=definition(
        organization_id=ORGANIZATION,
        quotas={"candidates_hour": quota, "source_minute": 10000},
    ))
    env.fast_settings = FastPathSettings(enabled=enabled, **(fast_settings or {}))
    configure_fast_path(env, env.fast_settings)
    return env


def configure_fast_path(env, settings):
    result = env.engine.fast_path.configure(
        env.cookie,
        env.installation.scope.source_instance_id,
        FastPathConfigure(
            expected_revision=env.fast_revision,
            settings=settings,
            review_ref="SA08-controlled-owner-configuration",
        ),
    )
    env.fast_revision = result["revision"]
    env.fast_settings = settings
    return result


def policy(env, **changes):
    raw = env.definition.model_dump(mode="json")
    raw.update(changes)
    value = PolicyDefinition.model_validate(raw)
    env.engine.source_policies.save(
        env.cookie,
        env.policy_id,
        PolicySave(
            expected_revision=env.policy_revision,
            definition=value,
            review_ref="SA08-controlled-current-policy",
        ),
    )
    env.policy_revision += 1
    env.definition = value
    env.settings = env.settings.model_copy(update={"policy_revision": env.policy_revision})
    env.engine.source_ingress.configure(
        env.cookie,
        env.installation.scope.source_instance_id,
        IngressConfigure(
            expected_generation=env.generation,
            settings=env.settings,
            review_ref="SA08-controlled-policy-rebind",
        ),
    )
    env.generation += 1
    env.gateway = gateway(env)
    return value


def add_actor(env, *, actor="CI-bot", kind="bot", conversation="Chat-A", roles=None):
    binding, _ = bind(
        env.identities,
        env.cookie,
        env.installation,
        actor=actor,
        kind=kind,
        conversation=conversation,
        roles=roles or (("context_provider",) if kind != "human" else (
            "context_provider", "requester",
        )),
    )
    selector = env.definition.selector.model_dump(mode="json")
    if binding["actor_ref"] not in selector["actors"]:
        selector["actors"].append(binding["actor_ref"])
    if conversation not in selector["conversations"]:
        selector["conversations"].append(conversation)
    policy(env, selector=selector)
    return binding


def envelope(env, delivery="fp-1", entity="m1", body="Please investigate this issue", **changes):
    raw = json.loads(source_envelope(
        env, delivery=delivery, entity=entity, body=body, **changes,
    ).payload)
    return signed(raw)


def ingest(env, **changes):
    receipt = env.gateway.receive(envelope(env, **changes))
    assert receipt.committed
    return receipt


def observe(env, input_ids):
    return env.engine.fast_path.observe(
        env.cookie,
        env.installation.scope.source_instance_id,
        ObserveRequest(input_ids=tuple(input_ids)),
    )


def decisions(env):
    return env.engine.fast_path.decisions(env.cookie, env.installation.scope.source_instance_id)


def preview(env):
    return env.engine.fast_path.preview(env.cookie, env.installation.scope.source_instance_id)


def original_state(engine):
    """Every original business/effect/financial row must stay unchanged during observation."""
    tables = (
        "trigger_events", "threads", "tickets", "runs", "run_events", "actions", "outbox",
        "target_write_grants", "approvals", "usage_calls", "router_calls", "read_jobs",
        "artifacts", "evidence", "verdicts", "run_inputs", "router_inbox",
    )
    with engine.transaction() as conn:
        return {
            table: conn.execute(
                "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                "'[]'::jsonb) AS value FROM app." + table + " t"
            ).fetchone()["value"]
            for table in tables
        }


def input_row(env, input_id):
    with env.engine.transaction() as conn:
        return conn.execute(
            "SELECT * FROM app.source_ingress_inputs WHERE input_id=%s", (input_id,),
        ).fetchone()


def text_item(env=None, *, addressed="mention", actor_kind="human", topic="topic-1", **changes):
    """Metadata-only rule fixture; never submitted as a platform or authority proof."""
    from uuid import uuid4

    from mgar_v3.sources.domain import SourceEvent, SourceScope, SourceThreadRef

    scope = env.installation.scope if env is not None else SourceScope(
        source_kind="feishu", source_instance_id=uuid4(),
        installation_id="Synthetic-installation", tenant_id="Synthetic-tenant",
    )
    at = datetime.now(UTC)
    event = SourceEvent(
        scope=scope,
        project_id="orders-demo",
        source_revision=1,
        delivery_id="synthetic-rule-delivery",
        remote_event_id="synthetic-rule-event",
        entity_id="synthetic-rule-entity",
        entity_revision={"origin": "native", "value": "1"},
        event_kind="message.created",
        source_thread=SourceThreadRef(
            scope=scope, conversation_id="Chat-A", native_thread_id=topic,
        ),
        sender=ActorRef(scope=scope, id_type="open_id", actor_id="Alice", actor_kind=actor_kind),
        addressed=addressed,
        occurred_at=at - timedelta(seconds=1),
        received_at=at,
        verification_ref="synthetic-rules-only-not-authority",
        provenance="verified_source",
        **changes,
    )
    return ItemMetadata(
        event=event, entity_namespace="messages", operation="create", native_order=1,
    )
