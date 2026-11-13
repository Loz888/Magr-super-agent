"""SA13 current original-PG authority and typed target integration."""

import json
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from sa04_support import envelope
from sa13_support import setup, snapshot

from mgar_v3.domain import DomainError, TriggerInput, digest
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.super_agent.policy_domain import PolicyRevoke
from mgar_v3.super_agent.source_coding import SourceCoding
from mgar_v3.super_agent.source_coding_domain import (
    CodingCancelRequest,
    CodingInspectRequest,
)


def test_coding_constructor_is_inert_and_has_no_default_runtime():
    class NoIO:
        def __getattr__(self, key):
            raise AssertionError("Constructor IO: " + key)

    service = SourceCoding(NoIO())
    assert service.runtime is None and service.cipher is None and not service._profiles


def test_admission_uses_original_graph_principal_scope_and_no_plaintext(engine, tmp_path):
    env = setup(engine, tmp_path, ready=False)
    with engine.transaction() as conn:
        run, mandate = env.service._mandate(conn, env.target.run_id)
        assert run["graph_version"] == "source-coding-v1"
        assert run["question"] == "Reviewed Source Coding target"
        assert mandate.origin.engine_actor.startswith("sa_")
        assert mandate.origin.engine_actor != engine.settings.local_actor
        assert env.service.managed(conn, run["run_id"])
        for table in ("trigger_events", "run_inputs"):
            assert not conn.execute("SELECT 1 FROM app." + table).fetchone()
        assert conn.execute("SELECT count(*) AS n FROM app.tickets").fetchone()["n"] == 1
        queue = conn.execute("SELECT * FROM app.sa_scheduler_queue").fetchone()
        assert queue["run_id"] == env.target.run_id and queue["input_id"] == env.input.input_id
        assert "synthetic private repair" not in json.dumps(mandate.model_dump(mode="json"))


def test_admission_replays_same_receipt_without_new_fact(engine, tmp_path):
    env = setup(engine, tmp_path, ready=False)
    before = snapshot(env)
    assert env.service.admit(env.cookie, env.request) == env.binding
    assert snapshot(env) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_epoch": 1},
        {"expected_revision": 1},
        {"expected_policy_revision": 1},
        {"target_sha256": "0" * 64},
        {"profile_sha256": "0" * 64},
        {"run_id": uuid4()},
    ],
)
def test_admission_stale_bindings_roll_back_all_writes(engine, tmp_path, changes):
    env = setup(engine, tmp_path, admitted=False, ready=False)
    before = snapshot(env)
    with pytest.raises(DomainError):
        env.service.admit(env.cookie, env.request.model_copy(update=changes))
    assert snapshot(env) == before


def test_admission_request_collision_cannot_change_original_target(engine, tmp_path):
    env = setup(engine, tmp_path, ready=False)
    before = snapshot(env)
    with pytest.raises(DomainError) as caught:
        env.service.admit(
            env.cookie, env.request.model_copy(update={"review_ref": "different-review"})
        )
    assert caught.value.code == "SOURCE_CODING_IDEMPOTENCY_CONFLICT"
    assert snapshot(env) == before


def test_legacy_readonly_original_run_is_never_upgraded(engine, tmp_path):
    env = setup(engine, tmp_path, admitted=False, ready=False)
    old = engine.ingest(
        TriggerInput(
            source="web",
            external_event_id=uuid4().hex,
            project_id=engine.settings.project_id,
            repository=engine.settings.repository,
            issue_number=8181,
            actor=engine.settings.local_actor,
            body="/mgar investigate controlled readonly scope",
        )
    )
    before = snapshot(env)
    with pytest.raises(DomainError) as caught:
        env.service.admit(env.cookie, env.request.model_copy(update={"run_id": old.run_id}))
    assert caught.value.code == "SOURCE_CODING_NEW_MANDATE_REQUIRED"
    assert snapshot(env) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"private": True},
        {"model_disclosure": "approved_live"},
        {"source_kind": "github_snapshot"},
    ],
)
def test_trusted_profile_still_cannot_enable_private_or_live(engine, tmp_path, changes):
    env = setup(engine, tmp_path, ready=False)
    profile = replace(
        env.profile,
        profile_sha256=digest(changes),
        repository=env.profile.repository.model_copy(update=changes),
    )
    before = snapshot(env)
    with pytest.raises(DomainError) as caught:
        env.service.register_profile(profile)
    assert caught.value.code == "SOURCE_CODING_FIXTURE_ENGINEERING_ONLY"
    assert snapshot(env) == before


def test_caller_pass_claim_does_not_override_fixed_failed_h12(engine, tmp_path):
    env = setup(engine, tmp_path, ready=False)
    h12 = env.profile.harness.h12.model_copy(update={"live_agent": "PASS", "mvp": "PASS"})
    profile = replace(
        env.profile,
        profile_sha256=digest("forged-pass"),
        harness=env.profile.harness.model_copy(update={"h12": h12}),
    )
    with pytest.raises(DomainError) as caught:
        env.service.register_profile(profile)
    assert caught.value.code == "SOURCE_CODING_FIXTURE_ENGINEERING_ONLY"
    assert env.service.capabilities(env.cookie)["live_agent"] == "FAIL"


def test_unvalidated_extra_context_reference_is_rejected(engine, tmp_path):
    env = setup(engine, tmp_path, ready=False)
    extra = env.input.model_copy(update={"input_id": uuid4()})
    mandate = env.mandate.model_copy(update={"context_refs": (env.input, extra)})
    before = snapshot(env)
    with pytest.raises(DomainError) as caught:
        env.service.create_target(env.cookie, mandate)
    assert caught.value.code == "SOURCE_CODING_SINGLE_ORIGIN_CONTEXT_REQUIRED"
    assert snapshot(env) == before


def test_duplicate_target_factory_does_not_create_another_task(engine, tmp_path):
    env = setup(engine, tmp_path, ready=False)
    target = env.target.model_copy(
        update={
            "thread_id": uuid4(),
            "ticket_id": uuid4(),
            "run_id": uuid4(),
            "mandate_id": uuid4(),
        }
    )
    candidate = env.mandate.model_copy(update={"coding_id": uuid4(), "target": target})
    before = snapshot(env)
    with pytest.raises(DomainError) as caught:
        env.service.create_target(env.cookie, candidate)
    assert caught.value.code == "SOURCE_CODING_DUPLICATE_TARGET"
    assert snapshot(env) == before


def test_original_connection_is_mandatory(engine):
    with pytest.raises(DomainError) as caught:
        engine.source_coding.managed(object(), uuid4())
    assert caught.value.code == "SOURCE_CODING_ORIGINAL_CONNECTION_REQUIRED"


@pytest.mark.parametrize("field", ["identity_id", "policy_id", "expires_at"])
def test_origin_metadata_is_exact_current_source_fact(engine, tmp_path, field):
    env = setup(engine, tmp_path, ready=False)
    value = (
        env.mandate.origin.expires_at + timedelta(seconds=1) if field == "expires_at" else uuid4()
    )
    mandate = env.mandate.model_copy(
        update={"origin": env.mandate.origin.model_copy(update={field: value})}
    )
    before = snapshot(env)
    with engine.transaction() as conn, pytest.raises(DomainError):
        env.service._source(conn, mandate)
    assert snapshot(env) == before


def test_current_assignment_projection_and_attempt_replay_are_exact(engine, tmp_path):
    env = setup(engine, tmp_path)
    with engine.transaction() as conn:
        mandate, facts = env.service.current(conn, env.worker["bearer"], env.run_request)
        assert facts.workspace_id == env.claimed["workspace_id"]
        assert facts.target == env.target and not facts.original_read_only
        first = env.service.begin_attempt(conn, env.worker["bearer"], env.run_request)
        again = env.service.begin_attempt(conn, env.worker["bearer"], env.run_request)
        assert first == again and first["state"] == "running"
        assert first["profile_sha256"] == mandate.worker_profile_sha256


@pytest.mark.parametrize("field", ["coding_id", "run_id", "assignment_id", "session_id", "epoch"])
def test_worker_cannot_substitute_assignment_tuple(engine, tmp_path, field):
    env = setup(engine, tmp_path)
    value = env.run_request.epoch + 1 if field == "epoch" else uuid4()
    request = env.run_request.model_copy(update={field: value})
    before = snapshot(env)
    with engine.transaction() as conn, pytest.raises(DomainError):
        env.service.current(conn, env.worker["bearer"], request)
    assert snapshot(env) == before


def test_missing_runtime_rejects_after_auth_without_persistent_work(engine, tmp_path):
    env = setup(engine, tmp_path)
    before = snapshot(env)
    with pytest.raises(DomainError) as caught:
        env.service.run(env.worker["bearer"], env.run_request)
    assert caught.value.code == "SOURCE_CODING_RUNTIME_REQUIRED"
    assert snapshot(env) == before


@pytest.mark.parametrize("kind", ["context", "requester", "policy", "edited", "deleted", "target"])
def test_committed_source_and_target_changes_block_next_boundary(engine, tmp_path, kind):
    env = setup(engine, tmp_path)
    if kind in {"context", "requester"}:
        roles = ("requester",) if kind == "context" else ("context_provider",)
        env.source.identities.roles(
            env.cookie,
            env.source.binding["binding_id"],
            RoleChange(
                expected_revision=env.source.binding["revision"],
                roles=roles,
                review_ref="SA13-current-role-withdrawal",
            ),
        )
    elif kind == "policy":
        engine.source_policies.revoke(
            env.cookie,
            env.source.policy_id,
            PolicyRevoke(expected_revision=2, review_ref="SA13-current-policy-withdrawal"),
        )
    elif kind in {"edited", "deleted"}:
        env.source.gateway.receive(
            envelope(
                env.source,
                delivery="d2",
                version=2,
                operation="edit" if kind == "edited" else "delete",
                body="changed private source",
            )
        )
    else:
        with engine.transaction() as conn:
            conn.execute(
                "UPDATE app.runs SET question='changed original target' WHERE run_id=%s",
                (env.target.run_id,),
            )
    before = snapshot(env)
    with engine.transaction() as conn, pytest.raises(DomainError):
        env.service.current(conn, env.worker["bearer"], env.run_request)
    assert snapshot(env) == before


def test_guard_holds_mutex_without_sql_transaction_across_io(engine, tmp_path):
    env = setup(engine, tmp_path)
    with env.service.guard(env.worker["bearer"], env.run_request):
        assert engine.connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        assert engine._mutex._is_owned()


def test_cancel_fences_attempt_and_does_not_complete_business_task(engine, tmp_path):
    env = setup(engine, tmp_path)
    with engine.transaction() as conn:
        attempt = env.service.begin_attempt(conn, env.worker["bearer"], env.run_request)
    result = env.service.cancel(
        env.cookie,
        CodingCancelRequest(
            request_id=uuid4(),
            coding_id=env.mandate.coding_id,
            run_id=env.target.run_id,
            expected_epoch=env.run_request.epoch,
            expected_revision=1,
            review_ref="SA13-stop-current",
        ),
    )
    assert result["state"] == "revoked" and result["revision"] == 2
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM app.sa_coding_attempts WHERE attempt_id=%s", (attempt["attempt_id"],)
        ).fetchone()
        assert row["state"] == "stopped" and row["fenced_at"] is not None
        assert engine._run(conn, env.target.run_id)["status"] != "succeeded"
    with pytest.raises(DomainError):
        env.service.state(
            env.worker["bearer"],
            CodingInspectRequest(coding_id=env.mandate.coding_id, run_id=env.target.run_id),
        )


def test_restart_fences_open_attempt_without_resend_or_financial_reset(engine, tmp_path):
    env = setup(engine, tmp_path)
    with engine.transaction() as conn:
        env.service.begin_attempt(conn, env.worker["bearer"], env.run_request)
        before = conn.execute("SELECT * FROM app.budgets").fetchall()
        env.service.recover_conn(conn)
        assert (
            conn.execute("SELECT state FROM app.sa_coding_attempts").fetchone()["state"]
            == "stopped"
        )
        assert conn.execute("SELECT * FROM app.budgets").fetchall() == before
    with (
        engine.transaction() as conn,
        pytest.raises(DomainError) as caught,
    ):
        env.service.begin_attempt(conn, env.worker["bearer"], env.run_request)
    assert caught.value.code == "SOURCE_CODING_ATTEMPT_FENCED"


def test_original_scope_tampering_never_changes_bound_permissions(engine, tmp_path):
    env = setup(engine, tmp_path)
    changed = env.mandate.model_dump(mode="json")
    changed["grant"]["allowed_commands"].append("sh")
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.mandates SET scope=%s WHERE mandate_id=%s",
            (Jsonb(changed), env.target.mandate_id),
        )
    with engine.transaction() as conn, pytest.raises(DomainError):
        env.service.current(conn, env.worker["bearer"], env.run_request)
