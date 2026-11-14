"""Three actual SIGKILL boundaries over the original owning Engine process."""

import json
import os
import signal
import subprocess
import sys

import pytest
from sa12_crash_actor import private_json
from sa13_runtime_support import docker
from test_super_agent_sa12_recovery import recovered_engine
from test_super_agent_sa13_recovery import reopen_driver

from mgar_v3.settings import ROOT


@pytest.mark.parametrize("point", ["input_committed", "model_dispatched", "job_running"])
def test_actual_sigkill_preserves_input_job_unknown_and_original_budget_without_resend(
    engine, tmp_path, point
):
    settings = engine.settings
    directory = tmp_path / "sa14-crash-private"
    directory.mkdir(mode=0o700)
    work = directory / "work"
    work.mkdir(mode=0o700)
    state, marker, failure = (
        directory / name for name in ["state.json", "marker.json", "failure.json"]
    )
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
            "root": str(work),
            "point": point,
            "marker": str(marker),
            "failure": str(failure),
        },
    )
    engine.close()
    child = subprocess.run(
        [sys.executable, str(ROOT / "tests/sa14_crash_actor.py"), str(state)],
        cwd=ROOT,
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src") + os.pathsep + str(ROOT / "tests")),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    assert child.returncode == -signal.SIGKILL, (
        json.loads(failure.read_text()) if failure.exists() else child.returncode
    )
    checkpoint = json.loads(marker.read_text())
    assert checkpoint["point"] == point and checkpoint["transaction_status"] == "IDLE"
    assert checkpoint["pid"] != os.getpid() and marker.stat().st_mode & 0o777 == 0o600
    recovered = recovered_engine(settings)
    try:
        assert recovered.connection.info.backend_pid != checkpoint["backend_pid"]
        recovered.source_scheduler.driver = reopen_driver(recovered, checkpoint["driver"])
        with recovered.transaction() as conn:
            before_inputs = conn.execute(
                "SELECT input_id,kind,request_sha256 FROM app.sa_async_inputs ORDER BY input_id"
            ).fetchall()
            before_jobs = conn.execute(
                "SELECT job_id,resource_operation_id FROM app.sa_async_jobs ORDER BY job_id"
            ).fetchall()
            before_budgets = conn.execute("SELECT * FROM app.budgets ORDER BY run_id").fetchall()
            before_calls = conn.execute("SELECT * FROM app.usage_calls ORDER BY call_id").fetchall()
            job_ids = conn.execute(
                "SELECT operation_id FROM app.sa_scheduler_operations WHERE kind='job' ORDER BY operation_id"
            ).fetchall()
        recovered.recover()
        recovered.source_scheduler.maintain()
        from uuid import UUID

        recovered.source_async.reconcile_jobs(UUID(checkpoint["coding_id"]))
        with recovered.transaction() as conn:
            bindings = conn.execute("SELECT * FROM app.sa_async_bindings").fetchall()
            assert len(bindings) == 1 and bindings[0]["state"] == "fenced"
            run = recovered._run(conn, bindings[0]["run_id"])
            assert run["execution_epoch"] > checkpoint["epoch"] and run["status"] != "succeeded"
            current_inputs = conn.execute(
                "SELECT input_id,kind,request_sha256 FROM app.sa_async_inputs ORDER BY input_id"
            ).fetchall()
            assert all(item in current_inputs for item in before_inputs)
            assert (
                conn.execute(
                    "SELECT job_id,resource_operation_id FROM app.sa_async_jobs ORDER BY job_id"
                ).fetchall()
                == before_jobs
            )
            assert (
                conn.execute(
                    "SELECT operation_id FROM app.sa_scheduler_operations WHERE kind='job' ORDER BY operation_id"
                ).fetchall()
                == job_ids
            )
            assert (
                conn.execute("SELECT * FROM app.budgets ORDER BY run_id").fetchall()
                == before_budgets
            )
            after_calls = conn.execute("SELECT * FROM app.usage_calls ORDER BY call_id").fetchall()
            assert len(after_calls) == len(before_calls)
            for before, after in zip(before_calls, after_calls, strict=True):
                assert (
                    before["call_id"] == after["call_id"]
                    and before["reserved_micros"] == after["reserved_micros"]
                )
                assert after["status"] == (
                    "unknown" if before["status"] == "reserved" else before["status"]
                )
            assert not conn.execute(
                "SELECT 1 FROM app.sa_coding_payloads WHERE ciphertext IS NOT NULL"
            ).fetchone()
            assert not conn.execute("SELECT 1 FROM app.actions").fetchone()
            assert not conn.execute("SELECT 1 FROM app.outbox").fetchone()
            resources = conn.execute(
                "SELECT provider_operation_id FROM app.sa_scheduler_operations WHERE kind IN ('start','job')"
            ).fetchall()
        assert recovered.source_async.runtime is None
        for resource in resources:
            assert docker("container", "inspect", resource["provider_operation_id"]).returncode != 0
    finally:
        recovered.close()
