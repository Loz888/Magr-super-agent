"""Newly observed live failure: omitted/default cwd must use fixed test scope."""

import json

import httpx
import pytest
from sa13_runtime_support import GOOD
from sa22_coding_support import coding_environment


@pytest.mark.parametrize("cwd", [None, "."])
def test_test_command_default_cwd_uses_the_signed_profile_before_scope_check(engine, tmp_path, cwd):
    seen = []

    def supplier(request):
        body = json.loads(request.content)
        seen.append(body)
        name, args = "read_file", {"path": "src/calc.py"}
        if len(seen) == 2:
            read = json.loads(next(m["content"] for m in reversed(body["messages"])
                                   if m["role"] == "tool"))
            name, args = "apply_edits", {
                "expected_tree": read["tree_hash"],
                "edits": [{"path": read["path"], "expected_sha256": read["sha256"],
                           "content": GOOD}],
            }
        elif len(seen) == 3:
            scope = json.loads(next(m["content"] for m in body["messages"]
                                   if m["content"].startswith("Trusted execution scope"))
                               .split("\n")[1])
            name, args = "run_tests", {"argv": scope["test_argv"]}
            if cwd is not None:
                args["cwd"] = cwd
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "cwd-default-" + str(len(seen)), "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]}
        if len(seen) > 3:
            message = {"role": "assistant", "content": "Isolated proposal; independent pending."}
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"finish_reason": "stop" if len(seen) > 3 else "tool_calls",
                         "message": message}],
        })

    env = coding_environment(engine, tmp_path, supplier_hook=supplier)
    try:
        env.experiment(env.input_id)
        result = json.loads(next(env.experiment.artifacts.glob("*/result.json")).read_text())
        assert result["tree_after"] != result["tree_before"]
        assert result["verification"] == "incomplete"
        assert result["business_task_completed"] is False
        assert result["coding_status"] not in {"denied", "unknown"}
        with engine.transaction() as conn:
            commands = conn.execute(
                "SELECT result_code FROM app.sa_coding_operations "
                "WHERE capability='execute' AND step_key NOT LIKE '%:baseline'"
            ).fetchall()
        # The operation journal uses "ok" for an observed passed Broker receipt.
        assert commands and any(item["result_code"] == "ok" for item in commands)
        assert len(seen) >= 4
    finally:
        env.close()
