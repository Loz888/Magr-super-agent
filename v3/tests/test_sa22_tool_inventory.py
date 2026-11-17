"""New model selection boundary over the real protected workspace inventory."""

import json

import httpx
from sa13_runtime_support import GOOD
from sa22_coding_support import coding_environment


def test_live_tool_schema_uses_existing_scoped_paths_and_keeps_shape_only_diagnostics(
    engine, tmp_path,
):
    observed = []
    private_key = "private model text must not become an event key"

    def supplier(request):
        body = json.loads(request.content)
        observed.append(body)
        specs = {tool["function"]["name"]: tool["function"]["parameters"]
                 for tool in body["tools"]}
        readable = specs["read_file"]["properties"]["path"]["enum"]
        directories = specs["list_dir"]["properties"]["path"]["enum"]
        writable = specs["apply_edits"]["properties"]["edits"]["items"][
            "properties"]["path"]["enum"]
        assert set(readable) == {"src/calc.py", "tests/test_calc.py"}
        assert set(directories) == {"src", "tests"}
        assert writable == ["src/calc.py"]
        assert all(path not in readable + directories for path in (".", "src/", "../secret"))
        name, args = "read_file", {"path": writable[0], private_key: "not exported"}
        if len(observed) == 2:
            value = json.loads(next(m["content"] for m in reversed(body["messages"])
                                    if m["role"] == "tool"))
            name, args = "apply_edits", {
                "expected_tree": value["tree_hash"],
                "edits": [{"path": writable[0], "expected_sha256": value["sha256"],
                           "content": GOOD}],
            }
        elif len(observed) == 3:
            scope = json.loads(next(m["content"] for m in body["messages"]
                                    if m["content"].startswith("Trusted execution scope"))
                               .split("\n")[1])
            name, args = "run_tests", {"argv": scope["test_argv"], "cwd": scope["test_cwd"]}
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "inventory-" + str(len(observed)), "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]}
        if len(observed) > 3:
            message = {"role": "assistant", "content": "Isolated proposal, unverified."}
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"finish_reason": "stop" if len(observed) > 3 else "tool_calls",
                         "message": message}],
        })

    env = coding_environment(engine, tmp_path, supplier_hook=supplier)
    try:
        env.experiment(env.input_id)
        result = json.loads(next(env.experiment.artifacts.glob("*/result.json")).read_text())
        assert result["tree_before"] != result["tree_after"]
        assert result["verification"] == "incomplete"
        assert result["business_task_completed"] is False
        with engine.transaction() as conn:
            events = conn.execute(
                "SELECT safe_payload FROM app.run_events WHERE kind='source_trial_tool_shape'"
            ).fetchall()
        assert {row["safe_payload"]["tool_name"] for row in events} == {
            "read_file", "apply_edits", "run_tests",
        }
        assert private_key not in json.dumps(events)
        assert GOOD not in json.dumps(events)
        assert all("path" not in shape for row in events for shape in row["safe_payload"]["paths"])
    finally:
        env.close()
