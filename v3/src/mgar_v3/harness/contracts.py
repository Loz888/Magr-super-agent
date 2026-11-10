"""Versioned contracts for the Harness V4 composition layer.

The harness owns orchestration ports only.  Threads, runs, execution epochs,
budgets, findings, approvals and final business status stay with the existing
Engine and PostgreSQL transaction boundaries.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from mgar_v3.domain import DomainError, Model, digest

CONTRACT_VERSION = "harness-contract-v1"
TASK_KINDS = ("inspect", "bugfix", "feature", "refactor", "run_app")

TaskKind = Literal["inspect", "bugfix", "feature", "refactor", "run_app"]
OperationKind = Literal["model", "tool"]
ReservationStatus = Literal["reserved", "settled", "released", "unknown"]
ToolStatus = Literal["ok", "denied", "error", "cancelled"]
ExecutionStatus = Literal["passed", "failed", "timeout", "denied", "error"]
VerificationStatus = Literal["verified", "failed", "incomplete"]
HarnessStatus = Literal["verified", "incomplete", "cancelled", "denied", "unknown"]
FinishReason = Literal["stop", "tool_calls", "length", "cancelled", "error"]

STATE_OWNERSHIP: dict[str, str] = {
    "session": "app.threads",
    "turn": "app.runs",
    "step": "run_events.operation_id (harness envelope)",
    "execution_epoch": "app.runs.execution_epoch",
    "budget_reservation": "Engine model/tool ledger",
    "budget_settlement": "Engine model/tool ledger",
    "finding": "existing finding store",
    "approval": "existing approval record",
    "task_status": "Engine task transaction",
    "final_completion": "Engine transaction plus verifier receipt",
    "event_cursor": "app.run_events.seq",
    "workspace_version": "WorkspaceSnapshot.tree_hash",
    "review_bundle": "existing Review Bundle service",
}

HARNESS_DOES_NOT_OWN = (
    "task status",
    "finding lifecycle",
    "approval decision",
    "budget settlement",
    "publication",
    "final business completion",
)


def canonical_hash(value: Any) -> str:
    return digest(value)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_relative_path(value: str) -> str:
    """Normalize a repository-relative path and reject traversal."""
    if value in {"", "."}:
        return ""
    if "\x00" in value or "\\" in value or value.startswith("/"):
        raise DomainError("HARNESS_PATH_DENIED", "Path must be repository-relative", 403)
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise DomainError("HARNESS_PATH_DENIED", "Path cannot traverse outside the workspace", 403)
    return "/".join(parts)


def _within(path: str, roots: Sequence[str]) -> bool:
    normalized = normalize_relative_path(path)
    for root in roots:
        prefix = normalize_relative_path(root)
        if prefix == "" or normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


class CancellationToken:
    """Cooperative cancellation checked at every model and tool boundary."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def ensure_active(self) -> None:
        if self._cancelled:
            raise DomainError("HARNESS_CANCELLED", "Harness execution was cancelled", 409)


class VerificationRequirement(Model):
    requirement_id: str = Field(min_length=1, max_length=100)
    kind: Literal["test", "build"] = "test"
    command_contains: tuple[str, ...] = ()
    min_tests: int = Field(default=1, ge=1, le=100000)


class TaskBinding(Model):
    task_id: str = Field(min_length=1, max_length=100)
    thread_id: UUID
    run_id: UUID
    execution_epoch: int = Field(ge=1)
    actor: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    workspace_id: str = Field(min_length=1, max_length=100)
    acceptance_revision: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=4000)
    task_kind: TaskKind = "bugfix"
    requirements: tuple[VerificationRequirement, ...] = Field(min_length=1, max_length=32)

    @field_validator("actor", "repository")
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        return value.lower()


class CapabilityGrant(Model):
    actor: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    workspace_id: str = Field(min_length=1, max_length=100)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    read_paths: tuple[str, ...] = ("",)
    write_paths: tuple[str, ...] = ()
    execute: bool = False
    network: bool = False
    allowed_commands: tuple[str, ...] = ()
    max_seconds: int = Field(default=120, ge=1, le=1800)

    def can_read(self, path: str) -> bool:
        return _within(path, self.read_paths)

    def can_write(self, path: str) -> bool:
        return _within(path, self.write_paths)

    def can_execute(self, argv: Sequence[str]) -> bool:
        if not self.execute or not argv:
            return False
        name = argv[0].rsplit("/", 1)[-1]
        return name in self.allowed_commands


class BudgetLimits(Model):
    max_model_calls: int = Field(default=8, ge=1, le=64)
    max_tool_calls: int = Field(default=32, ge=1, le=256)
    max_cost_micros: int = Field(default=500000, ge=0, le=100000000)
    max_seconds: int = Field(default=900, ge=1, le=3600)


class BudgetSnapshot(Model):
    task_id: str
    model_calls: int
    tool_calls: int
    cost_micros: int


class BudgetReservation(Model):
    reservation_id: str
    task_id: str
    operation_id: str
    kind: OperationKind
    reserved_micros: int = Field(ge=0)
    reserved_units: int = Field(ge=1)
    status: ReservationStatus = "reserved"
    actual_micros: int = Field(default=0, ge=0)
    actual_units: int = Field(default=0, ge=0)
    reason: str = ""


class ModelMessage(Model):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(max_length=200000)
    name: str | None = None
    tool_call_id: str | None = None


class ToolSpec(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,60}$")
    description: str = Field(min_length=1, max_length=1000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    capability: Literal["read", "write", "execute"] = "read"


class HarnessToolCall(Model):
    call_id: str = Field(min_length=1, max_length=100)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,60}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelUsage(Model):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_micros: int = Field(default=0, ge=0)


class ModelRequest(Model):
    operation_id: str = Field(min_length=1, max_length=100)
    task: TaskBinding
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    model_id: str = Field(min_length=1, max_length=100)
    price_version: str = Field(min_length=1, max_length=100)
    max_output_tokens: int = Field(ge=1, le=8192)
    reservation_micros: int = Field(ge=0)
    context_revision: str = Field(min_length=1, max_length=100)
    workspace_tree: str = Field(pattern=r"^[a-f0-9]{64}$")


class ModelResponse(Model):
    text: str = Field(default="", max_length=200000)
    tool_calls: tuple[HarnessToolCall, ...] = ()
    finish_reason: FinishReason = "stop"
    usage: ModelUsage = Field(default_factory=ModelUsage)


class WorkspaceSnapshot(Model):
    workspace_id: str
    tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    files: dict[str, str] = Field(default_factory=dict)


class WorkspaceEdit(Model):
    path: str = Field(min_length=1, max_length=300)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: str = Field(max_length=2000000)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        return normalize_relative_path(value)


class WorkspaceChange(Model):
    path: str
    change: Literal["added", "modified"]
    before_sha256: str | None
    after_sha256: str


class WorkspaceDiff(Model):
    workspace_id: str
    tree_before: str
    tree_after: str
    changes: tuple[WorkspaceChange, ...]
    patch: str = ""


class ToolContext(Model):
    task: TaskBinding
    grant: CapabilityGrant
    operation_id: str
    workspace_tree: str = Field(pattern=r"^[a-f0-9]{64}$")


class HarnessToolResult(Model):
    call_id: str
    status: ToolStatus
    content: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    code: str = ""


class ExecutionRequest(Model):
    operation_id: str = Field(min_length=1, max_length=100)
    purpose: Literal["test", "build", "command"] = "test"
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    cwd: str = "."
    timeout_seconds: float = Field(default=60, gt=0, le=1800)


class ExecutionReceipt(Model):
    receipt_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation_id: str
    purpose: Literal["test", "build", "command"]
    status: ExecutionStatus
    argv: tuple[str, ...]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    log_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tests_run: int = Field(default=0, ge=0)
    tests_failed: int = Field(default=0, ge=0)


class VerificationCheck(Model):
    requirement_id: str
    status: Literal["passed", "failed", "missing"]
    detail: str = ""
    receipt_id: str | None = None


class VerificationRequest(Model):
    task: TaskBinding
    snapshot: WorkspaceSnapshot
    receipts: tuple[ExecutionReceipt, ...] = ()


class VerificationReceipt(Model):
    verification_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    task_id: str
    status: VerificationStatus
    tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    checks: tuple[VerificationCheck, ...]
    receipt_ids: tuple[str, ...] = ()


class ContextView(Model):
    revision: str
    system_prompt: str
    messages: tuple[ModelMessage, ...]
    estimated_tokens: int = Field(default=0, ge=0)


class EventDraft(Model):
    run_id: UUID
    operation_id: str = Field(min_length=1, max_length=100)
    execution_epoch: int = Field(ge=1)
    revision: int = Field(ge=0)
    kind: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


class EventEnvelope(Model):
    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    operation_id: str
    execution_epoch: int
    revision: int
    cursor: int
    kind: str
    payload: dict[str, Any]
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class HarnessResult(Model):
    task_id: str
    status: HarnessStatus
    summary: str = ""
    verification: VerificationReceipt | None = None
    events_cursor: int = 0
    unresolved: tuple[str, ...] = ()
    model_calls: int = 0
    tool_calls: int = 0


@runtime_checkable
class ModelAdapter(Protocol):
    def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse: ...


@runtime_checkable
class ToolRegistry(Protocol):
    def specs(self) -> Sequence[ToolSpec]: ...

    def invoke(
        self,
        call: HarnessToolCall,
        context: ToolContext,
        cancellation: CancellationToken,
    ) -> HarnessToolResult: ...


@runtime_checkable
class WorkspaceProvider(Protocol):
    def snapshot(self) -> WorkspaceSnapshot: ...

    def read(self, path: str) -> str: ...

    def list_dir(self, path: str) -> tuple[str, ...]: ...

    def apply(
        self, edits: Sequence[WorkspaceEdit], *, expected_tree: str
    ) -> WorkspaceSnapshot: ...

    def diff(self, tree_before: str) -> WorkspaceDiff: ...


@runtime_checkable
class ExecutionBroker(Protocol):
    def execute(
        self, request: ExecutionRequest, grant: CapabilityGrant, cancellation: CancellationToken
    ) -> ExecutionReceipt: ...


@runtime_checkable
class ContextManager(Protocol):
    def build(self, task: TaskBinding, events: Sequence[EventEnvelope]) -> ContextView: ...


@runtime_checkable
class Verifier(Protocol):
    def verify(self, request: VerificationRequest) -> VerificationReceipt: ...


@runtime_checkable
class EventSink(Protocol):
    def emit(self, draft: EventDraft) -> EventEnvelope: ...

    def events(self, run_id: UUID, after_cursor: int = 0) -> Sequence[EventEnvelope]: ...


@runtime_checkable
class BudgetLedger(Protocol):
    def reserve(
        self,
        task_id: str,
        operation_id: str,
        kind: OperationKind,
        *,
        reserved_micros: int = 0,
        reserved_units: int = 1,
    ) -> BudgetReservation: ...

    def settle(
        self,
        reservation_id: str,
        *,
        actual_micros: int,
        actual_units: int = 1,
    ) -> BudgetReservation: ...

    def mark_unknown(self, reservation_id: str, reason: str) -> BudgetReservation: ...

    def release(self, reservation_id: str) -> BudgetReservation: ...

    def snapshot(self, task_id: str) -> BudgetSnapshot: ...
