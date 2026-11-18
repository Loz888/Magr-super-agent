"""A new exact request can follow a reviewed write with a proven zero-send stop."""

import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from sa06_support import payload, signed
from sa13_runtime_support import BAD, GOOD
from sa22_coding_support import coding_environment

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_case_experiments import (
    ReviewedInterruptedCase,
    SourceCaseExperiments,
)


def test_fresh_request_reuses_trial_after_exact_reviewed_local_no_send(engine, tmp_path):
    def supplier(request):
        if len(env.requests) != 1:
            # This transport has not made a physical request to any supplier.
            raise httpx.ReadError(
                "Controlled request stopped before external send", request=request
            )
        with engine.transaction() as conn:
            assignment = conn.execute(
                "SELECT assignment_id FROM app.sa_scheduler_assignments WHERE state='active'"
            ).fetchone()
        driver = env.experiment.runtime_profile.driver
        workspace = env.experiment.registry.open_task_workspace(
            driver.registration, str(assignment["assignment_id"]),
        )
        arguments = {
            "expected_tree": workspace.snapshot().tree_hash,
            "edits": [{
                "path": "src/calc.py", "expected_sha256": hashlib.sha256(BAD.encode()).hexdigest(),
                "content": GOOD,
            }],
        }
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "tool_calls": [{
                    "id": "retained-real-write", "type": "function",
                    "function": {"name": "apply_edits", "arguments": json.dumps(arguments)},
                }]},
            }],
        })

    env = coding_environment(engine, tmp_path, supplier_hook=supplier)
    try:
        env.experiment(env.input_id)
        with engine.transaction() as conn:
            prior = conn.execute("SELECT * FROM app.sa_coding_bindings").fetchone()
            original_run = engine._run(conn, prior["run_id"])
            [outstanding] = conn.execute(
                "SELECT call_id FROM app.usage_calls WHERE status='unknown'"
            ).fetchall()
            assert conn.execute(
                "SELECT 1 FROM app.sa_coding_operations WHERE capability='write' AND state='known'"
            ).fetchone()
            conn.execute(
                "UPDATE app.source_ingress_bodies SET expires_at=clock_timestamp() "
                "WHERE input_id=%s", (env.input_id,),
            )
        engine.source_ingress.purge(env.source.cookie, env.source.source_id)
        proof = "f" * 64
        call = outstanding["call_id"]
        review = ReviewedInterruptedCase(
            coding_id=prior["coding_id"], run_id=prior["run_id"],
            execution_epoch=original_run["execution_epoch"], no_send_call_id=call,
            no_send_proof_sha256=proof,
        )
        request = "请修复 src/calc.py 中 total 的计算错误，保持测试不变。本次定向复验。"

        def receive():
            incoming = env.runtime.receive(signed(
                env.source, payload(message_id="retry-" + uuid4().hex, body=request + " @_user_1"),
            ))
            return incoming.input_ids[0]

        manager = SourceCaseExperiments(
            engine, {"L01": (request, env.experiment)}, env.experiment,
            reviewed_interrupted_cases={"L01": review},
        )
        fresh = receive()
        with pytest.raises(DomainError, match="already has an input"):
            manager.admit(fresh)
        engine.settle_call(call, 0, usage={"prompt_tokens": 0, "completion_tokens": 0})
        with pytest.raises(DomainError, match="already has an input"):
            manager.admit(fresh)
        with engine.transaction() as conn:
            engine._event(
                conn, prior["run_id"], "source_trial_local_nosend_reconciled",
                call_id=str(call), proof_sha256=proof, physical_http=False, actual_cost_micros=0,
            )
        new = manager.admit(fresh)
        assert new["run_id"] != prior["run_id"] and manager.admit(fresh)["run_id"] == new["run_id"]
        with pytest.raises(DomainError, match="already has an input"):
            manager.admit(receive())
        with engine.transaction() as conn:
            state = conn.execute(
                "SELECT status,execution_epoch FROM app.runs WHERE run_id=%s", (prior["run_id"],),
            ).fetchone()
            assert state == {"status": "paused", "execution_epoch": review.execution_epoch}
            assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 2
    finally:
        env.close()
