"""Real local roles, process SIGKILL/restart, original queue and zero-idle work."""

import json
import signal
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from sa20_process_support import (
    api_process,
    client_file,
    environment,
    listener,
    role_process,
    wait_for,
)
from sa20_support import configured, opened

from mgar_v3.api import create_app
from mgar_v3.coordination.engine import Engine
from mgar_v3.super_agent.source_operations import OperationsPort, TickOutcome
from mgar_v3.super_agent.source_operations_domain import ComponentTick
from mgar_v3.web_access import SESSION_COOKIE


class InertAvailableWorker(OperationsPort):
    role = "worker"

    def invoke(self, guard):
        guard()
        return TickOutcome()


def test_independent_role_processes_have_no_controller_secrets_and_idle_calls_stay_zero(
    engine, tmp_path
):
    from pydantic import SecretStr

    engine.settings = engine.settings.model_copy(
        update={"redis_url": SecretStr("redis://127.0.0.1:9/0")}
    )
    env = configured(engine, tmp_path)
    with listener(create_app(env.settings, engine=engine, run_background=False)) as url:
        for role in ["scheduler", "worker", "dispatcher", "watchdog"]:
            config = client_file(env, role, url)
            with role_process(config, tmp_path / (role + ".log"), once=True) as process:
                assert process.wait(25) == 0, (tmp_path / (role + ".log")).read_text()
            assert env.tokens[role] not in (tmp_path / (role + ".log")).read_text()
        with engine.transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM app.router_calls").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 0
            assert (
                conn.execute("SELECT count(*) AS n FROM app.sa_operations_components").fetchone()[
                    "n"
                ]
                == 4
            )
        role = "worker"
        with role_process(
            client_file(env, role, url), tmp_path / "worker-environment.log"
        ) as process:
            wait_for(lambda: Path(f"/proc/{process.pid}/environ").exists())
            names = {
                p.partition(b"=")[0].decode()
                for p in Path(f"/proc/{process.pid}/environ").read_bytes().split(b"\0")
                if p
            }
            assert not any(x.startswith("MGAR") or "TOKEN" in x or "DATABASE" in x for x in names)
            assert names <= {"PATH", "LANG", "PYTHONPATH"}


def test_controller_sigkill_and_restart_keep_original_queued_input_without_double_start(
    engine, tmp_path, trigger
):
    env = configured(engine, tmp_path)
    receipt = engine.ingest(trigger)
    engine.close()
    for turn in [1, 2]:
        with api_process(env.settings, env.secrets_path, tmp_path) as (process, url):
            with httpx.Client(
                base_url=url, trust_env=False, cookies={SESSION_COOKIE: env.cookie}
            ) as browser:
                metrics = browser.get("/api/v3/source-operations/metrics")
                assert metrics.status_code == 200, metrics.text
                run = next(r for r in metrics.json()["runs"] if r["run_id"] == str(receipt.run_id))
                assert run["state"] == "queued" and not metrics.json()["runner_available"]
            with role_process(
                client_file(env, "worker", url), tmp_path / f"waiting-worker-{turn}.log", once=True
            ) as worker:
                assert worker.wait(20) == 0
            if turn == 1:
                process.send_signal(signal.SIGKILL)
                assert process.wait(10) == -signal.SIGKILL
    recovered = Engine(env.settings, engine.artifacts)
    try:
        assert recovered.get_run(receipt.run_id)["status"] == "queued"
        with recovered.transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 1
            assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()
    finally:
        recovered.close()


def test_real_sigkill_after_tick_reservation_recovers_unknown_without_reinvocation(
    engine, tmp_path
):
    env = configured(engine, tmp_path)
    worker = opened(env)
    request = ComponentTick(
        generation=worker.generation, boot_id=worker.boot, sequence=1, request_id=uuid4()
    )
    marker = tmp_path / "reserved-marker.json"
    config = tmp_path / "kill-actor.json"
    config.write_text(
        json.dumps(
            {
                "marker": str(marker),
                "component_id": str(worker.id),
                "request": request.model_dump(mode="json"),
            }
        )
    )
    engine.close()
    child_env = environment(env.settings)
    with (tmp_path / "kill-actor.log").open("wb") as log:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("sa20_kill_actor.py")), str(config)],
            env=child_env,
            cwd=tmp_path,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_for(marker.exists, 20)
            child.send_signal(signal.SIGKILL)
            assert child.wait(10) == -signal.SIGKILL
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(5)
    replacement = Engine(env.settings, engine.artifacts)
    try:
        assert replacement.source_operations.recover() == 1
        assert replacement.source_operations.service_view(worker.id).tick.state == "unknown"
        result = replacement.source_operations.tick(worker.id, request)
        assert result["state"] == "unknown" and result["reused"]
        with replacement.transaction() as conn:
            assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()
    finally:
        replacement.close()


def test_actual_worker_control_process_loss_expires_liveness_and_reports_waiting_runner(
    engine, tmp_path, trigger
):
    env = configured(engine, tmp_path, lease_seconds=10)
    env.service.bind(InertAvailableWorker())
    receipt = engine.ingest(trigger)
    with listener(create_app(env.settings, engine=engine, run_background=False)) as url:
        with role_process(client_file(env, "worker", url), tmp_path / "only-worker.log") as worker:
            wait_for(lambda: env.service.snapshot(env.cookie).runner_available, 20)
            worker.send_signal(signal.SIGKILL)
            worker.wait(10)
        snapshot = wait_for(
            lambda: v if not (v := env.service.snapshot(env.cookie)).runner_available else None, 15
        )
        from mgar_v3.super_agent.source_operations_domain import signals

        assert any(
            f.kind == "waiting_runner" and f.subject_id == receipt.run_id
            for f in signals(snapshot, env.definition)
        )
        assert engine.get_run(receipt.run_id)["status"] == "queued"
        with engine.transaction() as conn:
            assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()
