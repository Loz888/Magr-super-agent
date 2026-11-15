"""Formal integration: original PG transactions, races, fences and unknown receipts."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from sa20_support import configured, disable, opened

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.super_agent.source_operations import OperationsPort, TickOutcome
from mgar_v3.super_agent.source_operations_domain import (
    ComponentHeartbeat,
    ComponentOpen,
    ComponentTick,
    IncidentDecision,
    OperationsSave,
    SignalSubmission,
    signals,
)


class CountingPort(OperationsPort):
    role = "worker"

    def __init__(self, engine, error=None):
        self.engine, self.calls, self.error = engine, 0, error

    def invoke(self, guard):
        guard()
        assert self.engine.connection.info.transaction_status.name == "IDLE"
        self.calls += 1
        if self.error:
            raise self.error
        return TickOutcome(processed=1)


def tick(worker, sequence=1):
    return ComponentTick(
        generation=worker.generation, boot_id=worker.boot, sequence=sequence, request_id=uuid4()
    )


def test_formal_0053_and_original_engine_are_not_a_draft_overlay(engine):
    root = Path(__file__).resolve().parents[1]
    sql = root / "migrations/0053_source_operations.sql"
    assert (
        sql.read_bytes()
        == (root / "docs/super-agent-v1/sa20-proposal/0053_source_operations.sql").read_bytes()
    )
    with engine.transaction() as conn:
        rows = conn.execute(
            "SELECT name,sha256 FROM app.schema_migrations ORDER BY name"
        ).fetchall()
        assert len(rows) == 50
        assert rows[-1]["name"] == sql.name
        assert rows[-1]["sha256"] == hashlib.sha256(sql.read_bytes()).hexdigest()
        assert engine.source_operations.engine.connection is conn
    with pytest.raises(DomainError, match="Another control"):
        Engine(engine.settings, ArtifactStore(engine.settings.artifact_root))


def test_owner_save_cas_concurrent_and_outer_rollback(engine, tmp_path):
    env = configured(engine, tmp_path)
    body = OperationsSave(expected_revision=1, definition=env.definition, review_ref="next")

    def save():
        try:
            return env.service.save(env.cookie, body)["revision"]
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(), range(2)))
    assert results.count(2) == 1 and results.count("SOURCE_OPERATIONS_REVISION_CONFLICT") == 1
    with pytest.raises(RuntimeError):
        with engine.transaction():
            env.service.save(env.cookie, body.model_copy(update={"expected_revision": 2}))
            raise RuntimeError("rollback")
    assert env.service.read(env.cookie)["revision"] == 2
    with pytest.raises(DomainError):
        env.service.save("missing-owner", body)


def test_component_generation_and_heartbeat_do_not_allow_old_process_or_replay(engine, tmp_path):
    env = configured(engine, tmp_path)
    worker = opened(env)
    beat = ComponentHeartbeat(generation=1, boot_id=worker.boot, sequence=1, state="ready")
    env.service.heartbeat(worker.id, beat)
    first = env.service.service_view(worker.id).snapshot.components[0]
    assert env.service.heartbeat(worker.id, beat)["extended"] is False
    again = env.service.service_view(worker.id).snapshot.components[0]
    assert first.expires_at == again.expires_at
    env.service.open(
        worker.id,
        ComponentOpen(
            config_revision=1, expected_generation=1, boot_id=uuid4(), request_id=uuid4()
        ),
    )
    with pytest.raises(DomainError):
        env.service.heartbeat(worker.id, beat.model_copy(update={"sequence": 2}))
    assert env.service.service_view(worker.id).snapshot.components[0].generation == 2


def test_new_configuration_fences_old_lease_until_new_generation(engine, tmp_path):
    env = configured(engine, tmp_path)
    worker = opened(env)
    env.service.save(
        env.cookie,
        OperationsSave(
            expected_revision=1, definition=env.definition, review_ref="new-configuration"
        ),
    )
    with pytest.raises(DomainError):
        env.service.tick(worker.id, tick(worker))
    view = env.service.service_view(worker.id)
    assert view.snapshot.components[0].expires_at <= view.snapshot.observed_at
    assert (
        env.service.open(
            worker.id,
            ComponentOpen(
                config_revision=2, expected_generation=1, boot_id=uuid4(), request_id=uuid4()
            ),
        )["generation"]
        == 2
    )


def test_known_tick_response_loss_reuses_result_and_no_extra_driver_call(engine, tmp_path):
    env = configured(engine, tmp_path)
    worker = opened(env)
    port = CountingPort(engine)
    env.service.bind(port)
    command = tick(worker)
    assert env.service.tick(worker.id, command)["processed"] == 1
    assert env.service.tick(worker.id, command)["reused"]
    assert port.calls == 1
    receipt = env.service.service_view(worker.id).tick
    assert receipt.state == "known" and receipt.request_id == command.request_id
    with pytest.raises(DomainError):
        env.service.tick(worker.id, command.model_copy(update={"request_id": uuid4()}))


@pytest.mark.parametrize("error", [OSError("lost reply"), DomainError("LATE", "partial work")])
def test_partial_port_failure_is_unknown_and_cannot_be_reset_or_resent(engine, tmp_path, error):
    env = configured(engine, tmp_path)
    worker = opened(env)
    port = CountingPort(engine, error)
    env.service.bind(port)
    command = tick(worker)
    assert env.service.tick(worker.id, command)["state"] == "unknown"
    assert env.service.tick(worker.id, command)["state"] == "unknown"
    with pytest.raises(DomainError):
        env.service.tick(worker.id, tick(worker, 2))
    with pytest.raises(DomainError):
        env.service.open(
            worker.id,
            ComponentOpen(
                config_revision=1, expected_generation=1, boot_id=uuid4(), request_id=uuid4()
            ),
        )
    assert port.calls == 1
    disable(env)
    assert env.service.service_view(worker.id).tick.state == "unknown"


def test_committed_disable_between_reservation_and_dispatch_blocks_io(
    engine, tmp_path, monkeypatch
):
    env = configured(engine, tmp_path)
    worker = opened(env)
    port = CountingPort(engine)
    env.service.bind(port)
    original = env.service.check_tick

    def boundary(*args):
        with engine.transaction() as conn:
            assert env.service._component(conn, worker.id)["tick_state"] == "reserved"
        disable(env)
        original(*args)

    monkeypatch.setattr(env.service, "check_tick", boundary)
    result = env.service.tick(worker.id, tick(worker))
    assert result["state"] == "known" and result["blocked"] == 1
    assert port.calls == 0


def test_engine_restart_preserves_reserved_tick_as_unknown(engine, tmp_path):
    env = configured(engine, tmp_path)
    worker = opened(env)
    request = tick(worker)
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.sa_operations_components SET tick_state='reserved',"
            "tick_sequence=1,tick_request_id=%s WHERE component_id=%s",
            (request.request_id, worker.id),
        )
    engine.close()
    recovered = Engine(env.settings, engine.artifacts)
    try:
        assert recovered.source_operations.recover() == 1
        assert recovered.source_operations.recover() == 0
        assert recovered.source_operations.service_view(worker.id).tick.state == "unknown"
    finally:
        recovered.close()


def test_watchdog_recomputes_fact_dedupes_and_can_only_record_wait_or_escalate(
    engine, tmp_path, trigger
):
    env = configured(engine, tmp_path)
    watchdog = opened(env, "watchdog")
    receipt = engine.ingest(trigger)
    # A real queued original Run without an available Runner must be visible.
    view = env.service.service_view(watchdog.id)
    fact = next(f for f in signals(view.snapshot, env.definition) if f.kind == "waiting_runner")
    request = SignalSubmission(
        kind=fact.kind,
        subject_id=fact.subject_id,
        observation_key=fact.key(env.service.project),
        snapshot_sha256=view.snapshot_sha256,
    )
    assert env.service.observe(watchdog.id, request)["saved"]
    assert env.service.observe(watchdog.id, request)["saved"]
    assert len(env.service.incidents(env.cookie)["items"]) == 1
    env.service.decide(
        env.cookie,
        request.observation_key,
        IncidentDecision(expected_revision=1, decision="wait", review_ref="no automatic retry"),
    )
    assert engine.get_run(receipt.run_id)["status"] == "queued"
    with engine.transaction() as conn:
        assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()
    forged = request.model_copy(update={"observation_key": "a" * 64})
    with pytest.raises(DomainError):
        env.service.observe(watchdog.id, forged)
    engine.cancel(receipt.run_id)
    with pytest.raises(DomainError):
        env.service.observe(watchdog.id, request)


def test_missing_worker_reports_waiting_and_heartbeats_do_not_make_semantic_progress(
    engine, tmp_path, trigger
):
    env = configured(engine, tmp_path)
    worker = opened(env)
    receipt = engine.ingest(trigger)
    before = env.service.snapshot(env.cookie)
    for sequence in (1, 2, 3):
        env.service.heartbeat(
            worker.id,
            ComponentHeartbeat(generation=1, boot_id=worker.boot, sequence=sequence, state="ready"),
        )
    after = env.service.snapshot(env.cookie)
    assert not after.runner_available
    assert after.runs[0].progress_cursor == before.runs[0].progress_cursor
    assert after.runs[0].progress_at == before.runs[0].progress_at
    result = env.service.tick(worker.id, tick(worker))
    assert result["waiting_runner"] and result["processed"] == 0
    assert engine.get_run(receipt.run_id)["status"] == "queued"
