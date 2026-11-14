"""Remaining first formal coverage: original Memory/Action/Outbox and correlation keys."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from sa15_support import path, ref, setup, snapshot

from mgar_v3.domain import DomainError, digest
from mgar_v3.memory.model import MemoryDraft, WorkingDetail
from mgar_v3.memory.store import MemoryStore
from mgar_v3.super_agent.source_coding_domain import CodingAdmitRequest, CodingTarget
from mgar_v3.super_agent.source_recall_domain import (
    RecallPageRequest,
    RecallResolveRequest,
    RecallWalkRequest,
)


def second_task(env):
    engine = env.engine
    with engine.transaction() as conn:
        source = engine.source_correlations._input(conn, env.extra_input_id)
        target = CodingTarget(
            project_id=engine.settings.project_id,
            thread_id=uuid4(),
            ticket_id=uuid4(),
            run_id=uuid4(),
            mandate_id=uuid4(),
        )
        origin = env.mandate.origin.model_copy(
            update={"input": source.ref, "expires_at": source.expires}
        )
        mandate = env.mandate.model_copy(
            update={
                "coding_id": uuid4(),
                "target": target,
                "origin": origin,
                "context_refs": (source.ref,),
                "context_sha256": engine.source_coding.context_sha256(source),
            }
        )
        engine.source_coding._create_original(conn, mandate)
        run = engine._run(conn, target.run_id)
        engine.source_coding.admit(
            env.cookie,
            CodingAdmitRequest(
                request_id=uuid4(),
                run_id=target.run_id,
                expected_epoch=0,
                source_input=source.ref,
                expected_policy_revision=origin.policy_revision,
                expected_revision=0,
                target_sha256=engine.source_scheduler._target_hash(conn, run),
                profile_sha256=mandate.worker_profile_sha256,
                review_ref="SA15-second-trusted-target",
            ),
        )
    return target


def test_worker_recalls_other_task_same_current_authorized_conversation(engine, tmp_path):
    env = setup(engine, tmp_path)
    other = second_task(env)
    before = snapshot(engine)
    result = env.recall.resolve(
        env.worker_caller, RecallResolveRequest(path=path(ref("run", other.run_id)))
    )
    assert result["node"]["state"] == "queued"
    assert result["node"]["ref"]["id"] == str(other.run_id)
    assert snapshot(engine) == before


@pytest.mark.parametrize("caller", ["owner", "worker_caller"])
def test_existing_v33_working_memory_context_relation_and_lifecycle(engine, tmp_path, caller):
    env = setup(engine, tmp_path)
    mid = uuid4()
    draft = MemoryDraft(
        project_id=engine.settings.project_id,
        repository=engine.settings.repository.lower(),
        actor=env.mandate.origin.engine_actor,
        memory_kind="working",
        scope="repository",
        subject_key="sa15-current-working",
        summary_hash=env.report_hash,
        evidence_ids=[env.evidence_id],
        artifact_hashes=[env.report_hash],
        confidence=0.8,
        verification_status="supported",
        detail=WorkingDetail(
            run_id=env.target.run_id, expires_at=datetime.now(UTC) + timedelta(minutes=10)
        ),
    )
    with engine.transaction() as conn:
        MemoryStore(engine.settings)._insert(conn, mid, draft)
        conn.execute(
            "INSERT INTO knowledge.context_edges(edge_id,project_id,repository,actor,"
            "source_kind,source_id,target_kind,target_id,relation,run_id) "
            "VALUES(%s,%s,%s,%s,'run',%s,'memory',%s,'cites',%s)",
            (
                uuid4(),
                engine.settings.project_id,
                engine.settings.repository.lower(),
                env.mandate.origin.engine_actor,
                env.target.run_id,
                mid,
                env.target.run_id,
            ),
        )
    request = RecallPageRequest(path=path(env.run_ref, ref("memory", mid)))
    before = snapshot(engine)
    value = env.recall.page(getattr(env, caller), request)
    assert value["content"] and value["content_is_untrusted"] is True
    assert snapshot(engine) == before
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE knowledge.memory_records SET status='stale',stale_at=clock_timestamp(),"
            "stale_reason='source changed' WHERE memory_id=%s",
            (mid,),
        )
    with pytest.raises(DomainError):
        env.recall.page(getattr(env, caller), request)


def test_original_action_review_outbox_unknown_are_readonly_real_status(engine, tmp_path):
    env = setup(engine, tmp_path)
    action, outbox = uuid4(), uuid4()
    with engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.actions(action_id,run_id,action_type,target,payload,payload_hash,"
            "base_revision,execution_epoch,fencing_token,dedupe_key,status,expires_at) "
            "VALUES(%s,%s,'issue_comment','fixture://sa15-no-send',%s,%s,0,%s,1,%s,"
            "'outcome_unknown',clock_timestamp()+interval '10 minutes')",
            (
                action,
                env.target.run_id,
                Jsonb({"body": "PRIVATE_DELIVERY_BODY"}),
                digest({"body": "PRIVATE_DELIVERY_BODY"}),
                env.run_request.epoch,
                str(action),
            ),
        )
        conn.execute(
            "INSERT INTO app.outbox(outbox_id,action_id,status) VALUES(%s,%s,'outcome_unknown')",
            (outbox, action),
        )
    before = snapshot(engine)
    for kind, identifier in [("review", action), ("delivery", outbox)]:
        result = env.recall.resolve(
            env.owner, RecallResolveRequest(path=path(env.run_ref, ref(kind, identifier)))
        )
        assert result["node"]["state"] == "outcome_unknown"
        assert "PRIVATE_DELIVERY_BODY" not in json.dumps(result)
        with pytest.raises(DomainError):
            env.recall.page(
                env.owner, RecallPageRequest(path=path(env.run_ref, ref(kind, identifier)))
            )
    assert snapshot(engine) == before


def test_owner_sees_minimal_tombstone_when_body_ttl_expires_before_metadata(engine, tmp_path):
    env = setup(engine, tmp_path)
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.source_ingress_bodies SET expires_at=clock_timestamp()-interval '1s' "
            "WHERE input_id=%s",
            (env.input_id,),
        )
    result = env.recall.resolve(env.owner, RecallResolveRequest(path=path(env.source_ref)))
    assert result["node"]["availability"] == "unavailable"
    assert result["node"]["content_available"] is False
    with pytest.raises(DomainError):
        env.recall.page(env.owner, RecallPageRequest(path=path(env.source_ref)))


def test_original_sa10_correlation_indexes_source_to_real_task_without_duplication(engine):
    from sa10_support import link
    from sa10_support import setup as correlation_setup

    from mgar_v3.super_agent.source_recall_access import RecallCaller

    env = correlation_setup(engine)
    link(env)
    before = snapshot(engine)
    result = engine.source_recall.walk(
        RecallCaller("owner", env.cookie),
        RecallWalkRequest(path=path(ref("source", env.native_input_id)), max_hops=3),
    )
    assert str(env.target.run_id) in {node["ref"]["id"] for node in result["nodes"]}
    assert snapshot(engine) == before


def test_all_formal_migration_checksums_and_current_artifact_references(engine):
    with engine.transaction() as conn:
        installed = conn.execute(
            "SELECT name,sha256 FROM app.schema_migrations ORDER BY name"
        ).fetchall()
    assert len(installed) == 44
    assert all(
        hashlib.sha256((Path("migrations") / r["name"]).read_bytes()).hexdigest() == r["sha256"]
        for r in installed
    )
    for p in Path("config").glob("source-*.json"):
        for artifact in json.loads(p.read_text()).get("artifacts", []):
            assert (
                hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest()
                == artifact["sha256"]
            ), artifact["path"]


def test_recall_original_async_job_reports_actual_status_without_dispatching_another(
    engine, tmp_path, monkeypatch
):
    from sa14_support import actual, start, wait_for

    from mgar_v3.super_agent.source_recall_access import RecallCaller

    with actual(engine, tmp_path, monkeypatch, slow_tests=True) as env:
        start(env)

        def job():
            rows = env.async_service.jobs(env.cookie, env.mandate.coding_id)["jobs"]
            return rows[0] if rows else None

        observed = wait_for(job, timeout=30)
        result = engine.source_recall.resolve(
            RecallCaller("owner", env.cookie),
            RecallResolveRequest(
                path=path(ref("run", env.target.run_id), ref("job", observed["job_id"]))
            ),
        )
        assert result["node"]["state"] in {
            "prepared",
            "running",
            "stop_requested",
            "unknown",
            "known",
        }
        assert result["node"]["content_available"] is False
        assert result["node"]["grants_authority"] is False


@pytest.mark.parametrize("boundary", ["revoke_during_io", "worker_cannot_inherit_owner_acl"])
def test_original_knowledge_acl_is_not_replaced_by_source_or_cas_relation(
    engine, tmp_path, monkeypatch, boundary
):
    env = setup(engine, tmp_path)
    document, version, chunk, snapshot_id = uuid4(), uuid4(), uuid4(), uuid4()
    locator = {"start_line": 1, "end_line": 2}
    origin = {
        "kind": "knowledge",
        "snapshot_id": str(snapshot_id),
        "chunk_id": str(chunk),
        "document_id": str(document),
        "version_id": str(version),
        "raw_hash": env.report_hash,
        "locator": locator,
    }
    with engine.transaction() as conn:
        conn.execute(
            "INSERT INTO knowledge.documents(document_id,project_id,repository,owner,"
            "source_uri,title) VALUES(%s,%s,%s,'owner','fixture://sa15/report',"
            "'private knowledge')",
            (document, engine.settings.project_id, engine.settings.repository.lower()),
        )
        conn.execute(
            "INSERT INTO knowledge.document_versions(version_id,document_id,source_version,"
            "raw_hash,media_type) VALUES(%s,%s,'controlled',%s,'text/plain')",
            (version, document, env.report_hash),
        )
        conn.execute(
            "INSERT INTO knowledge.chunks(chunk_id,version_id,content_hash,text_content,"
            "locator,terms,token_count) VALUES(%s,%s,%s,'private knowledge',%s,%s,2)",
            (chunk, version, env.report_hash, Jsonb(locator), Jsonb({"private": 1})),
        )
        conn.execute(
            "INSERT INTO knowledge.snapshots(snapshot_id,project_id,repository,actor,"
            "status,config,config_hash,dimension) VALUES(%s,%s,%s,'owner','building',%s,%s,1)",
            (
                snapshot_id,
                engine.settings.project_id,
                engine.settings.repository.lower(),
                Jsonb({}),
                digest({}),
            ),
        )
        conn.execute(
            "INSERT INTO knowledge.snapshot_chunks(snapshot_id,chunk_id,embedding) "
            "VALUES(%s,%s,'[1]')",
            (snapshot_id, chunk),
        )
        conn.execute(
            "UPDATE knowledge.snapshots SET status='ready' WHERE snapshot_id=%s", (snapshot_id,)
        )
        conn.execute(
            "INSERT INTO app.r4_reads(evidence_id,role,task_id,origin) "
            "VALUES(%s,'knowledge',%s,%s)",
            (env.evidence_id, str(uuid4()), Jsonb(origin)),
        )
    if boundary == "worker_cannot_inherit_owner_acl":
        with pytest.raises(DomainError):
            env.recall.page(env.worker_caller, RecallPageRequest(path=env.evidence_path))
        return
    read = engine.artifacts.read

    def committed_revoke(content_hash):
        value = read(content_hash)
        with engine.transaction() as conn:
            conn.execute(
                "UPDATE knowledge.documents SET status='revoked',acl_revision=acl_revision+1 "
                "WHERE document_id=%s",
                (document,),
            )
        return value

    monkeypatch.setattr(engine.artifacts, "read", committed_revoke)
    with pytest.raises(DomainError):
        env.recall.page(env.owner, RecallPageRequest(path=env.evidence_path))
