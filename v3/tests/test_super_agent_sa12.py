"""Original-Engine PG scheduling; controlled filesystem resources, no live calls."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sa12_support import claim, enqueue, ready, session, setup

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_scheduler_domain import (
    SchedulerAssignmentRequest,
    SchedulerHeartbeatRequest,
    SchedulerOwnerDrainRequest,
    SchedulerQueueCancelRequest,
    SchedulerSessionRevokeRequest,
)


def rows(env, table):
    with env.engine.transaction() as conn:
        return conn.execute("SELECT * FROM app." + table).fetchall()


def request(worker, assignment):
    return SchedulerAssignmentRequest(
        assignment_id=assignment["assignment_id"],
        session_id=worker["session_id"],
        epoch=assignment["execution_epoch"],
        expected_revision=assignment["revision"],
    )


def test_formal_claim_uses_original_run_epoch_once_and_reserves_shared_slot(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    queued = enqueue(env)
    worker = session(env)
    claimed = claim(env, worker)
    assert claimed["run_id"] == queued["run_id"]
    assert claimed["execution_epoch"] == queued["expected_epoch"] + 1
    assert claimed["state"] == "preparing"
    active = ready(env, worker, claimed)
    assert active["state"] == "active"
    with env.service.worker_context(worker["bearer"], request(worker, active)):
        same = engine.begin_run(active["run_id"])
        assert same["execution_epoch"] == active["execution_epoch"]
    assert len(rows(env, "runs")) == len(rows(env, "tickets")) == 1
    quotas = rows(env, "sa_budget_quota")
    assert sum(q["released_at"] is None for q in quotas if q["kind"] == "queue") == 0
    assert sum(q["released_at"] is None for q in quotas if q["kind"] == "slot") == 1
    assert rows(env, "usage_calls") == rows(env, "router_calls") == []
    assert not engine.settings.allow_paid and not engine.settings.allow_external_write


def test_managed_run_cannot_start_through_legacy_graph_without_worker_context(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    queued = enqueue(env)
    with pytest.raises(DomainError):
        engine.begin_run(queued["run_id"])
    assert engine.get_run(queued["run_id"])["status"] == "queued"
    worker = session(env)
    active = ready(env, worker, claim(env, worker))
    with pytest.raises(DomainError):
        engine.begin_run(active["run_id"])
    with engine.transaction() as conn, pytest.raises(DomainError):
        engine._run(conn, active["run_id"], active["execution_epoch"])


def test_duplicate_claim_returns_one_assignment_and_does_not_prepare_twice(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    enqueue(env)
    worker = session(env)
    key = uuid4()
    one = claim(env, worker, request_id=key)
    two = claim(env, worker, request_id=key)
    assert one["assignment_id"] == two["assignment_id"]
    assert one["workspace_id"] == two["workspace_id"]
    assert one["prepare_operation"] == two["prepare_operation"]
    assert len(rows(env, "sa_scheduler_assignments")) == 1
    assert len(rows(env, "sa_scheduler_operations")) == 1
    assert len(list(env.driver.root.iterdir())) == 1


def test_parallel_claims_do_not_duplicate_run_or_resource_occupancy(engine, tmp_path):
    env = setup(engine, tmp_path)
    enqueue(env)
    workers = [session(env), session(env)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda worker: claim(env, worker), workers))
    claimed = [r for r in results if r.get("assignment") is not None]
    assert len(claimed) == len(rows(env, "sa_scheduler_assignments")) == 1
    assert len(rows(env, "runs")) == 1
    assert len(list(env.driver.root.iterdir())) == 1


def test_two_original_tasks_have_different_actual_writable_directories(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    enqueue(env)
    enqueue(env)
    workers = [session(env), session(env)]
    claimed = [claim(env, worker) for worker in workers]
    assert claimed[0]["run_id"] != claimed[1]["run_id"]
    assert claimed[0]["workspace_id"] != claimed[1]["workspace_id"]
    directories = [env.driver.root / str(c["workspace_id"]) for c in claimed]
    assert directories[0].resolve() != directories[1].resolve()
    for n, directory in enumerate(directories):
        (directory / "independent-marker").write_text(str(n))
    assert [
        (directory / "independent-marker").read_text() for directory in directories
    ] == ["0", "1"]
    assert env.service.capacity(env.cookie)["usage"]["slots"] == 2


def test_preparing_limit_blocks_other_task_before_second_workspace_io(engine, tmp_path):
    env = setup(engine, tmp_path, preparing=1)
    enqueue(env)
    enqueue(env)
    first, second = session(env), session(env)
    one = claim(env, first)
    no_capacity = claim(env, second)
    assert no_capacity["assignment"] is None
    assert len(list(env.driver.root.iterdir())) == 1
    ready(env, first, one)
    two = claim(env, second)
    assert two["assignment"] is not None
    assert two["assignment_id"] != one["assignment_id"]


def test_session_revoke_fences_existing_worker_and_retains_resource_slot(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    enqueue(env)
    worker = session(env)
    active = ready(env, worker, claim(env, worker))
    env.service.revoke_session(
        env.cookie,
        SchedulerSessionRevokeRequest(
            session_id=worker["session_id"],
            expected_revision=worker["revision"],
            review_ref="sa12-revoke-worker-session",
        ),
    )
    with pytest.raises(DomainError):
        env.service.heartbeat(
            worker["bearer"],
            SchedulerHeartbeatRequest(**request(worker, active).model_dump()),
        )
    row = env.service.get_assignment(env.cookie, active["assignment_id"])
    assert row["state"] == "draining" and row["released_at"] is None
    assert env.service.capacity(env.cookie)["usage"]["slots"] == 1
    assert (
        engine.get_run(active["run_id"])["execution_epoch"] > active["execution_epoch"]
    )


def test_cancel_queue_releases_queue_quota_but_does_not_complete_original_task(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    queued = enqueue(env)
    result = env.service.cancel_queue(
        env.cookie,
        SchedulerQueueCancelRequest(
            queue_id=queued["queue_id"],
            expected_revision=queued["revision"],
            review_ref="sa12-cancel-only-resource-admission",
        ),
    )
    assert result["state"] == "closed"
    assert engine.get_run(queued["run_id"])["status"] == "queued"
    assert rows(env, "sa_scheduler_assignments") == []
    quotas = [q for q in rows(env, "sa_budget_quota") if q["kind"] == "queue"]
    assert len(quotas) == 1 and quotas[0]["released_at"] is not None


def test_current_workspace_tree_is_checked_before_ready(engine, tmp_path):
    env = setup(engine, tmp_path)
    enqueue(env)
    worker = session(env)
    claimed = claim(env, worker)
    (env.driver.root / str(claimed["workspace_id"]) / "fixture.txt").write_text(
        "changed"
    )
    with pytest.raises(DomainError):
        ready(env, worker, claimed)
    row = env.service.get_assignment(env.cookie, claimed["assignment_id"])
    assert row["state"] != "active"


def test_owner_drain_is_not_quiescence_and_unknown_driver_does_not_release_slot(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    enqueue(env)
    worker = session(env)
    active = ready(env, worker, claim(env, worker))
    env.driver.available = False
    drained = env.service.drain(
        env.cookie,
        SchedulerOwnerDrainRequest(
            assignment_id=active["assignment_id"],
            expected_revision=active["revision"],
            reason_code="OWNER_DRAIN",
            review_ref="sa12-stop-with-unknown-provider",
        ),
    )
    assert drained["state"] == "draining" and drained["released_at"] is None
    assert env.service.capacity(env.cookie)["usage"]["slots"] == 1
    assert rows(env, "usage_calls") == []
