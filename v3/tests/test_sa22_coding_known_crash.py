"""Exercise a real exit after a trial's encrypted max-thinking receipt commits."""

import json
import os
import signal
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import httpx
from sa12_crash_actor import private_json
from sa22_coding_support import coding_environment
from test_super_agent_sa12_recovery import child_environment, recovered_engine
from test_super_agent_sa13_recovery import reopen_driver

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import ROOT, Settings
from mgar_v3.super_agent.source_coding_journal import SourceCodingJournal


def crash_after_known_reply(configuration):
    settings = Settings(_env_file=None, **configuration["settings"])
    engine = Engine(settings, ArtifactStore(settings.artifact_root))

    def supplier(request):
        wire = json.loads(request.content)
        assert wire["thinking"] == {"type": "enabled"} and wire["reasoning_effort"] == "max"
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "Synthetic result for a recovery check.",
                            "reasoning_content": "synthetic-private-trial-receipt",
                        },
                    }
                ],
            },
        )

    env = coding_environment(
        engine,
        Path(configuration["work"]),
        supplier_hook=supplier,
        thinking_mode="enabled",
    )
    target = env.experiment.submit(env.input_id)
    driver = env.experiment.runtime_profile.driver
    persist = SourceCodingJournal.put_payload

    def committed(journal, operation, kind, clear):
        persist(journal, operation, kind, clear)
        if kind != "model_response":
            return
        assert engine.connection.info.transaction_status.name == "IDLE"
        assert len(env.requests) == 1
        assert json.loads(clear)["reasoning_content"] == "synthetic-private-trial-receipt"
        profile = asdict(driver.profile)
        profile["grant"] = driver.profile.grant.model_dump(mode="json")
        profile["request"] = driver.profile.request.model_dump(mode="json")
        private_json(
            Path(configuration["marker"]),
            {
                "backend_pid": engine.connection.info.backend_pid,
                "operation_id": operation["operation_id"],
                "run_id": target["run_id"],
                "driver": {
                    "registry_root": str(driver.registry_root),
                    "source_root": str(driver.source_root),
                    "receipt_root": str(driver.receipt_root),
                    "image_lock": str(driver.image_lock),
                    "registration": driver.registration.model_dump(mode="json"),
                    "profile": profile,
                },
            },
        )
        os.kill(os.getpid(), signal.SIGKILL)

    SourceCodingJournal.put_payload = committed
    try:
        env.experiment.run_one()
        raise AssertionError("The committed model receipt did not reach the crash point")
    finally:
        env.close()
        engine.close()


def test_f09_max_trial_known_receipt_survives_owner_sigkill_without_resend(engine, tmp_path):
    settings = engine.settings
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    config, marker = tmp_path / "config.json", tmp_path / "marker.json"
    accepted = {
        "artifact_root",
        "project_id",
        "repository",
        "local_actor",
        "allowed_actors",
        "graph_version",
        "policy_version",
    }
    private_json(
        config,
        {
            "settings": {
                **settings.model_dump(mode="json", include=accepted),
                "database_url": settings.database_url.get_secret_value(),
                "local_token": settings.local_token.get_secret_value(),
                "allow_paid": False,
                "allow_external_write": False,
                "deepseek_api_key": None,
            },
            "work": str(work),
            "marker": str(marker),
        },
    )
    engine.close()
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(config)],
        cwd=ROOT,
        env=child_environment(),
        capture_output=True,
        text=True,
        timeout=100,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr
    observed = json.loads(marker.read_text())
    recovered = recovered_engine(settings)
    try:
        assert recovered.connection.info.backend_pid != observed["backend_pid"]
        recovered.source_scheduler.driver = reopen_driver(recovered, observed["driver"])
        with recovered.transaction() as conn:
            [bill] = conn.execute("SELECT * FROM app.usage_calls").fetchall()
            assert bill["status"] == "known" and bill["settled_micros"] > 0
            receipt = conn.execute(
                "SELECT ciphertext FROM app.sa_coding_payloads WHERE operation_id=%s "
                "AND payload_kind='model_response'",
                (observed["operation_id"],),
            ).fetchone()
            assert receipt["ciphertext"] is not None
            assert b"synthetic-private-trial-receipt" not in bytes(receipt["ciphertext"])
            prior_epoch = recovered._run(conn, observed["run_id"])["execution_epoch"]
        recovered.recover()
        recovered.source_scheduler.maintain()
        with recovered.transaction() as conn:
            run = recovered._run(conn, observed["run_id"])
            assert run["status"] == "paused" and run["execution_epoch"] > prior_epoch
            assert conn.execute("SELECT * FROM app.usage_calls").fetchall() == [bill]
            assert conn.execute(
                "SELECT state FROM app.sa_coding_operations WHERE operation_id=%s",
                (observed["operation_id"],),
            ).fetchone() == {"state": "known"}
            assert conn.execute(
                "SELECT ciphertext FROM app.sa_coding_payloads WHERE operation_id=%s "
                "AND payload_kind='model_response'",
                (observed["operation_id"],),
            ).fetchone() == {"ciphertext": None}
            assert not conn.execute("SELECT 1 FROM app.sa_coding_verifications").fetchone()
            assert not conn.execute("SELECT 1 FROM app.actions").fetchone()
    finally:
        recovered.close()


if __name__ == "__main__":
    crash_after_known_reply(json.loads(Path(sys.argv[1]).read_text()))
