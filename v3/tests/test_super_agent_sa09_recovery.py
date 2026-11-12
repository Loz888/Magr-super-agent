"""Original-transaction and real-process Source evaluation recovery boundaries."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from psycopg.conninfo import conninfo_to_dict
from sa08_support import original_state
from sa09_support import configure_eval, enqueue, fast_batch, ingest_case, prepared, setup
from test_super_agent_sa09 import frames, live

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.routing.worker import RouterWorker
from mgar_v3.settings import ROOT
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.policy_domain import PolicyRevoke
from mgar_v3.super_agent.source_eval import SourceEval, SourceEvalProvider
from mgar_v3.super_agent.source_eval_domain import SourceDecision
from mgar_v3.super_agent.source_eval_model import SourceEvalModel


def provider(env, batch, permit, profile):
    posts = []

    def respond(request):
        posts.append(request)
        group_id = str(batch["source_snapshot"]["groups"][0]["group_id"])
        return httpx.Response(200, content=frames(group_id))

    client = httpx.Client(transport=httpx.MockTransport(respond))
    model = SourceEvalModel(profile, permit, client=client, api_key="synthetic-no-platform-key")
    return SourceEvalProvider(env.service, model), client, posts


def router_calls(engine):
    with engine.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM app.router_calls").fetchone()["n"]


def test_model_provider_rejects_outer_engine_transaction_before_reserve_or_post(engine):
    env = setup(engine)
    permit, profile = live(env)
    batch = prepared(env, mode="live")
    value, client, posts = provider(env, batch, permit, profile)
    before = original_state(engine)
    caught = None

    class SyntheticOuterRollback(Exception):
        pass

    try:
        try:
            with engine.transaction():
                try:
                    value.propose(batch)
                except DomainError as error:
                    caught = error
                raise SyntheticOuterRollback()
        except SyntheticOuterRollback:
            pass
        assert router_calls(engine) == 0
        assert original_state(engine) == before
        assert posts == [], "A model POST escaped an uncommitted outer reservation"
        assert caught is not None and caught.code == "SOURCE_EVAL_TRANSACTION"
    finally:
        client.close()


@pytest.mark.parametrize("bound", ["max_context_bytes", "max_output_bytes"])
def test_runtime_model_bounds_cannot_exceed_current_signed_source_settings(engine, bound):
    env = setup(engine)
    permit, profile = live(env)
    configure_eval(env, env.eval_settings.model_copy(update={bound: 4096}))
    batch = prepared(env, mode="live")
    value, client, posts = provider(env, batch, permit, profile)
    before = original_state(engine)
    try:
        with pytest.raises(DomainError) as error:
            value.propose(batch)
        assert error.value.code == "SOURCE_EVAL_MODEL_BOUND_MISMATCH"
        assert not posts and router_calls(engine) == 0
        assert original_state(engine) == before
    finally:
        client.close()


def append_post(path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(b"POST\n")
        output.flush()
        os.fsync(output.fileno())


def post_count(path):
    if not path.exists():
        return 0
    lines = path.read_text().splitlines()
    assert all(line == "POST" for line in lines)
    assert path.stat().st_mode & 0o077 == 0
    return len(lines)


def restart_provider(env, batch, permit, profile, post_log):
    def respond(request):
        append_post(post_log)
        group_id = str(batch["source_snapshot"]["groups"][0]["group_id"])
        return httpx.Response(200, content=frames(group_id))

    client = httpx.Client(transport=httpx.MockTransport(respond))
    model = SourceEvalModel(profile, permit, client=client, api_key="synthetic-never-egress-key")
    return SourceEvalProvider(env.service, model), client


def source_worker(env, value):
    return RouterWorker(
        env.engine, value, pipeline=env.service,
        decision_decoder=lambda data: SourceDecision.model_validate_json(json.dumps(data)),
    )


def financial_rows(engine, batch_id):
    with engine.transaction() as conn:
        call = conn.execute(
            "SELECT * FROM app.router_calls WHERE batch_id=%s", (batch_id,),
        ).fetchone()
        budget = conn.execute(
            "SELECT * FROM app.authorization_budgets WHERE profile_hash=%s",
            (call["budget_profile_hash"],),
        ).fetchone() if call is not None else None
        return call, budget


def assert_source_body_absent_from_result_facts(engine, private):
    with engine.transaction() as conn:
        for table in (
            "router_batches", "router_calls", "sa_source_eval_snapshots",
            "sa_source_eval_results", "sa_source_eval_decisions", "router_audit",
        ):
            # Table names are a fixed test-only allowlist, not runtime input.
            rows = conn.execute("SELECT to_jsonb(t) AS row FROM app." + table + " t").fetchall()
            assert private not in json.dumps(rows, default=str)


def private_child_state(env, permit, profile, point, receipt, batch, directory):
    directory.mkdir(mode=0o700)
    dsn = env.engine.settings.database_url.get_secret_value()
    assert conninfo_to_dict(dsn)["dbname"].startswith("mgar_v3_test_")
    data = {
        "dsn": dsn, "artifact_root": str(env.engine.settings.artifact_root),
        "project_id": env.engine.settings.project_id,
        "repository": env.engine.settings.repository,
        "local_actor": env.engine.settings.local_actor,
        "allowed_actors": env.engine.settings.allowed_actors,
        "graph_version": env.engine.settings.graph_version,
        "policy_version": env.engine.settings.policy_version,
        "key": env.key.decode(), "cookie": env.cookie, "point": point,
        "source_id": str(env.installation.scope.source_instance_id),
        "fast_batch_id": str(fast_batch(env, receipt)),
        "batch_id": str(batch["batch_id"]) if batch else None,
        "router_profile": permit.profile.model_dump(mode="json"),
        "router_authorization": permit.authorization.model_dump(mode="json"),
        "root_budget": permit.root_budget.model_dump(mode="json"),
        "source_profile": profile.model_dump(mode="json"),
        "marker": str(directory / "checkpoint.json"),
        "post_log": str(directory / "synthetic-posts.log"),
    }
    state = directory / "private-state.json"
    descriptor = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(data, output)
        output.flush()
        os.fsync(output.fileno())
    return state, Path(data["marker"]), Path(data["post_log"])


@pytest.mark.parametrize("point", [
    "after_snapshot_commit", "after_reserve_before_send",
    "after_known_result_before_wrap", "after_ready_before_commit",
])
def test_real_sigkill_source_pipeline_recovers_without_reposting_or_business_effects(
    engine, tmp_path, point,
):
    env = setup(engine)
    permit, profile = live(env)
    private = "SA09_PRIVATE_SIGKILL_SOURCE_BODY_" + point
    receipt = ingest_case(env, body=private)
    batch = None if point == "after_snapshot_commit" else prepared(env, receipt, mode="live")
    before = original_state(engine)
    state, marker, post_log = private_child_state(
        env, permit, profile, point, receipt, batch, tmp_path / "private-child",
    )
    settings = engine.settings
    engine.close()  # Child must acquire the original sole-writer PG advisory lock.
    child_env = dict(os.environ, PYTHONPATH=str(ROOT / "src") + os.pathsep + str(ROOT / "tests"))
    log = state.parent / "child.log"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        child = subprocess.run(
            [sys.executable, str(ROOT / "tests/sa09_crash_actor.py"), str(state)],
            cwd=ROOT, env=child_env, stdout=output, stderr=subprocess.STDOUT, timeout=30,
        )
    assert child.returncode == -signal.SIGKILL, "Child did not reach the real SIGKILL boundary"
    assert marker.exists() and marker.stat().st_mode & 0o077 == 0
    checkpoint = json.loads(marker.read_text())
    assert checkpoint["point"] == point and checkpoint["transaction_status"] == "IDLE"
    assert checkpoint["process_pid"] != os.getpid()
    batch_id = UUID(checkpoint["batch_id"])
    assert post_count(post_log) == int(point in {
        "after_known_result_before_wrap", "after_ready_before_commit",
    })
    recovered = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        assert recovered.connection.info.backend_pid != checkpoint["backend_pid"]
        recovered.source_ingress.cipher = BodyCipher(env.key)
        recovered.recover()  # Existing original recovery marks reservations unknown safely.
        env.engine, env.service = recovered, SourceEval(recovered)
        permit.settings = recovered.settings
        with recovered.transaction() as conn:
            batch = env.service._batch(conn, batch_id)
        value, client = restart_provider(env, batch, permit, profile, post_log)
        try:
            if point == "after_reserve_before_send":
                assert env.service.prepare("live") is None
                with pytest.raises(DomainError) as error:
                    value.propose(batch)
                assert error.value.code == "SOURCE_EVAL_CALL_ALREADY_RECORDED"
                configure_eval(env, env.eval_settings)
                retry = enqueue(env, receipt, mode="live")["evaluations"][0]
                assert retry["reason_code"] == "SOURCE_EVAL_INPUT_ALREADY_ATTEMPTED"
                assert post_count(post_log) == 0
            else:
                worker = source_worker(env, value)
                assert worker.tick() == batch_id
                assert worker.tick() is None and post_count(post_log) == 1
                with recovered.transaction() as conn:
                    rows = conn.execute(
                        "SELECT payload FROM app.sa_source_eval_decisions WHERE batch_id=%s",
                        (batch_id,),
                    ).fetchall()
                    assert len(rows) == 1
                decisions = [SourceDecision.model_validate_json(json.dumps(row["payload"]))
                             for row in rows]
                assert env.service.commit(batch_id, decisions) == [rows[0]["payload"]]
            call, budget = financial_rows(recovered, batch_id)
            assert call is not None and budget is not None and budget["used_calls"] == 1
            with recovered.transaction() as conn:
                assert conn.execute(
                    "SELECT count(*) AS n FROM app.sa_budget_calls WHERE call_id=%s",
                    (call["call_id"],),
                ).fetchone()["n"] == 1
            assert call["result"] is None and call["response_hash"] is None
            if point == "after_reserve_before_send":
                assert call["status"] == "unknown" and call["settled_micros"] is None
                assert budget["reserved_micros"] == permit.profile.reservation_micros
                assert budget["settled_micros"] == 0
            else:
                assert call["status"] == "known" and call["settled_micros"] == 120
                assert budget["reserved_micros"] == 0 and budget["settled_micros"] == 120
            after = original_state(recovered)
            assert {k: v for k, v in before.items() if k != "router_calls"} == {
                k: v for k, v in after.items() if k != "router_calls"
            }
            assert_source_body_absent_from_result_facts(recovered, private)
        finally:
            client.close()
    finally:
        recovered.close()


@pytest.mark.parametrize("change", ["revoke", "edit"])
def test_inflight_source_change_rejects_suggestion_but_preserves_actual_known_cost(engine, change):
    env = setup(engine)
    permit, profile = live(env)
    private = "SA09_PRIVATE_INFLIGHT_SOURCE_BODY_" + change
    receipt = ingest_case(env, body=private)
    batch = prepared(env, receipt, mode="live")
    before = original_state(engine)
    posts = []

    def respond(request):
        posts.append(request)
        assert engine.connection.info.transaction_status.name == "IDLE"
        if change == "revoke":
            engine.source_policies.revoke(env.cookie, env.policy_id, PolicyRevoke(
                expected_revision=env.policy_revision,
                review_ref="SA09-inflight-trusted-withdrawal",
            ))
        else:
            ingest_case(env, delivery="changed-during-model", event="changed-during-model",
                        revision="2", order=2, operation="edit", body="New authorized revision")
        return httpx.Response(200, content=frames(str(batch["source_snapshot"]["groups"][0][
            "group_id"
        ])))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = SourceEvalModel(profile, permit, client=client, api_key="synthetic-controlled-key")
        value = SourceEvalProvider(env.service, model)
        worker = source_worker(env, value)
        assert worker.tick() == batch["batch_id"]
        assert worker.tick() is None and len(posts) == 1
    with engine.transaction() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM app.sa_source_eval_decisions WHERE batch_id=%s",
            (batch["batch_id"],),
        ).fetchone()["n"] == 0
        assert env.service._batch(conn, batch["batch_id"])["status"] == "failed"
    call, budget = financial_rows(engine, batch["batch_id"])
    assert call["status"] == "known" and call["settled_micros"] == 120
    assert budget["settled_micros"] == 120 and budget["reserved_micros"] == 0
    after = original_state(engine)
    assert {k: v for k, v in before.items() if k != "router_calls"} == {
        k: v for k, v in after.items() if k != "router_calls"
    }
    assert_source_body_absent_from_result_facts(engine, private)
