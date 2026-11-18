"""A retained task reference is fenced by a newer ingress configuration."""

import pytest
from sa15_support import path, setup

from mgar_v3.domain import DomainError
from mgar_v3.sources.ingress_domain import IngressConfigure
from mgar_v3.super_agent.source_recall_domain import RecallResolveRequest


def test_purged_task_status_loses_visibility_after_ingress_generation_changes(engine, tmp_path):
    env = setup(engine, tmp_path)
    ingress = engine.source_ingress
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.source_ingress_bodies SET expires_at=clock_timestamp()-interval '1s' "
            "WHERE input_id=%s", (env.input_id,),
        )
        control, settings = ingress._control(conn, env.input.source_instance_id)
    assert ingress.purge(env.cookie, env.input.source_instance_id)["expired_bodies"] == 1
    request = RecallResolveRequest(path=path(env.run_ref))
    assert env.recall.resolve(env.owner, request)["node"]["state"] == "running"
    ingress.configure(
        env.cookie, env.input.source_instance_id,
        IngressConfigure(
            expected_generation=control["generation"],
            settings=settings.model_copy(update={"max_pending": settings.max_pending + 1}),
            review_ref="supersede the fixture ingress configuration",
        ),
    )
    with pytest.raises(DomainError):
        env.recall.resolve(env.owner, request)
