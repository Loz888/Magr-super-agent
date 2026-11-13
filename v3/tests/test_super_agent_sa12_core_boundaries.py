"""Real-PG expiry, pending-work and quiet resource-release boundaries.

The controlled filesystem port proves only its empty process set. Real Harness
process stopping is covered separately. This file does not claim canonical
Source human/automation fairness integration; pure fairness checks are separate.
"""

import json
import time

import pytest
from sa12_support import FilesystemOnlyDriver, claim, enqueue, ready, session, setup

from mgar_v3.domain import DomainError, digest
from mgar_v3.super_agent.source_scheduler_domain import (
    SchedulerAssignmentRequest,
    SchedulerConfiguration,
    SchedulerConfigureRequest,
    SchedulerHeartbeatRequest,
    SchedulerOwnerDrainRequest,
    SchedulerReportRequest,
)


def assignment_request(worker, row):
    return SchedulerAssignmentRequest(
        assignment_id=row["assignment_id"],
        session_id=worker["session_id"],
        epoch=row["execution_epoch"],
        expected_revision=row["revision"],
    )


def report(worker, row, prepared, status="waiting_user"):
    return SchedulerReportRequest(
        **assignment_request(worker, row).model_dump(),
        operation_id=prepared["operation_id"],
        request_sha256=prepared["request_sha256"],
        status=status,
    )


def wait_for_pg_deadline(engine, deadline):
    limit = time.monotonic() + 5
    while True:
        with engine.transaction() as conn:
            now = conn.execute("SELECT clock_timestamp() AS at").fetchone()["at"]
        if now >= deadline:
            return
        assert time.monotonic() < limit
        time.sleep(0.01)


def occupied_slot(env):
    with env.engine.transaction() as conn:
        rows = conn.execute(
            "SELECT released_at FROM app.sa_budget_quota WHERE project_id=%s AND kind='slot'",
            (env.engine.settings.project_id,),
        ).fetchall()
    assert len(rows) == 1
    return rows[0]["released_at"] is None


def original_context(engine, run_id):
    with engine.transaction() as conn:
        return conn.execute(
            "SELECT r.question,r.input_revision,r.mailbox_cursor,t.ticket_id,"
            "t.status AS ticket_status,"
            "t.thread_id,t.mandate_id FROM app.runs r JOIN app.tickets t USING(ticket_id) "
            "WHERE r.run_id=%s",
            (run_id,),
        ).fetchone()


def test_real_pg_lease_expiry_fences_heartbeat_and_old_success_report(engine, tmp_path):
    env = setup(engine, tmp_path)
    queued = enqueue(env)
    worker = session(env)
    claimed = claim(env, worker)
    active = ready(env, worker, claimed)
    with engine.transaction() as conn:
        # A valid future short lease fixture crosses actual PG time; no fake past
        # clock, terminal status, grant, completion or business result is injected.
        shortened = conn.execute(
            "UPDATE app.sa_scheduler_assignments SET "
            "lease_until=clock_timestamp()+interval '0.15 seconds',"
            "last_heartbeat_at=clock_timestamp(),updated_at=clock_timestamp(),revision=revision+1 "
            "WHERE assignment_id=%s AND revision=%s RETURNING *",
            (active["assignment_id"], active["revision"]),
        ).fetchone()
        assert shortened is not None
    wait_for_pg_deadline(engine, shortened["lease_until"])
    with pytest.raises(DomainError) as heartbeat:
        env.service.heartbeat(
            worker["bearer"],
            SchedulerHeartbeatRequest(
                **assignment_request(worker, shortened).model_dump(),
            ),
        )
    assert heartbeat.value.code == "SOURCE_SCHEDULER_LEASE_FENCED"
    with pytest.raises(DomainError) as stale_report:
        env.service.report(
            worker["bearer"],
            report(
                worker,
                shortened,
                claimed["prepare_operation"],
                status="finished",
            ),
        )
    assert stale_report.value.code == "SOURCE_SCHEDULER_LEASE_FENCED"
    assert occupied_slot(env)
    env.service.driver = None
    maintained = env.service.maintain()
    row = env.service.get_assignment(env.cookie, active["assignment_id"])
    assert maintained["drained"] == maintained["driver_unavailable"] == 1
    assert row["state"] == "draining" and row["released_at"] is None
    assert (
        engine.get_run(queued["run_id"])["execution_epoch"]
        == active["execution_epoch"] + 1
    )
    assert occupied_slot(env)


def test_real_pg_session_expiry_never_restores_a_worker_or_releases_unconfirmed_resources(
    engine,
    tmp_path,
):
    env = setup(engine, tmp_path)
    enqueue(env)
    worker = session(env, ttl=2)
    claimed = claim(env, worker)
    active = ready(env, worker, claimed)
    wait_for_pg_deadline(engine, worker["expires_at"])
    with pytest.raises(DomainError) as expired:
        env.service.heartbeat(
            worker["bearer"],
            SchedulerHeartbeatRequest(
                **assignment_request(worker, active).model_dump(),
            ),
        )
    assert expired.value.code == "SOURCE_SCHEDULER_SESSION_FENCED"
    with pytest.raises(DomainError):
        env.service.report(
            worker["bearer"],
            report(worker, active, claimed["prepare_operation"], "finished"),
        )
    env.service.driver = None
    env.service.maintain()
    row = env.service.get_assignment(env.cookie, active["assignment_id"])
    assert row["state"] == "draining" and row["released_at"] is None
    assert occupied_slot(env)


@pytest.mark.parametrize("state", ["queued", "running"])
def test_pending_original_read_job_keeps_slot_after_wait_and_known_filesystem_quiescence(
    engine,
    tmp_path,
    state,
):
    env = setup(engine, tmp_path)
    enqueue(env)
    worker = session(env)
    claimed = claim(env, worker)
    active = ready(env, worker, claimed)
    with env.service.worker_context(
        worker["bearer"], assignment_request(worker, active)
    ):
        job = engine.submit_read_job(
            active["run_id"], active["execution_epoch"], "sa12-original-read-pending"
        )
        if state == "running":
            job = engine.claim_read_job(job["job_id"], active["execution_epoch"])
    assert job["status"] == state
    env.service.report(
        worker["bearer"], report(worker, active, claimed["prepare_operation"])
    )
    maintained = env.service.maintain()
    assert maintained["released"] == 0 and maintained["blocked"] == 1
    assert occupied_slot(env)
    with engine.transaction() as conn:
        preserved = conn.execute(
            "SELECT status FROM app.read_jobs WHERE job_id=%s", (job["job_id"],)
        ).fetchone()
    assert preserved["status"] in {"queued", "running"}
    row = env.service.get_assignment(env.cookie, active["assignment_id"])
    assert row["state"] == "draining" and row["released_at"] is None
    assert env.service.capacity(env.cookie)["usage"]["slots"] == 1


class CountingFilesystemDriver(FilesystemOnlyDriver):
    def __init__(self, root):
        super().__init__(root)
        self.stop_calls = 0

    def stop(self, context):
        self.stop_calls += 1
        return super().stop(context)


def test_unknown_operation_stays_occupied_and_cleanup_is_inspected_without_resend(
    engine, tmp_path
):
    driver = CountingFilesystemDriver(tmp_path / "unknown-port")
    env = setup(engine, tmp_path, driver=driver)
    enqueue(env)
    worker = session(env)
    claimed = claim(env, worker)
    active = ready(env, worker, claimed)
    context = env.service.begin_operation(
        worker["bearer"],
        assignment_request(worker, active),
        "job",
        digest("sa12-unknown-controlled-port"),
    )
    driver.available = False
    with env.service.dispatch_guard(context):
        unknown = driver.inspect(context)
    env.service.record_resource_receipt(context, unknown)
    assert env.service.pending_resource_operations() == (
        (active["assignment_id"], context.operation_id),
    )
    first = env.service.maintain()
    second = env.service.maintain()
    assert first["released"] == second["released"] == 0
    assert first["unknown"] == second["unknown"] == 1
    assert driver.stop_calls == 1
    assert occupied_slot(env)
    with engine.transaction() as conn:
        operations = conn.execute(
            "SELECT kind,state FROM app.sa_scheduler_operations WHERE assignment_id=%s",
            (active["assignment_id"],),
        ).fetchall()
    assert sum(row["kind"] == "stop" for row in operations) == 1
    assert next(row for row in operations if row["kind"] == "job")["state"] == "unknown"


def test_waiting_user_releases_only_after_known_quiescence_and_preserves_original_context(
    engine,
    tmp_path,
):
    env = setup(engine, tmp_path)
    enqueue(env)
    worker = session(env)
    claimed = claim(env, worker)
    active = ready(env, worker, claimed)
    before = original_context(engine, active["run_id"])
    waiting = env.service.report(
        worker["bearer"], report(worker, active, claimed["prepare_operation"])
    )
    assert waiting["business_outcome_changed"] is False
    assert waiting["state"] == "draining" and occupied_slot(env)
    maintained = env.service.maintain()
    assert maintained["released"] == 1
    assert not occupied_slot(env)
    released = env.service.get_assignment(env.cookie, active["assignment_id"])
    assert (
        released["state"] == "released"
        and released["quiescent_at"] >= released["fenced_at"]
    )
    assert original_context(engine, active["run_id"]) == before
    run = engine.get_run(active["run_id"])
    assert (
        run["status"] == "paused"
        and run["execution_epoch"] == active["execution_epoch"] + 1
    )
    assert env.service.maintain()["released"] == 0
    with engine.transaction() as conn:
        for table in ("usage_calls", "router_calls", "outbox"):
            assert (
                conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"]
                == 0
            )
        assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT count(*) AS n FROM app.tickets").fetchone()["n"] == 1
        )


@pytest.mark.parametrize("action", ["disable", "owner_drain"])
def test_configuration_and_owner_fences_advance_original_epoch_once(
    engine, tmp_path, action
):
    env = setup(engine, tmp_path)
    enqueue(env)
    worker = session(env)
    claimed = claim(env, worker)
    active = ready(env, worker, claimed)
    if action == "disable":
        saved = env.service.get_config(env.cookie)
        definition = saved["configuration"]
        definition["enabled"] = False
        env.service.configure(
            env.cookie,
            SchedulerConfigureRequest(
                expected_revision=saved["revision"],
                configuration=SchedulerConfiguration.model_validate_json(
                    json.dumps(definition)
                ),
                review_ref="sa12-explicit-disable-original-epoch-fence",
            ),
        )
    else:
        env.service.drain(
            env.cookie,
            SchedulerOwnerDrainRequest(
                assignment_id=active["assignment_id"],
                expected_revision=active["revision"],
                reason_code="OWNER_DRAIN",
                review_ref="sa12-original-epoch-owner-fence",
            ),
        )
    run = engine.get_run(active["run_id"])
    assert (
        run["status"] == "paused"
        and run["execution_epoch"] == active["execution_epoch"] + 1
    )
    env.service.driver = None
    env.service.maintain()
    env.service.maintain()
    assert engine.get_run(active["run_id"])["execution_epoch"] == run["execution_epoch"]
    assert occupied_slot(env)
