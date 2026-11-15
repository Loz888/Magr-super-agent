"""SA17 first preparation targets. Formal integration is a separate future check."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import ActorRef, ChannelCapabilities, SourceScope, SourceThreadRef
from mgar_v3.super_agent.policy_domain import NotificationDestination, NotificationGrant, QuietHours
from mgar_v3.super_agent.source_notification_domain import (
    NotificationConfigure,
    NotificationFacts,
    NotificationPlanRequest,
    NotificationSettings,
)
from mgar_v3.super_agent.source_notification_ports import NotificationProfile
from mgar_v3.super_agent.source_notification_rules import delivery_key, quiet_until, render

A = Path(__file__).resolve().parents[1]
P = A / "docs/super-agent-v1/sa17-proposal"
AT = datetime(2026, 11, 15, 4, 0, tzinfo=UTC)


def facts(kind="verified_result", **kw):
    return NotificationFacts(
        run_id=uuid4(),
        ticket_id=uuid4(),
        execution_epoch=1,
        input_revision=1,
        thread_revision=1,
        state="waiting_approval",
        content_class=kind,
        cause_sha256="a" * 64,
        **kw,
    )


def scope(kind="feishu"):
    return SourceScope(
        source_kind=kind, source_instance_id=uuid4(), tenant_id="Tenant", installation_id="Install"
    )


def settings():
    return NotificationSettings(
        policy_id=uuid4(),
        policy_revision=1,
        policy_sha256="b" * 64,
        profile_sha256="c" * 64,
        destination=NotificationDestination(
            thread=SourceThreadRef(scope=scope(), conversation_id="Chat", native_thread_id="Topic"),
            target="feishu:Chat:Topic",
            visibility="private",
        ),
        content_classes=("verified_result",),
        valid_from=AT,
        valid_until=AT + timedelta(days=1),
    )


@pytest.mark.parametrize(
    "kind,flag",
    [
        ("verified_result", "verified"),
        ("necessary_question", "needs_input"),
        ("review_available", "reviewable"),
        ("critical_fault", "critical"),
    ],
)
def test_minimal_templates_require_actual_fact_and_bind_hash(kind, flag):
    with pytest.raises(DomainError, match="Current verified cause"):
        render(facts(kind))
    f = facts(kind, **{flag: True})
    t = render(f)
    assert t.body_sha256 == hashlib.sha256(t.body.encode()).hexdigest()
    assert str(f.ticket_id) in t.body and len(t.body.encode()) < 512
    assert "http" not in t.body and "SECRET" not in t.body
    assert t.data_class == "public_summary"
    assert render(f) == t


@pytest.mark.parametrize(
    "update",
    [
        {"thread_revision": 2},
        {"state": "cancelled"},
    ],
)
def test_stale_or_cancelled_cause_is_blocked(update):
    with pytest.raises(DomainError):
        render(facts(verified=True).model_copy(update=update))


@pytest.mark.parametrize("extra", ["body", "logs", "url", "provider", "actor", "approved"])
def test_http_cannot_supply_notification_facts_or_plaintext(extra):
    with pytest.raises(ValidationError):
        NotificationPlanRequest.model_validate_json(
            json.dumps(
                {
                    "run_id": str(uuid4()),
                    "source_input": {},
                    "content_class": "verified_result",
                    extra: "untrusted",
                }
            )
        )


def test_default_is_disabled_and_bounded():
    s = settings()
    assert not s.enabled
    for updates in (
        {"ttl_seconds": 0},
        {"aggregation_seconds": 300, "ttl_seconds": 30},
        {"cooldown_seconds": 0},
        {"valid_until": AT + timedelta(days=8)},
        {"content_classes": ("verified_result", "verified_result")},
    ):
        with pytest.raises(ValidationError):
            NotificationSettings.model_validate({**s.model_dump(), **updates})


def test_review_and_compare_and_swap_are_required():
    with pytest.raises(ValidationError):
        NotificationConfigure(settings=settings(), expected_revision=-1, review_ref="")


def test_delivery_identity_is_stable_and_scope_sensitive():
    dest = settings().destination.model_dump(mode="json")
    k = delivery_key("project", "run", "a" * 64, dest, "verified_result")
    assert k == delivery_key("project", "run", "a" * 64, dest, "verified_result")
    assert (
        len(
            {
                k,
                delivery_key("project2", "run", "a" * 64, dest, "verified_result"),
                delivery_key("project", "run2", "a" * 64, dest, "verified_result"),
                delivery_key("project", "run", "b" * 64, dest, "verified_result"),
                delivery_key("project", "run", "a" * 64, dest, "necessary_question"),
            }
        )
        == 5
    )


def grant(quiet):
    return NotificationGrant(
        enabled=True,
        destinations=(settings().destination,),
        content_classes=("verified_result",),
        data_classes=("public_summary",),
        max_bytes=512,
        max_per_hour=5,
        quiet_hours=tuple(quiet),
    )


def test_quiet_boundary_is_current_and_utc_aware():
    q = QuietHours(timezone="Asia/Shanghai", weekdays=(6,), start="12:00", end="13:00")
    g = grant([q])
    assert quiet_until(g, AT - timedelta(seconds=1)) is None
    assert quiet_until(g, AT) == AT + timedelta(hours=1)
    assert quiet_until(g, AT + timedelta(hours=1)) is None


def test_dst_repeat_does_not_use_ambiguous_wall_clock_addition():
    q = QuietHours(timezone="America/New_York", weekdays=(6,), start="01:00", end="02:00")
    g = grant([q])
    for hour in (5, 6):
        at = datetime(2026, 11, 1, hour, 30, tzinfo=UTC)
        assert quiet_until(g, at) == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)


@pytest.mark.parametrize("kind", ["github", "feishu"])
def test_both_channel_profiles_use_explicit_capabilities(kind):
    s = scope(kind)
    caps = ChannelCapabilities(
        send="supported",
        idempotent_send="unsupported",
        query_delivery="unknown",
        delete_message="unsupported",
    )
    p = NotificationProfile(
        s, 1, ActorRef(scope=s, id_type="id", actor_id="Bot", actor_kind="bot"), caps, object()
    )
    p.validate()
    assert len(p.profile_sha256) == 64


@pytest.mark.parametrize("change", ["live", "human", "scope", "send", "revision"])
def test_unreviewed_channel_profile_is_rejected(change):
    from dataclasses import replace

    s = scope()
    caps = ChannelCapabilities(
        send="supported",
        idempotent_send="supported",
        query_delivery="supported",
        delete_message="unsupported",
    )
    p = NotificationProfile(
        s, 1, ActorRef(scope=s, id_type="id", actor_id="Bot", actor_kind="bot"), caps, object()
    )
    if change == "live":
        p = replace(p, mode="live")
    elif change == "human":
        p = replace(p, sender=p.sender.model_copy(update={"actor_kind": "human"}))
    elif change == "scope":
        p = replace(p, sender=p.sender.model_copy(update={"scope": scope()}))
    elif change == "send":
        p = replace(p, capabilities=caps.model_copy(update={"send": "unknown"}))
    else:
        p = replace(p, instance_revision=0)
    with pytest.raises(ValueError):
        p.validate()


@pytest.fixture
def draft(database):
    sql = (P / "0049_source_notifications.sql").read_text()
    with psycopg.connect(database) as conn:
        with conn.transaction(force_rollback=True):
            conn.execute(sql)
            yield conn


def test_draft_installs_five_tables_in_real_pg(draft):
    rows = draft.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='app' "
        "AND tablename LIKE 'sa_notification_%'"
    ).fetchall()
    assert {r[0] for r in rows} == {
        "sa_notification_controls",
        "sa_notification_versions",
        "sa_notification_intents",
        "sa_notification_receipts",
        "sa_notification_events",
    }


def test_draft_preserves_existing_action_types(draft):
    text = draft.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname='actions_action_type_check'"
    ).fetchone()[0]
    assert all(t in text for t in ("issue_comment", "draft_pr", "source_notification"))


def test_draft_revocations_and_append_only_guards_are_installed(draft):
    names = {
        r[0]
        for r in draft.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname LIKE 'sa_notification_%'"
        ).fetchall()
    }
    assert {
        "sa_notification_source_changed",
        "sa_notification_policy_changed",
        "sa_notification_control_changed",
        "sa_notification_input_changed",
        "sa_notification_identity_changed",
        "sa_notification_roles_changed",
        "sa_notification_intents_immutable",
        "sa_notification_receipts_immutable",
        "sa_notification_versions_immutable",
        "sa_notification_events_immutable",
    } <= names


def test_draft_does_not_change_original_migration_registration(draft):
    assert draft.execute("SELECT count(*) FROM app.schema_migrations").fetchone()[0] == 45
    assert not (A / "migrations/0049_source_notifications.sql").exists()


def test_proposal_binds_exact_shared_bytes_without_applying():
    m = json.loads((P / "proposal-manifest.json").read_text())
    assert (
        hashlib.sha256((P / m["migration"]["draft"]).read_bytes()).hexdigest()
        == m["migration"]["sha256"]
    )
    assert hashlib.sha256((P / m["patch"]["path"]).read_bytes()).hexdigest() == m["patch"]["sha256"]
    for f in m["patch"]["files"]:
        assert hashlib.sha256((A.parent / f["path"]).read_bytes()).hexdigest() == f["before_sha256"]
    assert len(m["interfaces"]) == 11
