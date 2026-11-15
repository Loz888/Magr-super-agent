"""Real process death at committed claim/remote acceptance; isolated PG only."""

import json
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from sa17_crash_actor import FileReceiptChannel
from sa17_support import plan, setup

from mgar_v3.coordination.engine import Engine


@pytest.mark.parametrize("point", ["after_claim", "after_remote_acceptance"])
def test_sigkill_never_resends_unknown_and_recovers_only_exact_receipt(point, engine, tmp_path):
    e = setup(engine, tmp_path)
    p = plan(e)
    before = engine.get_run(e.work.run_id)
    remote = tmp_path / "controlled-remote-receipt.json"
    config = tmp_path / "crash-input.json"
    config.write_text(
        json.dumps(
            {
                "database": engine.settings.database_url.get_secret_value(),
                "artifacts": str(engine.settings.artifact_root),
                "project": engine.settings.project_id,
                "repository": engine.settings.repository,
                "action_id": str(p["action_id"]),
                "remote": str(remote),
                "point": point,
            }
        )
    )
    engine.close()
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("sa17_crash_actor.py")), str(config)],
        capture_output=True,
        text=True,
        env={**os.environ, "MGAR_V3_ALLOW_PAID": "0", "MGAR_V3_ALLOW_EXTERNAL_WRITE": "0"},
        timeout=30,
    )
    assert completed.returncode == -signal.SIGKILL, completed.stdout + completed.stderr
    restarted = Engine(engine.settings, engine.artifacts)
    try:
        restarted.recover()
        service = restarted.source_notifications
        channel = FileReceiptChannel(restarted, remote)
        service.register_profile(replace(e.profile, channel=channel))
        assert service.get(e.cookie, p["action_id"])["status"] == "outcome_unknown"
        assert service.tick(e.cookie)["dispatched"] == []
        result = service.reconcile(e.cookie, p["action_id"])
        assert result["resolved"] == (point == "after_remote_acceptance")
        state = service.get(e.cookie, p["action_id"])
        assert state["status"] == (
            "succeeded" if point == "after_remote_acceptance" else "outcome_unknown"
        )
        assert state["receipts"][0]["outcome"] == "unknown"
        assert restarted.get_run(e.work.run_id) == before
        assert remote.exists() == (point == "after_remote_acceptance")
    finally:
        restarted.close()
