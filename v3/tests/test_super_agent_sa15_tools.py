"""Explicit ToolContext injection, with no model activation or business mutation."""

from uuid import uuid4

import pytest
from sa15_support import path, setup, snapshot

from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    HarnessToolCall,
    TaskBinding,
    ToolContext,
    VerificationRequirement,
)
from mgar_v3.super_agent.source_recall_tools import SourceRecallTools


def adapter(env):
    with env.engine.transaction() as conn:
        assignment = env.engine.source_scheduler._assignment(conn, env.run_request.assignment_id)
    task = TaskBinding(
        task_id=str(env.run_request.assignment_id),
        thread_id=env.target.thread_id,
        run_id=env.target.run_id,
        execution_epoch=env.run_request.epoch,
        actor=env.mandate.origin.engine_actor,
        repository=env.mandate.repository.repository,
        base_commit=env.mandate.repository.base_commit,
        workspace_id=str(assignment["workspace_id"]),
        acceptance_revision=env.mandate.acceptance_sha256,
        objective="SA15 controlled readonly Recall",
        requirements=(VerificationRequirement(requirement_id="sa15"),),
    )
    grant = CapabilityGrant(
        actor=task.actor,
        repository=task.repository,
        workspace_id=task.workspace_id,
        read_paths=env.mandate.grant.read_paths,
    )
    context = ToolContext(
        task=task,
        grant=grant,
        operation_id="sa15-call",
        workspace_tree=env.mandate.repository.materialized_tree_sha256,
    )
    return SourceRecallTools(
        env.engine, env.worker["bearer"], env.run_request, task, grant
    ), context


@pytest.mark.parametrize(
    "name", ["source_recall", "source_recall_walk", "source_recall_page", "source_recall_search"]
)
def test_injected_read_tools_use_current_authority_and_no_side_effects(engine, tmp_path, name):
    env = setup(engine, tmp_path)
    tools, context = adapter(env)
    before = snapshot(engine)
    arguments = (
        {"query": "登录"}
        if name == "source_recall_search"
        else {
            "path": path(env.source_ref).model_dump(mode="json"),
        }
    )
    result = tools.execute(
        HarnessToolCall(call_id="sa15", name=name, arguments=arguments),
        context,
        CancellationToken(),
    )
    assert result.status == "ok", result
    assert snapshot(engine) == before
    assert {s.capability for s in tools.specs()} == {"read"}
    assert engine.source_coding.runtime is None


@pytest.mark.parametrize(
    "fault",
    ["actor", "epoch", "run", "workspace", "grant", "authority_arg", "unknown_tool", "cancelled"],
)
def test_model_context_or_arguments_cannot_supply_authority(engine, tmp_path, fault):
    env = setup(engine, tmp_path)
    tools, context = adapter(env)
    arguments = {"path": path(env.source_ref).model_dump(mode="json")}
    cancellation = CancellationToken()
    name = "source_recall"
    if fault in {"actor", "epoch", "run", "workspace"}:
        updates = {
            "actor": {"actor": "owner"},
            "epoch": {"execution_epoch": 999},
            "run": {"run_id": uuid4()},
            "workspace": {"workspace_id": str(uuid4())},
        }[fault]
        context = context.model_copy(update={"task": context.task.model_copy(update=updates)})
    elif fault == "grant":
        context = context.model_copy(
            update={"grant": context.grant.model_copy(update={"network": True})}
        )
    elif fault == "authority_arg":
        arguments["actor"] = "owner"
    elif fault == "unknown_tool":
        name = "execute"
    else:
        cancellation.cancel()
    before = snapshot(engine)
    result = tools.execute(
        HarnessToolCall(call_id="sa15", name=name, arguments=arguments), context, cancellation
    )
    assert result.status in {"denied", "cancelled"}
    assert result.content == "" and result.data == {}
    assert snapshot(engine) == before
