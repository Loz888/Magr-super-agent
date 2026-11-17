import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from mgar_v3.settings import ROOT, Settings
from mgar_v3.store.migrate import migrate

sys.path.insert(0, str(ROOT / "scripts"))
from sa22_acceptance import inspect_journal, routing_scores, seal_record  # noqa: E402
from sa22_soak import Workload, configuration, save  # noqa: E402

IMAGE = "sha256:efad10932c12f9d6f2408fe960e9a292c4bdbac0f8365387838acbe4f0fdd2b6"


def records(seconds=120, **changes):
    result = []
    start = datetime(2026, 11, 17, tzinfo=UTC)
    for i in range(3):
        raw = {
            "UTC": (start + timedelta(seconds=i * seconds / 2)).isoformat(),
            "monotonic": 1000 + i * seconds / 2,
            "pid": 123,
            "boot_id": "controlled",
            "source_sha256": "a" * 64,
            "status": "healthy",
            "cycles": i + 1,
            "sandbox_probes": 1,
        }
        if i == 2:
            raw.update(changes)
        result.append(seal_record(raw, result))
    return result


def test_elapsed_clock_and_online_heartbeat_cannot_claim_72h():
    report = inspect_journal(records())
    assert report["continuous_segment"] == "IN_PROGRESS"
    assert report["continuous_operation_gate"].startswith("NOT_RUN")
    assert report["elapsed_seconds"] == 120


@pytest.mark.parametrize(
    "change",
    [
        {"pid": 456},
        {"boot_id": "new-host"},
        {"source_sha256": "b" * 64},
        {"monotonic": 900},
        {"UTC": "2026-11-17T01:00:00+00:00"},
    ],
)
def test_restarts_and_clock_changes_break_segment(change):
    assert inspect_journal(records(**change))["continuous_segment"] == "FAIL"


def test_wall_duration_cannot_be_compressed_or_samples_removed():
    report = inspect_journal(records(seconds=259200))
    assert report["duration_met"] is True
    assert report["continuous_segment"] == "FAIL" and report["problems"]
    sample = records()
    sample[1]["cycles"] = 999
    assert "chain:1" in inspect_journal(sample)["problems"]


def test_zero_create_precision_is_undefined_and_missing_keeps_recall_denominator():
    case = {
        "case_id": "d1",
        "actionable": True,
        "expected_action": "create",
        "repository": "example/project",
        "conversation": "topic",
        "task_ref": None,
    }
    result = routing_scores([case], [])
    assert result["create"]["precision"] is None
    assert result["actionable"] == {"correct": 0, "labelled": 1, "recall": 0}
    assert result["missing"] == ["d1"] and result["thresholds_met"] is False


def test_wrong_target_and_unauthorized_data_are_not_quality_success():
    label = {
        "case_id": "d1",
        "actionable": True,
        "expected_action": "create",
        "repository": "example/a",
        "conversation": "a",
        "task_ref": None,
        "receive_allowed": True,
        "model_input_allowed": False,
    }
    result = routing_scores(
        [label],
        [{**label, "action": "create", "repository": "example/b", "model_input_allowed": True}],
    )
    assert result["create"]["precision"] == 0 and result["actionable"]["recall"] == 0
    assert result["violations"][0]["forbidden"] == ["model_input_allowed"]


def test_duplicate_or_unregistered_predictions_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        routing_scores([], [{"case_id": "x"}, {"case_id": "x"}])
    with pytest.raises(ValueError, match="Unregistered"):
        routing_scores([], [{"case_id": "x"}])


@pytest.fixture
def soak_database():
    base = Settings().database_url.get_secret_value()
    name = "sa22_soak_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(base, dbname=name)
    migrate(dsn)
    try:
        yield SecretStr(dsn)
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def fixture_config(dsn):
    if isinstance(dsn, SecretStr):
        dsn = dsn.get_secret_value()
    return {
        "purpose": "SA22_CONTROLLED_SOAK",
        "database_url": dsn,
        "project_id": "sa22-controlled-" + uuid4().hex,
        "image_id": IMAGE,
        "expires_at": (datetime.now(UTC) + timedelta(days=4)).isoformat(),
    }


def test_config_refuses_main_or_live_database_and_public_file(tmp_path):
    path = tmp_path / "private.json"
    save(path, fixture_config("host=127.0.0.1 dbname=production"))
    with pytest.raises(ValueError, match="Dedicated"):
        configuration(path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        configuration(path)


def test_new_controlled_http_pg_cas_and_original_broker_cycle(soak_database, tmp_path):
    workload = Workload(fixture_config(soak_database), tmp_path)
    try:
        result = workload.cycle()
        probe = workload.sandbox(3)
        assert result["attempts"] == 1 and result["model_calls"] == 0
        assert len(workload.platform_requests) == 1
        assert workload.engine.artifacts.read(probe["artifact_sha256"])
        with workload.engine.transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 1
    finally:
        workload.close()


def test_real_http_accept_then_disconnect_stays_unknown_without_resend(soak_database, tmp_path):
    workload = Workload(fixture_config(soak_database), tmp_path)
    try:
        workload.lose_reply = True
        with pytest.raises(RuntimeError, match="UNKNOWN"):
            workload.cycle()
        assert len(workload.platform_requests) == 1
        with pytest.raises(RuntimeError, match="NO_RESEND"):
            workload.cycle()
        assert len(workload.platform_requests) == 1
        with workload.engine.transaction() as conn:
            assert (
                conn.execute("SELECT status FROM app.actions").fetchone()["status"]
                == "outcome_unknown"
            )
    finally:
        workload.close()


def test_real_controller_sigkill_reuses_committed_input_on_restart(soak_database, tmp_path):
    config = tmp_path / "config-private.json"
    save(config, fixture_config(soak_database))
    command = [
        sys.executable,
        str(ROOT / "scripts/sa22_soak.py"),
        "--config",
        str(config),
        "--once",
    ]
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(ROOT / "src"),
    }
    with (tmp_path / "controller.log").open("wb") as stream:
        process = subprocess.Popen(
            command + ["--crash-after-ingress"],
            env=environment,
            cwd=tmp_path,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        try:
            assert process.wait(60) == -9
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(10)
    pending = json.loads((tmp_path / "pending-cycle.json").read_text())
    assert pending["phase"] == "committed"
    with (tmp_path / "restart.log").open("wb") as stream:
        result = subprocess.run(
            command,
            env=environment,
            cwd=tmp_path,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
    assert result.returncode == 0, (tmp_path / "restart.log").read_text()
    observed = json.loads((tmp_path / "cycles.jsonl").read_text())
    assert observed["input_id"] == pending["input_id"] and observed["attempts"] == 1
    with psycopg.connect(soak_database.get_secret_value()) as conn:
        assert conn.execute("SELECT count(*) FROM app.source_ingress_inputs").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM app.actions").fetchone()[0] == 1


def test_contiguous_running_samples_report_progress_only():
    sample = records()
    assert inspect_journal(sample, minimum_seconds=120)["continuous_segment"] == "PASS"
    assert inspect_journal(sample, minimum_seconds=120)["continuous_operation_gate"].startswith(
        "NOT_RUN"
    )
