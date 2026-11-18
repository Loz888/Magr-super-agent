"""Exercise the real retention purge before resolving existing task metadata."""

import json

import pytest
from sa15_support import TEXT, path, ref, setup, snapshot

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_recall_domain import RecallPageRequest, RecallResolveRequest


def test_owner_retains_task_state_after_source_body_is_purged(engine, tmp_path):
    env = setup(engine, tmp_path)
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.source_ingress_bodies SET expires_at=clock_timestamp()-interval '1s' "
            "WHERE input_id=%s",
            (env.input_id,),
        )
    purged = engine.source_ingress.purge(env.cookie, env.input.source_instance_id)
    assert purged["expired_bodies"] == 1
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT reason_code,unavailable_at FROM app.source_ingress_inputs WHERE input_id=%s",
            (env.input_id,),
        ).fetchone()
        assert row["reason_code"] == "INGRESS_BODY_EXPIRED" and row["unavailable_at"]
        assert conn.execute(
            "SELECT 1 FROM app.source_ingress_bodies WHERE input_id=%s", (env.input_id,)
        ).fetchone() is None
    before = snapshot(engine)
    response = env.recall.resolve(env.owner, RecallResolveRequest(path=path(env.run_ref)))
    assert response["node"]["state"] == "running"
    assert response["node"]["content_available"] is False
    assert TEXT not in json.dumps(response, ensure_ascii=False)
    for excluded in (path(env.source_ref), env.report_path):
        with pytest.raises(DomainError):
            env.recall.page(env.owner, RecallPageRequest(path=excluded))
    assert snapshot(engine) == before

    # Superseding the retained head must revoke access even after its body purge.
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.source_ingress_entities SET deleted=true WHERE entity_key="
            "(SELECT entity_key FROM app.source_ingress_inputs WHERE input_id=%s)",
            (env.input_id,),
        )
    with pytest.raises(DomainError):
        env.recall.resolve(env.owner, RecallResolveRequest(path=path(ref("run", env.target.run_id))))
