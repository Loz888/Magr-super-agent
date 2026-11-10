"""Reference adapters for the Harness V4 contracts.

These adapters are intentionally small.  They provide a real local workspace,
a real subprocess execution path and a deterministic verifier so the public
interfaces can be exercised before H03-H07 replace them with the production
workspace/broker implementations.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.contracts import (
    BudgetLimits,
    BudgetReservation,
    BudgetSnapshot,
    CancellationToken,
    CapabilityGrant,
    ContextView,
    EventDraft,
    EventEnvelope,
    ExecutionBroker,
    ExecutionReceipt,
    ExecutionRequest,
    HarnessToolCall,
    HarnessToolResult,
    ModelMessage,
    TaskBinding,
    ToolContext,
    ToolSpec,
    VerificationCheck,
    VerificationReceipt,
    VerificationRequest,
    WorkspaceChange,
    WorkspaceDiff,
    WorkspaceEdit,
    WorkspaceProvider,
    WorkspaceSnapshot,
    canonical_hash,
    normalize_relative_path,
    sha256_text,
)

_TEST_PATTERNS = (
    re.compile(r"Ran (\d+) tests?"),
    re.compile(r"(\d+) passed"),
)
_FAILED_PATTERNS = (
    re.compile(r"FAILED \(([^)]*)\)"),
    re.compile(r"(\d+) failed"),
)


def _truncate(value: str, limit: int = 20000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[truncated]"


def _safe_environment(root: Path) -> dict[str, str]:
    keep = {}
    for key in ("PATH", "LANG", "LC_ALL", "PYTHONIOENCODING", "SYSTEMROOT"):
        value = os.environ.get(key)
        if value:
            keep[key] = value
    keep["HOME"] = str(root)
    keep["TMPDIR"] = str(root / ".harness-tmp")
    return keep


def _tree_hash(files: dict[str, str]) -> str:
    return digest({path: sha256_text(content) for path, content in sorted(files.items())})


class LocalWorkspace:
    """A filesystem workspace with pre-image checks and tree-hash snapshots."""

    def __init__(self, root: Path, *, workspace_id: str) -> None:
        self.root = root.resolve()
        self.workspace_id = workspace_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._snapshots: dict[str, dict[str, str]] = {}
        self._snapshots[self.snapshot().tree_hash] = self._read_all()

    def _path(self, value: str) -> Path:
        relative = normalize_relative_path(value)
        candidate = (self.root / relative).resolve()
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise DomainError("HARNESS_PATH_DENIED", "Path escapes the workspace", 403)
        return candidate

    def _read_all(self) -> dict[str, str]:
        files: dict[str, str] = {}
        ignored = {".git", "__pycache__", ".harness-tmp", ".venv", "node_modules"}
        for path in sorted(self.root.rglob("*")):
            if path.is_symlink():
                continue
            if not path.is_file():
                continue
            relative = path.relative_to(self.root).as_posix()
            parts = relative.split("/")
            if any(part in ignored for part in parts[:-1]):
                continue
            if relative.startswith(".harness-tmp/") or path.suffix == ".pyc":
                continue
            try:
                files[relative] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise DomainError(
                    "HARNESS_WORKSPACE_BINARY", "Workspace contains a non-text file", 422
                ) from exc
        return files

    def snapshot(self) -> WorkspaceSnapshot:
        files = self._read_all()
        tree = _tree_hash(files)
        self._snapshots[tree] = dict(files)
        return WorkspaceSnapshot(workspace_id=self.workspace_id, tree_hash=tree, files=files)

    def read(self, path: str) -> str:
        target = self._path(path)
        if not target.is_file():
            raise DomainError("HARNESS_FILE_NOT_FOUND", "Workspace file was not found", 404)
        return target.read_text(encoding="utf-8")

    def list_dir(self, path: str) -> tuple[str, ...]:
        target = self._path(path)
        if not target.is_dir():
            raise DomainError("HARNESS_DIR_NOT_FOUND", "Workspace directory was not found", 404)
        return tuple(sorted(child.name for child in target.iterdir()))

    def apply(self, edits: Sequence[WorkspaceEdit], *, expected_tree: str) -> WorkspaceSnapshot:
        current = self._read_all()
        if _tree_hash(current) != expected_tree:
            raise DomainError(
                "HARNESS_WORKSPACE_STALE", "Workspace tree changed before the edit", 409
            )
        staged: dict[str, str] = {}
        for edit in edits:
            relative = normalize_relative_path(edit.path)
            if relative in staged:
                raise DomainError("HARNESS_EDIT_DUPLICATED", "Duplicate edit for one path", 422)
            before = current.get(relative)
            if before is None:
                raise DomainError("HARNESS_FILE_NOT_FOUND", "Edit target does not exist", 404)
            if sha256_text(before) != edit.expected_sha256:
                raise DomainError("HARNESS_EDIT_STALE", "Edit pre-image hash does not match", 409)
            staged[relative] = edit.content
        for relative, content in staged.items():
            target = self._path(relative)
            if target.is_symlink():
                raise DomainError("HARNESS_PATH_DENIED", "Refusing to write through a symlink", 403)
            target.write_text(content, encoding="utf-8")
        return self.snapshot()

    def diff(self, tree_before: str) -> WorkspaceDiff:
        before = self._snapshots.get(tree_before)
        if before is None:
            raise DomainError("HARNESS_SNAPSHOT_UNKNOWN", "Unknown workspace snapshot", 404)
        after = self._read_all()
        changes: list[WorkspaceChange] = []
        patches: list[str] = []
        for path in sorted(set(before) | set(after)):
            old = before.get(path)
            new = after.get(path)
            if old == new:
                continue
            changes.append(
                WorkspaceChange(
                    path=path,
                    change="added" if old is None else "modified",
                    before_sha256=None if old is None else sha256_text(old),
                    after_sha256=sha256_text(new or ""),
                )
            )
            patches.extend(
                difflib.unified_diff(
                    (old or "").splitlines(keepends=True),
                    (new or "").splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
        return WorkspaceDiff(
            workspace_id=self.workspace_id,
            tree_before=tree_before,
            tree_after=_tree_hash(after),
            changes=tuple(changes),
            patch="".join(patches),
        )


class LocalExecutionBroker:
    """One-shot subprocess execution with grant checks and a sanitized environment."""

    def __init__(self, workspace: LocalWorkspace) -> None:
        self.workspace = workspace

    def execute(
        self,
        request: ExecutionRequest,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
    ) -> ExecutionReceipt:
        cancellation.ensure_active()
        if not grant.execute:
            raise DomainError("HARNESS_EXECUTE_DENIED", "Execution is not granted", 403)
        if not grant.can_execute(request.argv):
            raise DomainError("HARNESS_COMMAND_DENIED", "Command is outside the grant", 403)
        if request.timeout_seconds > grant.max_seconds:
            raise DomainError("HARNESS_TIMEOUT_DENIED", "Timeout exceeds the grant", 403)
        cwd = self.workspace._path(request.cwd)  # noqa: SLF001 - same package adapter
        if not cwd.is_dir():
            raise DomainError("HARNESS_CWD_DENIED", "Command cwd is not a directory", 403)
        environment = _safe_environment(self.workspace.root)
        (self.workspace.root / ".harness-tmp").mkdir(exist_ok=True)
        started = datetime.now(UTC)
        timed_out = False
        try:
            completed = subprocess.run(
                list(request.argv),
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        duration = (datetime.now(UTC) - started).total_seconds()
        stdout = _truncate(stdout)
        stderr = _truncate(stderr)
        combined = stdout + "\n" + stderr
        tests_run = 0
        tests_failed = 0
        for pattern in _TEST_PATTERNS:
            match = pattern.search(combined)
            if match:
                tests_run = max(tests_run, int(match.group(1)))
        for pattern in _FAILED_PATTERNS:
            match = pattern.search(combined)
            if match:
                tests_failed = max(tests_failed, int(match.group(1)))
        status = "timeout" if timed_out else ("passed" if exit_code == 0 else "failed")
        snapshot = self.workspace.snapshot()
        payload = {
            "operation_id": request.operation_id,
            "purpose": request.purpose,
            "status": status,
            "argv": list(request.argv),
            "cwd": request.cwd,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration_seconds": duration,
            "tree_hash": snapshot.tree_hash,
            "log_sha256": sha256_text(combined),
            "environment_hash": canonical_hash(environment),
            "tests_run": tests_run,
            "tests_failed": tests_failed,
        }
        return ExecutionReceipt(
            receipt_id=canonical_hash(payload),
            operation_id=request.operation_id,
            purpose=request.purpose,
            status=cast(Any, status),
            argv=request.argv,
            cwd=request.cwd,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            tree_hash=snapshot.tree_hash,
            log_sha256=sha256_text(combined),
            environment_hash=canonical_hash(environment),
            tests_run=tests_run,
            tests_failed=tests_failed,
        )


def _tool_specs() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="read_file",
            description="Read a UTF-8 file inside the granted workspace.",
            input_schema={"type": "object", "required": ["path"]},
            capability="read",
        ),
        ToolSpec(
            name="list_dir",
            description="List one directory inside the granted workspace.",
            input_schema={"type": "object", "required": ["path"]},
            capability="read",
        ),
        ToolSpec(
            name="apply_edits",
            description="Apply versioned edits after checking the expected tree hash.",
            input_schema={"type": "object", "required": ["edits", "expected_tree"]},
            capability="write",
        ),
        ToolSpec(
            name="run_tests",
            description="Run one test command inside the granted workspace.",
            input_schema={"type": "object", "required": ["argv"]},
            capability="execute",
        ),
    )


class HarnessToolRegistry:
    """Built-in tools; permission checks are repeated at the execution boundary."""

    def __init__(self, workspace: WorkspaceProvider, broker: ExecutionBroker) -> None:
        self.workspace = workspace
        self.broker = broker

    def specs(self) -> Sequence[ToolSpec]:
        return _tool_specs()

    def invoke(
        self,
        call: HarnessToolCall,
        context: ToolContext,
        cancellation: CancellationToken,
    ) -> HarnessToolResult:
        try:
            cancellation.ensure_active()
            if call.name == "read_file":
                return self._read(call, context)
            if call.name == "list_dir":
                return self._list(call, context)
            if call.name == "apply_edits":
                return self._apply(call, context)
            if call.name == "run_tests":
                return self._run(call, context, cancellation)
            return HarnessToolResult(
                call_id=call.call_id,
                status="error",
                code="HARNESS_TOOL_UNKNOWN",
                content="Unknown tool",
            )
        except DomainError as exc:
            status = "denied" if exc.status == 403 else "error"
            return HarnessToolResult(
                call_id=call.call_id,
                status=cast(Any, status),
                code=exc.code,
                content=str(exc),
            )

    def _path_arg(self, call: HarnessToolCall) -> str:
        value = call.arguments.get("path", ".")
        if not isinstance(value, str):
            raise DomainError("HARNESS_TOOL_ARGUMENT", "path must be a string", 422)
        return value

    def _read(
        self, call: HarnessToolCall, context: ToolContext
    ) -> HarnessToolResult:
        path = self._path_arg(call)
        if not context.grant.can_read(path):
            raise DomainError("HARNESS_READ_DENIED", "Read is outside the grant", 403)
        content = self.workspace.read(path)
        return HarnessToolResult(
            call_id=call.call_id, status="ok", content=content, data={"path": path}
        )

    def _list(
        self, call: HarnessToolCall, context: ToolContext
    ) -> HarnessToolResult:
        path = self._path_arg(call)
        if not context.grant.can_read(path):
            raise DomainError("HARNESS_READ_DENIED", "Read is outside the grant", 403)
        entries = list(self.workspace.list_dir(path))
        return HarnessToolResult(
            call_id=call.call_id,
            status="ok",
            content="\n".join(entries),
            data={"path": path, "entries": entries},
        )

    def _apply(
        self, call: HarnessToolCall, context: ToolContext
    ) -> HarnessToolResult:
        raw_edits = call.arguments.get("edits")
        expected_tree = call.arguments.get("expected_tree")
        if not isinstance(raw_edits, list) or not isinstance(expected_tree, str):
            raise DomainError("HARNESS_TOOL_ARGUMENT", "Invalid edit arguments", 422)
        try:
            edits = [WorkspaceEdit.model_validate(item) for item in raw_edits]
        except ValidationError as exc:
            raise DomainError("HARNESS_TOOL_ARGUMENT", "Invalid workspace edit", 422) from exc
        for edit in edits:
            if not context.grant.can_write(edit.path):
                raise DomainError("HARNESS_WRITE_DENIED", "Write is outside the grant", 403)
        snapshot = self.workspace.apply(edits, expected_tree=expected_tree)
        changes = [
            {
                "path": change.path,
                "change": change.change,
                "after_sha256": change.after_sha256,
            }
            for change in self.workspace.diff(expected_tree).changes
        ]
        return HarnessToolResult(
            call_id=call.call_id,
            status="ok",
            content="edits applied",
            data={"tree_hash": snapshot.tree_hash, "changes": changes},
        )

    def _run(
        self,
        call: HarnessToolCall,
        context: ToolContext,
        cancellation: CancellationToken,
    ) -> HarnessToolResult:
        raw_argv = call.arguments.get("argv")
        if not isinstance(raw_argv, list) or not all(isinstance(item, str) for item in raw_argv):
            raise DomainError("HARNESS_TOOL_ARGUMENT", "Invalid command argv", 422)
        argv = tuple(raw_argv)
        if not context.grant.can_execute(argv):
            raise DomainError("HARNESS_COMMAND_DENIED", "Command is outside the grant", 403)
        cwd = call.arguments.get("cwd", ".")
        timeout = call.arguments.get("timeout_seconds", 60)
        if not isinstance(cwd, str) or not isinstance(timeout, int | float):
            raise DomainError("HARNESS_TOOL_ARGUMENT", "Invalid command options", 422)
        receipt = self.broker.execute(
            ExecutionRequest(
                operation_id=context.operation_id,
                purpose="test",
                argv=argv,
                cwd=cwd,
                timeout_seconds=float(timeout),
            ),
            context.grant,
            cancellation,
        )
        return HarnessToolResult(
            call_id=call.call_id,
            status="ok" if receipt.status == "passed" else "error",
            content=receipt.stdout if receipt.status == "passed" else receipt.stderr,
            data={"receipt": receipt.model_dump(mode="json")},
            code="" if receipt.status == "passed" else "HARNESS_TESTS_FAILED",
        )


class LocalVerifier:
    """Deterministic verifier: a task is verified only by matching receipts."""

    def verify(self, request: VerificationRequest) -> VerificationReceipt:
        checks: list[VerificationCheck] = []
        used: list[str] = []
        for requirement in request.task.requirements:
            receipt = self._find(request, requirement)
            if receipt is None:
                checks.append(
                    VerificationCheck(
                        requirement_id=requirement.requirement_id,
                        status="missing",
                        detail="No matching execution receipt",
                    )
                )
                continue
            used.append(receipt.receipt_id)
            passed = receipt.status == "passed"
            if requirement.kind == "test":
                passed = passed and receipt.tests_run >= requirement.min_tests
            checks.append(
                VerificationCheck(
                    requirement_id=requirement.requirement_id,
                    status="passed" if passed else "failed",
                    detail=(
                        f"exit={receipt.exit_code} tests_run={receipt.tests_run} "
                        f"tests_failed={receipt.tests_failed}"
                    ),
                    receipt_id=receipt.receipt_id,
                )
            )
        if all(check.status == "passed" for check in checks):
            status = "verified"
        elif any(check.status == "failed" for check in checks):
            status = "failed"
        else:
            status = "incomplete"
        payload = {
            "task_id": request.task.task_id,
            "status": status,
            "tree_hash": request.snapshot.tree_hash,
            "checks": [check.model_dump(mode="json") for check in checks],
            "receipt_ids": used,
        }
        return VerificationReceipt(
            verification_id=canonical_hash(payload),
            task_id=request.task.task_id,
            status=cast(Any, status),
            tree_hash=request.snapshot.tree_hash,
            checks=tuple(checks),
            receipt_ids=tuple(used),
        )

    def _find(self, request: VerificationRequest, requirement: Any) -> ExecutionReceipt | None:
        for receipt in reversed(request.receipts):
            if receipt.tree_hash != request.snapshot.tree_hash:
                continue
            command = " ".join(receipt.argv)
            if any(token not in command for token in requirement.command_contains):
                continue
            if receipt.status not in {"passed", "failed", "timeout"}:
                continue
            return receipt
        return None


class SimpleContextManager:
    """Minimal context projection; H10 replaces it with compaction and paging."""

    def build(self, task: TaskBinding, events: Sequence[EventEnvelope]) -> ContextView:
        revision = canonical_hash(
            {
                "task_id": task.task_id,
                "acceptance_revision": task.acceptance_revision,
                "last_cursor": events[-1].cursor if events else 0,
            }
        )
        messages = (
            ModelMessage(
                role="system",
                content=(
                    "You are a coding harness. Use only granted tools. "
                    "A task is complete only after the verifier returns verified."
                ),
            ),
            ModelMessage(role="user", content=task.objective),
        )
        return ContextView(
            revision=revision,
            system_prompt=messages[0].content,
            messages=messages,
            estimated_tokens=sum(len(item.content) for item in messages) // 4,
        )


class InMemoryEventSink:
    """Deterministic event envelope store used by contract tests and fixtures."""

    def __init__(self) -> None:
        self._events: dict[UUID, list[EventEnvelope]] = {}

    def emit(self, draft: EventDraft) -> EventEnvelope:
        existing = self._events.setdefault(draft.run_id, [])
        envelope = EventEnvelope(
            **draft.model_dump(),
            cursor=len(existing) + 1,
        )
        existing.append(envelope)
        return envelope

    def events(self, run_id: UUID, after_cursor: int = 0) -> Sequence[EventEnvelope]:
        return tuple(item for item in self._events.get(run_id, []) if item.cursor > after_cursor)

    def last_cursor(self, run_id: UUID) -> int:
        return len(self._events.get(run_id, []))


class InMemoryBudgetLedger:
    """Reservation-first ledger; unknown outcomes keep their reservation."""

    def __init__(self, limits: BudgetLimits | None = None) -> None:
        self.limits = limits or BudgetLimits()
        self._reservations: dict[str, BudgetReservation] = {}
        self._task_order: dict[str, list[str]] = {}

    def _consumed(self, task_id: str, kind: str) -> tuple[int, int, int]:
        units = 0
        micros = 0
        calls = 0
        for reservation_id in self._task_order.get(task_id, []):
            item = self._reservations[reservation_id]
            if item.kind != kind:
                continue
            if item.status == "settled":
                units += item.actual_units
                micros += item.actual_micros
                calls += item.actual_units
            elif item.status in {"reserved", "unknown"}:
                units += item.reserved_units
                micros += item.reserved_micros
                calls += item.reserved_units
        return units, micros, calls

    def reserve(
        self,
        task_id: str,
        operation_id: str,
        kind: str,
        *,
        reserved_micros: int = 0,
        reserved_units: int = 1,
    ) -> BudgetReservation:
        model_calls = self._consumed(task_id, "model")[2]
        tool_calls = self._consumed(task_id, "tool")[2]
        cost = self._consumed(task_id, "model")[1] + self._consumed(task_id, "tool")[1]
        if kind == "model" and model_calls + reserved_units > self.limits.max_model_calls:
            raise DomainError("BUDGET_EXCEEDED", "Model call budget exhausted", 429)
        if kind == "tool" and tool_calls + reserved_units > self.limits.max_tool_calls:
            raise DomainError("BUDGET_EXCEEDED", "Tool call budget exhausted", 429)
        if cost + reserved_micros > self.limits.max_cost_micros:
            raise DomainError("BUDGET_EXCEEDED", "Cost budget exhausted", 429)
        reservation = BudgetReservation(
            reservation_id=canonical_hash(
                {
                    "task_id": task_id,
                    "operation_id": operation_id,
                    "kind": kind,
                    "n": len(self._reservations),
                }
            ),
            task_id=task_id,
            operation_id=operation_id,
            kind=cast(Any, kind),
            reserved_micros=reserved_micros,
            reserved_units=reserved_units,
        )
        self._reservations[reservation.reservation_id] = reservation
        self._task_order.setdefault(task_id, []).append(reservation.reservation_id)
        return reservation

    def settle(
        self,
        reservation_id: str,
        *,
        actual_micros: int,
        actual_units: int = 1,
    ) -> BudgetReservation:
        current = self._reservations[reservation_id]
        updated = current.model_copy(
            update={
                "status": "settled",
                "actual_micros": actual_micros,
                "actual_units": actual_units,
            }
        )
        self._reservations[reservation_id] = updated
        return updated

    def mark_unknown(self, reservation_id: str, reason: str) -> BudgetReservation:
        current = self._reservations[reservation_id]
        updated = current.model_copy(update={"status": "unknown", "reason": reason})
        self._reservations[reservation_id] = updated
        return updated

    def release(self, reservation_id: str) -> BudgetReservation:
        current = self._reservations[reservation_id]
        updated = current.model_copy(update={"status": "released", "actual_units": 0})
        self._reservations[reservation_id] = updated
        return updated

    def snapshot(self, task_id: str) -> BudgetSnapshot:
        model_calls = self._consumed(task_id, "model")[2]
        tool_calls = self._consumed(task_id, "tool")[2]
        cost = self._consumed(task_id, "model")[1] + self._consumed(task_id, "tool")[1]
        return BudgetSnapshot(
            task_id=task_id,
            model_calls=model_calls,
            tool_calls=tool_calls,
            cost_micros=cost,
        )

    def get(self, reservation_id: str) -> BudgetReservation:
        return self._reservations[reservation_id]


def python_argv(*arguments: str) -> tuple[str, ...]:
    return (sys.executable, *arguments)
