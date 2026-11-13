"""First-run real PG/H03/Docker resource checks; no model/platform/write activation."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.pq import TransactionStatus
from sa12_support import enqueue, ready, session, setup

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import IsolatedExecutionBroker
from mgar_v3.harness.contracts import (
    CapabilityGrant,
    ExecutionRequest,
    WorkspaceEdit,
    sha256_text,
)
from mgar_v3.harness.workspace import WorkspaceRegistry
from mgar_v3.super_agent.source_scheduler_domain import (
    SchedulerAssignmentRequest,
    SchedulerClaimRequest,
    SchedulerConfiguration,
    SchedulerConfigureRequest,
    SchedulerHeartbeatRequest,
    SchedulerLimits,
    SchedulerOwnerDrainRequest,
    SchedulerSessionRevokeRequest,
)
from mgar_v3.super_agent.source_scheduler_runtime import (
    H03SchedulerResourceDriver,
    RuntimeLeaseSupervisor,
    SchedulerRuntimeProfile,
)

IMAGE = "sha256:efad10932c12f9d6f2408fe960e9a292c4bdbac0f8365387838acbe4f0fdd2b6"


def docker(*args):
    return subprocess.run(
        ["docker", *args], capture_output=True, timeout=20, check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "DOCKER_HOST": os.environ.get("DOCKER_HOST", "")},
    )


def wait_for(predicate, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.04)
    raise AssertionError("Bound runtime observation did not arrive before the frozen deadline")


def assignment_request(worker, row):
    return SchedulerAssignmentRequest(
        assignment_id=row["assignment_id"], session_id=worker["session_id"],
        epoch=row["execution_epoch"], expected_revision=row["revision"],
    )


def operation_rows(env, assignment_id):
    with env.engine.transaction() as conn:
        return conn.execute(
            "SELECT * FROM app.sa_scheduler_operations WHERE assignment_id=%s "
            "ORDER BY created_at,operation_id", (assignment_id,),
        ).fetchall()


def names(env, assignment_id):
    return [r["provider_operation_id"] for r in operation_rows(env, assignment_id)
            if r["kind"] in {"start", "job"}]


def assert_absent(env, assignment_id):
    for name in names(env, assignment_id):
        probe = docker("container", "inspect", "--format", "{{json .}}", name)
        assert probe.returncode != 0 and not probe.stdout.strip()
        assert probe.returncode == 1
        message = probe.stderr.decode("utf-8", "replace").strip()
        assert "No such" in message
        assert message in {
            f"Error response from daemon: No such container: {name}",
            f"Error: No such container: {name}",
            f"Error: No such object: {name}",
        }


@contextmanager
def runtime_env(engine, tmp_path, *, code="print('sa12-real-resource')", timeout=8, lease=120):
    # Failure is explicit; no skip or network image pull substitutes for real Docker.
    assert docker("image", "inspect", IMAGE).returncode == 0
    source = tmp_path / "registered-source"
    source.mkdir()
    (source / "fixture.txt").write_text("synthetic initial\n")
    commands = [
        ["init", "--quiet"], ["config", "user.name", "SA12 Fixture"],
        ["config", "user.email", "sa12@example.invalid"],
        ["remote", "add", "origin", "https://github.com/example/mgar-test"],
        ["add", "fixture.txt"], ["commit", "--quiet", "-m", "synthetic fixed source"],
    ]
    for command in commands:
        subprocess.run(["git", "-C", str(source), *command], capture_output=True,
                       check=True, timeout=10)
    registry_root = tmp_path / "registry"
    registry = WorkspaceRegistry(registry_root)
    registration = registry.register_local(source, actor=engine.settings.local_actor)
    image_lock = tmp_path / "image-lock.json"
    image_lock.write_text(json.dumps({"image_id": IMAGE}))
    image_lock.chmod(0o600)
    profile = SchedulerRuntimeProfile(
        actor=engine.settings.local_actor, repository=engine.settings.repository,
        grant=CapabilityGrant(
            actor=engine.settings.local_actor, workspace_id=registration.workspace_id,
            repository=engine.settings.repository, read_paths=("",), write_paths=(),
            execute=True, allowed_commands=("python3",), network=False, max_seconds=180,
        ),
        request=ExecutionRequest(operation_id="fixed-sa12-fixture", argv=("python3", "-c", code),
                                 timeout_seconds=timeout),
        image_id=IMAGE, review_ref="explicit-server-controlled-SA12-fixture",
    )
    kwargs = dict(scheduler=engine.source_scheduler, registry_root=registry_root,
                  registration=registration, source_root=source, profile=profile,
                  receipt_root=tmp_path / "private-receipts", image_lock=image_lock)
    driver = H03SchedulerResourceDriver(**kwargs)
    env = setup(engine, tmp_path, driver=driver)
    env.registry, env.registration, env.driver_kwargs = registry, registration, kwargs
    env.config_revision = 1
    if lease != 120:
        now = datetime.now(UTC)
        env.service.configure(env.cookie, SchedulerConfigureRequest(
            expected_revision=1,
            configuration=SchedulerConfiguration(
                enabled=True, valid_from=now - timedelta(minutes=1),
                valid_until=now + timedelta(hours=1),
                limits=SchedulerLimits(slots=2, preparing=2, cpu_millis=4000, memory_mib=4096,
                                       queue=32, source_slots=2, lease_seconds=lease,
                                       preparation_seconds=120, session_ttl_seconds=3600),
            ), review_ref="explicit-real-lease-loss-boundary",
        ))
        env.config_revision = 2
    try:
        yield env
    finally:
        # Only this test's original PG-bound assignments and exact container IDs.
        for row in env.service.assignments(env.cookie)["items"]:
            if row["state"] != "released":
                env.service.drain(env.cookie, SchedulerOwnerDrainRequest(
                    assignment_id=row["assignment_id"], expected_revision=row["revision"],
                    reason_code="OWNER_DRAIN", review_ref="SA12-isolated-test-cleanup",
                ))
        env.service.maintain()
        for row in env.service.assignments(env.cookie)["items"]:
            for name in names(env, row["assignment_id"]):
                probe = docker("container", "inspect", name)
                if probe.returncode == 0:
                    value = json.loads(probe.stdout)[0]
                    labels = value["Config"]["Labels"]
                    assert labels["mgar.sa12.assignment"] == str(row["assignment_id"])
                    assert labels["mgar.sa12.project"] == engine.settings.project_id
                    assert labels["mgar.v4.h04.broker"] == "1"
                    assert docker("rm", "-f", value["Id"]).returncode == 0


def activate(env, *, ttl=3600):
    queued = enqueue(env)
    worker = session(env, ttl=ttl)
    claimed = env.service.claim(worker["bearer"], SchedulerClaimRequest(
        request_id=uuid4(), session_id=worker["session_id"],
        expected_config_revision=env.config_revision,
    ))
    active = ready(env, worker, claimed)
    return SimpleNamespace(queued=queued, worker=worker, claimed=claimed, active=active)


def test_real_h03_copies_are_independently_writable_by_storage_owner_only(engine, tmp_path):
    with runtime_env(engine, tmp_path) as env:
        tasks = [activate(env), activate(env)]
        copies = [env.registry.open_task_workspace(env.registration,
                  str(t.active["assignment_id"])) for t in tasks]
        assert copies[0].root != copies[1].root
        assert copies[0].snapshot().workspace_id == copies[1].snapshot().workspace_id
        assert tasks[0].active["workspace_id"] != tasks[1].active["workspace_id"]
        for number, copy in enumerate(copies):
            before = copy.snapshot()
            copy.apply([WorkspaceEdit(path="fixture.txt", expected_sha256=sha256_text(
                before.files["fixture.txt"]), content=f"owner fixture {number}\n")],
                expected_tree=before.tree_hash)
        assert [c.read("fixture.txt") for c in copies] == [
            "owner fixture 0\n", "owner fixture 1\n",
        ]
        assert (env.driver.source_root / "fixture.txt").read_text() == "synthetic initial\n"
        assert env.driver.profile.grant.write_paths == ()
        for path in env.driver.receipt_root.rglob("*.json"):
            assert path.stat().st_mode & 0o777 == 0o600


def test_real_h06_python_terminal_receipt_does_not_complete_original_run(engine, tmp_path):
    with runtime_env(engine, tmp_path) as env:
        task = activate(env)
        receipt = env.driver.execute_preconfigured(
            task.worker["bearer"], assignment_request(task.worker, task.active),
        )
        assert receipt.state == "known_terminal" and receipt.result_code == "succeeded"
        assert receipt.exit_code == 0
        assert engine.get_run(task.active["run_id"])["status"] == "running"
        assert env.service.capacity(env.cookie)["usage"]["slots"] == 1
        with engine.transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 0
        env.service.drain(env.cookie, SchedulerOwnerDrainRequest(
            assignment_id=task.active["assignment_id"], expected_revision=task.active["revision"],
            reason_code="OWNER_DRAIN", review_ref="physical-stop-before-slot-release",
        ))
        env.service.maintain()
        assert env.service.capacity(env.cookie)["usage"]["slots"] == 0
        assert_absent(env, task.active["assignment_id"])


def test_command_deadline_rebuilds_empty_driver_cache_from_pg_and_original_metadata(
    engine, tmp_path, monkeypatch,
):
    with runtime_env(engine, tmp_path, code="import time;time.sleep(30)", timeout=2) as env:
        task = activate(env)
        # Controlled loss of the polling Worker after actual Docker launch.
        monkeypatch.setattr(
            env.driver, "wait_preconfigured", lambda context: env.driver._load(context).receipt,
        )
        running = env.driver.execute_preconfigured(
            task.worker["bearer"], assignment_request(task.worker, task.active),
        )
        assert running.state == "running"
        original = next(env.driver.receipt_root.rglob("dispatch-intent.json")).read_bytes()
        rebuilt = H03SchedulerResourceDriver(**env.driver_kwargs)
        assert rebuilt._active == {}
        env.service.driver = rebuilt
        with RuntimeLeaseSupervisor(env.service, interval=0.1):
            wait_for(lambda: env.service.get_assignment(env.cookie,
                task.active["assignment_id"])["state"] == "released", 8)
        assert next(rebuilt.receipt_root.rglob("dispatch-intent.json")).read_bytes() == original
        assert env.service.capacity(env.cookie)["usage"]["slots"] == 0
        assert_absent(env, task.active["assignment_id"])


@pytest.mark.parametrize("boundary", ["committed-session-revoke", "slow-inspect-expiry"])
def test_actual_create_then_revocation_or_expiry_never_reaches_actual_start(
    engine, tmp_path, monkeypatch, boundary,
):
    with runtime_env(engine, tmp_path) as env:
        task = activate(env, ttl=4 if boundary == "slow-inspect-expiry" else 3600)
        original = IsolatedExecutionBroker._docker
        seen = {"created": 0, "start": 0, "boundary": False}

        def at_boundary(broker, args, **kwargs):
            assert engine.connection.info.transaction_status == TransactionStatus.IDLE
            if args[0] == "start":
                seen["start"] += 1
            result = original(broker, args, **kwargs)
            if args[0] == "create":
                seen["created"] += 1
                if boundary == "committed-session-revoke":
                    env.service.revoke_session(env.cookie, SchedulerSessionRevokeRequest(
                        session_id=task.worker["session_id"], expected_revision=1,
                        review_ref="exact-after-create-before-start-revocation",
                    ))
                    seen["boundary"] = True
            if list(args[:2]) == ["container", "inspect"] and not seen["boundary"]:
                if boundary == "slow-inspect-expiry" and seen["created"]:
                    remaining = (task.worker["expires_at"] - datetime.now(UTC)).total_seconds()
                    time.sleep(max(0, remaining) + 0.03)
                    seen["boundary"] = True
            return result

        with monkeypatch.context() as patch:
            patch.setattr(IsolatedExecutionBroker, "_docker", at_boundary)
            receipt = env.driver.execute_preconfigured(
                task.worker["bearer"], assignment_request(task.worker, task.active),
            )
        assert seen == {"created": 1, "start": 0, "boundary": True}
        assert receipt.result_code != "succeeded"
        env.service.maintain()
        assert_absent(env, task.active["assignment_id"])


def test_daemon_error_is_unknown_not_missing_or_a_slot_release(engine, tmp_path, monkeypatch):
    with runtime_env(engine, tmp_path) as env:
        task = activate(env)
        context = env.service.begin_operation(task.worker["bearer"],
            assignment_request(task.worker, task.active), "start", "a" * 64)
        facts = env.service.capability_context(context)
        broker = env.driver._broker(context, facts, cleanup=True)
        with monkeypatch.context() as patch:
            patch.setattr(broker, "_docker", lambda args, **kw: subprocess.CompletedProcess(
                args, 1, b"", b"Cannot connect to the Docker daemon"))
            with pytest.raises(DomainError) as error:
                broker.inspect_bound_command(env.driver._name(context), env.driver._labels(context))
            assert error.value.code == "HARNESS_COMMAND_OUTCOME_UNKNOWN"
        assert env.service.capacity(env.cookie)["usage"]["slots"] == 1
        row = env.service.get_assignment(env.cookie, task.active["assignment_id"])
        assert row["released_at"] is None


def test_sigkill_heartbeat_worker_real_lease_expiry_stops_container_before_slot_release(
    engine, tmp_path,
):
    with runtime_env(engine, tmp_path, code="import time;time.sleep(90)",
                     timeout=120, lease=30) as env:
        task = activate(env)
        result = []
        errors = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get("Authorization") != "Bearer " + task.worker["bearer"]:
                    self.send_error(403)
                    return
                row = env.service.get_assignment(env.cookie, task.active["assignment_id"])
                env.service.heartbeat(task.worker["bearer"], SchedulerHeartbeatRequest(
                    **assignment_request(task.worker, row).model_dump()))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        state = tmp_path / "worker-heartbeat.json"
        marker = tmp_path / "worker-ready"
        state.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/heartbeat",
                                     "bearer": task.worker["bearer"], "marker": str(marker)}))
        state.chmod(0o600)
        script = (
            "import json,pathlib,sys,time,urllib.request;"
            "s=json.loads(pathlib.Path(sys.argv[1]).read_text());"
            "r=urllib.request.Request(s['url'],data=b'',"
            "headers={'Authorization':'Bearer '+s['bearer']});"
            "urllib.request.urlopen(r,timeout=3).read();"
            "pathlib.Path(s['marker']).write_text('ready');"
            "exec(\"while True:\\n time.sleep(.5)\\n urllib.request.urlopen(r,timeout=3).read()\")"
        )
        child = subprocess.Popen([sys.executable, "-c", script, str(state)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def execute():
            try:
                row = env.service.get_assignment(env.cookie, task.active["assignment_id"])
                result.append(env.driver.execute_preconfigured(task.worker["bearer"],
                    assignment_request(task.worker, row)))
            except Exception as error:
                errors.append(error)

        execution = threading.Thread(target=execute, daemon=True)
        try:
            wait_for(marker.exists)
            with RuntimeLeaseSupervisor(env.service, interval=0.1):
                execution.start()
                name = wait_for(
                    lambda: next(iter(names(env, task.active["assignment_id"])), None),
                )
                wait_for(lambda: docker("container", "inspect", "--format", "{{.State.Running}}",
                                        name).stdout.strip() == b"true")
                os.kill(child.pid, signal.SIGKILL)
                assert child.wait(timeout=3) == -signal.SIGKILL
                assert env.service.capacity(env.cookie)["usage"]["slots"] == 1
                wait_for(lambda: env.service.get_assignment(env.cookie,
                    task.active["assignment_id"])["state"] == "released", 38)
                execution.join(timeout=5)
                assert not execution.is_alive() and not errors
                assert result and result[0].state == "known_terminal"
                assert_absent(env, task.active["assignment_id"])
                assert env.service.capacity(env.cookie)["usage"]["slots"] == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)
