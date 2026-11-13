"""Real owner-process loss and independent-process queue claim competition."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID, uuid4

import pytest
from sa12_crash_actor import ReopenedFilesystemDriver, private_json
from sa12_support import claim, enqueue, session, setup

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import ROOT
from mgar_v3.super_agent.source_scheduler_domain import (
    SchedulerClaimRequest,
    SchedulerHeartbeatRequest,
)


def child_environment():
    return dict(
        os.environ,
        PYTHONPATH=str(ROOT / "src") + os.pathsep + str(ROOT / "tests"),
    )


def snapshot(engine, run_id):
    with engine.transaction() as conn:
        run = conn.execute(
            "SELECT status,execution_epoch FROM app.runs WHERE run_id=%s",
            (run_id,),
        ).fetchone()
        queue = conn.execute(
            "SELECT * FROM app.sa_scheduler_queue WHERE run_id=%s",
            (run_id,),
        ).fetchone()
        assignments = conn.execute(
            "SELECT * FROM app.sa_scheduler_assignments WHERE run_id=%s ORDER BY created_at",
            (run_id,),
        ).fetchall()
        operations = conn.execute(
            "SELECT * FROM app.sa_scheduler_operations WHERE run_id=%s ORDER BY created_at",
            (run_id,),
        ).fetchall()
        slots = conn.execute(
            "SELECT operation_hash,released_at FROM app.sa_budget_quota "
            "WHERE project_id=%s AND kind='slot' ORDER BY operation_hash",
            (engine.settings.project_id,),
        ).fetchall()
        queues = conn.execute(
            "SELECT operation_hash,released_at FROM app.sa_budget_quota "
            "WHERE project_id=%s AND kind='queue' ORDER BY operation_hash",
            (engine.settings.project_id,),
        ).fetchall()
        started = conn.execute(
            "SELECT count(*) AS n FROM app.run_events WHERE run_id=%s AND kind='run_started'",
            (run_id,),
        ).fetchone()["n"]
    return {
        "run": run,
        "queue": queue,
        "assignments": assignments,
        "operations": operations,
        "slots": slots,
        "queue_quotas": queues,
        "started": started,
    }


def recovered_engine(settings):
    deadline = time.monotonic() + 5
    while True:
        try:
            return Engine(settings, ArtifactStore(settings.artifact_root))
        except DomainError as error:
            if error.code != "ENGINE_ALREADY_RUNNING" or time.monotonic() >= deadline:
                raise
            # Wait only for the killed process's TCP session to release its lock.
            time.sleep(0.01)


@pytest.mark.parametrize(
    "point",
    [
        "before_claim_commit",
        "after_claim_commit_before_prepare",
        "after_prepare_receipt_commit",
    ],
)
def test_real_sigkill_preserves_atomic_claim_and_conservative_resource_recovery(
    engine,
    tmp_path,
    point,
):
    env = setup(engine, tmp_path)
    queued = enqueue(env)
    worker = session(env)
    request_id = uuid4()
    initial = snapshot(engine, queued["run_id"])
    assert initial["run"] == {"status": "queued", "execution_epoch": 0}
    assert initial["assignments"] == initial["operations"] == initial["slots"] == []
    assert len(initial["queue_quotas"]) == 1
    assert initial["queue_quotas"][0]["released_at"] is None
    settings = engine.settings
    directory = tmp_path / "scheduler-crash-private"
    directory.mkdir(mode=0o700)
    marker = directory / "checkpoint.json"
    state = directory / "private-state.json"
    private_json(
        state,
        {
            "settings": {
                "database_url": settings.database_url.get_secret_value(),
                "local_token": settings.local_token.get_secret_value(),
                "artifact_root": str(settings.artifact_root),
                "project_id": settings.project_id,
                "repository": settings.repository,
                "local_actor": settings.local_actor,
                "allowed_actors": settings.allowed_actors,
                "graph_version": settings.graph_version,
                "policy_version": settings.policy_version,
                "allow_paid": False,
                "allow_external_write": False,
                "deepseek_api_key": None,
            },
            "cookie": env.cookie,
            "bearer": worker["bearer"],
            "session_id": worker["session_id"],
            "request_id": request_id,
            "driver_root": str(env.driver.root),
            "point": point,
            "marker": str(marker),
        },
    )
    assert state.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    engine.close()
    child = subprocess.run(
        [sys.executable, str(ROOT / "tests/sa12_crash_actor.py"), "crash", str(state)],
        cwd=ROOT,
        env=child_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    assert child.returncode == -signal.SIGKILL
    checkpoint = json.loads(marker.read_text())
    assert checkpoint["point"] == point
    assert checkpoint["transaction_status"] == (
        "INTRANS" if point == "before_claim_commit" else "IDLE"
    )
    assert checkpoint["pid"] != os.getpid()
    assert marker.stat().st_mode & 0o777 == 0o600
    recovered = recovered_engine(settings)
    try:
        assert recovered.connection.info.backend_pid != checkpoint["backend_pid"]
        recovered.source_scheduler.driver = ReopenedFilesystemDriver(env.driver.root)
        before_recovery = snapshot(recovered, queued["run_id"])
        summary = recovered.recover()
        after = snapshot(recovered, queued["run_id"])
        if point == "before_claim_commit":
            assert before_recovery == initial
            assert after == initial
            assert summary["paused_runs"] == 0
            assert list(env.driver.root.iterdir()) == []
            # A known rolled-back, never-dispatched attempt can be explicitly claimed.
            env.engine = recovered
            env.service = recovered.source_scheduler
            resumed = claim(env, worker, request_id=request_id)
            committed = snapshot(recovered, queued["run_id"])
            assert len(committed["assignments"]) == len(committed["slots"]) == 1
            assert len(committed["operations"]) == 1
            assert committed["run"] == {"status": "running", "execution_epoch": 1}
            assert committed["started"] == 1
            assert (
                committed["assignments"][0]["assignment_id"] == resumed["assignment_id"]
            )
            assert committed["slots"][0]["released_at"] is None
            return
        assert before_recovery["run"] == {"status": "running", "execution_epoch": 1}
        assert len(before_recovery["assignments"]) == len(before_recovery["slots"]) == 1
        assert len(before_recovery["operations"]) == 1
        assert before_recovery["started"] == 1
        assert summary["paused_runs"] == 1
        assert after["run"] == {"status": "paused", "execution_epoch": 2}
        assignment = after["assignments"][0]
        assert assignment["assignment_id"] == UUID(checkpoint["assignment_id"])
        assert assignment["state"] == "draining"
        assert assignment["fenced_at"] is not None
        assert assignment["released_at"] is None
        assert len(after["slots"]) == 1 and after["slots"][0]["released_at"] is None
        assert len(after["queue_quotas"]) == 1
        assert after["queue_quotas"][0]["released_at"] is not None
        operation = after["operations"][0]
        assert operation["operation_id"] == UUID(checkpoint["operation_id"])
        assert operation["request_sha256"] == checkpoint["request_sha256"]
        if point == "after_claim_commit_before_prepare":
            assert operation["state"] == "requested"
            assert operation["result_sha256"] is None
            assert list(env.driver.root.iterdir()) == []
        else:
            assert operation["state"] == "known_terminal"
            assert operation["result_code"] == "succeeded"
            assert operation["result_sha256"] is not None
            workspace = env.driver.root / str(assignment["workspace_id"])
            metadata = json.loads((workspace / "prepare-metadata.json").read_text())
            assert metadata["receipt_sha256"] == operation["result_sha256"]
            assert (workspace / "fixture.txt").is_file()
        capacity = recovered.source_scheduler.capacity(env.cookie)
        assert capacity["usage"]["slots"] == capacity["usage"]["preparing"] == 1
        stale = SchedulerHeartbeatRequest(
            assignment_id=assignment["assignment_id"],
            session_id=worker["session_id"],
            epoch=1,
            expected_revision=assignment["revision"],
        )
        with pytest.raises(DomainError) as rejected:
            recovered.source_scheduler.heartbeat(worker["bearer"], stale)
        assert rejected.value.code == "SOURCE_SCHEDULER_EPOCH_FENCED"
        assert snapshot(recovered, queued["run_id"]) == after
        repeated = recovered.recover()
        assert repeated["paused_runs"] == 0
        assert snapshot(recovered, queued["run_id"]) == after
        # Recovery alone neither resends preparation nor invents a stop receipt.
        assert recovered.source_scheduler.driver.receipts == {}
    finally:
        recovered.close()


@contextmanager
def claim_controller(engine):
    barrier = threading.Barrier(2)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *args):
            pass

        def do_POST(self):
            status, result = 500, {"code": "CONTROLLED_CLAIM_FAILED"}
            try:
                if self.path != "/claim":
                    status, result = 404, {"code": "NOT_FOUND"}
                else:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2048:
                        raise ValueError("Invalid controlled request size")
                    raw = self.rfile.read(length)
                    request = SchedulerClaimRequest.model_validate_json(raw)
                    authorization = self.headers.get("Authorization", "")
                    if not authorization.startswith("Bearer "):
                        raise ValueError("Missing controlled bearer")
                    barrier.wait(timeout=10)
                    result = engine.source_scheduler.claim(authorization[7:], request)
                    status = 200
            except DomainError as error:
                status, result = error.status, {"code": error.code}
            except Exception:
                pass
            encoded = json.dumps(result, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/claim"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_two_independent_claim_processes_share_one_original_engine_writer(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    queued = enqueue(env)
    workers = [session(env), session(env)]
    original_backend = engine.connection.info.backend_pid
    directory = tmp_path / "claim-client-private"
    directory.mkdir(mode=0o700)
    processes = []
    result_paths = []
    with claim_controller(engine) as url:
        try:
            for index, worker in enumerate(workers):
                path = directory / f"client-{index}.json"
                result = directory / f"result-{index}.json"
                private_json(
                    path,
                    {
                        "url": url,
                        "bearer": worker["bearer"],
                        "session_id": worker["session_id"],
                        "request_id": uuid4(),
                        "result": str(result),
                    },
                )
                result_paths.append(result)
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            str(ROOT / "tests/sa12_crash_actor.py"),
                            "claim-client",
                            str(path),
                        ],
                        cwd=ROOT,
                        env=child_environment(),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )
            for process in processes:
                assert process.wait(timeout=30) == 0
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
    results = [json.loads(path.read_text()) for path in result_paths]
    assert {item["pid"] for item in results} == {process.pid for process in processes}
    assert len({item["pid"] for item in results}) == 2
    assert all(item["pid"] != os.getpid() and item["status"] == 200 for item in results)
    winners = [
        item["result"] for item in results if item["result"]["assignment"] is not None
    ]
    assert len(winners) == 1
    assert engine.connection.info.backend_pid == original_backend
    committed = snapshot(engine, queued["run_id"])
    assert len(committed["assignments"]) == len(committed["slots"]) == 1
    assert len(committed["operations"]) == 1
    assert committed["assignments"][0]["assignment_id"] == UUID(
        winners[0]["assignment_id"]
    )
    assert committed["run"] == {"status": "running", "execution_epoch": 1}
    assert committed["started"] == 1
    assert committed["slots"][0]["released_at"] is None
    assert committed["operations"][0]["state"] == "known_terminal"
    assert len(list(env.driver.root.iterdir())) == 1
