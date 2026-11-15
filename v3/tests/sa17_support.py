"""SA17 private-service setup; no shared patch, listener or channel send is applied."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sa08_support import policy
from sa16_support import admitted
from sa16_support import setup as autonomy_setup

from mgar_v3.sources.domain import ActorRef, ChannelCapabilities, SourceThreadRef
from mgar_v3.sources.ports import DeliveryReceipt
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.policy_domain import NotificationDestination
from mgar_v3.super_agent.source_autonomy_domain import AutonomyRuleSave
from mgar_v3.super_agent.source_notification_domain import (
    NotificationConfigure,
    NotificationPlanRequest,
    NotificationSettings,
)
from mgar_v3.super_agent.source_notification_ports import NotificationProfile


class ControlledChannel:
    def __init__(self, engine, *, outcome="accepted"):
        self.engine = engine
        self.outcome = outcome
        self.sends, self.queries = [], []

    def send(self, instance, message, body):
        from psycopg.pq import TransactionStatus

        assert self.engine.connection.info.transaction_status == TransactionStatus.IDLE
        self.sends.append((instance.scope.key, message, body))
        if self.outcome == "lost":
            raise OSError("controlled remote accepted, response lost")
        return DeliveryReceipt(
            scope=instance.scope,
            idempotency_key=message.idempotency_key,
            outcome=self.outcome,
            remote_message_id="notification-remote-1" if self.outcome == "accepted" else None,
        )

    def query(self, instance, lookup):
        from psycopg.pq import TransactionStatus

        assert self.engine.connection.info.transaction_status == TransactionStatus.IDLE
        self.queries.append(lookup)
        return DeliveryReceipt(
            scope=instance.scope,
            idempotency_key=lookup.idempotency_key,
            outcome="accepted",
            remote_message_id="notification-remote-1",
        )


def install_draft(engine):
    path = (
        Path(__file__).resolve().parents[1]
        / "docs/super-agent-v1/sa17-proposal/0049_source_notifications.sql"
    )
    raw = path.read_text()  # Filesystem IO outside the business transaction.
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT current_database() AS n")
            .fetchone()["n"]
            .startswith("mgar_v3_test_")
        )
        if not conn.execute("SELECT to_regclass('app.sa_notification_controls') AS t").fetchone()[
            "t"
        ]:
            conn.execute(raw)


def setup(
    engine,
    tmp_path,
    *,
    kind="critical_fault",
    notification_changes=None,
    query="supported",
    fail=True,
    aggregation=0,
):
    install_draft(engine)
    env = autonomy_setup(engine, tmp_path)
    caps = ChannelCapabilities(
        send="supported",
        idempotent_send="supported",
        query_delivery=query,
        delete_message="unsupported",
    )
    env.installation = env.installation.model_copy(
        update={"revision": 2, "channel_capabilities": caps}
    )
    SourceRegistry(engine).register_instance(env.installation)
    destination = NotificationDestination(
        thread=SourceThreadRef(
            scope=env.installation.scope, conversation_id="Chat-A", native_thread_id="same-topic"
        ),
        target="feishu:Chat-A:same-topic",
        visibility="private",
    )
    raw = {
        "enabled": True,
        "destinations": [destination.model_dump(mode="json")],
        "content_classes": [
            "verified_result",
            "necessary_question",
            "review_available",
            "critical_fault",
        ],
        "data_classes": ["public_summary"],
        "max_bytes": 512,
        "max_per_hour": 5,
        "quiet_hours": [],
    }
    if notification_changes:
        raw.update(notification_changes)
    selector = env.definition.selector.model_dump(mode="json")
    selector["source_revision"] = env.installation.revision
    policy(env, notification=raw, selector=selector)
    with engine.transaction() as conn:
        current = engine.source_policies._current(conn, env.policy_id)
    env.rule = env.rule.model_copy(
        update={
            "policy_revision": env.policy_revision,
            "policy_sha256": current["definition_sha256"],
        }
    )
    env.autonomy.save_rule(
        env.cookie,
        env.rule_id,
        AutonomyRuleSave(
            expected_revision=1,
            definition=env.rule,
            review_ref="SA17-current-rule-after-explicit-notification-grant",
        ),
    )
    work = admitted(env)
    if fail:
        engine.fail_run(work.run_id, "SA17_CONTROLLED_FAILURE")
    with engine.transaction() as conn:
        source = engine.source_correlations._input(conn, work.receipt.input_ids[0])
    service = engine.source_notifications
    channel = ControlledChannel(engine)
    profile = NotificationProfile(
        env.installation.scope,
        2,
        ActorRef(
            scope=env.installation.scope, id_type="open_id", actor_id="Bot-SA17", actor_kind="bot"
        ),
        caps,
        channel,
    )
    service.register_profile(profile)
    at = datetime.now(UTC)
    settings = NotificationSettings(
        enabled=True,
        policy_id=env.policy_id,
        policy_revision=env.policy_revision,
        policy_sha256=current["definition_sha256"],
        profile_sha256=profile.profile_sha256,
        destination=destination,
        content_classes=tuple(raw["content_classes"]),
        cooldown_seconds=1,
        aggregation_seconds=aggregation,
        ttl_seconds=120,
        valid_from=max(env.definition.valid_from, at - timedelta(seconds=1)),
        valid_until=min(env.definition.valid_until, at + timedelta(minutes=30)),
    )
    configured = service.configure(
        env.cookie,
        env.installation.scope.source_instance_id,
        NotificationConfigure(
            expected_revision=0,
            settings=settings,
            review_ref="SA17-explicit-controlled-notification-only-no-live-platform",
        ),
    )
    return SimpleNamespace(
        engine=engine,
        source=env,
        service=service,
        cookie=env.cookie,
        work=work,
        input=source.ref,
        profile=profile,
        channel=channel,
        settings=settings,
        config=configured,
        request=NotificationPlanRequest(
            run_id=work.run_id, source_input=source.ref, content_class=kind
        ),
    )


def plan(env):
    return env.service.plan(env.cookie, env.request)


def row(env, action_id):
    with env.engine.transaction() as conn:
        return env.engine._action(conn, action_id)
