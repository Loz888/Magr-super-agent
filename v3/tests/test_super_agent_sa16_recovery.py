"""Real process death/PG rollback and restart dedup, not simulated status edits."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.fernet import Fernet

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.source_autonomy_domain import AutonomyCycleRequest
from mgar_v3.super_agent.source_autonomy_profiles import AutonomyProfile
from mgar_v3.super_agent.source_scheduler_domain import ResourceRequest


@pytest.mark.parametrize(
    "point", ["before_admission_commit", "after_admission_commit", "after_disable_commit"]
)
def test_real_sigkill_keeps_original_atomicity_claim_and_stop_fence(engine, tmp_path, point):
    settings = engine.settings
    engine.close()
    cfg = {
        "settings": {
            "database_url": settings.database_url.get_secret_value(),
            "artifact_root": str(settings.artifact_root),
            "repository": settings.repository,
            "project_id": settings.project_id,
            "local_actor": settings.local_actor,
            "allowed_actors": settings.allowed_actors,
            "allow_paid": False,
            "allow_external_write": False,
            "graph_version": "r1-quotes-v1",
        },
        "root": str(tmp_path),
        "point": point,
        "marker": str(tmp_path / "private-marker.json"),
        "source_key": Fernet.generate_key().decode(),
    }
    path = tmp_path / "private-crash-input.json"
    path.write_text(json.dumps(cfg))
    path.chmod(0o600)
    env = os.environ.copy()
    env["MGAR_V3_ALLOW_PAID"] = "0"
    env["MGAR_V3_ALLOW_EXTERNAL_WRITE"] = "0"
    app = Path(__file__).resolve().parents[1]
    process = subprocess.run(
        [sys.executable, str(app / "tests/sa16_crash_actor.py"), str(path)],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert process.returncode == -signal.SIGKILL, (process.stdout, process.stderr)
    marker = json.loads(Path(cfg["marker"]).read_text())
    assert marker["point"] == point and marker["pid"] > 0
    recovered = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        recovered.source_ingress.cipher = BodyCipher(cfg["source_key"].encode())
        recovered.source_autonomy.register_profile(
            AutonomyProfile(
                profile_sha256=digest("sa16-readonly-profile"),
                mode="readonly",
                repository=settings.repository,
                base_sha="a" * 40,
                objective="Quote the approved fixture's documented symptoms",
                budget_profile_sha256=digest("sa16-readonly-zero-payment-profile"),
                resources=ResourceRequest(cpu_millis=1000, memory_mib=256),
            )
        )
        with recovered.transaction() as conn:
            n = conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"]
            assert n == (0 if point == "before_admission_commit" else 1)
            assert (
                conn.execute("SELECT count(*) AS n FROM app.sa_autonomy_claims").fetchone()["n"]
                == n
            )
        recovered.recover()
        result = recovered.source_autonomy.cycle(marker["cookie"], AutonomyCycleRequest())
        if point == "before_admission_commit":
            assert len(result["outcomes"]) == 1 and result["outcomes"][0]["created"], result
        else:
            assert not any(x["created"] for x in result["outcomes"]), result
        with recovered.transaction() as conn:
            runs = conn.execute("SELECT * FROM app.runs").fetchall()
            assert len(runs) == 1
            assert (
                conn.execute("SELECT count(*) AS n FROM app.sa_autonomy_attempts").fetchone()["n"]
                == 1
            )
            if point == "after_disable_commit":
                assert runs[0]["status"] == "cancelled"
                assert not conn.execute(
                    "SELECT app.sa_autonomy_run_current(%s) AS ok", (runs[0]["run_id"],)
                ).fetchone()["ok"]
                assert (
                    recovered.source_autonomy._rule(conn, UUID(marker["rule_id"]))["mode"]
                    == "disabled"
                )
            assert not conn.execute(
                "SELECT 1 FROM app.usage_calls UNION ALL SELECT 1 FROM app.actions"
            ).fetchone()
    finally:
        recovered.close()
        path.unlink(missing_ok=True)
        Path(cfg["marker"]).unlink(missing_ok=True)
