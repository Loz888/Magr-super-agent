"""First SA17 GitHub controlled channel and no-private-log disclosure checks."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sa10_support import ingest_native, link
from sa10_support import setup as github_setup
from sa17_support import ControlledChannel

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import ActorRef
from mgar_v3.sources.github_broker import GitHubReadBroker
from mgar_v3.sources.github_domain import GitHubConfigure
from mgar_v3.sources.github_runtime import GitHubRuntime
from mgar_v3.sources.ingress_domain import IngressConfigure
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.policy_domain import NotificationDestination, PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_notification_domain import (
    NotificationConfigure,
    NotificationPlanRequest,
    NotificationSettings,
)
from mgar_v3.super_agent.source_notification_ports import NotificationProfile


def setup(engine, *, data_classes=("public_summary",)):
    env = github_setup(engine)
    gh = env.github
    with engine.transaction() as conn:
        original = engine.source_correlations._input(conn, env.native_input_id)
    thread = original.item.event.source_thread
    instance = SourceRegistry(engine).get_instance(gh.source_id)
    caps = instance.channel_capabilities.model_copy(
        update={
            "send": "supported",
            "query_delivery": "supported",
            "idempotent_send": "unsupported",
        }
    )
    instance = instance.model_copy(
        update={"revision": instance.revision + 1, "channel_capabilities": caps}
    )
    SourceRegistry(engine).register_instance(instance)
    gh.sources.configure(
        gh.cookie,
        gh.source_id,
        GitHubConfigure(
            expected_revision=1,
            settings=gh.settings,
            review_ref="SA17-controlled-current-channel-capabilities",
        ),
    )
    instance = SourceRegistry(engine).get_instance(gh.source_id)
    gh.instance = instance
    gh.activation = replace(gh.activation, config_revision=2)
    gh.broker = GitHubReadBroker(
        gh.sources, gh.source_id, 2, gh.secrets, gh.activation, transport=gh.broker._transport
    )
    gh.runtime = GitHubRuntime(gh.broker)
    raw = gh.policy.model_dump(mode="json")
    raw["selector"]["source_revision"] = instance.revision
    destination = NotificationDestination(
        thread=thread, target="github:example/mgar-test#7", visibility="public"
    )
    raw["notification"] = {
        "enabled": True,
        "destinations": [destination.model_dump(mode="json")],
        "content_classes": ["critical_fault"],
        "data_classes": list(data_classes),
        "max_bytes": 512,
        "max_per_hour": 10,
        "quiet_hours": [],
    }
    gh.policy = PolicyDefinition.model_validate(raw)
    engine.source_policies.save(
        gh.cookie,
        gh.policy_id,
        PolicySave(
            expected_revision=gh.policy_revision,
            definition=gh.policy,
            review_ref="SA17-explicit-original-GitHub-notification-only",
        ),
    )
    gh.policy_revision += 1
    gh.control = gh.control.model_copy(update={"policy_revision": gh.policy_revision})
    engine.source_ingress.configure(
        gh.cookie,
        gh.source_id,
        IngressConfigure(
            expected_generation=gh.generation,
            settings=gh.control,
            review_ref="SA17-current-controlled-GitHub-source",
        ),
    )
    gh.generation += 1
    receipt, resource = ingest_native(env, family="issue_comment", number=7)
    env.native_input_id, env.resource_key = receipt.input_ids[0], resource.resource_key
    link(env)
    engine.fail_run(env.target.run_id, "SA17_CONTROLLED_GITHUB_FAULT")
    with engine.transaction() as conn:
        source = engine.source_correlations._input(conn, env.native_input_id)
        current = engine.source_policies._current(conn, gh.policy_id)
    service = engine.source_notifications
    channel = ControlledChannel(engine)
    profile = NotificationProfile(
        instance.scope,
        instance.revision,
        ActorRef(scope=instance.scope, id_type="github_user_id", actor_id="99", actor_kind="bot"),
        caps,
        channel,
    )
    service.register_profile(profile)
    now = datetime.now(UTC)
    settings = NotificationSettings(
        enabled=True,
        policy_id=gh.policy_id,
        policy_revision=gh.policy_revision,
        policy_sha256=current["definition_sha256"],
        profile_sha256=profile.profile_sha256,
        destination=destination,
        content_classes=("critical_fault",),
        aggregation_seconds=0,
        cooldown_seconds=1,
        valid_from=now - timedelta(seconds=1),
        valid_until=min(gh.policy.valid_until, now + timedelta(minutes=30)),
    )
    return SimpleNamespace(
        env=env,
        gh=gh,
        service=service,
        channel=channel,
        profile=profile,
        settings=settings,
        request=NotificationPlanRequest(
            run_id=env.target.run_id, source_input=source.ref, content_class="critical_fault"
        ),
    )


def test_original_github_controlled_channel_never_copies_private_source_to_public_issue(engine):
    e = setup(engine)
    e.service.configure(
        e.gh.cookie,
        e.gh.source_id,
        NotificationConfigure(
            expected_revision=0,
            settings=e.settings,
            review_ref="SA17-controlled-public-summary-template",
        ),
    )
    p = e.service.plan(e.gh.cookie, e.request)
    original = engine.get_run(e.env.target.run_id)
    assert e.service.tick(e.gh.cookie)["dispatched"] == [p["action_id"]]
    assert (
        len(e.channel.sends) == 1 and "SA10_SYNTHETIC_PRIVATE_NATIVE" not in e.channel.sends[0][2]
    )
    assert "synthetic content" not in e.channel.sends[0][2] and "http" not in e.channel.sends[0][2]
    assert e.channel.sends[0][1].destination.scope.source_kind == "github"
    assert engine.get_run(e.env.target.run_id) == original
    assert not e.profile.capabilities.idempotent_send == "supported"


@pytest.mark.parametrize("data_classes", [("private_log",), ("private_summary",)])
def test_private_data_grant_does_not_become_public_summary_permission(data_classes, engine):
    e = setup(engine, data_classes=data_classes)
    with pytest.raises(DomainError):
        e.service.configure(
            e.gh.cookie,
            e.gh.source_id,
            NotificationConfigure(
                expected_revision=0,
                settings=e.settings,
                review_ref="SA17-no-implicit-disclosure-upgrade",
            ),
        )
    assert not e.channel.sends
