"""First integration of fixed H11 contracts alongside the H01/H02 namespace."""

import inspect
import json
from collections import UserDict
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness import (
    DEEPSEEK_CHAT,
    CancellationToken,
    CapabilityGrant,
    DeepSeekChatAdapter,
    HarnessToolCall,
    HarnessToolRegistry,
    LocalExecutionBroker,
    LocalWorkspace,
    ModelMessage,
    ModelRequest,
    TaskBinding,
    ToolContext,
    VerificationRequirement,
    sha256_text,
)
from mgar_v3.harness import adapters as reference
from mgar_v3.harness import source_coding_adapters as fixed
from mgar_v3.harness.contracts import (
    ExecutionReceipt,
    ExecutionRequest,
    WorkspaceEdit,
)
from mgar_v3.harness.incremental import (
    apply_replacements,
    check_output_budget,
    output_bytes,
    protocol,
    staged_content,
)

OLD = "def add(a, b):\n    return a - b\n"
NEW = "def add(a, b):\n    return a + b\n"


def binding():
    return TaskBinding(
        task_id="sa13-compatibility-original-reference", thread_id=uuid4(), run_id=uuid4(),
        execution_epoch=3, actor="owner", repository="example/project", base_commit="a" * 40,
        workspace_id="sa13-reference-workspace", acceptance_revision="acceptance-1",
        objective="Check the full-content contract through unchanged H01 and H02 ports",
        requirements=(VerificationRequirement(requirement_id="unit"),),
    )


def grant():
    return CapabilityGrant(
        actor="owner", workspace_id="sa13-reference-workspace", repository="example/project",
        read_paths=("",), write_paths=("calc.py",), execute=False,
    )


def old_receipt():
    return {
        "receipt_id": "1" * 64, "operation_id": "sa13-old-reference-execution",
        "purpose": "test", "status": "passed", "argv": ["python", "-m", "unittest"],
        "cwd": ".", "exit_code": 0, "stdout": "Ran 1 test", "stderr": "",
        "duration_seconds": 0.01, "tree_hash": "2" * 64, "log_sha256": "3" * 64,
        "environment_hash": "4" * 64, "tests_run": 1, "tests_failed": 0,
    }


def test_historical_full_content_wire_and_sa12_epoch_default_remain_supported():
    wire = {"path": "calc.py", "expected_sha256": sha256_text(OLD), "content": NEW}
    edit = WorkspaceEdit.model_validate_json(json.dumps(wire))
    assert edit.content == NEW
    assert edit.model_dump(mode="json") == wire
    assert "replacements" not in edit.model_dump_json()
    assert output_bytes([edit]) == len(NEW.encode())
    assert staged_content(edit, OLD) == NEW and protocol([edit]) == "full"
    check_output_budget([edit], max_output_bytes=len(NEW.encode()), max_replacements=1)
    with pytest.raises(DomainError) as oversized:
        check_output_budget([edit], max_output_bytes=len(NEW.encode()) - 1, max_replacements=1)
    assert oversized.value.code == "PATCH_LIMIT"
    request = ExecutionRequest(operation_id="old-sa12-operation", argv=("pytest",))
    assert request.execution_epoch == 0
    assert ExecutionRequest(
        operation_id="current-sa12-operation", argv=("pytest",), execution_epoch=3,
    ).execution_epoch == 3
    with pytest.raises(ValidationError):
        ExecutionRequest(operation_id="invalid-epoch", argv=("pytest",), execution_epoch=-1)


@pytest.mark.parametrize("replacement", [None, [], [{"old_text": "-", "new_text": "+"}]])
@pytest.mark.parametrize("include_content", [False, True])
@pytest.mark.parametrize("mapping", [dict, UserDict])
def test_replacement_key_is_rejected_even_for_general_mapping_or_empty_payload(
    replacement, include_content, mapping,
):
    wire = {"path": "calc.py", "expected_sha256": sha256_text(OLD), "replacements": replacement}
    if include_content:
        wire["content"] = NEW
    with pytest.raises(ValidationError):
        WorkspaceEdit.model_validate(mapping(wire))


@pytest.mark.parametrize("value", [None, 7, True])
def test_full_content_is_still_required_and_cannot_be_replaced_by_an_optional_field(value):
    wire = {"path": "calc.py", "expected_sha256": sha256_text(OLD)}
    if value is not None:
        wire["content"] = value
    with pytest.raises(ValidationError):
        WorkspaceEdit.model_validate(wire)


def test_direct_incremental_helper_fails_closed_without_touching_old_content():
    with pytest.raises(DomainError) as denied:
        apply_replacements(OLD, ())
    assert denied.value.code == "SOURCE_WORKER_INCREMENTAL_NOT_AVAILABLE"
    assert denied.value.status == 403
    assert OLD == "def add(a, b):\n    return a - b\n"


def test_old_receipt_json_loads_with_explicitly_untrusted_null_provenance():
    archived = old_receipt()
    original_hash = digest(archived)
    receipt = ExecutionReceipt.model_validate_json(json.dumps(archived))
    assert receipt.provenance is None
    serialized = receipt.model_dump(mode="json")
    assert serialized == {**archived, "provenance": None}
    # Old archives remain readable, while new full serialization honestly adds
    # null provenance. Its changed canonical hash cannot authenticate old proof.
    assert digest(serialized) != original_hash
    assert receipt.receipt_id == archived["receipt_id"]
    assert ExecutionReceipt.model_validate_json(receipt.model_dump_json()) == receipt


def test_fixed_strict_registry_does_not_replace_public_reference_constructor(tmp_path):
    root = tmp_path / "reference"
    root.mkdir()
    (root / "calc.py").write_text(OLD)
    workspace = LocalWorkspace(root, workspace_id="sa13-reference-workspace")
    broker = LocalExecutionBroker(workspace)
    registry = HarnessToolRegistry(workspace, broker)
    assert HarnessToolRegistry is reference.HarnessToolRegistry
    assert isinstance(registry, reference.HarnessToolRegistry)
    assert fixed.HarnessToolRegistry is not HarnessToolRegistry
    authority = inspect.signature(fixed.HarnessToolRegistry).parameters["authority"]
    assert authority.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        fixed.HarnessToolRegistry(workspace, broker)
    task = binding()
    before = workspace.snapshot()
    result = registry.invoke(
        HarnessToolCall(call_id="reference-read", name="read_file", arguments={"path": "calc.py"}),
        ToolContext(
            task=task, grant=grant(), operation_id="reference-read",
            workspace_tree=before.tree_hash,
        ),
        CancellationToken(),
    )
    assert result.status == "ok" and result.content == OLD
    assert workspace.snapshot() == before


@pytest.mark.parametrize("replacement", ["ABSENT", None, []])
def test_h02_transport_preserves_full_content_and_forbidden_replacement_key(tmp_path, replacement):
    """Existing transport preserves both allowed edits and newly forbidden keys."""
    root = tmp_path / "reference"
    root.mkdir()
    (root / "calc.py").write_text(OLD)
    workspace = LocalWorkspace(root, workspace_id="sa13-reference-workspace")
    registry = HarnessToolRegistry(workspace, LocalExecutionBroker(workspace))
    task, before = binding(), workspace.snapshot()
    arguments = {
        "edits": [{
            "path": "calc.py", "expected_sha256": sha256_text(OLD),
            "content": NEW,
        }],
        "expected_tree": before.tree_hash,
    }
    if replacement != "ABSENT":
        arguments["edits"][0]["replacements"] = replacement
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "sa13-wire-edit", "function": {
                "name": "apply_edits", "arguments": json.dumps(arguments),
            },
        }]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
    ]
    content = b"".join(
        ("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks
    ) + b"data: [DONE]\n\n"
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = DeepSeekChatAdapter(api_key="sa13-controlled-not-a-live-key", client=client)
        response = model.complete(ModelRequest(
            operation_id="sa13-wire-model", task=task,
            messages=(ModelMessage(role="user", content="Check contract compatibility"),),
            tools=tuple(registry.specs()), model_id=DEEPSEEK_CHAT.model_id,
            price_version=DEEPSEEK_CHAT.price.version, max_output_tokens=2048,
            reservation_micros=10000, context_revision="sa13-contract-v1",
            workspace_tree=before.tree_hash,
        ), CancellationToken())
    assert len(sent) == 1
    assert response.tool_calls[0].arguments == arguments
    result = registry.invoke(
        response.tool_calls[0], ToolContext(
            task=task, grant=grant(), operation_id="sa13-wire-edit",
            workspace_tree=before.tree_hash,
        ), CancellationToken(),
    )
    if replacement == "ABSENT":
        assert result.status == "ok"
        assert workspace.snapshot().tree_hash != before.tree_hash
        assert (root / "calc.py").read_text() == NEW
    else:
        assert result.status == "error" and result.code == "HARNESS_TOOL_ARGUMENT"
        assert workspace.snapshot() == before
        assert (root / "calc.py").read_text() == OLD
