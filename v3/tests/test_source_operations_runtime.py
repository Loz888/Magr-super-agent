"""First original SA14/17 composition, signed relay and real dependency faults."""

import json
import subprocess
from uuid import uuid4

import httpx
from pydantic import SecretStr
from sa20_process_support import client_file, listener, role_process, wait_for
from sa20_support import configured, opened

from mgar_v3.api import create_app
from mgar_v3.super_agent.source_operations import OperationsPort, TickOutcome
from mgar_v3.super_agent.source_operations_domain import ComponentTick, ServiceClient, signals
from mgar_v3.super_agent.source_operations_runtime import (
    OriginalAsyncWorkerPort,
    OriginalDispatcherPort,
    OriginalSchedulerPort,
)


def request(worker, sequence=1):
    return ComponentTick(
        generation=worker.generation, boot_id=worker.boot, sequence=sequence, request_id=uuid4()
    )


def test_original_async_cloud_job_finishes_after_http_browser_and_role_client_exit(
    engine, tmp_path, monkeypatch
):
    from sa13_runtime_support import BAD, GOOD
    from sa14_support import AsyncRepair, actual

    model = AsyncRepair(gate=True)
    with actual(engine, tmp_path, monkeypatch, model=model, slow_tests=True) as coding:
        env = configured(engine, tmp_path, owner_env=coding)
        port = OriginalAsyncWorkerPort(
            engine, coding.async_runtime, coding.worker["session_id"], coding.worker["bearer"]
        )
        env.service.bind(port)
        with listener(create_app(env.settings, engine=engine, run_background=False)) as url:
            # Close the user HTTP client before the independent role begins work.
            with httpx.Client(
                base_url=url, trust_env=False, cookies={"reposage_session": env.cookie}
            ) as browser:
                assert browser.get("/api/v3/source-operations/metrics").status_code == 200
            with role_process(
                client_file(env, "worker", url), tmp_path / "cloud-worker.log", once=True
            ) as process:
                assert process.wait(30) == 0, (tmp_path / "cloud-worker.log").read_text()
            assert model.started.wait(45), coding.async_runtime.failures
            before = env.service.snapshot(env.cookie)
            model.release.set()

            def running_job():
                snapshot = env.service.snapshot(env.cookie)
                return snapshot if any(r.pending_jobs for r in snapshot.runs) else None

            observed = wait_for(running_job, 45)
            assert any(r.pending_jobs > 0 for r in observed.runs)
            assert coding.async_runtime.join(coding.mandate.coding_id, 90), (
                coding.async_runtime.failures
            )
            assert coding.async_runtime.failures == {}, coding.async_runtime.failures
            assert (coding.raw.root / "src/calc.py").read_text() == GOOD
            assert (coding.source_root / "src/calc.py").read_text() == BAD
            result = coding.async_runtime.results[coding.mandate.coding_id]
            assert result["verification"] == "verified" and not result["business_task_completed"]
            after = env.service.snapshot(env.cookie)
            assert after.runs[0].progress_cursor > before.runs[0].progress_cursor
            with engine.transaction() as conn:
                calls = conn.execute(
                    "SELECT status FROM app.usage_calls WHERE run_id=%s", (coding.target.run_id,)
                ).fetchall()
                assert len(calls) == len(model.requests) > 0 and all(
                    c["status"] == "known" for c in calls
                )
                assert not conn.execute("SELECT 1 FROM app.actions").fetchone()


def test_original_notification_unknown_creates_accurate_watchdog_fact_and_is_not_resent(
    engine, tmp_path
):
    from sa17_support import plan, setup

    notification = setup(engine, tmp_path, query="unsupported")
    notification.channel.outcome = "lost"
    planned = plan(notification)
    env = configured(engine, tmp_path, owner_env=notification)
    env.service.bind(OriginalDispatcherPort(engine))
    worker = opened(env, "dispatcher")
    first = env.service.tick(worker.id, request(worker))
    assert first["state"] == "known" and len(notification.channel.sends) == 1
    snapshot = env.service.snapshot(env.cookie)
    assert snapshot.notification_unknown == 1
    assert any(f.kind == "notification_unknown" for f in signals(snapshot, env.definition))
    env.service.tick(worker.id, request(worker, 2))
    assert len(notification.channel.sends) == 1
    with engine.transaction() as conn:
        action = engine._action(conn, planned["action_id"])[0]
        assert action["status"] == "outcome_unknown"
        assert not conn.execute(
            "SELECT 1 FROM app.web_sessions WHERE expires_at>clock_timestamp()+interval '8 days'"
        ).fetchone()


def test_separate_signed_source_relay_ack_requires_engine_commit_and_scope_then_loss_is_visible(
    engine, tmp_path
):
    from sa06_support import payload, setup, signed
    from mgar_v3.super_agent.source_operations_relay import RelayConfiguration, create_relay

    source = setup(engine)
    env = configured(
        engine,
        tmp_path,
        roles=("source", "watchdog"),
        owner_env=source,
        source_ids=(source.source_id,),
    )
    app = create_app(
        env.settings, engine=engine, run_background=False, feishu_runtime=source.runtime
    )
    with listener(app) as api:
        client = ServiceClient(
            api_url=api,
            component_id=env.ids["source"],
            role="source",
            token=env.tokens["source"],
            interval_seconds=2,
        )
        with listener(create_relay(RelayConfiguration(client=client))) as relay:
            wait_for(
                lambda: bool(env.service.service_view(env.ids["source"]).snapshot.components), 15
            )
            event = signed(source, payload(message_id="SA20-signed-through-relay"))
            with httpx.Client(base_url=relay, trust_env=False) as remote:
                result = remote.post(
                    "/callbacks/" + str(source.source_id),
                    content=event.payload,
                    headers=event.headers,
                )
                assert result.status_code == 200, result.text
                with engine.transaction() as conn:
                    rows = conn.execute(
                        "SELECT receipt_id,sealed FROM app.source_ingress_deliveries"
                    ).fetchall()
                    assert rows and all(r["sealed"] for r in rows)
                duplicate = remote.post(
                    "/callbacks/" + str(source.source_id),
                    content=event.payload,
                    headers=event.headers,
                )
                assert duplicate.status_code == 200
                denied = remote.post(
                    "/callbacks/" + str(uuid4()), content=event.payload, headers=event.headers
                )
                assert denied.status_code == 503
                corrupt = dict(event.headers)
                corrupt["X-Lark-Signature"] = "0" * 64
                assert (
                    remote.post(
                        "/callbacks/" + str(source.source_id),
                        content=event.payload,
                        headers=corrupt,
                    ).status_code
                    == 503
                )
            source.runtime.socket_lost("SA20_CONTROLLED_RUNTIME_LOST")
            snapshot = env.service.snapshot(env.cookie)
            observed = next(s for s in snapshot.sources if s.source_id == source.source_id)
            assert observed.connection == "offline" and observed.open_gaps >= 1
            kinds = {f.kind for f in signals(snapshot, env.definition)}
            assert {"source_offline", "source_gap"} <= kinds
            with engine.transaction() as conn:
                assert (
                    conn.execute(
                        "SELECT count(*) AS n FROM app.source_ingress_deliveries"
                    ).fetchone()["n"]
                    == 1
                )
                assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()


def test_credential_removed_during_reserved_tick_blocks_actual_port_dispatch(engine, tmp_path):
    from fastapi.testclient import TestClient
    from mgar_v3.super_agent.source_operations_domain import ComponentOpen

    env = configured(engine, tmp_path)
    calls = []

    class RotatedPort(OperationsPort):
        role = "worker"

        def invoke(self, guard):
            env.secrets_path.write_text('{"components":[]}')
            guard()
            calls.append("actual-write")
            return TickOutcome(processed=1)

    env.service.bind(RotatedPort())
    headers = {"X-MGAR-Operations-Token": env.tokens["worker"]}
    path = f"/api/v3/source-operations/components/{env.ids['worker']}"
    with TestClient(
        create_app(env.settings, engine=engine, run_background=False),
        base_url="http://127.0.0.1:18080",
        client=("127.0.0.1", 1234),
    ) as client:
        raw = ComponentOpen(
            config_revision=1, expected_generation=0, boot_id=uuid4(), request_id=uuid4()
        ).model_dump(mode="json")
        assert client.post(path + "/open", json=raw, headers=headers).status_code == 200
        result = client.post(
            path + "/tick",
            json={
                "generation": 1,
                "boot_id": raw["boot_id"],
                "sequence": 1,
                "request_id": str(uuid4()),
            },
            headers=headers,
        )
        assert result.status_code == 200 and result.json()["state"] == "unknown"
        assert calls == []


def test_real_redis_loss_is_observed_without_losing_or_starting_original_queue(
    engine, tmp_path, trigger
):
    name = "sa20-redis-fault-" + uuid4().hex[:12]
    image = "redis:7.4.7-bookworm@sha256:976ab0f0939b78c06d802bcd3bea44c5142ea9a756a57b861e08819817f768c8"
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "-p",
            "127.0.0.1::6379",
            image,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    stopped = False
    try:
        port = (
            subprocess.check_output(["docker", "port", name, "6379/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        from redis import Redis

        def ready():
            try:
                with Redis(host="127.0.0.1", port=int(port), socket_timeout=0.2) as redis:
                    return redis.ping()
            except OSError:
                return False

        wait_for(ready, 15)
        engine.settings = engine.settings.model_copy(
            update={"redis_url": SecretStr(f"redis://127.0.0.1:{port}/0")}
        )
        env = configured(engine, tmp_path)
        receipt = engine.ingest(trigger)
        env.service.bind(OriginalSchedulerPort(engine))
        scheduler = opened(env, "scheduler")
        assert env.service.tick(scheduler.id, request(scheduler))["state"] == "known"
        before = env.service.snapshot(env.cookie)
        assert before.redis == "ready"
        subprocess.run(
            ["docker", "stop", "--time", "2", name], capture_output=True, check=True, timeout=10
        )
        stopped = True
        assert env.service.tick(scheduler.id, request(scheduler, 2))["state"] == "known"
        after = env.service.snapshot(env.cookie)
        assert after.redis == "unavailable" and before.dependency_cursor != after.dependency_cursor
        assert any(f.kind == "dependency_unavailable" for f in signals(after, env.definition))
        assert engine.get_run(receipt.run_id)["status"] == "queued"
        with engine.transaction() as conn:
            assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()
            assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 1
    finally:
        if not stopped:
            subprocess.run(["docker", "stop", "--time", "2", name], capture_output=True, timeout=10)
