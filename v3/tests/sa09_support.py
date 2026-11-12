"""Signed synthetic Source fixtures; no real model, platform, worker or publication."""

from sa08_support import ingest, policy
from sa08_support import setup as fast_setup

from mgar_v3.super_agent.source_eval import SourceEval
from mgar_v3.super_agent.source_eval_domain import (
    SourceEvalConfigure,
    SourceEvalProfile,
    SourceEvalSettings,
    SourceEvaluateRequest,
    SourceProposals,
)
from mgar_v3.super_agent.source_eval_model import PROMPT_SHA256, PROMPT_VERSION


def setup(engine, *, mode="shadow", eval_settings=None):
    env = fast_setup(engine, mode=mode, fast_settings={"cooldown_seconds": 0})
    policy(env, execution={
        "enabled": True,
        "repositories": [{"repository": engine.settings.repository, "base_sha": "a" * 40,
                          "paths": ["fixtures/orders/service.py"]}],
        "capabilities": ["model_call"], "network_hosts": [],
    }, budget={
        "total_limit_micros": 10**9, "router_limit_micros": 10**9,
        "accounting_ref": "SA09-synthetic-project-ledger", "timezone": "UTC",
    })
    env.service = SourceEval(engine)
    env.eval_revision = 0
    env.eval_settings = eval_settings or SourceEvalSettings(
        enabled=True,
        profile=SourceEvalProfile(
            provider_id="fixture", profile_ref="synthetic-source-router-no-model-call",
            price={"model_id": "fixture-chat", "price_version": "fixture-v1",
                   "input_micros_per_million": 1000000, "output_micros_per_million": 1000000},
            prompt_version=PROMPT_VERSION, prompt_sha256=PROMPT_SHA256,
        ),
    )
    configure_eval(env, env.eval_settings)
    return env


def configure_eval(env, settings):
    value = env.service.configure(
        env.cookie, env.installation.scope.source_instance_id,
        SourceEvalConfigure(expected_revision=env.eval_revision, settings=settings,
                            review_ref="SA09-synthetic-current-owner"),
    )
    env.eval_revision = value["revision"]
    env.eval_settings = settings
    return value


def ingest_case(env, **changes):
    return ingest(env, **changes)


def fast_batch(env, receipt):
    with env.engine.transaction() as conn:
        row = conn.execute(
            "SELECT batch_id FROM app.sa_fast_path_decisions WHERE input_id=%s",
            (receipt.input_ids[0],),
        ).fetchone()
        assert row is not None and row["batch_id"] is not None
        return row["batch_id"]


def enqueue(env, receipt=None, *, mode="fixture"):
    receipt = receipt or ingest_case(env)
    return env.service.enqueue(
        env.cookie, env.installation.scope.source_instance_id,
        SourceEvaluateRequest(fast_batch_ids=(fast_batch(env, receipt),)), mode=mode,
    )


def prepared(env, receipt=None, *, mode="fixture"):
    queued = enqueue(env, receipt, mode=mode)
    assert queued["evaluations"][0]["status"] == "prepared", queued
    batch = env.service.prepare(mode)
    assert batch is not None
    return batch


def proposals(batch, action="create", target=None):
    reasons = {"ignore": "IRRELEVANT", "observe": "CONTEXT", "attach": "EXISTING_TASK",
               "create": "ACTIONABLE", "ask": "NEEDS_CONTEXT"}
    import json

    return SourceProposals.model_validate_json(json.dumps({
        "schema_version": "source-router-proposals-v1",
        "proposals": [{"group_id": g["group_id"], "action": action,
                       "target_id": str(target) if target else None,
                       "reason_code": reasons[action]}
                      for g in batch["source_snapshot"]["groups"]],
    }))


def apply(env, batch=None, *, action="create", target=None):
    batch = batch or prepared(env)
    decisions = env.service.wrap(batch, proposals(batch, action, target))
    env.service.save(batch["batch_id"], decisions)
    env.service.commit(batch["batch_id"], decisions)
    return batch, decisions
