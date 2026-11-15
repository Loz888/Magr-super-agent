"""First SA17 review notification from trusted actual fixture Coding verification."""

from datetime import UTC, datetime, timedelta

from sa17_support import ControlledChannel

from mgar_v3.sources.domain import ActorRef, SourceThreadRef
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.policy_domain import NotificationDestination, PolicyDefinition
from mgar_v3.super_agent.source_notification_domain import (
    NotificationConfigure,
    NotificationPlanRequest,
    NotificationSettings,
)
from mgar_v3.super_agent.source_notification_ports import NotificationProfile


def test_actual_coding_verification_notifies_review_without_completing_or_publishing(
    engine, tmp_path, monkeypatch
):
    import sa13_runtime_support
    from sa13_runtime_support import runtime_env, run

    original_new_source = sa13_runtime_support.new_source

    def notification_source(engine):
        source = original_new_source(engine)
        instance = source.installation
        caps = instance.channel_capabilities.model_copy(
            update={
                "send": "supported",
                "query_delivery": "supported",
                "idempotent_send": "supported",
            }
        )
        source.installation = instance.model_copy(
            update={"revision": instance.revision + 1, "channel_capabilities": caps}
        )
        SourceRegistry(engine).register_instance(source.installation)
        destination = NotificationDestination(
            thread=SourceThreadRef(scope=instance.scope, conversation_id="Chat-A"),
            target="feishu:Chat-A",
            visibility="private",
        )
        raw = source.definition.model_dump(mode="json")
        raw["selector"]["source_revision"] = source.installation.revision
        raw["notification"] = {
            "enabled": True,
            "destinations": [destination.model_dump(mode="json")],
            "content_classes": ["review_available"],
            "data_classes": ["public_summary"],
            "max_bytes": 512,
            "max_per_hour": 5,
            "quiet_hours": [],
        }
        source.definition = PolicyDefinition.model_validate(raw)
        return source

    monkeypatch.setattr(sa13_runtime_support, "new_source", notification_source)
    with runtime_env(engine, tmp_path) as env:
        result = run(env)
        assert result["verification"] == "verified"
        before = engine.get_run(env.target.run_id)
        assert before["status"] == "waiting_approval" and before["outcome"] != "published"
        source = env.source
        instance = source.installation
        service = engine.source_notifications
        channel = ControlledChannel(engine)
        profile = NotificationProfile(
            instance.scope,
            instance.revision,
            ActorRef(
                scope=instance.scope,
                id_type="open_id",
                actor_id="SA17-Review-Bot",
                actor_kind="bot",
            ),
            instance.channel_capabilities,
            channel,
        )
        service.register_profile(profile)
        with engine.transaction() as conn:
            current = engine.source_policies._current(conn, source.policy_id)
            ref = engine.source_correlations._input(conn, env.input_id).ref
        now = datetime.now(UTC)
        settings = NotificationSettings(
            enabled=True,
            policy_id=source.policy_id,
            policy_revision=current["current_revision"],
            policy_sha256=current["definition_sha256"],
            profile_sha256=profile.profile_sha256,
            destination=source.definition.notification.destinations[0],
            content_classes=("review_available",),
            cooldown_seconds=1,
            aggregation_seconds=0,
            valid_from=now - timedelta(seconds=1),
            valid_until=min(source.definition.valid_until, now + timedelta(minutes=30)),
        )
        service.configure(
            env.cookie,
            instance.scope.source_instance_id,
            NotificationConfigure(
                expected_revision=0,
                settings=settings,
                review_ref="SA17-trusted-fixture-verification-review-notification",
            ),
        )
        p = service.plan(
            env.cookie,
            NotificationPlanRequest(
                run_id=env.target.run_id, source_input=ref, content_class="review_available"
            ),
        )
        assert service.tick(env.cookie)["dispatched"] == [p["action_id"]]
        assert len(channel.sends) == 1 and channel.sends[0][2].startswith("已有核验产物可供审查")
        assert engine.get_run(env.target.run_id) == before
        assert "source-notification" not in str(result)
