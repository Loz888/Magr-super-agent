"""Real-PG rich-post control regression with exact current authority and exhausted quota."""

import json

from conftest import controlled_model_network_only as controlled_model_network_only
from conftest import database as database
from conftest import engine as engine
from conftest import settings as settings
from conftest import trigger as trigger
from sa08_support import decisions, ingest, original_state, setup
from test_super_agent_sa08 import controller


def candidate_occupancy(engine):
    with engine.transaction() as conn:
        return conn.execute(
            "SELECT * FROM app.sa_budget_quota WHERE kind='candidate' "
            "AND project_id=%s ORDER BY operation_hash",
            (engine.settings.project_id,),
        ).fetchall()


def test_current_exact_controller_post_bypasses_exhausted_candidate_quota_without_effects(
    engine, trigger,
):
    env = setup(engine, quota=1)
    task = controller(env, trigger)
    with engine.transaction() as conn:
        binding = conn.execute(
            "SELECT * FROM app.source_role_bindings WHERE binding_id=%s AND active",
            (env.binding["binding_id"],),
        ).fetchone()
        assert binding is not None and "task_controller" in binding["roles"]
        assert conn.execute(
            "SELECT 1 FROM app.source_task_controllers WHERE binding_id=%s "
            "AND ticket_id=%s AND project_id=%s AND active",
            (env.binding["binding_id"], task.ticket_id, engine.settings.project_id),
        ).fetchone() is not None
    assert "follow_up" in env.definition.admission.request_kinds
    before = original_state(engine)
    ordinary = ingest(env, entity="post-probe-ordinary", delivery="post-probe-ordinary")
    normal = next(row for row in decisions(env) if row["input_id"] == ordinary.input_ids[0])
    assert normal["disposition"] == "candidate"
    blocked = ingest(env, entity="post-probe-blocked", delivery="post-probe-blocked")
    rejected = next(row for row in decisions(env) if row["input_id"] == blocked.input_ids[0])
    assert rejected["disposition"] == "context"
    assert rejected["reason_code"] == "PROJECT_QUOTA_EXHAUSTED"
    quota_before = candidate_occupancy(engine)
    assert len(quota_before) == 1 and quota_before[0]["units"] == 1
    content = {
        "schema_version": "feishu-content-v1", "message_type": "post",
        "content": {"zh_cn": {"title": "", "content": [[
            {"tag": "at", "user_id": "Ou_SA08-Post-Probe-Bot"},
            {"tag": "text", "text": f"/mgar pause {task.ticket_id}"},
        ]]}},
    }
    receipt = ingest(
        env, entity="post-probe-control", delivery="post-probe-control",
        addressed="mention", body=json.dumps(content, ensure_ascii=False),
    )
    decision = next(row for row in decisions(env) if row["input_id"] == receipt.input_ids[0])
    # These checks run before the expected failing priority assertion, so the
    # original business and financial side-effect boundaries remain observable.
    assert original_state(engine) == before
    assert candidate_occupancy(engine) == quota_before
    assert engine.source_ingress.body(env.cookie, receipt.input_ids[0])
    assert (
        decision["disposition"], decision["priority"], decision["intended_action"],
    ) == ("control", 100, "would_pause")
