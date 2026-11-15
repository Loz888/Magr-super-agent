"""New writer, competing scheduling, cancellation and project-inspection composition."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sa04_support import setup as source_setup
from sa12_support import claim, enqueue, ready, session, setup
from sa21_recovery_support import open_engine
from sa21_support import observation

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.source_scheduler_domain import SchedulerAssignmentRequest
from mgar_v3.super_agent.source_security import SourceSecurity


def test_two_scheduler_claims_share_original_writer_and_cancel_fences_winning_worker(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    queued = enqueue(env)
    workers = [session(env), session(env)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda worker: claim(env, worker), workers))
    winners = [
        (worker, result)
        for worker, result in zip(workers, results, strict=True)
        if result.get("assignment_id") is not None
    ]
    assert len(winners) == 1
    worker, claimed = winners[0]
    active = ready(env, worker, claimed)
    request = SchedulerAssignmentRequest(
        assignment_id=active["assignment_id"],
        session_id=worker["session_id"],
        epoch=active["execution_epoch"],
        expected_revision=active["revision"],
    )
    engine.cancel(queued["run_id"])
    with pytest.raises(DomainError):
        with env.service.worker_context(worker["bearer"], request):
            engine.begin_run(queued["run_id"])
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.sa_scheduler_assignments WHERE run_id=%s",
                (queued["run_id"],),
            ).fetchone()["n"]
            == 1
        )
        assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()
    observation("competing_claim_and_cancel", assignments=1, old_worker_blocked=True, model_calls=0)


def test_second_engine_cannot_become_a_security_maintenance_writer(engine):
    with pytest.raises(DomainError) as caught:
        Engine(engine.settings, ArtifactStore(engine.settings.artifact_root))
    assert caught.value.code == "ENGINE_ALREADY_RUNNING"
    assert not engine.connection.closed


def test_owner_inspection_does_not_include_a_legitimately_registered_other_project(engine):
    env = source_setup(engine)
    original_settings = engine.settings
    engine.close()
    other = open_engine(original_settings.model_copy(update={"project_id": "sa21-other-project"}))
    foreign_id = uuid4()
    try:
        installation = env.installation.model_copy(
            update={
                "scope": env.installation.scope.model_copy(
                    update={
                        "source_instance_id": foreign_id,
                        "tenant_id": "SA21-Other-Tenant",
                        "installation_id": "SA21-Other-Install",
                    }
                ),
                "project_id": "sa21-other-project",
            }
        )
        SourceRegistry(other).register_instance(installation)
    finally:
        other.close()
    current = open_engine(original_settings)
    try:
        inspection = SourceSecurity(current).inspect(env.cookie)
        ids = {r["source_id"] for r in inspection["sources"]}
        assert ids == {env.installation.scope.source_instance_id} and foreign_id not in ids
        observation("project_inspection_scope", visible_sources=1, foreign_source_excluded=True)
    finally:
        current.close()
