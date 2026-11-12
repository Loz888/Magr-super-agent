"""Reference Harness V4 runtime.

The loop is deliberately small: it demonstrates the contract order (budget
reservation, model call, permission check, tool execution, verifier receipt)
without becoming a second business authority.  Production code in H03-H11
replaces the adapters, not the order.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from mgar_v3.domain import DomainError
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
    HarnessResult,
    HarnessStatus,
    HarnessToolCall,
    HarnessToolResult,
    ModelAdapter,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TaskBinding,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    VerificationReceipt,
    VerificationRequest,
    Verifier,
    WorkspaceProvider,
)


class UnknownModelOutcome(DomainError):
    """The request may have reached the provider; the result must not be retried."""

    def __init__(self, message: str = "Model outcome is unknown") -> None:
        super().__init__("HARNESS_MODEL_UNKNOWN", message, 409)


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


class HarnessRuntime:
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
        model_id: str = "deepseek-chat",
        price_version: str = "harness-contract-v1",
        max_output_tokens: int = 2048,
        reservation_micros: int = 2000,
        max_steps: int = 12,
    ) -> None:
        self.model = model
        self.tools = tools
        self.workspace = workspace
        self.broker = broker
        self.context = context
        self.verifier = verifier
        self.events = events
        self.budgets = budgets
        self.model_id = model_id
        self.price_version = price_version
        self.max_output_tokens = max_output_tokens
        self.reservation_micros = reservation_micros
        self.max_steps = max_steps

    def run(
        self,
        task: TaskBinding,
        grant: CapabilityGrant,
        *,
        cancellation: CancellationToken | None = None,
    ) -> HarnessResult:
        token = cancellation or CancellationToken()
        self._authorize(task, grant)
        view = self.context.build(task, self.events.events(task.run_id))
        history: list[ModelMessage] = list(view.messages)
        receipts: list[ExecutionReceipt] = []
        model_calls = 0
        tool_calls = 0
        revision = 0
        declared = self._declared_tools()
        self._emit(task, "run_started", revision, {"task_kind": task.task_kind})
        last_verification: VerificationReceipt | None = None
        unresolved: list[str] = []
        for step in range(self.max_steps):
            if token.cancelled:
                return self._finish(task, "cancelled", "Cancelled before the next step")
            revision += 1
            operation_id = f"{task.task_id}:model:{step + 1}"
            reservation = self.budgets.reserve(
                task.task_id,
                operation_id,
                "model",
                reserved_micros=self.reservation_micros,
                reserved_units=1,
            )
            self._emit(task, "model_requested", revision, {"operation_id": operation_id})
            request = ModelRequest(
                operation_id=operation_id,
                task=task,
                messages=tuple(history),
                tools=tuple(declared.values()),
                model_id=self.model_id,
                price_version=self.price_version,
                max_output_tokens=self.max_output_tokens,
                reservation_micros=self.reservation_micros,
                context_revision=view.revision,
                workspace_tree=self.workspace.snapshot().tree_hash,
            )
            try:
                response = self.model.complete(request, token)
            except UnknownModelOutcome as exc:
                self.budgets.mark_unknown(reservation.reservation_id, str(exc))
                self._emit(task, "model_unknown", revision, {"code": exc.code})
                return self._finish(task, "unknown", "Model outcome is unknown")
            except DomainError as exc:
                self.budgets.release(reservation.reservation_id)
                if exc.code == "HARNESS_CANCELLED":
                    return self._finish(task, "cancelled", "Cancelled during the model step")
                raise
            if not response.usage.known:
                self.budgets.mark_unknown(
                    reservation.reservation_id, "Model usage is unavailable"
                )
                self._emit(
                    task,
                    "model_unknown",
                    revision,
                    {"code": "MODEL_USAGE_UNKNOWN"},
                )
                return self._finish(
                    task, "unknown", "Model usage is unavailable; reservation retained"
                )
            self.budgets.settle(
                reservation.reservation_id,
                actual_micros=response.usage.cost_micros,
                actual_units=1,
            )
            model_calls += 1
            self._emit(
                task,
                "model_completed",
                revision,
                {
                    "finish_reason": response.finish_reason,
                    "tool_calls": len(response.tool_calls),
                    "cost_micros": response.usage.cost_micros,
                    "cached": response.cached,
                },
            )
            if token.cancelled or response.finish_reason == "cancelled":
                return self._finish(task, "cancelled", "Cancelled during the model step")
            if response.finish_reason in {"length", "error"}:
                # Failed or truncated responses may still carry proposed tool
                # calls and authoritative usage. The usage was settled above,
                # but no proposal is executable without an authoritative
                # "tool_calls" completion signal.
                suffix = f":{response.error_code}" if response.error_code else ""
                unresolved = [f"model:{response.finish_reason}{suffix}"]
                break
            if response.tool_calls:
                if response.finish_reason != "tool_calls":
                    unresolved = ["model:tool_calls_without_authoritative_finish"]
                    break
                denied, executed = self._dispatch_tools(
                    task,
                    grant,
                    token,
                    response,
                    step,
                    history,
                    receipts,
                    declared,
                )
                tool_calls += executed
                if denied is not None:
                    return denied
                continue
            verification = self.verifier.verify(
                VerificationRequest(
                    task=task,
                    snapshot=self.workspace.snapshot(),
                    receipts=tuple(receipts),
                )
            )
            last_verification = verification
            self._emit(
                task,
                "verification_result",
                revision,
                {
                    "status": verification.status,
                    "checks": [check.model_dump(mode="json") for check in verification.checks],
                },
            )
            if verification.status == "verified":
                return self._finish(
                    task,
                    "verified",
                    "Verifier accepted the workspace tree",
                    verification=verification,
                    model_calls=model_calls,
                    tool_calls=tool_calls,
                )
            unresolved = [
                check.requirement_id
                for check in verification.checks
                if check.status != "passed"
            ]
            return self._finish(
                task,
                "incomplete",
                "Verification did not pass; no further tool calls were proposed",
                verification=verification,
                unresolved=tuple(unresolved),
                model_calls=model_calls,
                tool_calls=tool_calls,
            )
        return self._finish(
            task,
            "incomplete",
            "Step budget or verification requirements were not satisfied",
            verification=last_verification,
            unresolved=tuple(unresolved),
            model_calls=model_calls,
            tool_calls=tool_calls,
        )

    def _dispatch_tools(
        self,
        task: TaskBinding,
        grant: CapabilityGrant,
        token: CancellationToken,
        response: ModelResponse,
        step: int,
        history: list[ModelMessage],
        receipts: list[ExecutionReceipt],
        declared: dict[str, ToolSpec],
    ) -> tuple[HarnessResult | None, int]:
        executed = 0
        history.append(
            ModelMessage(
                role="assistant",
                content=response.text,
                tool_calls=response.tool_calls,
            )
        )
        for index, call in enumerate(response.tool_calls):
            if token.cancelled:
                return self._finish(task, "cancelled", "Cancelled before the tool call"), executed
            operation_id = f"{task.task_id}:tool:{step + 1}:{index + 1}"
            denied = self._precheck(call, task, grant, declared)
            if denied is not None:
                return denied, executed
            reservation = self.budgets.reserve(
                task.task_id, operation_id, "tool", reserved_units=1
            )
            self._emit(
                task,
                "tool_started",
                step + 1,
                {"operation_id": operation_id, "name": call.name},
            )
            result = self.tools.invoke(
                call,
                ToolContext(
                    task=task,
                    grant=grant,
                    operation_id=operation_id,
                    workspace_tree=self.workspace.snapshot().tree_hash,
                ),
                token,
            )
            executed += 1
            self.budgets.settle(reservation.reservation_id, actual_micros=0, actual_units=1)
            self._emit(
                task,
                "tool_completed",
                step + 1,
                {
                    "operation_id": operation_id,
                    "name": call.name,
                    "status": result.status,
                    "code": result.code,
                },
            )
            if result.status == "denied":
                return (
                    self._finish(
                        task, "denied", f"Tool {call.name} was denied", tool_calls=executed
                    ),
                    executed,
                )
            if result.status == "cancelled":
                return (
                    self._finish(task, "cancelled", "Tool was cancelled", tool_calls=executed),
                    executed,
                )
            self._collect_receipt(result, receipts)
            history.append(
                ModelMessage(
                    role="tool",
                    name=call.name,
                    tool_call_id=call.call_id,
                    content=result.content,
                )
            )
        return None, executed

    def _collect_receipt(
        self, result: HarnessToolResult, receipts: list[ExecutionReceipt]
    ) -> None:
        raw = result.data.get("receipt")
        if not isinstance(raw, dict):
            return
        try:
            receipt = ExecutionReceipt.model_validate(raw)
        except ValueError:
            return
        receipts.append(receipt)

    def _declared_tools(self) -> dict[str, ToolSpec]:
        declared: dict[str, ToolSpec] = {}
        for spec in self.tools.specs():
            if spec.name in declared:
                raise DomainError(
                    "HARNESS_TOOL_DUPLICATE", "Tool registry declared a duplicate name", 409
                )
            declared[spec.name] = spec
        return declared

    def _precheck(
        self,
        call: HarnessToolCall,
        task: TaskBinding,
        grant: CapabilityGrant,
        declared: dict[str, ToolSpec],
    ) -> HarnessResult | None:
        spec = declared.get(call.name)
        if spec is None:
            return self._denied(
                task,
                call,
                "tool is not declared by the registry",
                "HARNESS_TOOL_UNDECLARED",
            )
        if spec.capability == "read":
            paths = _path_arguments(call.arguments)
            if paths is None or any(not grant.can_read(path) for path in paths):
                return self._denied(
                    task, call, "read path is outside the grant", "HARNESS_READ_DENIED"
                )
            return None
        if spec.capability == "write":
            paths = _path_arguments(call.arguments)
            if paths is None or any(not grant.can_write(path) for path in paths):
                return self._denied(
                    task, call, "write path is outside the grant", "HARNESS_WRITE_DENIED"
                )
            return None
        argv = _tool_list(call.arguments.get("argv"))
        if argv is None or not grant.can_execute(argv):
            return self._denied(
                task, call, "command is outside the grant", "HARNESS_COMMAND_DENIED"
            )
        return None

    def _denied(
        self,
        task: TaskBinding,
        call: HarnessToolCall,
        reason: str,
        code: str,
    ) -> HarnessResult:
        self._emit(
            task,
            "tool_denied",
            1,
            {"call_id": call.call_id, "name": call.name, "node": "runtime", "code": code},
        )
        return self._finish(task, "denied", f"Tool {call.name} denied: {reason}")

    def _authorize(self, task: TaskBinding, grant: CapabilityGrant) -> None:
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
                operation_id=f"{task.task_id}:event",
                execution_epoch=task.execution_epoch,
                revision=revision,
                kind=kind,
                payload=payload,
            )
        )

    def _finish(
        self,
        task: TaskBinding,
        status: str,
        summary: str,
        *,
        verification: VerificationReceipt | None = None,
        unresolved: Sequence[str] = (),
        model_calls: int = 0,
        tool_calls: int = 0,
    ) -> HarnessResult:
        self._emit(task, "run_finished", 0, {"status": status})
        cursor = self.events.events(task.run_id)
        return HarnessResult(
            task_id=task.task_id,
            status=cast(HarnessStatus, status),
            summary=summary,
            verification=verification,
            events_cursor=cursor[-1].cursor if cursor else 0,
            unresolved=tuple(unresolved),
            model_calls=model_calls,
            tool_calls=tool_calls,
        )
