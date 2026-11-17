"""New private-service mode, red feedback, and renewed original lease boundary."""

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sa13_runtime_support import GOOD, WRONG
from sa22_coding_support import coding_environment

import mgar_v3.super_agent.source_coding_runtime as coding_runtime
from mgar_v3.super_agent.source_coding_domain import CodingCancelRequest


@pytest.mark.parametrize("revoke", [False, True])
def test_private_process_permissions_red_output_and_derived_lease_follow_original_authority(
    engine, tmp_path, monkeypatch, revoke,
):
    sessions, seen = [], []
    original_init = coding_runtime._Attempt.__init__

    def capture(session, *args, **kwargs):
        original_init(session, *args, **kwargs)
        sessions.append(session)

    monkeypatch.setattr(coding_runtime._Attempt, "__init__", capture)
    holder = {}

    def supplier(request):
        body = json.loads(request.content)
        seen.append(body)
        name, args = "read_file", {"path": "src/calc.py"}
        turn = len(seen)
        if turn in {2, 5}:
            read = json.loads(next(m["content"] for m in reversed(body["messages"])
                                   if m["role"] == "tool"))
            name, args = "apply_edits", {
                "expected_tree": read["tree_hash"],
                "edits": [{"path": read["path"], "expected_sha256": read["sha256"],
                           "content": WRONG if turn == 2 else GOOD}],
            }
        elif turn in {3, 6}:
            scope = json.loads(next(m["content"] for m in body["messages"]
                                   if m["content"].startswith("Trusted execution scope"))
                               .split("\n")[1])
            name, args = "run_tests", {"argv": scope["test_argv"]}
        elif turn == 4:
            output = next(m["content"] for m in reversed(body["messages"])
                          if m["role"] == "tool")
            assert "FAILED" in output and "AssertionError" in output
            assert "PermissionError" not in output
            session = sessions[0]
            metadata = session.raw.root / ".mgar"
            assert stat.S_IMODE(metadata.stat().st_mode) & 0o077 == 0
            assert stat.S_IMODE(session.raw.root.stat().st_mode) & 0o077 == 0
        if turn == 5:
            session = sessions[0]
            assert session._lease is not None
            stale = session._lease.model_copy(update={
                "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            })
            session._lease = stale.model_copy(update={"lease_hash": stale.computed_hash()})
            if revoke:
                env = holder["env"]
                with engine.transaction() as conn:
                    binding = engine.source_coding.binding(conn, session.request.coding_id)
                    run = engine._run(conn, session.request.run_id)
                engine.source_coding.cancel(env.source.cookie, CodingCancelRequest(
                    request_id=uuid4(), coding_id=binding["coding_id"], run_id=run["run_id"],
                    expected_epoch=run["execution_epoch"], expected_revision=binding["revision"],
                    review_ref="revoke original authority before cached lease renewal",
                ))
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "sandbox-io-" + str(turn), "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]}
        if turn > 6:
            message = {"role": "assistant", "content": "Isolated proposal, unverified."}
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"finish_reason": "stop" if turn > 6 else "tool_calls",
                         "message": message}],
        })

    previous_mask = os.umask(0o077)
    env = None
    try:
        env = coding_environment(engine, tmp_path, supplier_hook=supplier)
        holder["env"] = env
        env.experiment(env.input_id)
        with engine.transaction() as conn:
            writes = conn.execute(
                "SELECT step_key FROM app.sa_coding_operations WHERE capability='write'"
            ).fetchall()
            unsettled = conn.execute(
                "SELECT count(*)n FROM app.usage_calls WHERE status<>'known'"
            ).fetchone()["n"]
        if revoke:
            assert len(writes) == 1
            assert len(seen) == 5
            # Revocation committed inside the final send guard, before the
            # response body was parsed. No validated bill survived; keep its
            # original unknown reservation rather than pretending it is known.
            assert unsettled == 1
        else:
            assert unsettled == 0
            assert len(writes) == 2
            result = json.loads(next(env.experiment.artifacts.glob("*/result.json")).read_text())
            assert result["verification"] == "incomplete"
            assert result["business_task_completed"] is False
            assert result["coding_status"] not in {"denied", "unknown"}
            with engine.transaction() as conn:
                records = conn.execute(
                    "SELECT tests_run,tests_failed FROM app.sa_coding_verifications"
                ).fetchall()
            assert records and any(row["tests_run"] == 2 and row["tests_failed"] == 0
                                   for row in records)
    finally:
        if env is not None:
            env.close()
        os.umask(previous_mask)
