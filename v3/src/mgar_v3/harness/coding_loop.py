"""H07 unified coding loop: investigate, edit, execute, verify and feed back.

The loop composes the frozen H01/H02 ports with the H04 execution broker, the
H05 workspace provider and the H06 command adapters.  It adds no execution
entry point of its own: every command still goes through ``ExecutionBroker``
with the same grant, epoch and budget checks.

New in H07:

* ``TaskPolicy`` selects the required acceptance evidence per task kind
  (inspect/bugfix/feature/refactor/run_app);
* ``AcceptanceChecklist`` binds the user objective and trusted requirements to
  the latest worktree, and model proposals can only add items, never remove or
  downgrade the required ones;
* a bounded progress policy distinguishes real progress (new worktree, new
  failure signature, new evidence, more passing items) from patch round-trips,
  repeated failures, empty searches and text-only rewrites;
* ``CodingResult`` binds the conclusion to the final tree hash, the real diff
  and matching receipts; a failed/timeout/unknown/budget-exhausted attempt is
  never reported as completed.
"""

from __future__ import annotations

import ast
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import Field, ValidationError

from mgar_v3.context.compactor import estimate_tokens
from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.harness.contracts import (
    BudgetLedger,
    CancellationToken,
    CapabilityGrant,
    ContextManager,
    EventDraft,
    EventEnvelope,
    EventSink,
    ExecutionBroker,
    ExecutionReceipt,
    ExecutionRequest,
    HarnessToolCall,
    HarnessToolResult,
    ModelAdapter,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TaskBinding,
    TaskKind,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    VerificationCheck,
    VerificationReceipt,
    VerificationRequest,
    VerificationRequirement,
    Verifier,
    WorkspaceDiff,
    WorkspaceProvider,
    WorkspaceSnapshot,
    canonical_hash,
    sha256_text,
)
from mgar_v3.harness.long_context import LongContextManager, WorkingState

CodingStatus = Literal[
    "completed",
    "incomplete",
    "blocked",
    "budget_exhausted",
    "cancelled",
    "unknown",
    "denied",
]
AcceptanceKind = Literal[
    "reproduction",
    "behavior",
    "regression",
    "build",
    "readiness",
    "cleanup",
    "evidence",
    "signature",
]
AcceptanceStatus = Literal["pending", "passed", "failed", "unmet"]
AcceptanceSource = Literal["user_goal", "trusted_command", "model"]
AttemptPolicy = Literal["legacy", "resource"]
FeedbackKind = Literal[
    "tool_failed",
    "tool_denied",
    "acceptance_failed",
    "repeated_failure",
    "no_progress",
    "empty_search",
    "patch_round_trip",
    "progress",
    "checkpoint",
]
NodeKind = Literal[
    "plan",
    "baseline",
    "model",
    "tool",
    "command",
    "acceptance",
    "verification",
    "feedback",
    "checkpoint",
]

_MISSING_APPLICATION = "application runtime is not configured"


class TaskPolicy(Model):
    """Per-kind acceptance and attempt policy.

    ``repair_limit`` follows the frozen TASK_TYPES.md defaults.  The legacy
    three-repair allowance stays an upper bound for grants that still use the
    old fixed permit; the resource policy may exceed it only while each repair
    round produces real progress and the budget still allows the call.
    """

    task_kind: TaskKind
    allow_write: bool
    allow_execute: bool
    require_reproduction: bool = False
    require_signature: bool = False
    require_service: bool = False
    repair_limit: int = Field(ge=1, le=12)
    legacy_repair_limit: int = Field(default=3, ge=1, le=12)


_POLICIES: dict[TaskKind, TaskPolicy] = {
    "inspect": TaskPolicy(
        task_kind="inspect",
        allow_write=False,
        allow_execute=False,
        repair_limit=1,
    ),
    "bugfix": TaskPolicy(
        task_kind="bugfix",
        allow_write=True,
        allow_execute=True,
        require_reproduction=True,
        repair_limit=8,
    ),
    "feature": TaskPolicy(
        task_kind="feature",
        allow_write=True,
        allow_execute=True,
        repair_limit=10,
    ),
    "refactor": TaskPolicy(
        task_kind="refactor",
        allow_write=True,
        allow_execute=True,
        require_signature=True,
        repair_limit=8,
    ),
    "run_app": TaskPolicy(
        task_kind="run_app",
        allow_write=True,
        allow_execute=True,
        require_service=True,
        repair_limit=10,
    ),
}


class ServicePlan(Model):
    """Trusted application start plan; the loop never invents a start command."""

    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    port: int = Field(default=8000, ge=1, le=65535)
    path: str = "/"
    timeout_seconds: float = Field(default=10, gt=0, le=300)


class ServiceReceipt(Model):
    receipt_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    argv: tuple[str, ...]
    tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    started: bool
    ready: bool
    stopped: bool
    address: str = ""
    process_id: int | None = None
    probe_detail: str = ""
    cleanup_detail: str = ""
    log_sha256: str = ""


class ApplicationPort(Protocol):
    """H06 owns the real container/app lifecycle; H07 only requires this seam."""

    def start(
        self,
        plan: ServicePlan,
        tree: str,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
    ) -> ServiceReceipt: ...

    def stop(self, receipt: ServiceReceipt) -> ServiceReceipt: ...


class AcceptanceItem(Model):
    item_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,60}$")
    description: str = Field(min_length=1, max_length=500)
    kind: AcceptanceKind
    source: AcceptanceSource
    requirement_id: str | None = None
    required: bool = True
    status: AcceptanceStatus = "pending"
    detail: str = ""
    evidence: tuple[str, ...] = ()


class AcceptanceChecklist(Model):
    task_id: str
    task_kind: TaskKind
    revision: int = Field(default=1, ge=1)
    items: tuple[AcceptanceItem, ...] = Field(min_length=1, max_length=24)

    def required_items(self) -> tuple[AcceptanceItem, ...]:
        return tuple(item for item in self.items if item.required)

    def unmet_ids(self) -> tuple[str, ...]:
        return tuple(
            item.item_id for item in self.items if item.required and item.status != "passed"
        )


class AcceptanceProposal(Model):
    item_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,60}$")
    description: str = Field(min_length=1, max_length=500)
    kind: AcceptanceKind
    requirement_id: str | None = None
    required: bool = False


class TaskPlan(Model):
    schema_version: Literal["harness-task-plan-v2"] = "harness-task-plan-v2"
    task_id: str
    objective: str
    acceptance_revision: str
    task_kind: TaskKind
    checklist: AcceptanceChecklist
    checkpoint_tree: str = Field(pattern=r"^[a-f0-9]{64}$")
    baseline_command: tuple[str, ...] = ()
    service_plan: ServicePlan | None = None
    attempt_policy: AttemptPolicy = "resource"


class LoopFeedback(Model):
    attempt: int
    node: NodeKind
    kind: FeedbackKind
    tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    signature: str = ""
    receipt_id: str = ""
    evidence: tuple[str, ...] = ()
    message: str = Field(default="", max_length=4000)


class AttemptRecord(Model):
    attempt: int
    operation_id: str
    tree_before: str
    tree_after: str
    passed_items: tuple[str, ...] = ()
    failed_items: tuple[str, ...] = ()
    failure_signature: str = ""
    progress: bool = False
    reason: str = ""


class RunNode(Model):
    position: int = Field(ge=1)
    kind: NodeKind
    attempt: int = Field(ge=0)
    operation_id: str = ""
    status: str = "ok"
    detail: str = ""
    tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: tuple[str, ...] = ()


class RunGraph(Model):
    task_id: str
    nodes: tuple[RunNode, ...] = ()


class LoopCheckpoint(Model):
    tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt: int = Field(ge=0)
    unmet_items: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    next_step: str = ""
    context_state: WorkingState | None = None
    context_ref: str | None = None


class CodingResult(Model):
    task_id: str
    task_kind: TaskKind
    status: CodingStatus
    reason: str
    summary: str
    objective: str
    acceptance_revision: str
    checklist_revision: int = Field(ge=1)
    tree_before: str
    tree_after: str
    diff: WorkspaceDiff
    receipts: tuple[ExecutionReceipt, ...] = ()
    service_receipts: tuple[ServiceReceipt, ...] = ()
    verification: VerificationReceipt | None = None
    checklist: AcceptanceChecklist
    attempts: tuple[AttemptRecord, ...] = ()
    feedback: tuple[LoopFeedback, ...] = ()
    graph: RunGraph
    checkpoint: LoopCheckpoint | None = None
    model_calls: int = 0
    tool_calls: int = 0
    cost_micros: int = 0
    events_cursor: int = 0
    context_state: WorkingState | None = None
    context_ref: str | None = None
    context_metrics: tuple[dict[str, Any], ...] = ()


@dataclass
class _Evaluation:
    tree: str
    checks: list[VerificationCheck]
    verification: VerificationReceipt
    item_status: dict[str, tuple[AcceptanceStatus, str, tuple[str, ...]]]
    failed: list[str]
    passed: list[str]
    signature: str
    missing_information: list[str]
    tests_weakened: bool


@dataclass
class _RunState:
    task: TaskBinding
    grant: CapabilityGrant
    plan: TaskPlan
    policy: TaskPolicy
    token: CancellationToken
    baseline_files: dict[str, str]
    receipts: list[ExecutionReceipt] = field(default_factory=list)
    service_receipts: list[ServiceReceipt] = field(default_factory=list)
    attempts: list[AttemptRecord] = field(default_factory=list)
    feedback: list[LoopFeedback] = field(default_factory=list)
    nodes: list[RunNode] = field(default_factory=list)
    evidence_ids: set[str] = field(default_factory=set)
    evidence_paths: set[str] = field(default_factory=set)
    empty_queries: set[str] = field(default_factory=set)
    seen_trees: set[str] = field(default_factory=set)
    seen_signatures: dict[str, int] = field(default_factory=dict)
    seen_receipts: set[tuple[str, str, str]] = field(default_factory=set)
    service_cache: dict[str, list[ServiceReceipt]] = field(default_factory=dict)
    model_calls: int = 0
    tool_calls: int = 0
    cost_micros: int = 0
    revision: int = 0
    instruction_revision: int = 0
    open_questions: list[str] = field(default_factory=list)
    evaluation: _Evaluation | None = None


class _SavedEvaluation(Model):
    tree: str
    checks: tuple[VerificationCheck, ...]
    verification: VerificationReceipt
    item_status: dict[str, tuple[AcceptanceStatus, str, tuple[str, ...]]]
    failed: tuple[str, ...]
    passed: tuple[str, ...]
    signature: str
    missing_information: tuple[str, ...]
    tests_weakened: bool

    @classmethod
    def capture(cls, value: _Evaluation | None) -> _SavedEvaluation | None:
        if value is None:
            return None
        return cls(
            tree=value.tree,
            checks=tuple(value.checks),
            verification=value.verification,
            item_status=value.item_status,
            failed=tuple(value.failed),
            passed=tuple(value.passed),
            signature=value.signature,
            missing_information=tuple(value.missing_information),
            tests_weakened=value.tests_weakened,
        )

    def restore(self) -> _Evaluation:
        return _Evaluation(
            tree=self.tree,
            checks=list(self.checks),
            verification=self.verification,
            item_status=dict(self.item_status),
            failed=list(self.failed),
            passed=list(self.passed),
            signature=self.signature,
            missing_information=list(self.missing_information),
            tests_weakened=self.tests_weakened,
        )


class _SavedRunState(Model):
    """Observed loop state only; authority and budget ledgers remain external."""

    task: TaskBinding
    grant: CapabilityGrant
    policy: TaskPolicy
    baseline_files: dict[str, str]
    receipts: tuple[ExecutionReceipt, ...]
    service_receipts: tuple[ServiceReceipt, ...]
    attempts: tuple[AttemptRecord, ...]
    feedback: tuple[LoopFeedback, ...]
    nodes: tuple[RunNode, ...]
    evidence_ids: tuple[str, ...]
    evidence_paths: tuple[str, ...]
    empty_queries: tuple[str, ...]
    seen_trees: tuple[str, ...]
    seen_signatures: dict[str, int]
    seen_receipts: tuple[tuple[str, str, str], ...]
    service_cache: dict[str, tuple[ServiceReceipt, ...]]
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    cost_micros: int = Field(ge=0)
    revision: int = Field(ge=0)
    instruction_revision: int = Field(ge=0)
    open_questions: tuple[str, ...]
    evaluation: _SavedEvaluation | None

    @classmethod
    def capture(cls, state: _RunState) -> _SavedRunState:
        return cls(
            task=state.task,
            grant=state.grant,
            policy=state.policy,
            baseline_files=state.baseline_files,
            receipts=tuple(state.receipts),
            service_receipts=tuple(state.service_receipts),
            attempts=tuple(state.attempts),
            feedback=tuple(state.feedback),
            nodes=tuple(state.nodes),
            evidence_ids=tuple(sorted(state.evidence_ids)),
            evidence_paths=tuple(sorted(state.evidence_paths)),
            empty_queries=tuple(sorted(state.empty_queries)),
            seen_trees=tuple(sorted(state.seen_trees)),
            seen_signatures=state.seen_signatures,
            seen_receipts=tuple(sorted(state.seen_receipts)),
            service_cache={key: tuple(value) for key, value in state.service_cache.items()},
            model_calls=state.model_calls,
            tool_calls=state.tool_calls,
            cost_micros=state.cost_micros,
            revision=state.revision,
            instruction_revision=state.instruction_revision,
            open_questions=tuple(state.open_questions),
            evaluation=_SavedEvaluation.capture(state.evaluation),
        )

    def restore(
        self,
        task: TaskBinding,
        grant: CapabilityGrant,
        plan: TaskPlan,
        token: CancellationToken,
    ) -> _RunState:
        # The store has fenced the recovered run. Only the epoch changes; an
        # instruction already acknowledged in this checkpoint stays applied.
        if (
            self.task.model_dump(exclude={"execution_epoch", "objective"})
            != task.model_dump(exclude={"execution_epoch", "objective"})
            or self.grant != grant
            or self.policy != _POLICIES[task.task_kind]
            or self.task.execution_epoch > task.execution_epoch
        ):
            raise DomainError(
                "HARNESS_CONTINUATION_SCOPE", "Continuation authority does not match", 409
            )
        return _RunState(
            task=self.task.model_copy(update={"execution_epoch": task.execution_epoch}),
            grant=grant,
            plan=plan,
            policy=self.policy,
            token=token,
            baseline_files=dict(self.baseline_files),
            receipts=list(self.receipts),
            service_receipts=list(self.service_receipts),
            attempts=list(self.attempts),
            feedback=list(self.feedback),
            nodes=list(self.nodes),
            evidence_ids=set(self.evidence_ids),
            evidence_paths=set(self.evidence_paths),
            empty_queries=set(self.empty_queries),
            seen_trees=set(self.seen_trees),
            seen_signatures=dict(self.seen_signatures),
            seen_receipts=set(self.seen_receipts),
            service_cache={key: list(value) for key, value in self.service_cache.items()},
            model_calls=self.model_calls,
            tool_calls=self.tool_calls,
            cost_micros=self.cost_micros,
            revision=self.revision,
            instruction_revision=self.instruction_revision,
            open_questions=list(self.open_questions),
            evaluation=self.evaluation.restore() if self.evaluation else None,
        )


class _Continuation(Model):
    schema_version: Literal["harness-loop-continuation-v1"] = "harness-loop-continuation-v1"
    phase: Literal["before_baseline", "model_request"]
    plan: TaskPlan
    state: _SavedRunState
    expected_tree: str = Field(pattern=r"^[a-f0-9]{64}$")
    history: tuple[ModelMessage, ...] = ()
    request: ModelRequest | None = None
    repair_rounds: int = Field(default=0, ge=0)
    first_failure: bool = False
    streak: int = Field(default=0, ge=0)
    last_tree: str = Field(pattern=r"^[a-f0-9]{64}$")
    last_evaluation: _SavedEvaluation | None = None
    elapsed_seconds: float = Field(default=0, ge=0)


class ContinuationPort(Protocol):
    """Engine-owned durable checkpoint, after operation/tree reconciliation.

    ``load`` supplies the reconciled ``expected_tree``. Known operations are
    replayed by their original IDs through the persistent model/tool ports;
    unknown operations retain their reservation and must never be resent.
    """

    def load(self) -> dict[str, Any] | None: ...

    def save(self, payload: dict[str, Any]) -> None: ...


def _item_id(prefix: str, requirement_id: str) -> str:
    return f"{prefix}.{sha256_text(requirement_id)[:10]}"


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith("tests/")
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def _test_strength(text: str) -> tuple[int, int] | None:
    """Count test functions and assertion sites; None when unparseable.

    Every ``assert`` statement (with or without a message) and every
    ``self.assert*(...)`` call counts as one site. Counting per file, rather
    than per function, keeps equivalent refactors neutral: moving a
    ``self.assertEqual`` into a helper that uses a bare ``assert`` keeps the
    file total unchanged, while dropping any assertion lowers it.
    """
    try:
        module = ast.parse(text)
    except SyntaxError:
        return None
    tests = 0
    assertions = 0
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith("test"):
                tests += 1
        elif isinstance(node, ast.Assert):
            assertions += 1
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr.startswith("assert"):
                assertions += 1
    return tests, assertions


def _weakened_test_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Test files whose test count or assertion count dropped."""
    weakened: list[str] = []
    for path, old_text in before.items():
        if not _is_test_path(path) or path not in after:
            continue
        old = _test_strength(old_text)
        new = _test_strength(after[path])
        if old is None or new is None:
            continue
        if new[0] < old[0] or new[1] < old[1]:
            weakened.append(path)
    return sorted(weakened)


def _argument_text(node: ast.arguments) -> str:
    names = [argument.arg for argument in node.posonlyargs]
    names.extend(argument.arg for argument in node.args)
    if node.vararg is not None:
        names.append("*" + node.vararg.arg)
    names.extend(argument.arg for argument in node.kwonlyargs)
    if node.kwarg is not None:
        names.append("**" + node.kwarg.arg)
    return ",".join(names)


def _public_signatures(files: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """Return the public API surface of Python files, for refactor checks."""
    signatures: dict[str, tuple[str, ...]] = {}
    for path, text in files.items():
        if not path.endswith(".py"):
            continue
        try:
            module = ast.parse(text)
        except SyntaxError:
            continue
        entries: list[str] = []
        for node in module.body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                bases = ",".join(
                    base.id if isinstance(base, ast.Name) else ast.unparse(base)
                    for base in node.bases
                )
                entries.append(f"class {node.name}({bases})")
                for child in node.body:
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                        if child.name.startswith("_"):
                            continue
                        entries.append(f"{node.name}.{child.name}({_argument_text(child.args)})")
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name.startswith("_"):
                    continue
                entries.append(f"{node.name}({_argument_text(node.args)})")
        signatures[path] = tuple(entries)
    return signatures


def _tool_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _path_arguments(arguments: dict[str, Any]) -> list[str] | None:
    paths: list[str] = []
    direct = arguments.get("path")
    if isinstance(direct, str):
        paths.append(direct)
    edits = arguments.get("edits")
    if edits is not None:
        if not isinstance(edits, list):
            return None
        for item in edits:
            if not isinstance(item, dict):
                return None
            path = item.get("path")
            if not isinstance(path, str):
                return None
            paths.append(path)
    return paths or None


def _human(items: Sequence[AcceptanceItem]) -> str:
    return "; ".join(f"{item.item_id} [{item.kind}] {item.description}" for item in items)


def _plan_message(
    task: TaskBinding,
    policy: TaskPolicy,
    checklist: AcceptanceChecklist,
    snapshot: WorkspaceSnapshot,
) -> ModelMessage:
    lines = [
        f"Task kind: {task.task_kind}.",
        f"Objective: {task.objective}",
        f"Acceptance revision: {task.acceptance_revision}.",
        f"Current workspace tree: {snapshot.tree_hash}.",
        f"Required acceptance items: {_human(checklist.required_items())}.",
    ]
    if policy.require_reproduction:
        lines.append(
            "This bugfix must show a failing baseline receipt on the original tree "
            "before the fix; a passing test alone is not completion."
        )
    if policy.require_signature:
        lines.append("This refactor must keep every public signature unchanged.")
    if task.task_kind == "inspect":
        lines.append("This is a read-only evidence task: cite paths you actually read.")
    return ModelMessage(role="system", content="\n".join(lines))


def _feedback_message(evaluation: _Evaluation, checklist: AcceptanceChecklist) -> ModelMessage:
    lines = [
        "The verifier rejected the current tree. Act on the failure; do not restate the plan.",
    ]
    for item in checklist.items:
        status, detail, _ = evaluation.item_status.get(item.item_id, ("unmet", "not evaluated", ()))
        if status != "passed":
            lines.append(f"- {item.item_id} [{item.kind}] {status}: {detail}")
    if evaluation.missing_information:
        lines.append("Missing information: " + "; ".join(evaluation.missing_information))
    return ModelMessage(role="user", content="\n".join(lines))


@dataclass(frozen=True)
class SafePoint:
    """Result of one cooperative safe point inside the run."""

    instructions: tuple[str, ...] = ()
    objective: str = ""
    revision: int = 0
    cancelled: bool = False


class InteractionPort(Protocol):
    """Human-in-the-loop port used by the ``ask_user`` tool."""

    def ask(self, *, question: str, task: TaskBinding, revision: int) -> str: ...


class CodingLoop:
    """One bounded coding loop that binds plan, evidence and verdict to a tree."""

    def __init__(
        self,
        *,
        model: ModelAdapter,
        tools: ToolRegistry,
        workspace: WorkspaceProvider,
        broker: ExecutionBroker,
        context: ContextManager,
        verifier: Verifier,
        events: EventSink,
        budgets: BudgetLedger,
        application: ApplicationPort | None = None,
        interaction: InteractionPort | None = None,
        continuation: ContinuationPort | None = None,
        model_id: str = "deepseek-chat",
        price_version: str = "harness-contract-v1",
        max_output_tokens: int = 2048,
        reservation_micros: int = 2000,
        max_seconds: int = 900,
    ) -> None:
        self.model = model
        self.tools = tools
        self.workspace = workspace
        self.broker = broker
        self.context = context
        self.verifier = verifier
        self.events = events
        self.budgets = budgets
        self.application = application
        self.interaction = interaction
        self.continuation = continuation
        self._loaded_continuation: _Continuation | None = None
        self._continuation_loaded = False
        self.model_id = model_id
        self.price_version = price_version
        self.max_output_tokens = max_output_tokens
        self.reservation_micros = reservation_micros
        self.max_seconds = max_seconds

    # -- planning -------------------------------------------------------

    def plan(
        self,
        task: TaskBinding,
        *,
        baseline_command: Sequence[str] = (),
        service_plan: ServicePlan | None = None,
        attempt_policy: AttemptPolicy = "resource",
    ) -> TaskPlan:
        saved = self._load_continuation()
        if saved is not None:
            if (
                saved.plan.task_id != task.task_id
                or saved.plan.task_kind != task.task_kind
                or saved.plan.objective != task.objective
                or saved.plan.acceptance_revision != task.acceptance_revision
                or saved.plan.baseline_command != tuple(baseline_command)
                or saved.plan.service_plan != service_plan
                or saved.plan.attempt_policy != attempt_policy
            ):
                raise DomainError(
                    "HARNESS_CONTINUATION_SCOPE", "Recovered plan does not match", 409
                )
            return saved.plan
        snapshot = self.workspace.snapshot()
        checklist = AcceptanceChecklist(
            task_id=task.task_id,
            task_kind=task.task_kind,
            items=self._base_items(task),
        )
        return TaskPlan(
            task_id=task.task_id,
            objective=task.objective,
            acceptance_revision=task.acceptance_revision,
            task_kind=task.task_kind,
            checklist=checklist,
            checkpoint_tree=snapshot.tree_hash,
            baseline_command=tuple(baseline_command),
            service_plan=service_plan,
            attempt_policy=attempt_policy,
        )

    def accept_proposal(
        self,
        task: TaskBinding,
        plan: TaskPlan,
        proposals: Sequence[AcceptanceProposal],
    ) -> TaskPlan:
        """Accept additive model proposals; required items and goals stay fixed."""
        trusted = {requirement.requirement_id for requirement in task.requirements}
        existing = {item.item_id for item in plan.checklist.items}
        if plan.task_id != task.task_id:
            raise DomainError("HARNESS_ACCEPTANCE_REJECTED", "Plan belongs to another task", 409)
        if plan.objective != task.objective:
            raise DomainError(
                "HARNESS_ACCEPTANCE_REJECTED", "The objective cannot be rewritten", 409
            )
        if plan.acceptance_revision != task.acceptance_revision:
            raise DomainError(
                "HARNESS_ACCEPTANCE_REJECTED",
                "The acceptance revision cannot be changed by the model",
                409,
            )
        additions: list[AcceptanceItem] = []
        for proposal in proposals:
            if proposal.item_id in existing:
                raise DomainError(
                    "HARNESS_ACCEPTANCE_REJECTED",
                    f"Proposal {proposal.item_id} would replace an existing item",
                    409,
                )
            existing.add(proposal.item_id)
            if proposal.requirement_id is not None and proposal.requirement_id not in trusted:
                raise DomainError(
                    "HARNESS_ACCEPTANCE_REJECTED",
                    f"Proposal {proposal.item_id} is not bound to a trusted command",
                    409,
                )
            if proposal.required:
                if proposal.kind not in {"behavior", "regression", "build"}:
                    raise DomainError(
                        "HARNESS_ACCEPTANCE_REJECTED",
                        f"Proposal {proposal.item_id} cannot require {proposal.kind} evidence",
                        409,
                    )
                if proposal.requirement_id is None:
                    raise DomainError(
                        "HARNESS_ACCEPTANCE_REJECTED",
                        f"Required proposal {proposal.item_id} needs a trusted requirement",
                        409,
                    )
            additions.append(
                AcceptanceItem(
                    item_id=proposal.item_id,
                    description=proposal.description,
                    kind=proposal.kind,
                    source="model",
                    requirement_id=proposal.requirement_id,
                    required=proposal.required,
                )
            )
        if len(additions) + len(plan.checklist.items) > 24:
            raise DomainError(
                "HARNESS_ACCEPTANCE_REJECTED", "Acceptance checklist is too large", 409
            )
        checklist = plan.checklist.model_copy(
            update={
                "revision": plan.checklist.revision + 1,
                "items": plan.checklist.items + tuple(additions),
            }
        )
        return plan.model_copy(update={"checklist": checklist})

    def _load_continuation(self) -> _Continuation | None:
        if not self._continuation_loaded:
            payload = self.continuation.load() if self.continuation is not None else None
            if payload is not None:
                try:
                    self._loaded_continuation = _Continuation.model_validate(payload)
                except ValidationError as exc:
                    raise DomainError(
                        "HARNESS_CONTINUATION_INVALID", "Invalid loop continuation", 409
                    ) from exc
            self._continuation_loaded = True
        return self._loaded_continuation

    def _save_continuation(
        self,
        state: _RunState,
        *,
        phase: Literal["before_baseline", "model_request"],
        history: Sequence[ModelMessage] = (),
        request: ModelRequest | None = None,
        repair_rounds: int = 0,
        first_failure: bool = False,
        streak: int = 0,
        last_tree: str | None = None,
        last_evaluation: _Evaluation | None = None,
        elapsed_seconds: float = 0,
    ) -> None:
        if self.continuation is None:
            return
        tree = self.workspace.snapshot().tree_hash
        if request is not None and request.workspace_tree != tree:
            raise DomainError(
                "HARNESS_CONTINUATION_TREE", "Tree moved before the request checkpoint", 409
            )
        saved = _Continuation(
            phase=phase,
            plan=state.plan,
            state=_SavedRunState.capture(state),
            expected_tree=tree,
            history=tuple(history),
            request=request,
            repair_rounds=repair_rounds,
            first_failure=first_failure,
            streak=streak,
            last_tree=last_tree or state.plan.checkpoint_tree,
            last_evaluation=_SavedEvaluation.capture(last_evaluation),
            elapsed_seconds=elapsed_seconds,
        )
        self.continuation.save(saved.model_dump(mode="json"))

    # -- main loop ------------------------------------------------------

    def run(
        self,
        task: TaskBinding,
        grant: CapabilityGrant,
        plan: TaskPlan,
        *,
        cancellation: CancellationToken | None = None,
        checkpoint: Callable[[str, int], SafePoint] | None = None,
    ) -> CodingResult:
        token = cancellation or CancellationToken()
        self._authorize(task, grant, plan)
        snapshot = self.workspace.snapshot()
        saved = self._load_continuation()
        if saved is not None and (saved.plan != plan or snapshot.tree_hash != saved.expected_tree):
            raise DomainError(
                "HARNESS_CONTINUATION_TREE", "Reconciled continuation tree moved", 409
            )
        if saved is None and snapshot.tree_hash != plan.checkpoint_tree:
            raise DomainError(
                "HARNESS_PLAN_STALE",
                "The workspace tree moved after the plan was bound",
                409,
            )
        state = (
            saved.state.restore(task, grant, plan, token)
            if saved
            else _RunState(
                task=task,
                grant=grant,
                plan=plan,
                policy=_POLICIES[task.task_kind],
                token=token,
                baseline_files=dict(snapshot.files),
            )
        )
        if saved is None:
            state.seen_trees.add(plan.checkpoint_tree)
            self._node(
                state,
                "plan",
                0,
                status="bound",
                detail=f"checklist revision {plan.checklist.revision}",
            )
            self._emit(
                task,
                "run_started",
                0,
                {
                    "task_kind": task.task_kind,
                    "checklist_revision": plan.checklist.revision,
                    "tree": plan.checkpoint_tree,
                },
            )
            self._emit(
                task,
                "plan_bound",
                0,
                {
                    "required": [item.item_id for item in plan.checklist.required_items()],
                    "baseline_command": list(plan.baseline_command),
                },
            )
            self._save_continuation(state, phase="before_baseline")
        if saved is None or saved.phase == "before_baseline":
            baseline = self._run_baseline(state)
            if baseline is not None:
                return baseline
        view = self.context.build(state.task, self.events.events(task.run_id))
        history: list[ModelMessage] = list(saved.history) if saved else []
        if saved is None or saved.phase == "before_baseline":
            history = list(view.messages)
            history.append(_plan_message(state.task, state.policy, plan.checklist, snapshot))
        declared = self._declared_tools()
        started = time.monotonic() - (saved.elapsed_seconds if saved else 0)
        repair_rounds = saved.repair_rounds if saved else 0
        first_failure = saved.first_failure if saved else False
        streak = saved.streak if saved else 0
        last_tree = saved.last_tree if saved else plan.checkpoint_tree
        last_evaluation = (
            saved.last_evaluation.restore() if saved and saved.last_evaluation else None
        )
        replay = saved if saved and saved.phase == "model_request" else None
        while True:
            if token.cancelled:
                return self._finish(
                    state, "cancelled", "cancelled", "Cancelled before the next step"
                )
            if replay is None:
                try:
                    point = (
                        checkpoint("before_model", state.revision)
                        if checkpoint is not None
                        else None
                    )
                except DomainError as exc:
                    if exc.code == "HARNESS_CANCELLED":
                        return self._finish(
                            state,
                            "cancelled",
                            "cancelled",
                            "Cancelled at a safe point",
                        )
                    raise
                if point is not None and point.cancelled:
                    return self._finish(
                        state, "cancelled", "cancelled", "Cancelled at a safe point"
                    )
                if point is not None and point.instructions:
                    history.extend(
                        ModelMessage(role="user", content=text) for text in point.instructions
                    )
                    state.task = state.task.model_copy(update={"objective": point.objective})
                    state.revision += len(point.instructions)
                    state.instruction_revision = max(
                        point.revision, state.instruction_revision + len(point.instructions)
                    )
                    self._emit(
                        state.task,
                        "replanned",
                        state.revision,
                        {
                            "stage": "before_model",
                            "instructions": list(point.instructions),
                            "objective": point.objective,
                        },
                    )
            if time.monotonic() - started > self.max_seconds:
                return self._finish(
                    state,
                    "budget_exhausted",
                    "wall_clock",
                    "The wall-clock budget was exhausted",
                    last_evaluation,
                )
            allowed = (
                state.policy.legacy_repair_limit
                if plan.attempt_policy == "legacy"
                else state.policy.repair_limit
            )
            if first_failure and repair_rounds > allowed:
                return self._finish(
                    state,
                    "incomplete",
                    "repair_limit",
                    f"The repair allowance ({allowed}) was exhausted",
                    last_evaluation,
                )
            replaying = replay is not None
            if replay is not None:
                if replay.request is None:
                    raise DomainError(
                        "HARNESS_CONTINUATION_INVALID", "Missing saved model request", 409
                    )
                request = replay.request
                if (
                    request.task.model_dump(exclude={"execution_epoch"})
                    != state.task.model_dump(exclude={"execution_epoch"})
                    or request.operation_id
                    != f"{task.task_id[:60]}:model:{len(state.attempts) + 1}"
                    or request.model_id != self.model_id
                    or request.price_version != self.price_version
                    or request.tools != tuple(declared.values())
                    or request.max_output_tokens != self.max_output_tokens
                    or request.reservation_micros != self.reservation_micros
                    or request.messages != tuple(history)
                ):
                    raise DomainError(
                        "HARNESS_CONTINUATION_SCOPE", "Saved model request does not match", 409
                    )
                request = request.model_copy(update={"task": state.task})
                attempt = len(state.attempts) + 1
                operation_id = request.operation_id
                replay = None
            else:
                if isinstance(self.context, LongContextManager):
                    try:
                        overhead = {
                            "task": state.task.model_dump(mode="json"),
                            "tools": [spec.model_dump(mode="json") for spec in declared.values()],
                            "model_id": self.model_id,
                            "max_output_tokens": self.max_output_tokens,
                        }
                        view = self.context.prepare(
                            state.task,
                            self.events.events(state.task.run_id),
                            history,
                            self._working_state(state),
                            overhead_tokens=estimate_tokens(overhead),
                            overhead_bytes=len(json.dumps(overhead, ensure_ascii=False).encode()),
                        )
                        history = list(view.messages)
                    except DomainError as exc:
                        return self._finish(
                            state,
                            "blocked",
                            exc.code,
                            str(exc),
                            last_evaluation,
                            persist_context=False,
                        )
                state.revision += 1
                attempt = len(state.attempts) + 1
                operation_id = f"{task.task_id[:60]}:model:{attempt}"
                request = ModelRequest(
                    operation_id=operation_id,
                    task=state.task,
                    messages=tuple(history),
                    tools=tuple(declared.values()),
                    model_id=self.model_id,
                    price_version=self.price_version,
                    max_output_tokens=self.max_output_tokens,
                    reservation_micros=self.reservation_micros,
                    context_revision=view.revision
                    if isinstance(self.context, LongContextManager)
                    else f"{view.revision}:{plan.checklist.revision}",
                    workspace_tree=self.workspace.snapshot().tree_hash,
                )
                self._save_continuation(
                    state,
                    phase="model_request",
                    history=history,
                    request=request,
                    repair_rounds=repair_rounds,
                    first_failure=first_failure,
                    streak=streak,
                    last_tree=last_tree,
                    last_evaluation=last_evaluation,
                    elapsed_seconds=time.monotonic() - started,
                )
            try:
                reservation = self.budgets.reserve(
                    task.task_id,
                    operation_id,
                    "model",
                    reserved_micros=self.reservation_micros,
                    reserved_units=1,
                )
            except DomainError as exc:
                if exc.code == "BUDGET_EXCEEDED":
                    return self._finish(
                        state,
                        "budget_exhausted",
                        "budget_model_calls",
                        "The model-call budget was exhausted",
                        last_evaluation,
                    )
                raise
            self._node(state, "model", attempt, operation_id=operation_id, status="requested")
            self._emit(task, "model_requested", state.revision, {"operation_id": operation_id})
            tree_before_call = request.workspace_tree
            try:
                response = self.model.complete(request, token)
            except DomainError as exc:
                if exc.code == "HARNESS_MODEL_UNKNOWN":
                    self.budgets.mark_unknown(reservation.reservation_id, str(exc))
                    return self._finish(
                        state,
                        "unknown",
                        "model_unknown",
                        "The model outcome is unknown",
                        last_evaluation,
                    )
                self.budgets.release(reservation.reservation_id)
                if exc.code == "HARNESS_CANCELLED":
                    return self._finish(
                        state, "cancelled", "cancelled", "Cancelled during the model step"
                    )
                raise
            if not response.usage.known:
                self.budgets.mark_unknown(reservation.reservation_id, "Model usage is unavailable")
                return self._finish(
                    state,
                    "unknown",
                    "model_usage_unknown",
                    "Model usage is unavailable; the reservation was retained",
                    last_evaluation,
                )
            self.budgets.settle(
                reservation.reservation_id,
                actual_micros=response.usage.cost_micros,
                actual_units=1,
            )
            state.model_calls += 1
            state.cost_micros += response.usage.cost_micros
            self._node(
                state,
                "model",
                attempt,
                operation_id=operation_id,
                status=response.finish_reason,
                detail=f"{len(response.tool_calls)} tool calls",
            )
            self._emit(
                task,
                "model_completed",
                state.revision,
                {
                    "finish_reason": response.finish_reason,
                    "tool_calls": len(response.tool_calls),
                    "cost_micros": response.usage.cost_micros,
                },
            )
            if token.cancelled or response.finish_reason == "cancelled":
                return self._finish(
                    state, "cancelled", "cancelled", "Cancelled during the model step"
                )
            if checkpoint is not None:
                try:
                    point = checkpoint("after_model", state.revision)
                except DomainError as exc:
                    if exc.code == "HARNESS_CANCELLED":
                        return self._finish(
                            state,
                            "cancelled",
                            "cancelled",
                            "Cancelled at a safe point",
                        )
                    raise
                if point.cancelled:
                    return self._finish(
                        state, "cancelled", "cancelled", "Cancelled at a safe point"
                    )
                if point.instructions:
                    history.extend(
                        ModelMessage(role="user", content=text) for text in point.instructions
                    )
                    state.task = state.task.model_copy(update={"objective": point.objective})
                    state.revision += len(point.instructions)
                    state.instruction_revision = max(
                        point.revision, state.instruction_revision + len(point.instructions)
                    )
                    state.attempts.append(
                        AttemptRecord(
                            attempt=attempt,
                            operation_id=operation_id,
                            tree_before=tree_before_call,
                            tree_after=self.workspace.snapshot().tree_hash,
                            reason="stale_instruction",
                        )
                    )
                    self._emit(
                        state.task,
                        "model_discarded_stale",
                        state.revision,
                        {
                            "operation_id": operation_id,
                            "discarded_tool_calls": len(response.tool_calls),
                            "discarded_text_bytes": len(response.text.encode("utf-8")),
                            "reason": "instruction_applied_at_safe_point",
                        },
                    )
                    self._emit(
                        state.task,
                        "replanned",
                        state.revision,
                        {
                            "stage": "after_model",
                            "instructions": list(point.instructions),
                            "objective": point.objective,
                        },
                    )
                    continue
            if response.finish_reason in {"length", "error"}:
                return self._finish(
                    state,
                    "incomplete",
                    f"model_{response.finish_reason}",
                    "The model response was truncated or failed",
                    last_evaluation,
                )
            if self.workspace.snapshot().tree_hash != (
                saved.expected_tree if replaying and saved else tree_before_call
            ):
                # As with H09 inbox changes, discard the complete response.
                # Neither tool calls nor prose from an old tree may take effect.
                state.attempts.append(
                    AttemptRecord(
                        attempt=attempt,
                        operation_id=operation_id,
                        tree_before=tree_before_call,
                        tree_after=self.workspace.snapshot().tree_hash,
                        reason="stale_tree",
                    )
                )
                self._emit(
                    state.task,
                    "model_discarded_stale",
                    state.revision,
                    {"operation_id": operation_id, "reason": "workspace_tree_changed"},
                )
                continue
            if response.tool_calls:
                if response.finish_reason != "tool_calls":
                    return self._finish(
                        state,
                        "incomplete",
                        "tool_calls_without_finish",
                        "Tool calls arrived without an authoritative finish signal",
                        last_evaluation,
                    )
                outcome = self._dispatch_tools(state, response, attempt, history, declared)
                if outcome is not None:
                    return outcome
                state.attempts.append(
                    AttemptRecord(
                        attempt=attempt,
                        operation_id=operation_id,
                        tree_before=tree_before_call,
                        tree_after=self.workspace.snapshot().tree_hash,
                        reason="tool_calls",
                    )
                )
                continue
            history.append(ModelMessage(role="assistant", content=response.text))
            checklist = plan.checklist
            evaluation = self._evaluate(state, response.text)
            state.evaluation = evaluation
            last_evaluation = evaluation
            state.revision += 1
            self._emit(
                task,
                "acceptance_evaluated",
                state.revision,
                {
                    "passed": evaluation.passed,
                    "failed": evaluation.failed,
                    "signature": evaluation.signature,
                    "tree": evaluation.tree,
                },
            )
            for check in evaluation.checks:
                self._node(
                    state,
                    "acceptance",
                    attempt,
                    operation_id=operation_id,
                    status=check.status,
                    detail=f"{check.requirement_id}: {check.detail}",
                )
            self._emit(
                task,
                "verification_result",
                state.revision,
                {
                    "status": evaluation.verification.status,
                    "checks": [check.model_dump(mode="json") for check in evaluation.checks],
                    "tree": evaluation.tree,
                },
            )
            passed, failed = evaluation.passed, evaluation.failed
            if evaluation.verification.status == "verified" and not failed:
                state.attempts.append(
                    AttemptRecord(
                        attempt=attempt,
                        operation_id=operation_id,
                        tree_before=tree_before_call,
                        tree_after=evaluation.tree,
                        passed_items=tuple(passed),
                        progress=True,
                        reason="verified",
                    )
                )
                self._node(
                    state,
                    "verification",
                    attempt,
                    status="verified",
                    detail=evaluation.verification.verification_id,
                    evidence=(evaluation.verification.verification_id,),
                )
                return self._finish(
                    state,
                    "completed",
                    "verified",
                    "The verifier accepted the final workspace tree",
                    evaluation,
                )
            if not first_failure:
                first_failure = True
            repair_rounds += 1
            progress, reasons = self._progress(state, last_tree, evaluation)
            signature = evaluation.signature
            self._feedback(
                state,
                attempt,
                "progress" if progress else "no_progress",
                signature,
                detail="; ".join(reasons),
                tree=evaluation.tree,
                evidence=tuple(evaluation.verification.receipt_ids),
            )
            state.seen_signatures[signature] = state.seen_signatures.get(signature, 0) + 1
            if progress:
                streak = 0
            else:
                streak += 1
            if not progress and streak >= 2:
                self._feedback(
                    state,
                    attempt,
                    "no_progress",
                    signature,
                    detail="two consecutive repair rounds made no progress",
                    tree=evaluation.tree,
                )
                state.attempts.append(
                    AttemptRecord(
                        attempt=attempt,
                        operation_id=operation_id,
                        tree_before=tree_before_call,
                        tree_after=evaluation.tree,
                        passed_items=tuple(passed),
                        failed_items=tuple(failed),
                        failure_signature=signature,
                        progress=False,
                        reason="no_progress",
                    )
                )
                return self._finish(
                    state,
                    "blocked",
                    "no_progress",
                    "No substantive progress across repair rounds",
                    evaluation,
                )
            state.attempts.append(
                AttemptRecord(
                    attempt=attempt,
                    operation_id=operation_id,
                    tree_before=tree_before_call,
                    tree_after=evaluation.tree,
                    passed_items=tuple(passed),
                    failed_items=tuple(failed),
                    failure_signature=signature,
                    progress=progress,
                    reason="acceptance_unmet",
                )
            )
            last_tree = evaluation.tree
            history.append(_feedback_message(evaluation, checklist))

    # -- baseline -------------------------------------------------------

    def _run_baseline(self, state: _RunState) -> CodingResult | None:
        command = state.plan.baseline_command
        if not command:
            return None
        operation_id = f"{state.task.task_id[:60]}:baseline"
        try:
            reservation = self.budgets.reserve(
                state.task.task_id, operation_id, "tool", reserved_units=1
            )
        except DomainError as exc:
            if exc.code == "BUDGET_EXCEEDED":
                return self._finish(
                    state,
                    "budget_exhausted",
                    "budget_tool_calls",
                    "The tool-call budget was exhausted before the baseline",
                    None,
                )
            raise
        request = ExecutionRequest(
            operation_id=operation_id,
            purpose="test",
            argv=command,
            cwd=".",
            timeout_seconds=min(float(state.grant.max_seconds), 120.0),
            execution_epoch=state.task.execution_epoch,
        )
        self._node(state, "baseline", 0, operation_id=operation_id, status="running")
        try:
            receipt = self.broker.execute(request, state.grant, state.token)
        except DomainError as exc:
            self.budgets.release(reservation.reservation_id)
            if exc.code == "HARNESS_CANCELLED":
                return self._finish(
                    state, "cancelled", "cancelled", "Cancelled during the baseline command"
                )
            return self._finish(
                state,
                "denied",
                exc.code,
                f"The baseline command was denied: {exc}",
                None,
            )
        self.budgets.settle(reservation.reservation_id, actual_micros=0, actual_units=1)
        state.tool_calls += 1
        context_tools = getattr(self.tools, "context_tools", None)
        is_recovered_receipt = getattr(self.broker, "is_recovered_receipt", None)
        recovered = callable(is_recovered_receipt) and is_recovered_receipt(receipt) is True
        # The durable broker archived a known baseline under its generating
        # lease. Replaying that receipt must never bind old output to a new
        # epoch's lease; the broker has rechecked current fixed scope instead.
        if context_tools is not None and not recovered:
            try:
                context_tools.archive_command_log(
                    receipt.stdout + receipt.stderr,
                    ToolContext(
                        task=state.task,
                        grant=state.grant,
                        operation_id=operation_id,
                        workspace_tree=receipt.tree_hash,
                    ),
                    provenance=receipt.provenance,
                    # The old Broker receipt has no output-completeness flag.
                    # Preserve its retained text without certifying a full log.
                    complete=False,
                )
            except DomainError as exc:
                self._node(
                    state,
                    "baseline",
                    0,
                    operation_id=operation_id,
                    status="output_denied",
                    detail=exc.code,
                    evidence=(receipt.receipt_id,),
                )
                return self._finish(state, "blocked", exc.code, str(exc))
        # Publishing the receipt also publishes stdout. Keep it out of the
        # result until the generating authority has survived strict archiving.
        state.receipts.append(receipt)
        state.evidence_ids.add(receipt.receipt_id)
        self._node(
            state,
            "command",
            0,
            operation_id=operation_id,
            status=receipt.status,
            detail=f"{' '.join(receipt.argv)} exit={receipt.exit_code}",
            evidence=(receipt.receipt_id,),
        )
        self._emit(
            state.task,
            "command_completed",
            0,
            {
                "operation_id": operation_id,
                "status": receipt.status,
                "exit_code": receipt.exit_code,
                "receipt_id": receipt.receipt_id,
            },
        )
        return None

    # -- tools ----------------------------------------------------------

    def _dispatch_tools(
        self,
        state: _RunState,
        response: ModelResponse,
        attempt: int,
        history: list[ModelMessage],
        declared: dict[str, ToolSpec],
    ) -> CodingResult | None:
        history.append(
            ModelMessage(
                role="assistant",
                content=response.text,
                tool_calls=response.tool_calls,
            )
        )
        for index, call in enumerate(response.tool_calls):
            if state.token.cancelled:
                return self._finish(
                    state, "cancelled", "cancelled", "Cancelled before the tool call"
                )
            spec = declared.get(call.name)
            checked = self._precheck(call, spec, state)
            if checked is not None:
                self._feedback(
                    state,
                    attempt,
                    "tool_denied",
                    checked[1],
                    detail=checked[0],
                    tree=self.workspace.snapshot().tree_hash,
                )
                self._node(
                    state,
                    "tool",
                    attempt,
                    operation_id=call.call_id,
                    status="denied",
                    detail=checked[0],
                )
                self._emit(
                    state.task,
                    "tool_denied",
                    state.revision,
                    {
                        "call_id": call.call_id,
                        "name": call.name,
                        "node": "runtime",
                        "code": checked[1],
                    },
                )
                return self._finish(
                    state,
                    "denied",
                    checked[1],
                    f"Tool {call.name} was denied: {checked[0]}",
                    None,
                )
            assert spec is not None
            operation_id = f"{state.task.task_id[:60]}:tool:{attempt}:{index + 1}"
            try:
                reservation = self.budgets.reserve(
                    state.task.task_id, operation_id, "tool", reserved_units=1
                )
            except DomainError as exc:
                if exc.code == "BUDGET_EXCEEDED":
                    return self._finish(
                        state,
                        "budget_exhausted",
                        "budget_tool_calls",
                        "The tool-call budget was exhausted",
                        None,
                    )
                raise
            self._emit(
                state.task,
                "tool_started",
                state.revision,
                {"operation_id": operation_id, "name": call.name},
            )
            if call.name == "ask_user":
                invoke_interaction = getattr(self.tools, "invoke_interaction", None)
                if callable(invoke_interaction):
                    result = invoke_interaction(
                        call,
                        ToolContext(
                            task=state.task,
                            grant=state.grant,
                            operation_id=operation_id,
                            workspace_tree=self.workspace.snapshot().tree_hash,
                        ),
                        state.token,
                        lambda call=call: self._ask_user(state, call),
                    )
                else:
                    result = self._ask_user(state, call)
            else:
                result = self.tools.invoke(
                    call,
                    ToolContext(
                        task=state.task,
                        grant=state.grant,
                        operation_id=operation_id,
                        workspace_tree=self.workspace.snapshot().tree_hash,
                    ),
                    state.token,
                )
            state.tool_calls += 1
            self.budgets.settle(reservation.reservation_id, actual_micros=0, actual_units=1)
            self._emit(
                state.task,
                "tool_completed",
                state.revision,
                {
                    "operation_id": operation_id,
                    "name": call.name,
                    "status": result.status,
                    "code": result.code,
                },
            )
            payload = result.data if isinstance(result.data, dict) else {}
            if call.name == "apply_edits" and result.status == "ok":
                self._emit(
                    state.task,
                    "patch_applied",
                    state.revision,
                    {
                        "operation_id": operation_id,
                        "protocol": str(payload.get("protocol", "")),
                        "tree": str(payload.get("tree_hash", "")),
                        "changes": payload.get("changes", []),
                        "output_bytes": int(payload.get("output_bytes", 0) or 0),
                    },
                )
            if call.name == "run_tests":
                receipt = payload.get("receipt", {})
                if isinstance(receipt, dict):
                    self._emit(
                        state.task,
                        "test_completed",
                        state.revision,
                        {
                            "operation_id": operation_id,
                            "status": result.status,
                            "exit_code": int(receipt.get("exit_code", 0) or 0),
                            "tests_run": int(receipt.get("tests_run", 0) or 0),
                            "tests_failed": int(receipt.get("tests_failed", 0) or 0),
                            "receipt_id": str(receipt.get("receipt_id", "")),
                        },
                    )
            self._node(
                state,
                "tool",
                attempt,
                operation_id=operation_id,
                status=result.status,
                detail=call.name,
            )
            if result.status == "denied":
                return self._finish(
                    state, "denied", result.code, f"Tool {call.name} was denied", None
                )
            if result.status == "cancelled":
                return self._finish(state, "cancelled", "cancelled", "Tool was cancelled", None)
            receipt = self._collect_receipt(state, result)
            evidence = self._collect_evidence(state, call, result, spec)
            if receipt is not None:
                self._node(
                    state,
                    "command",
                    attempt,
                    operation_id=operation_id,
                    status=receipt.status,
                    detail=f"{' '.join(receipt.argv)} exit={receipt.exit_code}",
                    evidence=(receipt.receipt_id,),
                )
                self._emit(
                    state.task,
                    "command_completed",
                    state.revision,
                    {
                        "operation_id": operation_id,
                        "argv": list(receipt.argv),
                        "exit_code": receipt.exit_code,
                        "status": receipt.status,
                        "receipt_id": receipt.receipt_id,
                        "duration_seconds": receipt.duration_seconds,
                    },
                )
            empty = self._empty_read(state, call, result)
            if empty:
                self._feedback(
                    state,
                    attempt,
                    "empty_search",
                    "",
                    detail=empty,
                    tree=self.workspace.snapshot().tree_hash,
                    evidence=evidence,
                )
            if result.status == "error":
                self._feedback(
                    state,
                    attempt,
                    "tool_failed",
                    result.code,
                    detail=result.content[:500],
                    tree=self.workspace.snapshot().tree_hash,
                    receipt_id=receipt.receipt_id if receipt else "",
                    evidence=evidence,
                )
            history.append(
                ModelMessage(
                    role="tool",
                    name=call.name,
                    tool_call_id=call.call_id,
                    content=result.content,
                )
            )
        return None

    def _precheck(
        self,
        call: HarnessToolCall,
        spec: ToolSpec | None,
        state: _RunState,
    ) -> tuple[str, str] | None:
        if spec is None:
            return ("tool is not declared by the registry", "HARNESS_TOOL_UNDECLARED")
        if spec.capability == "write":
            if not state.policy.allow_write:
                return (
                    "this task kind is read-only and cannot write",
                    "HARNESS_WRITE_DENIED",
                )
            paths = _path_arguments(call.arguments)
            if paths is None or any(not state.grant.can_write(path) for path in paths):
                return ("write path is outside the grant", "HARNESS_WRITE_DENIED")
            return None
        if spec.capability == "read":
            # Tools without an explicit path (e.g. a search) read from the
            # workspace root, which the grant must still cover.
            paths = _path_arguments(call.arguments) or ["."]
            if any(not state.grant.can_read(path) for path in paths):
                return ("read path is outside the grant", "HARNESS_READ_DENIED")
            return None
        if not state.policy.allow_execute:
            return (
                "this task kind is read-only and cannot execute commands",
                "HARNESS_COMMAND_DENIED",
            )
        argv = _tool_list(call.arguments.get("argv"))
        if argv is None or not state.grant.can_execute(argv):
            return ("command is outside the grant", "HARNESS_COMMAND_DENIED")
        return None

    def _ask_user(self, state: _RunState, call: HarnessToolCall) -> HarnessToolResult:
        raw = call.arguments.get("question")
        if not isinstance(raw, str) or not raw.strip():
            return HarnessToolResult(
                call_id=call.call_id,
                status="denied",
                content="ask_user requires a non-empty question",
                code="HARNESS_TOOL_ARGUMENT",
            )
        if self.interaction is None:
            if raw.strip() not in state.open_questions:
                state.open_questions.append(raw.strip())
            return HarnessToolResult(
                call_id=call.call_id,
                status="denied",
                content="ask_user is not available for this task",
                code="HARNESS_ASK_UNAVAILABLE",
            )
        try:
            answer = self.interaction.ask(
                question=raw.strip(),
                task=state.task,
                revision=state.revision,
            )
        except DomainError as exc:
            if raw.strip() not in state.open_questions:
                state.open_questions.append(raw.strip())
            if exc.code == "HARNESS_CANCELLED":
                return HarnessToolResult(
                    call_id=call.call_id,
                    status="cancelled",
                    content=str(exc),
                    code=exc.code,
                )
            return HarnessToolResult(
                call_id=call.call_id,
                status="error",
                content=str(exc),
                code=exc.code,
            )
        if raw.strip() in state.open_questions:
            state.open_questions.remove(raw.strip())
        return HarnessToolResult(
            call_id=call.call_id,
            status="ok",
            content=answer,
            data={"question": raw.strip(), "answer": answer},
        )

    def _collect_receipt(
        self, state: _RunState, result: HarnessToolResult
    ) -> ExecutionReceipt | None:
        raw = result.data.get("receipt")
        if not isinstance(raw, dict):
            return None
        try:
            receipt = ExecutionReceipt.model_validate(raw)
        except ValueError:
            return None
        state.receipts.append(receipt)
        state.evidence_ids.add(receipt.receipt_id)
        return receipt

    def _collect_evidence(
        self,
        state: _RunState,
        call: HarnessToolCall,
        result: HarnessToolResult,
        spec: ToolSpec,
    ) -> tuple[str, ...]:
        if (
            result.status != "ok"
            or spec.capability != "read"
            or result.data.get("cache_hit") is True
        ):
            return ()
        paths: list[str] = []
        direct = result.data.get("path")
        if isinstance(direct, str):
            paths.append(direct)
        matches = result.data.get("matches")
        if isinstance(matches, list):
            for match in matches:
                if isinstance(match, dict) and isinstance(match.get("path"), str):
                    paths.append(match["path"])
        state.evidence_paths.update(paths)
        if call.name in {"artifact_page", "artifact_reread"}:
            # Every page points at the same immutable original. Re-reading it
            # must not manufacture a growing set of new evidence identities.
            evidence_id = canonical_hash(
                {
                    "artifact": result.data.get("artifact_hash"),
                    "path": result.data.get("path"),
                    "source_tree": result.data.get("source_tree"),
                }
            )
        else:
            evidence_id = canonical_hash(
                {
                    "tool": call.name,
                    "call_id": call.call_id,
                    "content": sha256_text(result.content),
                    "tree": self.workspace.snapshot().tree_hash,
                }
            )
        state.evidence_ids.add(evidence_id)
        return (evidence_id,)

    def _empty_read(
        self, state: _RunState, call: HarnessToolCall, result: HarnessToolResult
    ) -> str:
        if result.status != "ok":
            return ""
        data = result.data
        key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
        empty = ""
        if isinstance(data.get("matches"), list) and not data["matches"]:
            empty = "search returned no matches"
        elif isinstance(data.get("entries"), list) and not data["entries"]:
            empty = "directory listing is empty"
        if not empty:
            return ""
        if key in state.empty_queries:
            return empty + " (the same query was repeated)"
        state.empty_queries.add(key)
        return empty

    # -- evaluation -----------------------------------------------------

    def _evaluate(self, state: _RunState, answer_text: str) -> _Evaluation:
        current = self.workspace.snapshot()
        tree = current.tree_hash
        task = state.task
        plan = state.plan
        checks: list[VerificationCheck] = []
        status: dict[str, tuple[AcceptanceStatus, str, tuple[str, ...]]] = {}
        missing: list[str] = []
        tests_weakened = False
        verifier_checks: dict[str, VerificationCheck] = {}
        if task.task_kind != "inspect":
            verification = self.verifier.verify(
                VerificationRequest(
                    task=task,
                    snapshot=current,
                    receipts=tuple(state.receipts),
                )
            )
            checks.extend(verification.checks)
            verifier_checks = {check.requirement_id: check for check in verification.checks}
        service_receipts = self._service_receipts(state, tree)
        deleted_tests = sorted(
            path
            for path in state.baseline_files
            if _is_test_path(path) and path not in current.files
        )
        weakened_tests = _weakened_test_files(state.baseline_files, dict(current.files))
        if deleted_tests:
            tests_weakened = True
            missing.append("the removed test files")
        if weakened_tests:
            tests_weakened = True
            missing.append("the weakened test files")
        for item in plan.checklist.items:
            if item.kind in {"behavior", "regression", "build"}:
                requirement = self._requirement(task, item.requirement_id)
                check = verifier_checks.get(item.requirement_id or "")
                detail = check.detail if check is not None else "no matching receipt"
                is_passed = check is not None and check.status == "passed"
                evidence: tuple[str, ...] = (
                    (check.receipt_id,) if check is not None and check.receipt_id else ()
                )
                if requirement is not None and is_passed:
                    baseline = self._match(state.receipts, plan.checkpoint_tree, requirement)
                    final = self._match(state.receipts, tree, requirement)
                    if baseline is not None and final is not None:
                        if final.tests_run < baseline.tests_run:
                            is_passed = False
                            tests_weakened = True
                            detail += (
                                f"; tests_run {final.tests_run} < baseline {baseline.tests_run}"
                            )
                if deleted_tests:
                    is_passed = False
                    detail += "; tests removed: " + ", ".join(deleted_tests)
                if weakened_tests:
                    is_passed = False
                    detail += "; tests weakened: " + ", ".join(weakened_tests)
                status[item.item_id] = (
                    "passed" if is_passed else "failed",
                    detail,
                    evidence,
                )
            elif item.kind == "reproduction":
                requirement = self._requirement(task, item.requirement_id)
                failing = None
                if requirement is not None:
                    failing = self._match(state.receipts, plan.checkpoint_tree, requirement)
                    if failing is not None and (
                        failing.status == "passed" and failing.tests_failed == 0
                    ):
                        failing = None
                if failing is None:
                    status[item.item_id] = (
                        "failed",
                        "no failing receipt was recorded on the original tree",
                        (),
                    )
                    missing.append("a real failing baseline reproduction")
                    checks.append(
                        VerificationCheck(
                            requirement_id=item.item_id,
                            status="failed",
                            detail="no failing baseline receipt on the original tree",
                        )
                    )
                else:
                    status[item.item_id] = (
                        "passed",
                        f"baseline {failing.status} exit={failing.exit_code}",
                        (failing.receipt_id,),
                    )
                    checks.append(
                        VerificationCheck(
                            requirement_id=item.item_id,
                            status="passed",
                            detail=f"baseline {failing.status} exit={failing.exit_code}",
                            receipt_id=failing.receipt_id,
                        )
                    )
            elif item.kind == "signature":
                before = _public_signatures(state.baseline_files)
                after = _public_signatures(dict(current.files))
                changed = sorted(
                    path
                    for path, signature in before.items()
                    if path in after and after[path] != signature
                )
                if changed:
                    status[item.item_id] = (
                        "failed",
                        f"public signatures changed: {', '.join(changed)}",
                        (),
                    )
                    checks.append(
                        VerificationCheck(
                            requirement_id=item.item_id,
                            status="failed",
                            detail=f"public signatures changed: {', '.join(changed)}",
                        )
                    )
                else:
                    status[item.item_id] = ("passed", "public signatures unchanged", ())
                    checks.append(
                        VerificationCheck(
                            requirement_id=item.item_id,
                            status="passed",
                            detail="public signatures unchanged",
                        )
                    )
            elif item.kind == "readiness":
                ready = [receipt for receipt in service_receipts if receipt.ready]
                if ready:
                    status[item.item_id] = (
                        "passed",
                        f"ready at {ready[0].address}: {ready[0].probe_detail}",
                        (ready[0].receipt_id,),
                    )
                    checks.append(
                        VerificationCheck(
                            requirement_id=item.item_id,
                            status="passed",
                            detail=f"ready at {ready[0].address}",
                            receipt_id=ready[0].receipt_id,
                        )
                    )
                else:
                    detail = (
                        "a readiness probe did not succeed"
                        if service_receipts
                        else "no readiness receipt was produced"
                    )
                    status[item.item_id] = ("failed", detail, ())
                    checks.append(
                        VerificationCheck(
                            requirement_id=item.item_id,
                            status="failed",
                            detail=detail,
                        )
                    )
                    missing.append("a ready application process")
            elif item.kind == "cleanup":
                cleaned = [receipt for receipt in service_receipts if receipt.stopped]
                if cleaned:
                    status[item.item_id] = (
                        "passed",
                        cleaned[0].cleanup_detail or "application stopped",
                        (cleaned[0].receipt_id,),
                    )
                    checks.append(
                        VerificationCheck(
                            requirement_id=item.item_id,
                            status="passed",
                            detail=cleaned[0].cleanup_detail or "application stopped",
                            receipt_id=cleaned[0].receipt_id,
                        )
                    )
                else:
                    detail = "the started application was not stopped"
                    status[item.item_id] = ("failed", detail, ())
                    checks.append(
                        VerificationCheck(
                            requirement_id=item.item_id,
                            status="failed",
                            detail=detail,
                        )
                    )
            elif item.kind == "evidence":
                cited = sorted(path for path in state.evidence_paths if path in answer_text)
                if state.evidence_ids and cited:
                    status[item.item_id] = (
                        "passed",
                        f"evidence ids={len(state.evidence_ids)} cited={', '.join(cited)}",
                        tuple(sorted(state.evidence_ids)),
                    )
                    checks.append(
                        VerificationCheck(
                            requirement_id=item.item_id,
                            status="passed",
                            detail=f"cited {', '.join(cited)}",
                        )
                    )
                else:
                    detail = (
                        "no workspace evidence was collected"
                        if not state.evidence_ids
                        else "the answer does not cite any collected path"
                    )
                    status[item.item_id] = ("failed", detail, ())
                    checks.append(
                        VerificationCheck(
                            requirement_id=item.item_id,
                            status="failed",
                            detail=detail,
                        )
                    )
                    missing.append("a cited evidence trail")
        if tests_weakened:
            missing.append("the original number of tests")
        passed = [item_id for item_id, value in status.items() if value[0] == "passed"]
        failed = [item_id for item_id, value in status.items() if value[0] != "passed"]
        required_failed = [
            item.item_id
            for item in plan.checklist.required_items()
            if status[item.item_id][0] != "passed"
        ]
        checks_all = all(check.status == "passed" for check in checks)
        if not required_failed and checks_all:
            verification_status: Literal["verified", "failed", "incomplete"] = "verified"
        elif required_failed or any(check.status == "failed" for check in checks):
            verification_status = "failed"
        else:
            verification_status = "incomplete"
        receipt_ids: list[str] = []
        for check in checks:
            if check.receipt_id and check.receipt_id not in receipt_ids:
                receipt_ids.append(check.receipt_id)
        signature = digest(
            [
                (item.item_id, status[item.item_id][0], status[item.item_id][1])
                for item in plan.checklist.items
            ]
            + [(check.requirement_id, check.status, check.detail) for check in checks]
        )
        payload = {
            "task_id": task.task_id,
            "status": verification_status,
            "tree_hash": tree,
            "checks": [check.model_dump(mode="json") for check in checks],
            "receipt_ids": receipt_ids,
        }
        receipt = VerificationReceipt(
            verification_id=digest(payload),
            task_id=task.task_id,
            status=verification_status,
            tree_hash=tree,
            checks=tuple(checks),
            receipt_ids=tuple(receipt_ids),
        )
        return _Evaluation(
            tree=tree,
            checks=checks,
            verification=receipt,
            item_status=status,
            failed=failed,
            passed=passed,
            signature=signature,
            missing_information=missing,
            tests_weakened=tests_weakened,
        )

    def _service_receipts(self, state: _RunState, tree: str) -> list[ServiceReceipt]:
        plan = state.plan.service_plan
        if not state.policy.require_service or plan is None or self.application is None:
            return []
        cached = state.service_cache.get(tree)
        if cached is not None:
            return cached
        receipts: list[ServiceReceipt] = []
        try:
            started = self.application.start(plan, tree, state.grant, state.token)
            receipts.append(started)
            stopped = self.application.stop(started)
            receipts.append(stopped)
        except DomainError as exc:
            receipts.append(
                ServiceReceipt(
                    receipt_id=digest({"error": exc.code, "tree": tree}),
                    argv=plan.argv,
                    tree_hash=tree,
                    started=False,
                    ready=False,
                    stopped=False,
                    probe_detail=str(exc),
                    cleanup_detail=str(exc),
                )
            )
        state.service_cache[tree] = receipts
        state.service_receipts.extend(receipts)
        for receipt in receipts:
            self._node(
                state,
                "command",
                0,
                operation_id=receipt.receipt_id,
                status="ready" if receipt.ready else "stopped",
                detail=receipt.probe_detail or receipt.cleanup_detail,
                evidence=(receipt.receipt_id,),
            )
        return receipts

    def _requirement(
        self, task: TaskBinding, requirement_id: str | None
    ) -> VerificationRequirement | None:
        for requirement in task.requirements:
            if requirement.requirement_id == requirement_id:
                return requirement
        return None

    def _match(
        self,
        receipts: Sequence[ExecutionReceipt],
        tree: str,
        requirement: VerificationRequirement,
    ) -> ExecutionReceipt | None:
        best: ExecutionReceipt | None = None
        for receipt in receipts:
            if receipt.tree_hash != tree:
                continue
            command = " ".join(receipt.argv)
            if any(token not in command for token in requirement.command_contains):
                continue
            if receipt.status not in {"passed", "failed", "timeout"}:
                continue
            if best is None or receipt.tests_run >= best.tests_run:
                # Prefer the broadest evidence for a requirement: a narrower
                # re-run (one test) must not mask the full suite receipt.
                best = receipt
        return best

    # -- progress -------------------------------------------------------

    def _progress(
        self,
        state: _RunState,
        last_tree: str,
        evaluation: _Evaluation,
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        tree = evaluation.tree
        if tree != last_tree and tree in state.seen_trees:
            self._feedback(
                state,
                len(state.attempts) + 1,
                "patch_round_trip",
                evaluation.signature,
                detail="the worktree returned to a tree that was already seen",
                tree=tree,
            )
            return False, ["patch_round_trip"]
        receipt_keys = {
            (receipt.tree_hash, " ".join(receipt.argv), receipt.log_sha256)
            for receipt in state.receipts
        }
        new_evidence = bool(receipt_keys - state.seen_receipts)
        state.seen_receipts.update(receipt_keys)
        new_signature = evaluation.signature not in state.seen_signatures
        tree_changed = tree != last_tree
        if not tree_changed:
            reasons.append("the worktree did not change")
        if not new_signature:
            reasons.append("the same failure signature was repeated")
        if new_evidence:
            reasons.append("new command evidence was recorded")
        if evaluation.tests_weakened:
            reasons.append("the test count decreased")
        # A new failure signature only counts when it comes with a changed
        # worktree or new command evidence; re-answering the same evidence,
        # rephrased text and repeated empty searches are not progress.
        progress = tree_changed or new_evidence
        if evaluation.tests_weakened:
            progress = False
        state.seen_trees.add(tree)
        return progress, reasons

    # -- helpers --------------------------------------------------------

    def _base_items(self, task: TaskBinding) -> tuple[AcceptanceItem, ...]:
        kind = task.task_kind
        if kind == "inspect":
            return (
                AcceptanceItem(
                    item_id="evidence",
                    description="Collect and cite workspace evidence without edits.",
                    kind="evidence",
                    source="user_goal",
                ),
            )
        items: list[AcceptanceItem] = []
        if kind == "bugfix":
            for requirement in task.requirements:
                items.append(
                    AcceptanceItem(
                        item_id=_item_id("reproduce", requirement.requirement_id),
                        description=(
                            f"Reproduce {requirement.requirement_id} as a real failure "
                            "on the original tree."
                        ),
                        kind="reproduction",
                        source="user_goal",
                        requirement_id=requirement.requirement_id,
                    )
                )
                items.append(
                    AcceptanceItem(
                        item_id=_item_id("verify", requirement.requirement_id),
                        description=(
                            f"Satisfy trusted requirement {requirement.requirement_id} "
                            f"({requirement.kind})."
                        ),
                        kind="build" if requirement.kind == "build" else "regression",
                        source="trusted_command",
                        requirement_id=requirement.requirement_id,
                    )
                )
        elif kind == "feature":
            for requirement in task.requirements:
                items.append(
                    AcceptanceItem(
                        item_id=_item_id("behavior", requirement.requirement_id),
                        description=(
                            f"Deliver the new behavior for {requirement.requirement_id} "
                            f"({requirement.kind})."
                        ),
                        kind="build" if requirement.kind == "build" else "behavior",
                        source="trusted_command",
                        requirement_id=requirement.requirement_id,
                    )
                )
            for requirement in task.requirements:
                items.append(
                    AcceptanceItem(
                        item_id=_item_id("regression", requirement.requirement_id),
                        description=(
                            f"Keep the original behavior for {requirement.requirement_id} "
                            "and do not weaken its tests."
                        ),
                        kind="regression",
                        source="trusted_command",
                        requirement_id=requirement.requirement_id,
                    )
                )
        elif kind == "refactor":
            for requirement in task.requirements:
                items.append(
                    AcceptanceItem(
                        item_id=_item_id("regression", requirement.requirement_id),
                        description=(
                            f"Keep the frozen behavior for {requirement.requirement_id} "
                            "and do not weaken its tests."
                        ),
                        kind="build" if requirement.kind == "build" else "regression",
                        source="trusted_command",
                        requirement_id=requirement.requirement_id,
                    )
                )
            items.append(
                AcceptanceItem(
                    item_id="signature",
                    description="Keep every public signature unchanged.",
                    kind="signature",
                    source="user_goal",
                )
            )
        elif kind == "run_app":
            for requirement in task.requirements:
                prefix = "build" if requirement.kind == "build" else "behavior"
                items.append(
                    AcceptanceItem(
                        item_id=_item_id(prefix, requirement.requirement_id),
                        description=(
                            f"Satisfy trusted requirement {requirement.requirement_id} "
                            f"({requirement.kind})."
                        ),
                        kind="build" if requirement.kind == "build" else "behavior",
                        source="trusted_command",
                        requirement_id=requirement.requirement_id,
                    )
                )
            items.extend(
                (
                    AcceptanceItem(
                        item_id="readiness",
                        description="Start the application and pass a real readiness probe.",
                        kind="readiness",
                        source="trusted_command",
                    ),
                    AcceptanceItem(
                        item_id="cleanup",
                        description="Stop the application and record the cleanup receipt.",
                        kind="cleanup",
                        source="trusted_command",
                    ),
                )
            )
        return tuple(items)

    def _declared_tools(self) -> dict[str, ToolSpec]:
        declared: dict[str, ToolSpec] = {}
        for spec in self.tools.specs():
            if spec.name in declared:
                raise DomainError(
                    "HARNESS_TOOL_DUPLICATE", "Tool registry declared a duplicate name", 409
                )
            declared[spec.name] = spec
        return declared

    def _authorize(self, task: TaskBinding, grant: CapabilityGrant, plan: TaskPlan) -> None:
        if grant.actor != task.actor:
            raise DomainError("HARNESS_ACTOR_MISMATCH", "Grant actor does not match task", 403)
        if grant.repository.lower() != task.repository.lower():
            raise DomainError(
                "HARNESS_REPOSITORY_MISMATCH", "Grant repository does not match task", 403
            )
        if grant.workspace_id != task.workspace_id:
            raise DomainError(
                "HARNESS_WORKSPACE_MISMATCH", "Grant workspace does not match task", 403
            )
        if plan.task_id != task.task_id:
            raise DomainError("HARNESS_PLAN_MISMATCH", "Plan belongs to another task", 409)
        if plan.objective != task.objective:
            raise DomainError(
                "HARNESS_PLAN_MISMATCH", "Plan objective no longer matches the task", 409
            )
        if plan.acceptance_revision != task.acceptance_revision:
            raise DomainError(
                "HARNESS_PLAN_MISMATCH", "Acceptance revision moved after planning", 409
            )
        if plan.task_kind != task.task_kind:
            raise DomainError("HARNESS_PLAN_MISMATCH", "Plan kind no longer matches", 409)

    def _node(
        self,
        state: _RunState,
        kind: NodeKind,
        attempt: int,
        *,
        operation_id: str = "",
        status: str = "ok",
        detail: str = "",
        evidence: Sequence[str] = (),
    ) -> None:
        state.nodes.append(
            RunNode(
                position=len(state.nodes) + 1,
                kind=kind,
                attempt=attempt,
                operation_id=operation_id,
                status=status,
                detail=detail,
                tree_hash=self.workspace.snapshot().tree_hash,
                evidence=tuple(evidence),
            )
        )

    def _feedback(
        self,
        state: _RunState,
        attempt: int,
        kind: FeedbackKind,
        signature: str,
        *,
        detail: str = "",
        tree: str | None = None,
        receipt_id: str = "",
        evidence: Sequence[str] = (),
    ) -> None:
        item = LoopFeedback(
            attempt=attempt,
            node="feedback",
            kind=kind,
            tree_hash=tree or self.workspace.snapshot().tree_hash,
            signature=signature,
            receipt_id=receipt_id,
            evidence=tuple(evidence),
            message=detail[:4000],
        )
        state.feedback.append(item)
        self._node(
            state,
            "feedback",
            attempt,
            status=kind,
            detail=detail,
            evidence=tuple(evidence),
        )
        self._emit(
            state.task,
            "feedback",
            state.revision,
            {"kind": kind, "signature": signature, "message": detail[:500]},
        )

    def _emit(
        self,
        task: TaskBinding,
        kind: str,
        revision: int,
        payload: dict[str, Any],
    ) -> EventEnvelope:
        return self.events.emit(
            EventDraft(
                run_id=task.run_id,
                operation_id=f"{task.task_id[:80]}:event",
                execution_epoch=task.execution_epoch,
                revision=revision,
                kind=kind,
                payload=payload,
            )
        )

    def _working_state(
        self,
        state: _RunState,
        checklist: AcceptanceChecklist | None = None,
        *,
        snapshot: WorkspaceSnapshot | None = None,
        diff: WorkspaceDiff | None = None,
    ) -> WorkingState:
        """Project trusted runtime facts; this snapshot grants no authority."""
        snapshot = snapshot if snapshot is not None else self.workspace.snapshot()
        tree = snapshot.tree_hash
        diff = diff if diff is not None else self.workspace.diff(state.plan.checkpoint_tree)
        if diff.tree_after != tree:
            raise DomainError(
                "HARNESS_CONTEXT_TREE_STALE", "Working state and patch trees differ", 409
            )
        if checklist is None:
            checklist = state.plan.checklist
            if state.evaluation is not None and state.evaluation.tree == tree:
                checklist = self._evaluated_checklist(checklist, state.evaluation)
        context_tools = getattr(self.tools, "context_tools", None)
        refs = context_tools.references() if context_tools is not None else ()
        active = getattr(self.tools, "active_commands", None)
        pending = tuple(active()) if active is not None else ()
        budget = self.budgets.snapshot(state.task.task_id)
        authority = getattr(self.broker, "authority", None)
        lease = None
        if authority is not None:
            try:
                lease = authority.peek().model_dump(mode="json")
            except DomainError as exc:
                lease = {"unavailable": exc.code}
        failures = tuple(
            dict.fromkeys(
                [item.failure_signature for item in state.attempts if item.failure_signature]
                + [
                    f"{item.kind}:{item.signature}:{sha256_text(item.message)}"
                    for item in state.feedback
                    if item.kind
                    in {
                        "tool_failed",
                        "tool_denied",
                        "acceptance_failed",
                        "repeated_failure",
                    }
                ]
                + [
                    f"{receipt.receipt_id}:{receipt.status}:{receipt.log_sha256}"
                    for receipt in state.receipts
                    if receipt.status != "passed"
                ]
            )
        )
        return WorkingState.from_task(
            state.task,
            checklist_revision=checklist.revision,
            instruction_revision=state.instruction_revision,
            sequence=state.revision,
            tree=tree,
            checklist=checklist.model_dump(mode="json"),
            unmet_items=checklist.unmet_ids(),
            decisions=(
                state.plan.model_copy(
                    update={
                        "objective": state.task.objective,
                        "acceptance_revision": state.task.acceptance_revision,
                    }
                ).model_dump_json(),
            ),
            failed_attempts=failures,
            evidence_ids=tuple(sorted(state.evidence_ids)),
            pending_commands=pending,
            open_questions=tuple(state.open_questions),
            patch_hashes=(sha256_text(diff.patch),),
            artifact_hashes=tuple(sorted({str(ref["artifact_hash"]) for ref in refs})),
            artifact_refs=refs,
            permission_ref=canonical_hash(
                {
                    "grant": state.grant.model_dump(mode="json"),
                    "lease": lease,
                    "actor": state.task.actor,
                    "epoch": state.task.execution_epoch,
                }
            ),
            budget_ref=f"ledger:{state.task.task_id}:{canonical_hash(budget.model_dump(mode='json'))}",
            permissions=state.grant,
            budget=budget,
            available_tools=tuple(sorted(self._declared_tools())),
        )

    @staticmethod
    def _evaluated_checklist(
        checklist: AcceptanceChecklist,
        evaluation: _Evaluation,
    ) -> AcceptanceChecklist:
        return checklist.model_copy(
            update={
                "items": tuple(
                    item.model_copy(
                        update={
                            "status": evaluation.item_status.get(
                                item.item_id,
                                ("unmet", "not evaluated", ()),
                            )[0],
                            "detail": evaluation.item_status.get(
                                item.item_id,
                                ("unmet", "not evaluated", ()),
                            )[1],
                            "evidence": evaluation.item_status.get(
                                item.item_id,
                                ("unmet", "not evaluated", ()),
                            )[2],
                        }
                    )
                    for item in checklist.items
                )
            }
        )

    def _finish(
        self,
        state: _RunState,
        status: CodingStatus,
        reason: str,
        summary: str,
        evaluation: _Evaluation | None = None,
        *,
        persist_context: bool = True,
    ) -> CodingResult:
        # An observed drift permanently invalidates this completion attempt.
        # Rebuild a blocked checkpoint for a stable tree, never re-sign a receipt.
        for _ in range(4):
            state.revision += 1
            snapshot = self.workspace.snapshot()
            tree_after = snapshot.tree_hash
            diff = self.workspace.diff(state.plan.checkpoint_tree)
            checklist = state.plan.checklist
            if evaluation is not None and evaluation.tree == tree_after:
                checklist = self._evaluated_checklist(checklist, evaluation)
            if status == "completed" and not (
                evaluation is not None
                and evaluation.tree == tree_after
                and evaluation.verification.tree_hash == tree_after
                and evaluation.verification.task_id == state.task.task_id
                and evaluation.verification.status == "verified"
                and not evaluation.failed
                and checklist.required_items()
                and not checklist.unmet_ids()
            ):
                status, reason, summary = (
                    "blocked",
                    "HARNESS_COMPLETION_STALE",
                    "Completion needs a current verification and every required acceptance item",
                )
            current_tree = self.workspace.snapshot().tree_hash
            if diff.tree_after != tree_after or current_tree != tree_after:
                status, reason, summary = self._terminal_drift(state, tree_after, current_tree)
                continue
            context_state = None
            context_ref = None
            context_metrics: tuple[dict[str, Any], ...] = ()
            if isinstance(self.context, LongContextManager):
                context_state = self._working_state(state, checklist, snapshot=snapshot, diff=diff)
                if persist_context:
                    try:
                        context_ref = self.context.persist_state(context_state)
                    except DomainError as exc:
                        status, reason, summary = "blocked", exc.code, str(exc)
                context_metrics = tuple(
                    record.model_dump(
                        mode="json",
                        exclude={
                            "messages",
                            "bundle",
                            "working_state",
                            "authority_sources",
                        },
                    )
                    for record in self.context.metrics
                )
            current_tree = self.workspace.snapshot().tree_hash
            if current_tree != tree_after:
                status, reason, summary = self._terminal_drift(state, tree_after, current_tree)
                continue
            checkpoint = None
            if status != "completed":
                missing = evaluation.missing_information if evaluation is not None else []
                checkpoint = LoopCheckpoint(
                    tree_hash=tree_after,
                    attempt=len(state.attempts),
                    unmet_items=checklist.unmet_ids(),
                    missing_information=tuple(missing),
                    next_step=(
                        "Address the unmet acceptance items against the current tree "
                        "or ask for the missing input."
                    ),
                    context_state=context_state,
                    context_ref=context_ref,
                )
                self._node(
                    state,
                    "checkpoint",
                    len(state.attempts),
                    status=status,
                    detail=", ".join(checklist.unmet_ids()),
                )
                self._emit(
                    state.task,
                    "checkpoint",
                    state.revision,
                    {
                        "tree": tree_after,
                        "unmet": list(checklist.unmet_ids()),
                        "missing": list(missing),
                    },
                )
            self._emit(
                state.task,
                "run_finished",
                state.revision,
                {
                    "status": status,
                    "reason": reason,
                    "tree": tree_after,
                    "verification_tree": evaluation.verification.tree_hash
                    if evaluation is not None
                    else None,
                },
            )
            events = self.events.events(state.task.run_id)
            # This is the observation cutoff: all persistence/event callbacks
            # have returned. Build the result only from the captured version.
            current_tree = self.workspace.snapshot().tree_hash
            if current_tree != tree_after:
                status, reason, summary = self._terminal_drift(state, tree_after, current_tree)
                continue
            return CodingResult(
                task_id=state.task.task_id,
                task_kind=state.task.task_kind,
                status=status,
                reason=reason,
                summary=summary,
                objective=state.task.objective,
                acceptance_revision=state.task.acceptance_revision,
                checklist_revision=checklist.revision,
                tree_before=state.plan.checkpoint_tree,
                tree_after=tree_after,
                diff=diff,
                receipts=tuple(state.receipts),
                service_receipts=tuple(state.service_receipts),
                verification=evaluation.verification if evaluation is not None else None,
                checklist=checklist,
                attempts=tuple(state.attempts),
                feedback=tuple(state.feedback),
                graph=RunGraph(task_id=state.task.task_id, nodes=tuple(state.nodes)),
                checkpoint=checkpoint,
                model_calls=state.model_calls,
                tool_calls=state.tool_calls,
                cost_micros=state.cost_micros,
                events_cursor=events[-1].cursor if events else 0,
                context_state=context_state,
                context_ref=context_ref,
                context_metrics=context_metrics,
            )
        raise DomainError(
            "HARNESS_TERMINAL_TREE_UNSTABLE", "Workspace kept changing during finalization", 409
        )

    def _terminal_drift(
        self,
        state: _RunState,
        previous: str,
        current: str,
    ) -> tuple[CodingStatus, str, str]:
        self._emit(
            state.task,
            "terminal_invalidated",
            state.revision,
            {
                "previous_tree": previous,
                "current_tree": current,
                "status": "blocked",
                "reason": "HARNESS_COMPLETION_STALE",
            },
        )
        return (
            "blocked",
            "HARNESS_COMPLETION_STALE",
            "The tree changed during finalization; fresh verification is required",
        )
