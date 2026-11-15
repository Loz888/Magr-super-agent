"""First new SA17 timing, aggregation, follow-up and late-observation boundaries."""

from datetime import timedelta

from sa08_support import ingest, policy
from sa12_support import claim, ready, session
from sa17_support import plan, row, setup

from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.source_notification_domain import NotificationConfigure
from mgar_v3.super_agent.source_scheduler_domain import SchedulerAssignmentRequest


def new_cause(e):
    with e.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.runs SET reason_code='SA17_NEW_CONTROLLED_FAULT' WHERE run_id=%s",
            (e.work.run_id,),
        )


def test_real_waiting_input_question_notifies_without_advancing_wait_state(engine, tmp_path):
    e = setup(engine, tmp_path, kind="necessary_question", fail=False)
    scheduler = e.source.scheduler
    worker = session(scheduler)
    active = ready(scheduler, worker, claim(scheduler, worker))
    request = SchedulerAssignmentRequest(
        assignment_id=active["assignment_id"],
        session_id=worker["session_id"],
        epoch=active["execution_epoch"],
        expected_revision=active["revision"],
    )
    with engine.source_scheduler.worker_context(worker["bearer"], request):
        run = engine.begin_run(e.work.run_id)
        engine.fail_run(e.work.run_id, "CONTEXT_LIMIT", run["execution_epoch"])

    before = engine.get_run(e.work.run_id)
    assert before["status"] == "waiting_input"
    p = plan(e)
    assert e.service.tick(e.cookie)["dispatched"] == [p["action_id"]]
    assert engine.get_run(e.work.run_id) == before
    assert len(e.channel.sends) == 1 and "补充信息" in e.channel.sends[0][2]


def test_latest_cause_aggregates_to_one_unsent_outbox_without_rewriting_old_intent(
    engine, tmp_path
):
    e = setup(engine, tmp_path, aggregation=10)
    first = plan(e)
    new_cause(e)
    newest = plan(e)
    assert newest["created"] and newest["superseded"] == 1
    assert row(e, first["action_id"])[0]["status"] == "cancelled"
    assert row(e, newest["action_id"])[0]["status"] == "ready"
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.outbox WHERE status='ready'").fetchone()[
                "n"
            ]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM app.sa_notification_intents").fetchone()["n"]
            == 2
        )
    assert not e.service.tick(e.cookie)["dispatched"] and not e.channel.sends


def test_current_cooldown_defers_new_cause_without_remote_send(engine, tmp_path):
    e = setup(engine, tmp_path)
    e.service.configure(
        e.cookie,
        e.input.source_instance_id,
        NotificationConfigure(
            expected_revision=1,
            settings=e.settings.model_copy(update={"cooldown_seconds": 60}),
            review_ref="SA17-longer-cooldown",
        ),
    )
    first = plan(e)
    e.service.tick(e.cookie)
    new_cause(e)
    second = plan(e)
    assert second["created"] and second["action_id"] != first["action_id"]
    assert not e.service.tick(e.cookie)["dispatched"] and len(e.channel.sends) == 1
    assert row(e, second["action_id"])[0]["status"] == "ready"
    with engine.transaction() as conn:
        outbox = conn.execute(
            "SELECT next_retry_at,created_at FROM app.outbox WHERE action_id=%s",
            (second["action_id"],),
        ).fetchone()
        assert outbox["next_retry_at"] > outbox["created_at"] + timedelta(seconds=50)


def test_current_hourly_grant_counts_unknown_attempts_and_prevents_storm(engine, tmp_path):
    e = setup(engine, tmp_path, notification_changes={"max_per_hour": 1})
    e.channel.outcome = "lost"
    plan(e)
    e.service.tick(e.cookie)
    new_cause(e)
    second = plan(e)
    assert not e.service.tick(e.cookie)["dispatched"] and len(e.channel.sends) == 1
    with engine.transaction() as conn:
        outbox = conn.execute(
            "SELECT next_retry_at,created_at FROM app.outbox WHERE action_id=%s",
            (second["action_id"],),
        ).fetchone()
        assert outbox["next_retry_at"] > outbox["created_at"] + timedelta(minutes=59)


def test_pending_source_installation_change_cancels_original_outbox(engine, tmp_path):
    e = setup(engine, tmp_path)
    p = plan(e)
    current = e.source.installation
    SourceRegistry(engine).register_instance(
        current.model_copy(update={"revision": current.revision + 1})
    )
    assert row(e, p["action_id"])[0]["status"] == "cancelled"
    assert not e.service.tick(e.cookie)["dispatched"] and not e.channel.sends


def test_actual_new_ingress_edit_cancels_old_notification_without_copying_body(engine, tmp_path):
    e = setup(engine, tmp_path)
    p = plan(e)
    with engine.transaction() as conn:
        metadata = conn.execute(
            "SELECT metadata FROM app.source_ingress_inputs WHERE input_id=%s", (e.input.input_id,)
        ).fetchone()["metadata"]
    entity = metadata["event"]["entity_id"]
    ingest(
        e.source,
        entity=entity,
        version=2,
        operation="edit",
        delivery="sa17-real-controlled-edit",
        topic="same-topic",
        body="SA17_NEW_PRIVATE_EDIT_NEVER_COPY",
    )
    assert row(e, p["action_id"])[0]["status"] == "cancelled"
    assert not e.service.tick(e.cookie)["dispatched"] and not e.channel.sends


def test_late_accepted_receipt_is_recorded_after_committed_revoke_without_task_revival(
    engine, tmp_path
):
    e = setup(engine, tmp_path)
    p = plan(e)
    send = e.channel.send

    def accepted_then_revoked(instance, message, body):
        result = send(instance, message, body)
        policy(e.source, mode="disabled")
        return result

    e.channel.send = accepted_then_revoked
    before = engine.get_run(e.work.run_id)
    e.service.tick(e.cookie)
    assert len(e.channel.sends) == 1 and row(e, p["action_id"])[0]["status"] == "succeeded"
    assert engine.get_run(e.work.run_id)["outcome"] == before["outcome"]
    assert not e.service.tick(e.cookie)["dispatched"]


def test_source_body_deletion_does_not_turn_unknown_into_unsent_or_block_receipt_truth(
    engine, tmp_path
):
    e = setup(engine, tmp_path)
    e.channel.outcome = "lost"
    p = plan(e)
    e.service.tick(e.cookie)
    with engine.transaction() as conn:
        conn.execute(
            "SELECT app.expire_source_input(%s,'SA17_AFTER_UNKNOWN_DELETE')", (e.input.input_id,)
        )
    assert row(e, p["action_id"])[0]["status"] == "outcome_unknown"
    assert e.service.reconcile(e.cookie, p["action_id"])["resolved"]
    assert len(e.channel.sends) == len(e.channel.queries) == 1
