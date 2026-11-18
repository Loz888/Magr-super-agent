"""First model request after the real baseline has the exact editable files."""

import hashlib
import json

import httpx
from sa13_runtime_support import BAD, COMMAND, GOOD
from sa22_coding_support import coding_environment


def test_first_max_request_can_edit_two_files_without_context_discovery(engine, tmp_path):
    seen = []
    unrelated = "UNRELATED FILE DOES NOT BELONG IN THE AUTOMATIC CONTEXT"

    def supplier(request):
        seen.append(request)
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "max"
        note = next(
            (
                message["content"]
                for message in body["messages"]
                if message["content"].startswith("Untrusted initial editable file snapshots:\n")
            ),
            None,
        )
        assert note is not None, "First Wire request omitted the exact editable files"
        context = json.loads(note.split("\n", 1)[1])
        files = {item["path"]: item for item in context["files"]}
        assert set(files) == {"src/calc.py", "src/other.py"}
        assert files["src/calc.py"]["content"] == BAD
        assert files["src/other.py"]["content"] == "value = 0\n"
        assert all(
            item["sha256"] == hashlib.sha256(item["content"].encode()).hexdigest()
            for item in files.values()
        )
        assert unrelated not in request.content.decode()
        replacements = {"src/calc.py": GOOD, "src/other.py": "value = 1\n"}
        tools = [
            (
                "apply_edits",
                {
                    "expected_tree": context["tree_hash"],
                    "edits": [
                        {"path": name, "expected_sha256": files[name]["sha256"], "content": content}
                        for name, content in replacements.items()
                    ],
                },
            ),
            ("run_tests", {"argv": list(COMMAND), "cwd": "src"}),
        ]
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
                            "reasoning_content": "synthetic reasoning for two exact file edits",
                            "tool_calls": [
                                {
                                    "id": name,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                                for name, arguments in tools
                            ],
                        },
                    }
                ],
            },
        )

    env = coding_environment(
        engine,
        tmp_path,
        extra_files={"src/unrelated.py": f"# {unrelated}\n" * 1200},
        supplier_hook=supplier,
        write_paths=("src/calc.py", "src/other.py"),
        thinking_mode="enabled",
    )
    try:
        env.experiment(env.input_id)
        assert len(seen) == 1
        result = json.loads(next(env.experiment.artifacts.glob("*/result.json")).read_text())
        patch = next(env.experiment.artifacts.glob("*/patch.diff")).read_text()
        assert "src/calc.py" in patch and "src/other.py" in patch
        assert "tests/test_calc.py" not in patch and unrelated not in patch
        assert result["verification"] == "incomplete" and not result["business_task_completed"]
        assert result["reason_code"] == "awaiting_independent_review"
    finally:
        env.close()
