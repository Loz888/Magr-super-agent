"""Each full-file edit can use the previous edit receipt without rereading files."""

import json

import httpx
from sa13_runtime_support import COMMAND, GOOD
from sa22_coding_support import coding_environment


def test_two_max_thinking_full_file_edits_use_actual_intermediate_tree(engine, tmp_path):
    calls = []
    files = {}
    write_paths = ("src/other.py", "src/calc.py")

    def supplier(request):
        wire = json.loads(request.content)
        assert wire["thinking"] == {"type": "enabled"} and wire["reasoning_effort"] == "max"
        calls.append(wire)
        step = len(calls)
        if step == 1:
            seed = next(
                message["content"].split("\n", 1)[1]
                for message in wire["messages"]
                if message["content"].startswith("Untrusted initial editable file snapshots:\n")
            )
            baseline = json.loads(seed)
            files.update((item["path"], item) for item in baseline["files"])
            expected_tree = baseline["tree_hash"]
        else:
            receipt = next(
                message["content"]
                for message in reversed(wire["messages"])
                if message["role"] == "tool"
            )
            expected_tree = json.loads(receipt)["tree_hash"]
        if step <= 2:
            path = write_paths[step - 1]
            name = "apply_edits"
            arguments = {
                "expected_tree": expected_tree,
                "edits": [
                    {
                        "path": path,
                        "expected_sha256": files[path]["sha256"],
                        "content": "value = 1\n" if step == 1 else GOOD,
                    }
                ],
            }
        else:
            name = "run_tests"
            arguments = {"argv": list(COMMAND), "cwd": "src"}
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "synthetic staged edit planning",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "id": f"stage-{step}",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    env = coding_environment(
        engine,
        tmp_path,
        supplier_hook=supplier,
        write_paths=write_paths,
        thinking_mode="enabled",
    )
    try:
        target = env.experiment.submit(env.input_id)
        result = env.experiment.run_one()
        assert result and result["changed"]
        patch = (env.experiment.artifacts / str(target["run_id"]) / "patch.diff").read_text()
        assert all(path in patch for path in write_paths)
        assert len(calls) == 3
        assert any("one file per model response" in m["content"] for m in calls[0]["messages"])
        assert result["verification"] == "incomplete" and not result["business_task_completed"]
        assert result["reason_code"] == "awaiting_independent_review"
    finally:
        env.close()
