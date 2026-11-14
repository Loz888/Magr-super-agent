"""Independent SA14 final dispatch checks; original tests and code are unmodified."""
import json
from pathlib import Path
from uuid import uuid4
import pytest
from conftest import database, settings, engine, controlled_model_network_only
from sa14_support import actual, rows, start
from mgar_v3.harness.broker import IsolatedExecutionBroker
from mgar_v3.super_agent.source_async_domain import (
    AsyncJobCancelRequest, AsyncRestriction, AsyncRestrictRequest,
)

EVIDENCE = Path(__file__).parent
if (EVIDENCE / "evidence-path").exists():
    EVIDENCE = Path((EVIDENCE / "evidence-path").read_text().strip())

@pytest.mark.parametrize("change", ["restriction", "cancel"])
def test_committed_async_change_after_created_inspection_prevents_actual_docker_start(
    engine, tmp_path, monkeypatch, change,
):
    with actual(engine, tmp_path, monkeypatch, slow_tests=True) as env:
        inspect = IsolatedExecutionBroker.inspect_bound_command
        docker = IsolatedExecutionBroker._docker
        observed, starts = [], []

        def after_created_inspection(broker, name, labels):
            status = inspect(broker, name, labels)
            if status.state == "created" and not observed:
                assert engine.connection.info.transaction_status.name == "IDLE"
                jobs = rows(env, "sa_async_jobs")
                assert len(jobs) == 1 and jobs[0]["state"] == "running"
                job = jobs[0]
                before = env.async_service.get(env.cookie, env.mandate.coding_id)
                if change == "restriction":
                    result = env.async_service.restrict(
                        env.cookie,
                        AsyncRestrictRequest(
                            request_id=uuid4(), coding_id=env.mandate.coding_id,
                            run_id=env.target.run_id, expected_epoch=env.run_request.epoch,
                            expected_revision=before["revision"],
                            restriction=AsyncRestriction(deny_commands=("python3",)),
                            review_ref="independent-SA14-after-inspect-before-start-deny",
                        ),
                    )
                    assert result["state"] == "received"
                else:
                    result = env.async_service.cancel(
                        env.cookie,
                        AsyncJobCancelRequest(
                            request_id=uuid4(), coding_id=env.mandate.coding_id,
                            run_id=env.target.run_id, job_id=job["job_id"],
                            expected_revision=job["revision"],
                            review_ref="independent-SA14-after-inspect-before-start-cancel",
                        ),
                    )
                    assert result["state"] == "stop_requested"
                assert engine.connection.info.transaction_status.name == "IDLE"
                current = env.async_service.get(env.cookie, env.mandate.coding_id)
                observed.append({
                    "container_id":status.container_id, "resource_name":name,
                    "job_id":str(job["job_id"]), "change":change,
                    "state_after_commit":result["state"],
                    "input_revision_before":before["input_revision"],
                    "input_revision_after":current["input_revision"],
                    "original_engine_transaction_after_commit":engine.connection.info.transaction_status.name,
                })
            return status

        def record_start(broker, argv, **kwargs):
            if argv and argv[0] == "start":
                assert observed, "unexpected start before the controlled commit"
                current = env.async_service.get(env.cookie, env.mandate.coding_id)
                job = rows(env, "sa_async_jobs")[0]
                starts.append({
                    "argv":list(argv), "input_revision_at_start":current["input_revision"],
                    "job_state_at_start":job["state"],
                    "transaction_at_start":engine.connection.info.transaction_status.name,
                })
            return docker(broker, argv, **kwargs)

        monkeypatch.setattr(IsolatedExecutionBroker, "inspect_bound_command", after_created_inspection)
        monkeypatch.setattr(IsolatedExecutionBroker, "_docker", record_start)
        start(env)
        joined = env.async_runtime.join(env.mandate.coding_id, 60)
        data = {
            "change":change, "joined":joined, "commits":observed, "actual_docker_starts":starts,
            "model_dispatches":len(env.model.requests),
            "runtime_failures":{str(k):v for k,v in env.async_runtime.failures.items()},
            "jobs":[{"job_id":str(j["job_id"]),"state":j["state"],"result_code":j["result_code"]} for j in rows(env,"sa_async_jobs")],
        }
        (EVIDENCE / ("dispatch-"+change+"-observations.json")).write_text(json.dumps(data,indent=2)+"\n")
        assert joined, data
        assert observed, data
        assert starts == [], "Docker start occurred after committed async change: "+json.dumps(data)
        assert rows(env,"actions") == rows(env,"outbox") == []
