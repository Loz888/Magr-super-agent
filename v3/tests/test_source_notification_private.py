"""First private-service real-PG checks. Original Dispatcher integration is NOT RUN here."""

from datetime import timedelta

import psycopg
import pytest
from sa08_support import policy
from sa17_support import plan, row, setup

from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import DefinitiveFailure
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.super_agent.source_notification_domain import NotificationConfigure
from mgar_v3.super_agent.source_notification_transport import NotificationTransport
from mgar_v3.super_agent.source_notifications import SourceNotifications


def test_private_plan_uses_actual_action_approval_outbox_and_no_task_effect(engine, tmp_path):
    e = setup(engine, tmp_path)
    before = engine.get_run(e.work.run_id)
    p = plan(e)
    assert p["created"] and not p["sent"]
    action, _ = row(e, p["action_id"])
    assert action["action_type"] == "source_notification" and action["status"] == "ready"
    assert "synthetic" not in action["payload"]["body"]
    assert engine.get_run(e.work.run_id) == before
    with engine.transaction() as conn:
        approval = conn.execute(
            "SELECT * FROM app.approvals WHERE action_id=%s", (p["action_id"],)
        ).fetchone()
        assert approval["payload_hash"] == p["payload_sha256"]
        assert approval["actor"].startswith("notification-policy:")
        assert (
            conn.execute(
                "SELECT status FROM app.outbox WHERE action_id=%s", (p["action_id"],)
            ).fetchone()["status"]
            == "ready"
        )
        e.service.validate_action(conn, *engine._action(conn, p["action_id"]))
    assert not e.channel.sends


def test_reconnect_reconstructed_service_does_not_create_second_outbox(engine, tmp_path):
    e = setup(engine, tmp_path)
    first = plan(e)
    reconnected = SourceNotifications(engine)
    reconnected.register_profile(e.profile)
    again = reconnected.plan(e.cookie, e.request)
    assert not again["created"] and again["action_id"] == first["action_id"]
    with engine.transaction() as conn:
        for table in ("actions", "approvals", "outbox", "sa_notification_intents"):
            assert conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"] == 1


@pytest.mark.parametrize("kind", ["verified_result", "necessary_question", "review_available"])
def test_missing_actual_fact_never_creates_notification(kind, engine, tmp_path):
    e = setup(engine, tmp_path, kind=kind)
    with pytest.raises(DomainError, match="Current verified cause"):
        plan(e)
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.outbox").fetchone()["n"] == 0


@pytest.mark.parametrize("cookie", ["", "forged"])
def test_owner_session_is_required_for_private_mutation(cookie, engine, tmp_path):
    e = setup(engine, tmp_path)
    with pytest.raises(DomainError) as exc:
        e.service.plan(cookie, e.request)
    assert exc.value.code == "AUTH_REQUIRED"


def test_unrelated_run_cannot_borrow_source_thread_or_grant(engine, tmp_path, trigger):
    e = setup(engine, tmp_path)
    unrelated = engine.ingest(trigger)
    req = e.request.model_copy(update={"run_id": unrelated.run_id})
    with pytest.raises(DomainError) as exc:
        e.service.plan(e.cookie, req)
    assert exc.value.code == "NOTIFICATION_ORIGINAL_TASK_LINK_REQUIRED"


def test_wrong_input_authority_or_project_is_rejected(engine, tmp_path):
    e = setup(engine, tmp_path)
    for updates in ({"state_sha256": "f" * 64}, {"project_id": "other"}):
        with pytest.raises(DomainError):
            e.service.plan(
                e.cookie,
                e.request.model_copy(update={"source_input": e.input.model_copy(update=updates)}),
            )


def test_old_settings_compare_and_swap_fails_without_effect(engine, tmp_path):
    e = setup(engine, tmp_path)
    with pytest.raises(DomainError) as exc:
        e.service.configure(
            e.cookie,
            e.input.source_instance_id,
            NotificationConfigure(expected_revision=0, settings=e.settings, review_ref="stale"),
        )
    assert exc.value.code == "NOTIFICATION_REVISION_CONFLICT"
    assert e.service.read(e.cookie, e.input.source_instance_id)["revision"] == 1


@pytest.mark.parametrize("cause", ["configuration", "policy", "role", "input"])
def test_committed_revocation_cancels_unsent_in_same_original_transaction(cause, engine, tmp_path):
    e = setup(engine, tmp_path)
    p = plan(e)
    if cause == "configuration":
        e.service.configure(
            e.cookie,
            e.input.source_instance_id,
            NotificationConfigure(
                expected_revision=1,
                settings=e.settings.model_copy(update={"enabled": False}),
                review_ref="disable",
            ),
        )
    elif cause == "policy":
        policy(e.source, mode="disabled")
    elif cause == "role":
        r = e.source.binding
        e.source.identities.roles(
            e.cookie,
            r["binding_id"],
            RoleChange(
                expected_revision=r["revision"], roles=("requester",), review_ref="withdraw context"
            ),
        )
    else:
        with engine.transaction() as conn:
            conn.execute(
                "SELECT app.expire_source_input(%s,'SA17_TEST_DELETE')", (e.input.input_id,)
            )
    action, _ = row(e, p["action_id"])
    assert action["status"] == "cancelled"
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT status FROM app.outbox WHERE action_id=%s", (p["action_id"],)
            ).fetchone()["status"]
            == "cancelled"
        )
    assert not e.channel.sends


def test_rolled_back_revocation_does_not_cancel_committed_plan(engine, tmp_path):
    e = setup(engine, tmp_path)
    p = plan(e)
    with engine.transaction() as conn:
        with conn.transaction(force_rollback=True):
            conn.execute(
                "SELECT app.expire_source_input(%s,'SA17_TEST_ROLLBACK')", (e.input.input_id,)
            )
    assert row(e, p["action_id"])[0]["status"] == "ready"


def test_quiet_defers_pending_and_does_not_invent_receipt(engine, tmp_path):
    from datetime import UTC, datetime

    at = datetime.now(UTC)
    start = at.strftime("%H:%M")
    end = (at + timedelta(minutes=5)).strftime("%H:%M")
    e = setup(
        engine,
        tmp_path,
        notification_changes={
            "quiet_hours": [
                {"timezone": "UTC", "start": start, "end": end, "weekdays": [at.weekday()]}
            ]
        },
    )
    p = plan(e)
    result = e.service.maintain(e.cookie)
    assert result["deferred"] == 1 and result["cancelled"] == 0
    assert row(e, p["action_id"])[0]["status"] == "ready"
    assert e.service.get(e.cookie, p["action_id"])["receipts"] == []
    assert not e.channel.sends


def test_unclaimed_action_cannot_reach_channel_even_with_exact_hash(engine, tmp_path):
    e = setup(engine, tmp_path)
    p = plan(e)
    action, _ = row(e, p["action_id"])
    with pytest.raises(DefinitiveFailure):
        NotificationTransport(e.service).send(action)
    assert not e.channel.sends


def test_sql_immutable_intent_and_payload_cannot_be_rewritten(engine, tmp_path):
    e = setup(engine, tmp_path)
    p = plan(e)
    for sql in (
        "UPDATE app.sa_notification_intents SET content_class='verified_result' WHERE action_id=%s",
        "UPDATE app.actions SET payload='{}'::jsonb WHERE action_id=%s",
        "DELETE FROM app.sa_notification_intents WHERE action_id=%s",
    ):
        with pytest.raises(psycopg.Error), engine.transaction() as conn:
            conn.execute(sql, (p["action_id"],))
    assert row(e, p["action_id"])[0]["status"] == "ready"


def test_private_receipt_callback_preserves_business_task_and_old_types(engine, tmp_path):
    e = setup(engine, tmp_path)
    p = plan(e)
    action, _ = row(e, p["action_id"])
    before = engine.get_run(e.work.run_id)
    with engine.transaction() as conn:
        assert e.service.record_effect(conn, action, "succeeded", "controlled-receipt")
        assert not e.service.record_effect(
            conn, {"action_type": "issue_comment"}, "succeeded", "legacy"
        )
    assert engine.get_run(e.work.run_id) == before
    assert e.service.get(e.cookie, p["action_id"])["receipts"][0]["outcome"] == "accepted"


def test_current_facts_change_invalidates_old_template_even_when_body_stays_generic(
    engine, tmp_path
):
    e = setup(engine, tmp_path)
    p = plan(e)
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.runs SET reason_code='SA17_DIFFERENT_FAILURE' WHERE run_id=%s",
            (e.work.run_id,),
        )
    result = e.service.maintain(e.cookie)
    assert result["cancelled"] == 1 and row(e, p["action_id"])[0]["status"] == "cancelled"


def test_expired_pending_plan_is_cancelled_without_channel_io(engine, tmp_path):
    e = setup(engine, tmp_path)
    p = plan(e)
    original = e.service.now
    with engine.transaction() as conn:
        at = original(conn)
    e.service.now = lambda conn: at + timedelta(seconds=121)
    result = e.service.maintain(e.cookie)
    assert result["cancelled"] == 1 and row(e, p["action_id"])[0]["status"] == "cancelled"
    assert not e.channel.sends


def test_cancellation_is_exact_hash_and_cannot_claim_unknown_was_unsent(engine, tmp_path):
    e = setup(engine, tmp_path)
    p = plan(e)
    with pytest.raises(DomainError):
        e.service.cancel(e.cookie, p["action_id"], "f" * 64)
    assert e.service.cancel(e.cookie, p["action_id"], p["payload_sha256"])["status"] == "cancelled"


def test_default_constructor_and_idle_maintenance_have_zero_side_effects(engine):
    from sa17_support import install_draft

    install_draft(engine)
    service = SourceNotifications(engine)
    assert not service._profiles
    with engine.transaction() as conn:
        assert service._maintain(conn, 16) == {
            "checked": 0,
            "cancelled": 0,
            "deferred": 0,
            "model_calls": 0,
        }
        assert conn.execute("SELECT count(*) AS n FROM app.outbox").fetchone()["n"] == 0


def test_private_module_rejects_foreign_connection_even_inside_transaction(engine, database):
    from sa17_support import install_draft

    install_draft(engine)
    with psycopg.connect(database) as other:
        with other.transaction(), pytest.raises(DomainError):
            SourceNotifications(engine)._conn(other)
