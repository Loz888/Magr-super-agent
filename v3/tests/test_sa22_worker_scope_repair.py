"""New integration checks for denied live requests; historical PASS is excluded."""

import json

import httpx
import pytest
from sa13_runtime_support import BAD, GOOD
from sa22_coding_support import coding_environment


@pytest.mark.parametrize("operation", ["edit_from_read_receipt", "read_denied", "command_denied"])
def test_live_worker_sees_actual_scope_and_edit_hashes_without_expanding_authority(
    engine, tmp_path, operation
):
    seen = []

    def supplier(request):
        body = json.loads(request.content)
        seen.append(body)
        scope_messages = [
            m["content"]
            for m in body["messages"]
            if m["role"] == "system" and m["content"].startswith("Trusted execution scope")
        ]
        assert len(scope_messages) == 1
        scope = json.loads(scope_messages[0].split("\n")[1])
        assert scope["read_paths"] == ["src", "tests"]
        assert scope["write_paths"] == ["src"]
        assert scope["test_cwd"] == "src" and scope["network"] is False
        assert scope["test_argv"][0] == "python3"
        args = {"path": "src/calc.py"}
        name = "read_file"
        if operation == "read_denied":
            args = {"path": "outside-scope/private.py"}
        elif operation == "command_denied":
            name, args = "run_tests", {"argv": ["sh", "-c", "true"], "cwd": "src"}
        elif len(seen) == 2:
            tool = next(m for m in reversed(body["messages"]) if m["role"] == "tool")
            read = json.loads(tool["content"])
            assert read["path"] == "src/calc.py" and read["content"] == BAD
            assert len(read["sha256"]) == len(read["tree_hash"]) == 64
            name, args = (
                "apply_edits",
                {
                    "expected_tree": read["tree_hash"],
                    "edits": [
                        {"path": read["path"], "expected_sha256": read["sha256"], "content": GOOD}
                    ],
                },
            )
        elif len(seen) == 3:
            name, args = "run_tests", {"argv": scope["test_argv"], "cwd": scope["test_cwd"]}
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "new-scope-" + str(len(seen)),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        }
        if len(seen) > 3:
            message = {"role": "assistant", "content": "Proposed isolated change; not verified."}
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [
                    {
                        "finish_reason": "stop" if len(seen) > 3 else "tool_calls",
                        "message": message,
                    }
                ],
            },
        )

    env = coding_environment(engine, tmp_path, supplier_hook=supplier)
    try:
        text = env.experiment(env.input_id)
        result_file = next(env.experiment.artifacts.glob("*/result.json"))
        result = json.loads(result_file.read_text())
        assert result["business_task_completed"] is False
        if operation == "edit_from_read_receipt":
            # The intentionally incomplete trial Verifier may ask for another
            # response; it must not turn green merely because the public test did.
            assert 4 <= len(seen) <= env.experiment.runtime_profile.limits.max_model_calls
            assert result["verification"] == "incomplete"
            assert result["tree_before"] != result["tree_after"]
            assert "生成代码差异" in text
        else:
            assert len(seen) == 1 and result["coding_status"] == "denied"
            assert result["reason_code"] == (
                "HARNESS_READ_DENIED" if operation == "read_denied" else "HARNESS_COMMAND_DENIED"
            )
            assert result["tree_before"] == result["tree_after"]
        with engine.transaction() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) n FROM app.usage_calls WHERE status<>'known'"
                ).fetchone()["n"]
                == 0
            )
    finally:
        env.close()
