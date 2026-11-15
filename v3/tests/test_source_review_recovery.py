"""Real process death and read-only recovery; no historical cases repeated."""

import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
from sa12_crash_actor import private_json
from sa13_runtime_support import docker
from sa18_runtime_support import GitRemote, Reader, Writer
from test_super_agent_sa13_recovery import reopen_driver

from mgar_v3.coordination.engine import Engine
from mgar_v3.settings import ROOT
from mgar_v3.super_agent.source_review_ports import ReviewProfile


@pytest.mark.parametrize("point", ["after_claim", "after_step_commit", "after_remote_acceptance"])
def test_actual_sigkill_preserves_unknown_and_never_replays_publication(point, engine, tmp_path):
    settings = engine.settings
    root = tmp_path / "child"
    root.mkdir()
    config = tmp_path / "input.json"
    marker = tmp_path / "marker.json"
    values = {
        k: getattr(settings, k)
        for k in (
            "project_id",
            "repository",
            "local_actor",
            "allowed_actors",
            "graph_version",
            "policy_version",
        )
    }
    values.update(
        database_url=settings.database_url.get_secret_value(),
        local_token=settings.local_token.get_secret_value(),
        artifact_root=str(settings.artifact_root),
        allow_paid=False,
        allow_external_write=False,
        deepseek_api_key=None,
    )
    private_json(
        config, {"settings": values, "root": str(root), "point": point, "marker": str(marker)}
    )
    engine.close()
    child = subprocess.run(
        [sys.executable, str(ROOT / "tests/sa18_crash_actor.py"), str(config)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(ROOT / "tests"),
            "MGAR_V3_ALLOW_PAID": "0",
            "MGAR_V3_ALLOW_EXTERNAL_WRITE": "0",
        },
    )
    assert child.returncode == -signal.SIGKILL, child.stdout + child.stderr
    state = json.loads(marker.read_text())
    assert (
        state["point"] == point
        and state["pid"] != os.getpid()
        and state["transaction_status"] == "IDLE"
    )
    assert marker.stat().st_mode & 0o777 == 0o600
    restarted = Engine(settings, engine.artifacts)
    try:
        with restarted.transaction() as conn:
            calls = conn.execute("SELECT * FROM app.usage_calls ORDER BY call_id").fetchall()
            budgets = conn.execute("SELECT * FROM app.budgets ORDER BY mandate_id").fetchall()
        restarted.recover()
        restarted.source_scheduler.driver = reopen_driver(restarted, state["driver"])
        remote = GitRemote(state["remote"], engine=restarted)

        class NoCapture:
            def capture(self, context):
                raise AssertionError("unknown only uses read transport")

        p = state["profile"]
        p["valid_until"] = datetime.fromisoformat(p["valid_until"])
        profile = ReviewProfile(
            **p, capture=NoCapture(), reader=Reader(remote), writer=Writer(remote)
        )
        service = restarted.source_reviews
        service.register_profile(profile)
        ident = UUID(state["review_id"])
        assert service.get(state["cookie"], ident)["status"] == "outcome_unknown"
        before = list(remote.writes)
        assert service.tick(state["cookie"])["actions"] == []
        result = service.reconcile(state["cookie"], ident)
        assert result["reconciled"] == (point == "after_remote_acceptance")
        assert remote.writes == before
        assert len(remote.writes) == (3 if point == "after_remote_acceptance" else 0)
        with restarted.transaction() as conn:
            assert (
                conn.execute("SELECT * FROM app.usage_calls ORDER BY call_id").fetchall() == calls
            )
            assert (
                conn.execute("SELECT * FROM app.budgets ORDER BY mandate_id").fetchall() == budgets
            )
            if point == "after_step_commit":
                steps = conn.execute("SELECT status FROM app.repair_publication_steps").fetchall()
                assert steps == [{"status": "outcome_unknown"}]
    finally:
        try:
            restarted.source_scheduler.maintain()
        finally:
            with restarted.transaction() as conn:
                ops = conn.execute(
                    "SELECT provider_operation_id,assignment_id FROM app.sa_scheduler_operations WHERE kind IN ('start','job')"
                ).fetchall()
            for op in ops:
                probe = docker("container", "inspect", op["provider_operation_id"])
                if probe.returncode == 0:
                    value = json.loads(probe.stdout)[0]
                    assert value["Config"]["Labels"]["mgar.sa12.assignment"] == str(
                        op["assignment_id"]
                    )
                    assert docker("rm", "-f", value["Id"]).returncode == 0
            restarted.close()
