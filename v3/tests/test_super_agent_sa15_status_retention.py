"""First status-retention checks: current metadata never revives expired raw bytes."""

import pytest
from sa15_support import path, ref, setup, snapshot
from test_super_agent_sa15_relations import second_task

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_recall_domain import RecallPageRequest, RecallResolveRequest


def test_owner_still_reads_truthful_task_status_after_source_body_ttl(engine, tmp_path):
    env = setup(engine, tmp_path)
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.source_ingress_bodies SET expires_at=clock_timestamp()-interval '1s' "
            "WHERE input_id=%s",
            (env.input_id,),
        )
    before = snapshot(engine)
    result = env.recall.resolve(env.owner, RecallResolveRequest(path=path(env.run_ref)))
    assert result["node"]["state"] == "running"
    assert result["node"]["content_available"] is False
    with pytest.raises(DomainError):
        env.recall.page(env.owner, RecallPageRequest(path=env.report_path))
    assert snapshot(engine) == before


def test_worker_other_task_metadata_does_not_grant_expired_content(engine, tmp_path):
    env = setup(engine, tmp_path)
    other = second_task(env)
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.source_ingress_bodies SET expires_at=clock_timestamp()-interval '1s' "
            "WHERE input_id=%s",
            (env.extra_input_id,),
        )
        conn.execute(
            "UPDATE app.runs SET report_hash=%s WHERE run_id=%s", (env.report_hash, other.run_id)
        )
    before = snapshot(engine)
    result = env.recall.resolve(
        env.worker_caller, RecallResolveRequest(path=path(ref("run", other.run_id)))
    )
    assert result["node"]["state"] == "queued"
    with pytest.raises(DomainError):
        env.recall.page(
            env.worker_caller,
            RecallPageRequest(path=path(ref("run", other.run_id), env.artifact_ref)),
        )
    assert snapshot(engine) == before
