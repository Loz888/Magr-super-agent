"""First formal PG input, authorization, cursor and persistence checks."""

import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from sa14_support import followup, point, restrict, rows, setup, worker_request

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.super_agent.source_coding_journal import SourceCodingJournal


@pytest.fixture
def env(engine, tmp_path):
    return setup(engine, tmp_path)


def test_formal0047_original_engine_connection_and_checksum_catalogue(env):
    import hashlib

    root = Path(__file__).resolve().parents[1]
    with env.engine.transaction() as conn:
        actual = {
            r["name"]: r["sha256"]
            for r in conn.execute("SELECT name,sha256 FROM app.schema_migrations")
        }
        assert len(actual) == len(list((root / "migrations").glob("*.sql"))) == 44
        for p in (root / "migrations").glob("*.sql"):
            assert actual[p.name] == hashlib.sha256(p.read_bytes()).hexdigest()
        assert (
            actual["0047_source_async.sql"]
            == "9ce372e6a82cbf747c51624486ef7548faf056cc60912e0287279be8b569f041"
        )
        assert env.engine.source_async is env.async_service and env.async_service._ready(conn)
    with psycopg.connect(
        env.engine.settings.database_url.get_secret_value(), autocommit=True
    ) as foreign:
        with pytest.raises(DomainError):
            env.async_service._ready(foreign)
    assert env.async_service.runtime is None
    assert (
        rows(env, "sa_coding_attempts")
        == rows(env, "sa_async_jobs")
        == rows(env, "usage_calls")
        == []
    )


def test_original_target_and_assignment_are_frozen_once_on_owner_admission(env):
    first = env.async_service.get(env.cookie, env.mandate.coding_id)
    assert env.async_service.admit(env.cookie, env.async_admit) == first
    changed = env.async_admit.model_copy(update={"request_id": uuid4()})
    with pytest.raises(DomainError):
        env.async_service.admit(env.cookie, changed)
    assert len(rows(env, "runs")) == len(rows(env, "sa_async_bindings")) == 1
    assert rows(env, "actions") == rows(env, "outbox") == []


def test_duplicate_restriction_is_received_once_then_applied_once_and_only_narrows(env):
    request, receipt = restrict(env)
    assert receipt["state"] == "received"
    assert env.async_service.restrict(env.cookie, request) == receipt
    cp = point(env)
    assert cp.input_revision == cp.through_sequence == 1
    assert cp.restriction.deny_write_paths == ("src/calc.py",)
    assert rows(env, "sa_async_inputs")[0]["state"] == "applied"
    assert point(env).checkpoint_id == cp.checkpoint_id
    assert len(rows(env, "sa_async_checkpoints")) == len(rows(env, "sa_async_inputs")) == 1
    _, second = restrict(env, paths=("src/other.py",))
    assert second["state"] == "received"
    cp2 = point(env)
    assert cp2.restriction.deny_write_paths == ("src/calc.py", "src/other.py")
    assert len(rows(env, "sa_async_inputs")) == 2


def test_revision_conflict_and_outer_rollback_do_not_consume_inputs(env):
    request, _ = restrict(env)
    snapshot = rows(env, "sa_async_bindings"), rows(env, "sa_async_inputs")
    stale = request.model_copy(update={"request_id": uuid4()})
    with pytest.raises(DomainError):
        env.async_service.restrict(env.cookie, stale)
    assert (rows(env, "sa_async_bindings"), rows(env, "sa_async_inputs")) == snapshot
    with pytest.raises(RuntimeError):
        with env.engine.transaction():
            restrict(env, paths=("src/other.py",))
            point(env)
            raise RuntimeError("actual Engine savepoint rollback")
    assert (rows(env, "sa_async_bindings"), rows(env, "sa_async_inputs")) == snapshot
    assert rows(env, "sa_async_checkpoints") == []


def test_signed_followup_uses_original_target_and_duplicate_canonical_input_once(env):
    transport, receipt = followup(env)
    assert receipt.committed and receipt.disposition == "accepted"
    duplicate = env.source.gateway.receive(transport)
    assert duplicate.committed and duplicate.disposition == "duplicate"
    assert len(rows(env, "sa_async_inputs")) == 1
    cp = point(env)
    assert len(cp.source_refs) == 1
    assert cp.source_refs[0].input_id == receipt.input_ids[0]
    assert rows(env, "sa_async_inputs")[0]["state"] == "applied"
    assert env.async_service.materialize(
        env.worker["bearer"], env.run_request, cp, tree=cp.tree_sha256
    ) == ("synthetic follow-up",)
    assert len(rows(env, "runs")) == 1


def test_same_chat_without_exact_reply_or_topic_is_not_the_existing_task(env):
    _, receipt = followup(env, reply=None)
    assert receipt.committed
    assert rows(env, "sa_async_inputs") == []
    assert len(rows(env, "runs")) == 1


@pytest.mark.parametrize(
    "sender,kind,expected",
    [
        ("Bob-Control", "human", "applied"),
        ("Alice-AbC", "human", "blocked"),
        ("CI-Only", "bot", "blocked"),
    ],
)
def test_database_phrase_requires_current_human_exact_ticket_controller(
    env, sender, kind, expected
):
    _, receipt = followup(env, body="不要改数据库", sender=sender, actor_kind=kind)
    assert receipt.committed
    point(env)
    inputs = rows(env, "sa_async_inputs")
    assert len(inputs) == 1 and inputs[0]["state"] == expected
    restriction = env.async_service.get(env.cookie, env.mandate.coding_id)["restriction"]
    assert restriction["deny_write_paths"] == (["src/calc.py"] if expected == "applied" else [])


@pytest.mark.parametrize("change", ["roles", "edit", "delete"])
def test_committed_source_change_fences_view_and_cannot_revive_after_restore(env, change):
    followup(env, entity="extra")
    cp = point(env)
    if change == "roles":
        binding = env.source.controller
        updated = env.source.identities.roles(
            env.cookie,
            binding["binding_id"],
            RoleChange(
                expected_revision=binding["revision"],
                roles=("context_provider",),
                review_ref="SA14-withdraw-control",
            ),
        )
        env.source.identities.roles(
            env.cookie,
            binding["binding_id"],
            RoleChange(
                expected_revision=updated["revision"],
                roles=("context_provider", "task_controller"),
                review_ref="SA14-restored-role-new-review",
            ),
        )
    else:
        from sa04_support import envelope

        env.source.gateway.receive(
            envelope(
                env.source,
                delivery="changed-extra",
                entity="extra",
                version=2,
                sender="Bob-Control",
                body="changed",
                operation=change,
            )
        )
    assert env.async_service.get(env.cookie, env.mandate.coding_id)["state"] == "fenced"
    with pytest.raises(DomainError):
        env.async_service.materialize(
            env.worker["bearer"], env.run_request, cp, tree=cp.tree_sha256
        )
    assert any(item["kind"] == "invalidation" for item in rows(env, "sa_async_inputs"))


def test_new_revision_rejects_entire_old_view_before_any_mutation(env):
    cp = point(env)
    restrict(env)
    with pytest.raises(DomainError):
        with env.engine.transaction() as conn:
            env.async_service.validate(
                conn,
                env.worker["bearer"],
                env.run_request,
                cp,
                tree=cp.tree_sha256,
                capability="write",
                paths=("src/calc.py",),
            )
    assert rows(env, "sa_coding_operations") == []
    cp = point(env)
    with pytest.raises(DomainError):
        with env.engine.transaction() as conn:
            env.async_service.validate(
                conn,
                env.worker["bearer"],
                env.run_request,
                cp,
                tree=cp.tree_sha256,
                capability="write",
                paths=("src/calc.py",),
            )
    with env.engine.transaction() as conn:
        env.async_service.validate(
            conn,
            env.worker["bearer"],
            env.run_request,
            cp,
            tree=cp.tree_sha256,
            capability="write",
            paths=("src/other.py",),
        )


def test_old_coding_and_generic_scheduler_worker_cannot_bypass_async_management(env):
    from mgar_v3.super_agent.source_scheduler_domain import SchedulerAssignmentRequest

    with pytest.raises(DomainError):
        env.service.run(env.worker["bearer"], env.run_request)
    with env.engine.transaction() as conn:
        assignment = env.engine.source_scheduler._assignment(conn, env.run_request.assignment_id)
    request = SchedulerAssignmentRequest(
        assignment_id=env.run_request.assignment_id,
        session_id=env.run_request.session_id,
        epoch=env.run_request.epoch,
        expected_revision=assignment["revision"],
    )
    with pytest.raises(DomainError):
        env.engine.source_scheduler.begin_operation(
            env.worker["bearer"], request, "job", digest("forged-old-entry")
        )
    assert rows(env, "sa_coding_operations") == rows(env, "sa_async_jobs") == []


@pytest.mark.parametrize("field", ["run_id", "assignment_id", "session_id", "epoch"])
def test_current_worker_bearer_cannot_be_reused_for_another_scope(env, field):
    request = worker_request(env)
    bad = request.model_copy(update={field: 2 if field == "epoch" else uuid4()})
    with pytest.raises(DomainError):
        env.async_service.state(env.worker["bearer"], bad)
    assert len(rows(env, "sa_async_bindings")) == 1


def test_input_tables_and_events_do_not_store_source_body_tokens_or_success_claims(env):
    sentinel = "SA14-SOURCE-PRIVATE-SENTINEL-ONLY-IN-ENCRYPTED-BODY"
    followup(env, body=sentinel)
    point(env)
    for table in (
        "sa_async_inputs",
        "sa_async_checkpoints",
        "sa_async_bindings",
        "sa_async_events",
    ):
        value = json.dumps(rows(env, table), default=str)
        for forbidden in (
            sentinel,
            env.cookie,
            env.worker["bearer"],
            "task_succeeded",
            "ciphertext",
        ):
            assert forbidden not in value
    assert rows(env, "usage_calls") == rows(env, "actions") == rows(env, "outbox") == []


@pytest.mark.parametrize(
    "body",
    [
        "> 不要改数据库",
        "```\n不要改数据库\n```",
        '{"schema_version":"feishu-content-v1","message_type":"post","content":{"content":[[{"tag":"a","text":"不要改数据库","href":"https://example.invalid"}]]}}',
    ],
)
def test_quoted_code_or_link_label_is_data_without_control_authority(env, body):
    followup(env, body=body)
    point(env)
    assert rows(env, "sa_async_inputs")[0]["kind"] == "context"
    assert (
        env.async_service.get(env.cookie, env.mandate.coding_id)["restriction"]["deny_write_paths"]
        == []
    )


def test_original_encrypted_payload_is_erased_when_extra_source_dependency_is_withdrawn(env):
    followup(env)
    cp = point(env)
    with env.async_service.execution_scope(env.async_binding["async_id"]):
        with env.engine.transaction() as conn:
            attempt = env.service.begin_attempt(conn, env.worker["bearer"], env.run_request)
        journal = SourceCodingJournal(
            env.service, env.worker["bearer"], env.run_request, attempt["attempt_id"]
        )
        op = journal.operation(
            step_key="sa14-extra-context",
            kind="tool",
            capability="read",
            request_sha256=digest("source-snapshot"),
            tree_sha256=cp.tree_sha256,
        )
        journal.dispatch(op)
        journal.put_payload(op, "checkpoint", b'{"controlled_context":"synthetic follow-up"}')
    binding = env.source.controller
    env.source.identities.roles(
        env.cookie,
        binding["binding_id"],
        RoleChange(
            expected_revision=binding["revision"],
            roles=("requester",),
            review_ref="SA14-extra-source-read-withdrawal",
        ),
    )
    saved = rows(env, "sa_coding_payloads")
    assert saved and all(item["ciphertext"] is None for item in saved)
    with pytest.raises(DomainError):
        journal.get_payload(op, "checkpoint", current_tree=cp.tree_sha256)
    assert rows(env, "usage_calls") == []


def test_extra_source_ttl_caps_original_ciphertext_and_expiry_erasure_commits(env, monkeypatch):
    import time

    original = env.async_service.accepted_conn

    def short_body(conn, input_id, *, receipt_id):
        conn.execute(
            "UPDATE app.source_ingress_bodies SET expires_at=clock_timestamp()+interval '1 second' WHERE input_id=%s",
            (input_id,),
        )
        original(conn, input_id, receipt_id=receipt_id)

    monkeypatch.setattr(env.async_service, "accepted_conn", short_body)
    followup(env, body="short-lived synthetic Source")
    cp = point(env)
    with env.async_service.execution_scope(env.async_binding["async_id"]):
        with env.engine.transaction() as conn:
            attempt = env.service.begin_attempt(conn, env.worker["bearer"], env.run_request)
        journal = SourceCodingJournal(
            env.service, env.worker["bearer"], env.run_request, attempt["attempt_id"]
        )
        op = journal.operation(
            step_key="sa14-ttl",
            kind="tool",
            capability="read",
            request_sha256=digest("ttl-current-context"),
            tree_sha256=cp.tree_sha256,
        )
        journal.dispatch(op)
        journal.put_payload(op, "checkpoint", b'{"context":"short-lived synthetic Source"}')
        assert rows(env, "sa_coding_payloads")[0]["expires_at"] <= cp.expires_at
        time.sleep(1.05)
        with pytest.raises(DomainError):
            journal.get_payload(op, "checkpoint", current_tree=cp.tree_sha256)
    assert all(item["ciphertext"] is None for item in rows(env, "sa_coding_payloads"))
    assert env.async_service.get(env.cookie, env.mandate.coding_id)["state"] == "fenced"
