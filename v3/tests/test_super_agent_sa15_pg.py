"""First formal SA15 tests: original Engine, real isolated PG, actual local CAS."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from sa04_support import envelope
from sa15_support import TEXT, expire_source, legacy_memory, path, ref, setup, snapshot

from mgar_v3.domain import DomainError
from mgar_v3.sources.identity_domain import RevisionCommand, RoleChange
from mgar_v3.super_agent.source_recall_access import RecallAccess, RecallCaller
from mgar_v3.super_agent.source_recall_domain import (
    RecallPageRequest,
    RecallResolveRequest,
    RecallWalkRequest,
)
from mgar_v3.super_agent.source_recall_store import RecallStore


@pytest.mark.parametrize("caller", ["owner", "worker_caller"])
def test_metadata_is_bodyless_current_and_readonly(engine, tmp_path, caller):
    env = setup(engine, tmp_path)
    before = snapshot(engine)
    value = env.recall.resolve(
        getattr(env, caller), RecallResolveRequest(path=path(env.source_ref))
    )
    assert value["node"]["state"] == "current"
    assert value["node"]["grants_authority"] is False
    assert TEXT not in json.dumps(value, ensure_ascii=False)
    assert snapshot(engine) == before
    assert engine.connection.info.transaction_status == TransactionStatus.IDLE


def test_original_relation_chain_and_actual_current_status(engine, tmp_path):
    env = setup(engine, tmp_path)
    chain = path(
        env.source_ref,
        ref("thread", env.target.thread_id),
        ref("ticket", env.target.ticket_id),
        env.run_ref,
        env.artifact_ref,
    )
    value = env.recall.page(env.owner, RecallPageRequest(path=chain))
    assert "真实状态待验证" in value["content"]
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.runs SET status='waiting_input' WHERE run_id=%s", (env.target.run_id,)
        )
    resolved = env.recall.resolve(env.owner, RecallResolveRequest(path=path(env.run_ref)))
    assert resolved["node"]["state"] == "waiting_input"
    assert "succeeded" not in json.dumps(resolved)


@pytest.mark.parametrize("size", [128, 4096])
def test_source_raw_utf8_pages_reconstruct_without_copying_plaintext(engine, tmp_path, size):
    env = setup(engine, tmp_path)
    before = snapshot(engine)
    cursor, content = None, ""
    for _ in range(150):
        result = env.recall.page(
            env.owner, RecallPageRequest(path=path(env.source_ref), max_bytes=size, cursor=cursor)
        )
        assert len(result["content"].encode()) <= size
        content += result["content"]
        if result["eof"]:
            break
        cursor = result["cursor"]
        assert cursor and TEXT not in cursor
    assert content == TEXT
    assert snapshot(engine) == before
    assert list(engine.artifacts.root.rglob("*"))
    assert not any(
        p.is_file() and TEXT.encode() in p.read_bytes() for p in engine.artifacts.root.rglob("*")
    )


@pytest.mark.parametrize("which", ["source", "evidence"])
def test_worker_reads_only_explicit_provenance_pages(engine, tmp_path, which):
    env = setup(engine, tmp_path)
    selected = path(env.source_ref) if which == "source" else env.evidence_path
    value = env.recall.page(env.worker_caller, RecallPageRequest(path=selected))
    assert value["content"] and value["grants_authority"] is False


@pytest.mark.parametrize("fault", ["source_expire", "role_revoke", "logout", "run_version"])
def test_commit_during_cas_io_blocks_release(engine, tmp_path, monkeypatch, fault):
    env = setup(engine, tmp_path)
    read = engine.artifacts.read
    calls = []

    def boundary(content_hash):
        assert engine.connection.info.transaction_status == TransactionStatus.IDLE
        value = read(content_hash)
        calls.append(content_hash)
        if fault == "source_expire":
            expire_source(env)
        elif fault == "role_revoke":
            env.source.identities.roles(
                env.cookie,
                env.source.binding["binding_id"],
                RoleChange(
                    expected_revision=env.source.binding["revision"],
                    roles=("requester",),
                    review_ref="SA15-revoke",
                ),
            )
        elif fault == "logout":
            with engine.transaction() as conn:
                conn.execute("DELETE FROM app.web_sessions")
        else:
            with engine.transaction() as conn:
                conn.execute(
                    "UPDATE app.runs SET status='waiting_input' WHERE run_id=%s",
                    (env.target.run_id,),
                )
        return value

    monkeypatch.setattr(engine.artifacts, "read", boundary)
    with pytest.raises(DomainError):
        env.recall.page(env.owner, RecallPageRequest(path=env.report_path))
    assert calls == [env.report_hash]


@pytest.mark.parametrize("operation", ["edit", "delete", "expire", "revoke_identity"])
def test_source_page_invalidated_between_pages(engine, tmp_path, operation):
    env = setup(engine, tmp_path)
    request = RecallPageRequest(path=path(env.source_ref), max_bytes=128)
    first = env.recall.page(env.owner, request)
    assert first["cursor"]
    if operation in {"edit", "delete"}:
        env.source.gateway.receive(
            envelope(
                env.source,
                delivery="sa15-new-head",
                entity="m1",
                version=2,
                operation=operation,
                body="new text",
            )
        )
    elif operation == "expire":
        expire_source(env)
    else:
        env.source.identities.revoke_identity(
            env.cookie,
            env.source.binding["identity_id"],
            RevisionCommand(
                expected_revision=env.source.binding["identity_revision"],
                review_ref="SA15-identity-revoke",
            ),
        )
    with pytest.raises(DomainError):
        env.recall.page(env.owner, request.model_copy(update={"cursor": first["cursor"]}))
    value = env.recall.resolve(env.owner, RecallResolveRequest(path=path(env.source_ref)))
    assert value["node"]["availability"] == "unavailable"
    assert TEXT not in json.dumps(value, ensure_ascii=False)


@pytest.mark.parametrize("fault", ["tamper", "wrong_path", "wrong_size", "expired", "other_cookie"])
def test_real_page_cursor_cannot_expand_authority(engine, tmp_path, fault):
    env = setup(engine, tmp_path)
    request = RecallPageRequest(path=env.report_path, max_bytes=128)
    first = env.recall.page(env.owner, request)
    cursor = first["cursor"]
    caller = env.owner
    if fault == "tamper":
        cursor = ("A" if cursor[0] != "A" else "B") + cursor[1:]
    elif fault == "wrong_path":
        request = request.model_copy(update={"path": path(env.source_ref)})
    elif fault == "wrong_size":
        request = request.model_copy(update={"max_bytes": 256})
    elif fault == "other_cookie":
        from mgar_v3.web_access import WebAccounts

        accounts = WebAccounts(engine)
        cookie = accounts.issue_session(accounts.session(env.cookie).user_id)
        caller = RecallCaller("owner", cookie)
    else:
        with engine.transaction() as conn:
            scope = RecallAccess(engine, conn, env.owner).scope
        old = env.recall.cursors.decode(cursor, scope.caller_sha256, now=datetime.now(UTC))
        cursor = env.recall.cursors.encode(
            old.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
        )
    with pytest.raises(DomainError):
        env.recall.page(caller, request.model_copy(update={"cursor": cursor}))


@pytest.mark.parametrize("kind", ["artifact", "evidence", "run"])
def test_unknown_private_reference_and_hash_shortcut_not_readable(engine, tmp_path, kind):
    env = setup(engine, tmp_path)
    unknown = ref(kind, "b" * 64 if kind == "artifact" else uuid4())
    selected = path(env.run_ref, unknown) if kind == "artifact" else path(unknown)
    with pytest.raises(DomainError):
        env.recall.page(env.worker_caller, RecallPageRequest(path=selected))


def test_cross_project_repository_and_context_edges_do_not_grant(engine, tmp_path):
    env = setup(engine, tmp_path)
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.threads SET repository='other/private' WHERE thread_id=%s",
            (env.target.thread_id,),
        )
    with pytest.raises(DomainError):
        env.recall.resolve(env.owner, RecallResolveRequest(path=path(env.run_ref)))


def test_worker_current_exact_epoch_and_bearer_not_owner_cookie(engine, tmp_path):
    env = setup(engine, tmp_path)
    for caller in (
        RecallCaller("worker", env.cookie, env.run_request),
        RecallCaller(
            "worker",
            env.worker["bearer"],
            env.run_request.model_copy(update={"epoch": env.run_request.epoch + 1}),
        ),
        RecallCaller("owner", env.worker["bearer"]),
    ):
        with pytest.raises(DomainError):
            env.recall.resolve(caller, RecallResolveRequest(path=path(env.run_ref)))


def test_managed_async_reader_reuses_current_original_authority_without_start(engine, tmp_path):
    env = setup(engine, tmp_path, managed=True)
    before = snapshot(engine)
    value = env.recall.resolve(env.worker_caller, RecallResolveRequest(path=path(env.run_ref)))
    assert value["node"]["state"] == "running"
    assert snapshot(engine) == before
    assert env.async_service.runtime is None


def test_memory_untrusted_summary_cannot_change_task_or_permissions(engine, tmp_path):
    env = setup(engine, tmp_path)
    memory, _ = legacy_memory(env)
    request = RecallPageRequest(path=path(env.run_ref, memory))
    before = snapshot(engine)
    result = env.recall.page(env.owner, request)
    assert "pretend task succeeded" in result["content"]
    assert result["grants_authority"] is False and result["content_is_untrusted"] is True
    assert snapshot(engine) == before
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE knowledge.memories SET revoked_at=clock_timestamp() WHERE memory_id=%s",
            (UUID(memory.id),),
        )
    with pytest.raises(DomainError):
        env.recall.page(env.owner, request)


def test_memory_cas_final_check_detects_memory_revocation(engine, tmp_path, monkeypatch):
    env = setup(engine, tmp_path)
    memory, summary = legacy_memory(env)
    read = engine.artifacts.read

    def boundary(content_hash):
        value = read(content_hash)
        with engine.transaction() as conn:
            conn.execute(
                "UPDATE knowledge.memories SET revoked_at=clock_timestamp() WHERE memory_id=%s",
                (UUID(memory.id),),
            )
        return value

    monkeypatch.setattr(engine.artifacts, "read", boundary)
    with pytest.raises(DomainError):
        env.recall.page(env.owner, RecallPageRequest(path=path(env.run_ref, memory)))
    assert summary


def test_origin_added_after_precheck_cannot_launder_derived_bytes(engine, tmp_path, monkeypatch):
    env = setup(engine, tmp_path)
    memory, _ = legacy_memory(env)
    read = engine.artifacts.read

    def boundary(content_hash):
        value = read(content_hash)
        expire_source(env, env.extra_input_id)
        with engine.transaction() as conn:
            conn.execute(
                "UPDATE knowledge.memories SET evidence_ids=%s WHERE memory_id=%s",
                (Jsonb([str(uuid4())]), UUID(memory.id)),
            )
        return value

    monkeypatch.setattr(engine.artifacts, "read", boundary)
    with pytest.raises(DomainError):
        env.recall.page(env.owner, RecallPageRequest(path=path(env.run_ref, memory)))


def test_actual_walk_bounds_and_every_emitted_target_is_authorized(engine, tmp_path):
    env = setup(engine, tmp_path)
    before = snapshot(engine)
    result = env.recall.walk(
        env.worker_caller, RecallWalkRequest(path=path(env.source_ref), max_hops=4, max_nodes=5)
    )
    assert len(result["nodes"]) <= 5
    assert len(json.dumps(result).encode()) <= 16384
    ids = {n["ref"]["id"] for n in result["nodes"]}
    assert str(env.target.run_id) in ids
    assert all(edge["target"]["id"] in ids for edge in result["edges"])
    assert snapshot(engine) == before


def test_cas_corruption_is_not_returned(engine, tmp_path):
    env = setup(engine, tmp_path)
    engine.artifacts.path(env.report_hash).write_bytes(b"bad unauthenticated bytes")
    with pytest.raises(DomainError):
        env.recall.page(env.owner, RecallPageRequest(path=env.report_path))


def test_deadline_and_original_connection_are_enforced(engine, tmp_path):
    env = setup(engine, tmp_path)
    with engine.transaction() as conn:
        access = RecallAccess(engine, conn, env.owner)
        access.deadline = 0
        with pytest.raises(DomainError, match="deadline"):
            RecallStore(access).path(path(env.run_ref))
    with pytest.raises(DomainError):
        RecallAccess(engine, engine.connection, env.owner)
