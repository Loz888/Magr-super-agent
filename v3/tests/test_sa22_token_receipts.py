"""Original token quota must consume validated receipts and bounded request bytes."""

import json
from dataclasses import replace

import httpx
import pytest
import sa22_model_support
from sa13_runtime_support import GOOD
from sa22_coding_support import coding_environment

from mgar_v3.super_agent.source_coding_live_model import ExperimentalDeepSeekModel
from mgar_v3.super_agent.source_coding_runtime import SourceCodingRuntime


@pytest.mark.parametrize("known", [True, False])
def test_scoped_thinking_reserves_actual_wire_bound_and_settles_canonical_tokens(
    engine, tmp_path, monkeypatch, known,
):
    original_definition = sa22_model_support.definition

    def limited_definition(**kwargs):
        value = original_definition(**kwargs)
        return value.model_copy(update={
            "caps": value.caps.model_copy(update={
                "total_tokens": 100000, "day_tokens": 100000, "hour_tokens": 100000,
            }),
        })

    # Install the small real quota through the original save path BEFORE trial
    # signing. No live policy or pre-existing authorization is rewritten.
    monkeypatch.setattr(sa22_model_support, "definition", limited_definition)
    seen = []
    reservations = []

    def supplier(request):
        body = json.loads(request.content)
        seen.append(body)
        with engine.transaction() as conn:
            row = conn.execute(
                "SELECT b.reserved_tokens FROM app.sa_budget_calls b "
                "JOIN app.usage_calls u USING(call_id) WHERE u.status='reserved'"
            ).fetchone()
        assert row
        assert len(request.content) + body["max_tokens"] + 1024 <= row["reserved_tokens"]
        assert row["reserved_tokens"] < 131072 + 8192
        reservations.append(row["reserved_tokens"])
        if not known:
            return httpx.Response(200, json={"error": "controlled missing usage"})
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
        message = {
            "role": "assistant", "content": None, "reasoning_content": "synthetic " * 200,
            "tool_calls": [{"id": "tokens-" + str(len(seen)), "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}],
        }
        if len(seen) > 3:
            message = {"role": "assistant", "content": "Unverified isolated proposal.",
                       "reasoning_content": "synthetic final"}
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"finish_reason": "stop" if len(seen) > 3 else "tool_calls",
                         "message": message}],
        })

    env = coding_environment(engine, tmp_path, supplier_hook=supplier)
    try:
        runtime = replace(
            env.experiment.runtime_profile,
            model=ExperimentalDeepSeekModel(
                env.client, "controlled-not-a-key", env.experiment.runtime_profile.price,
                context_tokens=131072, controlled_transport=True, thinking_mode="enabled",
            ),
            reservation_micros=139264, reserved_tokens=139264,
        )
        env.experiment.runtime_profile = runtime
        engine.source_coding.runtime = SourceCodingRuntime(engine.source_coding, runtime)
        env.experiment(env.input_id)
        view = engine.project_budgets.inspect(
            env.source.cookie, "project", engine.settings.project_id
        )
        with engine.transaction() as conn:
            rows = conn.execute("SELECT status,usage FROM app.usage_calls").fetchall()
        if known:
            assert len(seen) >= 4 and len(rows) == len(seen)
            assert all(row["status"] == "known" for row in rows)
            assert all(row["usage"]["prompt_tokens"] == row["usage"]["input_tokens"] == 100
                       for row in rows)
            assert all(row["usage"]["completion_tokens"] == row["usage"]["output_tokens"] == 20
                       for row in rows)
            assert view["usage"]["total_tokens"] == len(rows) * 120
            assert view["usage"]["unknown_token_calls"] == 0
        else:
            assert len(seen) == len(rows) == 1
            assert rows[0]["status"] == "unknown"
            assert view["usage"]["unknown_calls"] == 1
            assert view["usage"]["total_tokens"] == reservations[0]
            assert view["usage"]["unknown_token_calls"] == 1
    finally:
        env.close()
