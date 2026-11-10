from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness import (
    CONTRACT_VERSION,
    HARNESS_DOES_NOT_OWN,
    STATE_OWNERSHIP,
    BudgetLimits,
    CancellationToken,
    CapabilityGrant,
    EventEnvelope,
    HarnessRuntime,
    HarnessToolCall,
    HarnessToolRegistry,
    InMemoryBudgetLedger,
    InMemoryEventSink,
    LocalExecutionBroker,
    LocalVerifier,
    LocalWorkspace,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    SimpleContextManager,
    TaskBinding,
    UnknownModelOutcome,
    VerificationRequirement,
    canonical_hash,
    contracts,
    python_argv,
    sha256_text,
)
from mgar_v3.harness.legacy import LegacyAnalysisAdapter, LegacyRepairAdapter

ROOT = Path(__file__).resolve().parents[1]
INITIAL = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


def _task(*, requirement: str = "unit", min_tests: int = 1) -> TaskBinding:
    return TaskBinding(
        task_id="task-1",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=1,
        actor="owner",
        repository="example/project",
        base_commit="a" * 40,
        workspace_id="ws-1",
        acceptance_revision="acceptance-1",
        objective="Fix add() and prove it with the unit tests.",
        task_kind="bugfix",
        requirements=(
            VerificationRequirement(
                requirement_id=requirement,
                kind="test",
                command_contains=("unittest",),
                min_tests=min_tests,
            ),
        ),
    )


def _grant(**overrides: Any) -> CapabilityGrant:
    values: dict[str, Any] = {
        "actor": "owner",
        "workspace_id": "ws-1",
        "repository": "example/project",
        "read_paths": ("",),
        "write_paths": ("calc.py",),
        "execute": True,
        "allowed_commands": (Path(__import__("sys").executable).name,),
        "max_seconds": 60,
    }
    values.update(overrides)
    return CapabilityGrant(**values)


def _project(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text(INITIAL, encoding="utf-8")
    (root / "test_calc.py").write_text(
        "import unittest\n\n"
        "from calc import add\n\n\n"
        "class CalcTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    return sha256_text(INITIAL)


class ScriptedModel:
    def __init__(self, steps: list[Callable[[ModelRequest, CancellationToken], ModelResponse]]):
        self.steps = steps
        self.calls = 0
        self.requests: list[ModelRequest] = []

    def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        self.requests.append(request)
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        return step(request, cancellation)


def _read_step() -> Callable[[ModelRequest, CancellationToken], ModelResponse]:
    def step(request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        cancellation.ensure_active()
        return ModelResponse(
            tool_calls=(
                HarnessToolCall(
                    call_id="call-read", name="read_file", arguments={"path": "calc.py"}
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(cost_micros=5),
        )

    return step


def _edit_step(
    before_sha: str,
) -> Callable[[ModelRequest, CancellationToken], ModelResponse]:
    def step(request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        cancellation.ensure_active()
        return ModelResponse(
            tool_calls=(
                HarnessToolCall(
                    call_id="call-edit",
                    name="apply_edits",
                    arguments={
                        "edits": [
                            {
                                "path": "calc.py",
                                "expected_sha256": before_sha,
                                "content": FIXED,
                            }
                        ],
                        "expected_tree": request.workspace_tree,
                    },
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(cost_micros=5),
        )

    return step


def _test_step() -> Callable[[ModelRequest, CancellationToken], ModelResponse]:
    def step(request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        cancellation.ensure_active()
        return ModelResponse(
            tool_calls=(
                HarnessToolCall(
                    call_id="call-test",
                    name="run_tests",
                    arguments={
                        "argv": list(python_argv("-m", "unittest", "-q")),
                        "timeout_seconds": 30,
                    },
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(cost_micros=5),
        )

    return step


def _stop_step() -> Callable[[ModelRequest, CancellationToken], ModelResponse]:
    def step(request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        cancellation.ensure_active()
        return ModelResponse(usage=ModelUsage(cost_micros=5))

    return step


def _runtime(
    tmp_path: Path,
    model: Any,
    *,
    broker: Any | None = None,
    budgets: InMemoryBudgetLedger | None = None,
) -> tuple[HarnessRuntime, LocalWorkspace, InMemoryEventSink, InMemoryBudgetLedger]:
    workspace = LocalWorkspace(tmp_path / "workspace", workspace_id="ws-1")
    actual_broker = broker or LocalExecutionBroker(workspace)
    tools = HarnessToolRegistry(workspace, actual_broker)
    events = InMemoryEventSink()
    ledger = budgets or InMemoryBudgetLedger(BudgetLimits())
    runtime = HarnessRuntime(
        model=model,
        tools=tools,
        workspace=workspace,
        broker=actual_broker,
        context=SimpleContextManager(),
        verifier=LocalVerifier(),
        events=events,
        budgets=ledger,
        reservation_micros=100,
    )
    return runtime, workspace, events, ledger


def _event_kinds(events: list[EventEnvelope]) -> list[str]:
    return [event.kind for event in events]


def test_contract_manifest_is_frozen_and_matches_code() -> None:
    manifest = json.loads(
        (ROOT / "experiments/harness-v4/harness-contract-v1.json").read_text(encoding="utf-8")
    )
    assert manifest["contract_version"] == CONTRACT_VERSION
    assert manifest["status"] == "FROZEN"
    frozen = manifest["frozen_interfaces"]
    for name in (
        "TaskBinding",
        "CapabilityGrant",
        "ModelAdapter",
        "ToolRegistry",
        "WorkspaceProvider",
        "ExecutionBroker",
        "ContextManager",
        "Verifier",
        "EventSink",
        "BudgetLedger",
    ):
        assert name in frozen
        target = getattr(contracts, name)
        for method in frozen[name]:
            assert callable(getattr(target, method))
    assert manifest["compatibility"]["deepseek_harness_backend"] == "NOT_ADOPTED_H01"


def test_state_ownership_is_explicit_and_engine_owned() -> None:
    assert STATE_OWNERSHIP["session"] == "app.threads"
    assert STATE_OWNERSHIP["turn"] == "app.runs"
    assert STATE_OWNERSHIP["execution_epoch"] == "app.runs.execution_epoch"
    assert STATE_OWNERSHIP["final_completion"] == "Engine transaction plus verifier receipt"
    assert "task status" in HARNESS_DOES_NOT_OWN
    assert "publication" in HARNESS_DOES_NOT_OWN


def test_reference_loop_reads_edits_and_runs_real_tests(tmp_path: Path) -> None:
    before_sha = _project(tmp_path / "workspace")
    model = ScriptedModel([_read_step(), _edit_step(before_sha), _test_step(), _stop_step()])
    runtime, workspace, events, budgets = _runtime(tmp_path, model)
    task = _task()
    result = runtime.run(task, _grant())
    assert result.status == "verified"
    assert result.verification is not None
    assert result.verification.status == "verified"
    assert result.verification.checks[0].status == "passed"
    assert result.model_calls == 4
    assert result.tool_calls == 3
    assert (workspace.root / "calc.py").read_text(encoding="utf-8") == FIXED
    stored = events.events(task.run_id)
    kinds = _event_kinds(list(stored))
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert "verification_result" in kinds
    assert [event.cursor for event in stored] == list(range(1, len(stored) + 1))
    assert result.events_cursor == stored[-1].cursor
    snapshot = budgets.snapshot(task.task_id)
    assert snapshot.model_calls == 4
    assert snapshot.tool_calls == 3


def test_model_budget_is_reserved_before_the_call(tmp_path: Path) -> None:
    _project(tmp_path / "workspace")
    observed: list[int] = []
    ledger = InMemoryBudgetLedger(BudgetLimits(max_model_calls=2, max_tool_calls=4))
    task = _task()

    class ObservingModel:
        def complete(
            self, request: ModelRequest, cancellation: CancellationToken
        ) -> ModelResponse:
            observed.append(ledger.snapshot(task.task_id).model_calls)
            return ModelResponse(usage=ModelUsage(cost_micros=10))

    runtime, _, _, _ = _runtime(tmp_path, ObservingModel(), budgets=ledger)
    result = runtime.run(task, _grant())
    assert observed == [1]
    assert result.status == "incomplete"


def test_unknown_model_outcome_keeps_reservation_and_never_retries(tmp_path: Path) -> None:
    _project(tmp_path / "workspace")
    task = _task()

    class UnknownModel:
        def __init__(self) -> None:
            self.calls = 0

        def complete(
            self, request: ModelRequest, cancellation: CancellationToken
        ) -> ModelResponse:
            self.calls += 1
            raise UnknownModelOutcome("provider stream ended without a settlement")

    model = UnknownModel()
    runtime, _, events, budgets = _runtime(tmp_path, model)
    result = runtime.run(task, _grant())
    assert result.status == "unknown"
    assert model.calls == 1
    assert budgets.snapshot(task.task_id).cost_micros == 100
    assert "model_unknown" in _event_kinds(list(events.events(task.run_id)))


def test_tool_permission_is_checked_before_any_mutation(tmp_path: Path) -> None:
    before_sha = _project(tmp_path / "workspace")
    model = ScriptedModel([_edit_step(before_sha), _stop_step()])
    runtime, workspace, events, budgets = _runtime(tmp_path, model)
    task = _task()
    result = runtime.run(task, _grant(write_paths=()))
    assert result.status == "denied"
    assert (workspace.root / "calc.py").read_text(encoding="utf-8") == INITIAL
    assert budgets.snapshot(task.task_id).tool_calls == 0
    kinds = _event_kinds(list(events.events(task.run_id)))
    assert "tool_denied" in kinds
    assert "tool_started" not in kinds


def test_cancellation_propagates_before_the_tool_runs(tmp_path: Path) -> None:
    before_sha = _project(tmp_path / "workspace")
    task = _task()

    def cancelling_step(
        request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.cancel()
        return ModelResponse(
            tool_calls=(
                HarnessToolCall(
                    call_id="call-edit",
                    name="apply_edits",
                    arguments={
                        "edits": [
                            {
                                "path": "calc.py",
                                "expected_sha256": before_sha,
                                "content": FIXED,
                            }
                        ],
                        "expected_tree": request.workspace_tree,
                    },
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(cost_micros=5),
        )

    runtime, workspace, events, budgets = _runtime(tmp_path, ScriptedModel([cancelling_step]))
    result = runtime.run(task, _grant())
    assert result.status == "cancelled"
    assert (workspace.root / "calc.py").read_text(encoding="utf-8") == INITIAL
    assert budgets.snapshot(task.task_id).tool_calls == 0
    assert "tool_started" not in _event_kinds(list(events.events(task.run_id)))


def test_verification_requires_a_real_test_receipt(tmp_path: Path) -> None:
    _project(tmp_path / "workspace")
    runtime, _, _, _ = _runtime(tmp_path, ScriptedModel([_stop_step()]))
    result = runtime.run(_task(), _grant())
    assert result.status == "incomplete"
    assert result.verification is not None
    assert result.verification.status == "incomplete"
    assert result.verification.receipt_ids == ()


def test_executor_is_replaceable_without_changing_the_runtime(tmp_path: Path) -> None:
    before_sha = _project(tmp_path / "workspace")
    workspace = LocalWorkspace(tmp_path / "workspace", workspace_id="ws-1")

    class FakeBroker:
        def __init__(self) -> None:
            self.calls = 0

        def execute(
            self, request: Any, grant: CapabilityGrant, cancellation: CancellationToken
        ) -> Any:
            self.calls += 1
            payload = {
                "operation_id": request.operation_id,
                "status": "passed",
                "tree_hash": workspace.snapshot().tree_hash,
                "tests_run": 1,
            }
            from mgar_v3.harness import ExecutionReceipt

            return ExecutionReceipt(
                receipt_id=canonical_hash(payload),
                operation_id=request.operation_id,
                purpose="test",
                status="passed",
                argv=request.argv,
                cwd=request.cwd,
                exit_code=0,
                stdout="Ran 1 test",
                stderr="",
                duration_seconds=0.01,
                tree_hash=workspace.snapshot().tree_hash,
                log_sha256=sha256_text("Ran 1 test"),
                environment_hash=canonical_hash({}),
                tests_run=1,
                tests_failed=0,
            )

    broker = FakeBroker()
    model = ScriptedModel([_edit_step(before_sha), _test_step(), _stop_step()])
    runtime, _, _, _ = _runtime(tmp_path, model, broker=broker)
    result = runtime.run(_task(), _grant())
    assert result.status == "verified"
    assert broker.calls == 1


def test_workspace_rejects_traversal_and_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _project(root)
    workspace = LocalWorkspace(root, workspace_id="ws-1")
    with pytest.raises(DomainError) as traversal:
        workspace.read("../outside.py")
    assert traversal.value.code == "HARNESS_PATH_DENIED"
    outside = tmp_path / "outside.py"
    outside.write_text("secret", encoding="utf-8")
    (root / "link.py").symlink_to(outside)
    with pytest.raises(DomainError) as escape:
        workspace.read("link.py")
    assert escape.value.code == "HARNESS_PATH_DENIED"


def test_legacy_repair_adapter_delegates_without_new_authority() -> None:
    sentinel = object()
    calls: list[tuple[object, object]] = []

    class FakeRunner:
        def run(self, request: Any, provider: Any) -> Any:
            calls.append((request, provider))
            return sentinel

    adapter = LegacyRepairAdapter(FakeRunner())
    request = object()
    provider = object()
    assert adapter.repair(request, provider) is sentinel  # type: ignore[arg-type]
    assert calls == [(request, provider)]
    assert not hasattr(adapter, "publish")
    assert not hasattr(adapter, "approve")


def test_legacy_analysis_adapter_is_read_only() -> None:
    run_id = uuid4()
    adapter = LegacyAnalysisAdapter(
        lambda value: {"summary": "ok", "evidence_ids": ["evidence-1", 7]}
    )
    view = adapter.inspect(run_id)
    assert view.run_id == run_id
    assert view.read_only is True
    assert view.evidence_ids == ("evidence-1",)
    assert not hasattr(adapter, "apply")
    assert not hasattr(adapter, "execute")


def test_model_and_tool_context_carry_the_same_tree(tmp_path: Path) -> None:
    _project(tmp_path / "workspace")
    seen: list[str] = []

    class TreeModel:
        def complete(
            self, request: ModelRequest, cancellation: CancellationToken
        ) -> ModelResponse:
            seen.append(request.workspace_tree)
            return ModelResponse(usage=ModelUsage(cost_micros=1))

    runtime, workspace, _, _ = _runtime(tmp_path, TreeModel())
    runtime.run(_task(), _grant())
    assert seen == [workspace.snapshot().tree_hash]
