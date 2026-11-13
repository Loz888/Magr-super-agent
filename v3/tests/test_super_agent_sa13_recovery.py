"""Actual Engine-owner SIGKILL: durable images, partial write and known-model truth."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from sa12_crash_actor import private_json
from sa13_runtime_support import BAD, FILES, GOOD, RepairModel, docker, facts, run, runtime_env
from test_super_agent_sa12_recovery import recovered_engine

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import CapabilityGrant, ExecutionRequest
from mgar_v3.harness.workspace import Registration
from mgar_v3.settings import ROOT
from mgar_v3.super_agent.source_coding_runtime import _Attempt
from mgar_v3.super_agent.source_scheduler_runtime import (
    H03SchedulerResourceDriver,
    SchedulerRuntimeProfile,
)


def reopen_driver(engine, saved):
    profile = saved["profile"]
    profile["grant"] = CapabilityGrant.model_validate(profile["grant"])
    profile["request"] = ExecutionRequest.model_validate(profile["request"])
    return H03SchedulerResourceDriver(
        scheduler=engine.source_scheduler,
        registry_root=Path(saved["registry_root"]),
        registration=Registration.model_validate(saved["registration"]),
        source_root=Path(saved["source_root"]),
        receipt_root=Path(saved["receipt_root"]),
        image_lock=Path(saved["image_lock"]),
        profile=SchedulerRuntimeProfile(**profile),
    )


@pytest.mark.parametrize(
    "point",
    [
        "durable_images_before_write",
        "first_physical_write_before_receipt",
        "known_model_before_worker_receipt",
    ],
)
def test_real_sigkill_recovery_fences_old_epoch_without_resend_refund_or_false_completion(
    engine,
    tmp_path,
    point,
):
    settings = engine.settings
    directory = tmp_path / "crash-private"
    directory.mkdir(mode=0o700)
    actual = directory / "actual-work"
    actual.mkdir(mode=0o700)
    state, marker, failure = (
        directory / name for name in ("state.json", "marker.json", "failure.json")
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
            "root": str(actual),
            "point": point,
            "marker": str(marker),
            "failure": str(failure),
        },
    )
    engine.close()
    child = subprocess.run(
        [sys.executable, str(ROOT / "tests/sa13_crash_actor.py"), str(state)],
        cwd=ROOT,
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src") + os.pathsep + str(ROOT / "tests")),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=45,
    )
    # A missing boundary is a failing check, never a skipped crash test.
    assert child.returncode == -signal.SIGKILL, (
        json.loads(failure.read_text()) if failure.exists() else child.returncode
    )
    checkpoint = json.loads(marker.read_text())
    assert checkpoint["point"] == point and checkpoint["transaction_status"] == "IDLE"
    assert checkpoint["pid"] != os.getpid() and checkpoint["model_calls"] == 1
    assert state.stat().st_mode & 0o777 == marker.stat().st_mode & 0o777 == 0o600
    root = Path(checkpoint["workspace_root"])
    if point == "first_physical_write_before_receipt":
        assert (root / "src/calc.py").read_text() == GOOD
        assert (root / "src/other.py").read_text() == "value = 0\n"
    else:
        assert (root / "src/calc.py").read_text() == BAD
    assert (root / "tests/test_calc.py").read_text() == FILES["tests/test_calc.py"]
    recovered = recovered_engine(settings)
    try:
        assert recovered.connection.info.backend_pid != checkpoint["backend_pid"]
        recovered.source_scheduler.driver = reopen_driver(recovered, checkpoint["driver"])
        with recovered.transaction() as conn:
            before_calls = conn.execute("SELECT * FROM app.usage_calls").fetchall()
            before_ops = conn.execute(
                "SELECT * FROM app.sa_coding_operations ORDER BY operation_id"
            ).fetchall()
            before_budget = conn.execute("SELECT * FROM app.budgets").fetchall()
            assert len(before_calls) == 1 and before_calls[0]["status"] == "known"
            assert before_calls[0]["settled_micros"] == 200
            assert not conn.execute("SELECT 1 FROM app.sa_coding_verifications").fetchone()
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM app.sa_coding_payloads WHERE ciphertext IS NOT NULL"
                ).fetchone()["n"]
                > 0
            )
        recovered.recover()
        recovered.source_scheduler.maintain()
        with recovered.transaction() as conn:
            attempt = conn.execute("SELECT * FROM app.sa_coding_attempts").fetchone()
            assert attempt["state"] == "stopped" and attempt["fenced_at"] is not None
            row = recovered._run(conn, attempt["run_id"])
            assert row["status"] != "succeeded" and row["execution_epoch"] > checkpoint["epoch"]
            assert conn.execute("SELECT * FROM app.usage_calls").fetchall() == before_calls
            assert conn.execute("SELECT * FROM app.budgets").fetchall() == before_budget
            after_ops = conn.execute(
                "SELECT * FROM app.sa_coding_operations ORDER BY operation_id"
            ).fetchall()
            assert len(after_ops) == len(before_ops)
            assert all(
                after["state"] == ("known" if before["state"] == "known" else "unknown")
                for before, after in zip(before_ops, after_ops, strict=True)
            )
            assert not conn.execute(
                "SELECT 1 FROM app.sa_coding_payloads WHERE ciphertext IS NOT NULL"
            ).fetchone()
            assert not conn.execute("SELECT 1 FROM app.sa_coding_verifications").fetchone()
            assert not conn.execute("SELECT 1 FROM app.actions").fetchone()
            operations = conn.execute(
                "SELECT provider_operation_id FROM app.sa_scheduler_operations "
                "WHERE kind IN ('start','job')"
            ).fetchall()
        for op in operations:
            assert docker("container", "inspect", op["provider_operation_id"]).returncode == 1
    finally:
        recovered.close()


@pytest.mark.parametrize("foreign", [False, True])
def test_same_controller_partial_write_reconciles_only_exact_pre_and_postimages(
    engine, tmp_path, foreign
):
    class LostWorker(BaseException):
        pass

    with runtime_env(engine, tmp_path, model=RepairModel(wrong_first=False, two_files=True)) as env:

        def lost(operation_id, path):
            raise LostWorker

        env.service.runtime.write_boundary = lost
        with pytest.raises(LostWorker):
            run(env)
        assert (env.raw.root / "src/calc.py").read_text() == GOOD
        assert (env.raw.root / "src/other.py").read_text() == "value = 0\n"
        assert len(env.model.requests) == 1
        env.service.runtime.write_boundary = None
        if foreign:
            (env.raw.root / "src/other.py").write_text("foreign unreviewed content\n")
        with engine.transaction() as conn:
            attempt = env.service.begin_attempt(conn, env.worker["bearer"], env.run_request)
        session = _Attempt(
            env.service, env.runtime_profile, env.worker["bearer"], env.run_request, attempt
        )
        if foreign:
            with pytest.raises(DomainError) as rejected:
                session.file_journal.recover(session.workspace)
            assert rejected.value.code == "SOURCE_CODING_JOURNAL_CONFLICT"
            assert (env.raw.root / "src/other.py").read_text() == "foreign unreviewed content\n"
        else:
            recovered = session.file_journal.recover(session.workspace)
            assert len(recovered) == 1
            assert (env.raw.root / "src/other.py").read_text() == "value = 1\n"
            assert all(
                row["state"] == "known"
                for row in facts(env)["sa_coding_operations"]
                if row["capability"] == "write"
            )
        assert len(env.model.requests) == 1 and len(facts(env)["usage_calls"]) == 1
        assert not facts(env)["sa_coding_verifications"]
