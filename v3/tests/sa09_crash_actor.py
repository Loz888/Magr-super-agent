"""Sole-Engine SIGKILL actor with a synthetic MockTransport and private input file."""

import json
import os
import signal
import sys
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import SecretStr

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.live import LiveProfile
from mgar_v3.routing.live import RouterAuthorization, RouterPermit, RouterProfile
from mgar_v3.routing.worker import RouterWorker
from mgar_v3.settings import Settings
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.source_eval import SourceEval, SourceEvalProvider
from mgar_v3.super_agent.source_eval_domain import (
    SourceDecision,
    SourceEvalProfile,
    SourceEvaluateRequest,
)
from mgar_v3.super_agent.source_eval_model import SourceEvalModel


def private_write(path, content, *, append=False):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_EXCL),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def main():
    state = Path(sys.argv[1])
    assert state.is_file() and state.stat().st_mode & 0o077 == 0
    data = json.loads(state.read_text())
    settings = Settings(
        database_url=data["dsn"], artifact_root=data["artifact_root"],
        project_id=data["project_id"], repository=data["repository"],
        local_actor=data["local_actor"], allowed_actors=data["allowed_actors"],
        graph_version=data["graph_version"], policy_version=data["policy_version"],
        allow_paid=True, allow_external_write=False,
        deepseek_api_key=SecretStr("synthetic-never-egress-key"),
    )
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    engine.source_ingress.cipher = BodyCipher(data["key"].encode())
    service = SourceEval(engine)
    permit = RouterPermit(
        RouterProfile.model_validate_json(json.dumps(data["router_profile"])),
        RouterAuthorization.model_validate_json(json.dumps(data["router_authorization"])),
        LiveProfile.model_validate_json(json.dumps(data["root_budget"])), engine.settings,
    )
    profile = SourceEvalProfile.model_validate_json(json.dumps(data["source_profile"]))
    batch = None

    def checkpoint(point):
        assert batch is not None
        private_write(data["marker"], json.dumps({
            "point": point, "batch_id": str(batch["batch_id"]),
            "transaction_status": engine.connection.info.transaction_status.name,
            "process_pid": os.getpid(), "backend_pid": engine.connection.info.backend_pid,
        }).encode())
        os.kill(os.getpid(), signal.SIGKILL)

    def respond(request):
        private_write(data["post_log"], b"POST\n", append=True)
        assert str(request.url) == "https://api.deepseek.com/v1/chat/completions"
        assert batch is not None
        value = {"schema_version": "source-router-proposals-v1", "proposals": [{
            "group_id": str(batch["source_snapshot"]["groups"][0]["group_id"]),
            "action": "create", "target_id": None, "reason_code": "ACTIONABLE",
        }]}
        events = [
            {"choices": [{"index": 0, "delta": {"content": json.dumps(value)}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        ]
        wire = b"".join(("data: " + json.dumps(value) + "\n\n").encode() for value in events)
        return httpx.Response(200, content=wire + b"data: [DONE]\n\n")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = SourceEvalModel(profile, permit, client=client, api_key="synthetic-controlled-key")
        if data["point"] == "after_snapshot_commit":
            queued = service.enqueue(
                data["cookie"], UUID(data["source_id"]),
                SourceEvaluateRequest(fast_batch_ids=(UUID(data["fast_batch_id"]),)), mode="live",
            )
            assert queued["evaluations"][0]["status"] == "prepared"
            batch = service.prepare("live")
            checkpoint(data["point"])
        batch = service.prepare("live")
        assert batch is not None and str(batch["batch_id"]) == data["batch_id"]
        if data["point"] == "after_reserve_before_send":
            model.propose = lambda request: checkpoint(data["point"])
        elif data["point"] == "after_known_result_before_wrap":
            service.wrap = lambda batch, proposals: checkpoint(data["point"])
        elif data["point"] == "after_ready_before_commit":
            service.commit = lambda batch_id, decisions, permit=None: checkpoint(data["point"])
        worker = RouterWorker(
            engine, SourceEvalProvider(service, model), pipeline=service,
            decision_decoder=lambda value: SourceDecision.model_validate_json(json.dumps(value)),
        )
        worker.tick()
    engine.close()
    raise RuntimeError("Synthetic crash checkpoint was not reached")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Child diagnostics deliberately omit configuration, text and secret values.
        sys.stderr.write("SOURCE_RECOVERY_CHILD_FAILED\n")
        raise SystemExit(2) from None
