"""First missing SA22 combinations; only explicitly owned disposable resources."""

import errno
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr
from redis import Redis
from redis.exceptions import RedisError
from sa04_support import envelope
from sa04_support import setup as source_setup
from sa20_support import configured, opened
from test_source_operations_backup import setup as unknown_setup

from mgar_v3.domain import DomainError
from mgar_v3.rate_limit import acquire
from mgar_v3.settings import ROOT
from mgar_v3.super_agent.source_operations_backup import backup, unpack
from mgar_v3.super_agent.source_operations_domain import ComponentTick
from mgar_v3.super_agent.source_operations_runtime import OriginalSchedulerPort


@pytest.fixture
def owned_resources():
    path = Path(os.environ["SA22_FAULT_PRIVATE_CONFIG"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and not path.is_symlink()
    data = json.loads(path.read_text())
    assert data["purpose"] == "SA22_NEW_FAULT_EXPERIMENTS"
    assert data["image_id"].startswith("sha256:")
    for kind in ("postgres", "redis"):
        resource = data[kind]
        info = json.loads(subprocess.check_output(["docker", "inspect", resource["id"]]))[0]
        assert info["Id"] == resource["id"] and info["Name"] == "/" + resource["name"]
        assert info["Config"]["Labels"]["mgar.sa22.experiment"] == data["experiment_id"]
    yield data


def record(case_id, **facts):
    print("SA22_FAULT_OBSERVATION " + json.dumps({"case_id": case_id, **facts}), flush=True)


def test_f12_actual_enospc_keeps_cas_atomic_and_restored_unknown_on_hold(
    engine, tmp_path, trigger, owned_resources
):
    data = owned_resources
    env = unknown_setup(engine, tmp_path, trigger)
    # Larger than the independently limited tmpfs, below the original CAS byte cap.
    engine.artifacts.put(b"SA22 restore CAS ENOSPC target\n" * 50000)
    with engine.transaction() as conn:
        before = conn.execute(
            "SELECT a.status,o.attempts FROM app.actions a JOIN app.outbox o "
            "USING(action_id) WHERE a.action_id=%s", (env.action_id,),
        ).fetchone()
    assert before == {"status": "outcome_unknown", "attempts": 1}
    engine.close()
    backup(env.config, env.package)
    manifest, _ = unpack(env.package, env.config.encryption_key)
    base = env.config.database_url.get_secret_value()
    name = "sa22_enospc_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = make_conninfo(base, dbname=name)
    try:
        values = conninfo_to_dict(target)
        values.update(host=data["postgres"]["name"], port="5432")
        config = {
            "package": "/control/" + env.package.name,
            "key": "/control/" + env.config.encryption_key.name,
            "target_dsn": make_conninfo(**values), "project_id": env.config.project_id,
            "migration_sha256": manifest.migration_sha256,
        }
        path = tmp_path / "f12-restore-private.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        container = "sa22-f12-" + uuid4().hex[:12]
        command = [
            "docker", "run", "--rm", "--pull=never", "--read-only",
            "--network", data["network_name"], "--user", f"{os.getuid()}:{os.getgid()}",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
            "--memory=512m", "--cpus=1", "--name", container,
            "--label", "mgar.sa22.experiment=" + data["experiment_id"],
            "--tmpfs", "/fault:size=1048576,mode=1777", "--tmpfs", "/tmp:size=32m,mode=1777",
            "--mount", f"type=bind,source={ROOT / 'src'},target=/app/src,readonly",
            "--mount", f"type=bind,source={Path(__file__).parent},target=/app/tests,readonly",
            "--mount", f"type=bind,source={tmp_path},target=/control,readonly",
            "-e", "PYTHONPATH=/app/src", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint=/opt/sa22-python/bin/python", data["image_id"],
            "/app/tests/sa22_enospc_actor.py", "/control/" + path.name,
        ]
        done = subprocess.run(command, capture_output=True, text=True, timeout=150)
        assert done.returncode == 0, done.stdout + done.stderr
        result = json.loads(done.stdout.strip().splitlines()[-1])
        assert result["cas_errno"] == result["restore_errno"] == errno.ENOSPC
        assert result["activation_blocked"] and result["database_restored"]
        with psycopg.connect(target) as conn:
            after = conn.execute(
                "SELECT a.status,o.attempts FROM app.actions a JOIN app.outbox o "
                "USING(action_id) WHERE a.action_id=%s", (env.action_id,),
            ).fetchone()
            assert after == ("outcome_unknown", 1)
            assert conn.execute("SELECT count(*) FROM app.usage_calls").fetchone()[0] == 0
        record("F12", **{k: v for k, v in result.items() if k != "case_id"},
               unknown_action_retained=True, outbox_attempts=1, model_calls=0)
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_f02_cache_loss_and_restart_preserve_committed_source_task_and_cursors(
    engine, tmp_path, trigger, owned_resources
):
    data = owned_resources
    redis_resource = data["redis"]
    url = redis_resource["url"]
    engine.settings = engine.settings.model_copy(update={"redis_url": SecretStr(url)})
    stopped = False
    try:
        source = source_setup(engine)
        receipt = engine.ingest(trigger)
        event = envelope(source, cursor={
            "stream_id": "sa22-redis-recovery", "epoch": "F02-E1", "sequence": 1,
            "value": "sa22-committed-cursor-1",
        })
        accepted = source.gateway.receive(event)
        assert accepted.disposition == "accepted"
        env = configured(engine, tmp_path, owner_env=source)
        env.service.bind(OriginalSchedulerPort(engine))
        scheduler = opened(env, "scheduler")

        def tick(sequence):
            response = env.service.tick(scheduler.id, ComponentTick(
                generation=scheduler.generation, boot_id=scheduler.boot,
                sequence=sequence, request_id=uuid4(),
            ))
            assert response["state"] == "known"
            return env.service.snapshot(env.cookie)

        def durable():
            with engine.transaction() as conn:
                return {
                    "runs": conn.execute(
                        "SELECT run_id,status FROM app.runs ORDER BY run_id"
                    ).fetchall(),
                    "inputs": conn.execute(
                        "SELECT input_id FROM app.source_ingress_inputs ORDER BY input_id"
                    ).fetchall(),
                    "cursors": conn.execute(
                        "SELECT * FROM app.source_ingress_cursors ORDER BY stream_key"
                    ).fetchall(),
                    "marks": conn.execute(
                        "SELECT * FROM app.source_ingress_cursor_marks ORDER BY stream_key,sequence"
                    ).fetchall(),
                }

        with Redis.from_url(url) as cache:
            assert cache.ping()
            cache.set("sa22:throwaway-cache", "not-authority")
        before_health = tick(1)
        before = durable()
        assert before_health.redis == "ready"
        assert len(before["inputs"]) == len(before["cursors"]) == len(before["marks"]) == 1
        assert before["cursors"][0]["committed_sequence"] == 1
        subprocess.run(["docker", "stop", "--time=2", redis_resource["id"]],
                       check=True, capture_output=True, timeout=15)
        stopped = True
        unavailable = tick(2)
        assert unavailable.redis == "unavailable"
        with pytest.raises(DomainError, match="New external calls are paused") as error:
            acquire(url, "sa22-f02-owned-external-scope")
        assert error.value.code == "RATE_LIMIT_UNAVAILABLE"
        duplicate = source.gateway.receive(event)
        assert duplicate.disposition == "duplicate" and durable() == before
        subprocess.run(["docker", "start", redis_resource["id"]],
                       check=True, capture_output=True, timeout=15)
        stopped = False
        port = subprocess.check_output(
            ["docker", "port", redis_resource["id"], "6379/tcp"], text=True,
        ).strip().rsplit(":", 1)[1]
        recovered_url = f"redis://127.0.0.1:{port}/0"
        deadline = time.monotonic() + 15
        while True:
            try:
                with Redis.from_url(recovered_url, socket_timeout=0.2) as cache:
                    assert cache.ping()
                    assert cache.get("sa22:throwaway-cache") is None
                    break
            except (RedisError, OSError):
                assert time.monotonic() < deadline, "Owned Redis did not restart"
                time.sleep(0.1)
        engine.settings = engine.settings.model_copy(update={"redis_url": SecretStr(recovered_url)})
        recovered = tick(3)
        assert recovered.redis == "ready" and durable() == before
        assert engine.get_run(receipt.run_id)["status"] == "queued"
        assert source.gateway.receive(event).disposition == "duplicate"
        with engine.transaction() as conn:
            assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()
            assert not conn.execute("SELECT 1 FROM app.actions").fetchone()
        record("F02", redis_states=[before_health.redis, unavailable.redis, recovered.redis],
               committed_source=str(accepted.input_ids[0]), task_run_id=str(receipt.run_id),
               cache_lost=True, durable_facts_unchanged=True, committed_cursor_sequence=1,
               external_calls_paused_while_unavailable=True, notification_attempts=0, model_calls=0)
    finally:
        if stopped:
            subprocess.run(["docker", "start", redis_resource["id"]],
                           capture_output=True, timeout=15)
