"""Stop a real trial at its trusted green public tree, without buying a verdict."""

import hashlib
import json

import httpx
import pytest
from sa13_runtime_support import COMMAND, GOOD, WRONG
from sa22_coding_support import coding_environment


@pytest.mark.parametrize("first_public_failure", [False, True])
def test_public_result_stops_only_after_current_green_and_keeps_independent_gate(
    engine, tmp_path, first_public_failure,
):
    holder, calls = {}, []

    def supplier(request):
        calls.append(json.loads(request.content))
        expected_calls = 2 if first_public_failure else 1
        if len(calls) <= expected_calls:
            if len(calls) == 2:
                feedback = next(message["content"] for message in reversed(calls[-1]["messages"])
                                if message["role"] == "tool")
                assert "FAILED" in feedback and "AssertionError" in feedback
            env = holder["env"]
            with engine.transaction() as conn:
                assignment = conn.execute(
                    "SELECT assignment_id FROM app.sa_scheduler_assignments WHERE state='active'"
                ).fetchone()
            workspace = env.experiment.registry.open_task_workspace(
                env.experiment.runtime_profile.driver.registration,
                str(assignment["assignment_id"]),
            )
            snapshot = workspace.snapshot()
            replacement = WRONG if first_public_failure and len(calls) == 1 else GOOD
            tool_calls = [{
                "id": "edit-" + str(len(calls)), "type": "function",
                "function": {"name": "apply_edits", "arguments": json.dumps({
                    "expected_tree": snapshot.tree_hash,
                    "edits": [{"path": "src/calc.py",
                               "expected_sha256": hashlib.sha256(
                                   workspace.read("src/calc.py").encode()).hexdigest(),
                               "content": replacement}],
                })},
            }, {
                "id": "test-" + str(len(calls)), "type": "function",
                "function": {"name": "run_tests", "arguments": json.dumps({
                    "argv": list(COMMAND), "cwd": "src", "timeout_seconds": 20,
                })},
            }]
            message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "Public checks passed; await review."}
            finish = "stop"
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"finish_reason": finish, "message": message}],
        })

    env = coding_environment(engine, tmp_path, supplier_hook=supplier)
    holder["env"] = env
    try:
        env.experiment(env.input_id)
        result = json.loads(next(env.experiment.artifacts.glob("*/result.json")).read_text())
        assert result["reason_code"] == "awaiting_independent_review"
        assert result["coding_status"] == "incomplete"
        assert result["verification"] == "incomplete"
        assert result["business_task_completed"] is False
        assert len(calls) == (2 if first_public_failure else 1)
        with engine.transaction() as conn:
            records = conn.execute(
                "SELECT result,tests_run,tests_failed FROM app.sa_coding_verifications"
            ).fetchall()
            unknown = conn.execute(
                "SELECT count(*)n FROM app.usage_calls WHERE status<>'known'"
            ).fetchone()["n"]
        assert unknown == 0
        assert records and all(row["result"] == "incomplete" for row in records)
        assert all(row["tests_run"] == 2 and row["tests_failed"] == 0 for row in records)
    finally:
        env.close()
