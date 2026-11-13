"""First checks for actual PG authority loss and bounded encrypted payload life."""

import time
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from sa04_support import envelope
from sa04_support import setup as source_setup
from sa07_support import PRICE
from sa13_support import setup

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.contracts import BudgetLimits
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.policy_domain import PolicyRevoke
from mgar_v3.super_agent.source_coding import SourceCoding
from mgar_v3.super_agent.source_coding_budget import CodingBudgetLedger
from mgar_v3.super_agent.source_coding_domain import CodingCancelRequest
from mgar_v3.super_agent.source_coding_journal import SourceCodingJournal

CLEAR = b"SA13 lifecycle synthetic private checkpoint; never a stored plaintext log"


class CountingCipher(BodyCipher):
    """Retain actual authenticated crypto and count Coding decryption boundaries."""

    def __init__(self, original):
        self.original = original
        self.decrypts = 0

    def encrypt(self, body, binding):
        return self.original.encrypt(body, binding)

    def decrypt(self, ciphertext, binding):
        self.decrypts += 1
        return self.original.decrypt(ciphertext, binding)


def payload_env(engine, tmp_path, *, retention=1800, source_env=None, model=False):
    env = setup(engine, tmp_path, source_env=source_env)
    previous = env.service
    assert previous.cipher is not None
    cipher = CountingCipher(previous.cipher)
    core = SourceCoding(engine, cipher=cipher, payload_retention_seconds=retention)
    core.register_profile(env.profile)
    engine.source_coding = core
    env.service = core
    with engine.transaction() as conn:
        attempt = core.begin_attempt(conn, env.worker["bearer"], env.run_request)
    journal = SourceCodingJournal(
        core, env.worker["bearer"], env.run_request, attempt["attempt_id"]
    )
    call_id = None
    if model:
        ledger = CodingBudgetLedger(
            journal,
            price=PRICE,
            limits=BudgetLimits(
                max_model_calls=2, max_tool_calls=2, max_cost_micros=20000, max_seconds=120
            ),
            reserved_tokens=1000,
        )
        reservation = ledger.reserve(
            journal.task_id, "lifecycle-reservation", "model", reserved_micros=2000
        )
        call_id = UUID(reservation.reservation_id)
    operation = journal.operation(
        step_key="lifecycle-payload",
        kind="model" if model else "tool",
        capability="model" if model else "read",
        request_sha256=digest("lifecycle stable actual PG intent"),
        tree_sha256=env.mandate.repository.materialized_tree_sha256,
        usage_call_id=call_id,
    )
    kind = "model_response" if model else "checkpoint"
    journal.put_payload(operation, kind, CLEAR)
    return SimpleNamespace(
        env=env,
        core=core,
        journal=journal,
        attempt=attempt,
        operation=operation,
        kind=kind,
        cipher=cipher,
        call_id=call_id,
    )


def row(state):
    with state.core.engine.transaction() as conn:
        return conn.execute(
            "SELECT * FROM app.sa_coding_payloads WHERE operation_id=%s",
            (state.operation["operation_id"],),
        ).fetchone()


def ledger_rows(state):
    with state.core.engine.transaction() as conn:
        return {
            name: conn.execute("SELECT * FROM app." + name + " ORDER BY 1").fetchall()
            for name in ("budgets", "authorization_budgets", "usage_calls", "sa_budget_calls")
        }


def wait_actual_expiry(engine, table, identifier, key):
    assert table in {"sa_coding_payloads", "source_ingress_bodies"}
    assert key in {"operation_id", "input_id"}
    with engine.transaction() as conn:
        remaining = (
            conn.execute(
                "SELECT expires_at-clock_timestamp() AS remaining FROM app."
                + table
                + " WHERE "
                + key
                + "=%s",
                (identifier,),
            )
            .fetchone()["remaining"]
            .total_seconds()
        )
    if remaining > 0:
        time.sleep(remaining + 0.03)
    with engine.transaction() as conn:
        assert conn.execute(
            "SELECT expires_at<=clock_timestamp() AS expired FROM app."
            + table
            + " WHERE "
            + key
            + "=%s",
            (identifier,),
        ).fetchone()["expired"]


@pytest.mark.parametrize("retention", [True, False, 0, -1, 1801, 1.5, "1"])
def test_server_retention_rejects_unsafe_values_without_io(retention):
    with pytest.raises(DomainError) as caught:
        SourceCoding(object(), payload_retention_seconds=retention)
    assert caught.value.code == "SOURCE_CODING_PAYLOAD_RETENTION_INVALID"


def test_actual_cipher_read_requires_current_authority_and_keeps_only_ciphertext(engine, tmp_path):
    state = payload_env(engine, tmp_path)
    saved = row(state)
    assert CLEAR not in bytes(saved["ciphertext"])
    assert saved["ciphertext"] and saved["unavailable_at"] is None
    assert state.journal.get_payload(state.operation, state.kind) == CLEAR
    assert state.cipher.decrypts == 1
    assert saved["expires_at"] <= state.env.mandate.valid_until


def test_another_journal_attempt_cannot_decrypt_a_current_payload(engine, tmp_path):
    state = payload_env(engine, tmp_path)
    foreign = SourceCodingJournal(
        state.core, state.env.worker["bearer"], state.env.run_request, uuid4()
    )
    with pytest.raises(DomainError) as caught:
        foreign.get_payload(state.operation, state.kind)
    assert caught.value.code == "SOURCE_CODING_PAYLOAD_UNAVAILABLE"
    assert state.cipher.decrypts == 0 and row(state)["ciphertext"] is not None


@pytest.mark.parametrize("entry", ["maintain", "read"])
def test_actual_one_second_ttl_erases_ciphertext_in_committed_transaction(engine, tmp_path, entry):
    state = payload_env(engine, tmp_path, retention=1)
    before = row(state)
    wait_actual_expiry(
        engine, "sa_coding_payloads", state.operation["operation_id"], "operation_id"
    )
    if entry == "maintain":
        result = state.core.maintain()
        assert result["payloads_erased"] == 1 and result["attempts_stopped"] == 0
    with pytest.raises(DomainError) as caught:
        state.journal.get_payload(state.operation, state.kind)
    assert caught.value.code == "SOURCE_CODING_PAYLOAD_UNAVAILABLE"
    after = row(state)
    assert after["ciphertext"] is None and after["reason_code"] == "TTL_EXPIRED"
    assert after["unavailable_at"] is not None and after["revision"] == before["revision"] + 1
    assert after["expires_at"] == before["expires_at"]
    assert after["content_sha256"] == before["content_sha256"]
    assert state.cipher.decrypts == 0


@pytest.mark.parametrize(
    "withdrawal", ["policy", "context_role", "requester_role", "edit", "delete"]
)
@pytest.mark.parametrize("entry", ["maintain", "read"])
def test_committed_authority_loss_durably_erases_payload_before_decrypt(
    engine, tmp_path, withdrawal, entry
):
    state = payload_env(engine, tmp_path, model=True)
    env = state.env
    finances = ledger_rows(state)
    if withdrawal == "policy":
        engine.source_policies.revoke(
            env.cookie,
            env.source.policy_id,
            PolicyRevoke(expected_revision=2, review_ref="SA13-lifecycle-policy-withdrawal"),
        )
    elif withdrawal in {"context_role", "requester_role"}:
        remaining = ("requester",) if withdrawal == "context_role" else ("context_provider",)
        env.source.identities.roles(
            env.cookie,
            env.source.binding["binding_id"],
            RoleChange(
                expected_revision=env.source.binding["revision"],
                roles=remaining,
                review_ref="SA13-lifecycle-role-withdrawal",
            ),
        )
    else:
        env.source.gateway.receive(
            envelope(
                env.source,
                delivery="lifecycle-d2",
                version=2,
                operation="edit" if withdrawal == "edit" else "delete",
                body="replacement private text",
            )
        )
    if entry == "maintain":
        outcome = state.core.maintain(coding_id=env.mandate.coding_id)
        assert outcome["payloads_erased"] == 1 and outcome["attempts_stopped"] == 1
        assert outcome["operations_unknown"] == 1
    with pytest.raises(DomainError):
        state.journal.get_payload(state.operation, state.kind)
    saved = row(state)
    assert saved["ciphertext"] is None and saved["unavailable_at"] is not None
    assert saved["reason_code"] == "CURRENT_AUTHORITY_LOST" and saved["revision"] == 2
    assert state.cipher.decrypts == 0
    assert ledger_rows(state) == finances
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT state FROM app.sa_coding_attempts").fetchone()["state"]
            == "stopped"
        )
        assert (
            conn.execute("SELECT state FROM app.sa_coding_operations").fetchone()["state"]
            == "unknown"
        )
        assert not conn.execute("SELECT 1 FROM app.sa_coding_verifications").fetchone()
        assert engine._run(conn, env.target.run_id)["status"] != "succeeded"


def test_source_body_actual_expiry_erases_dependent_coding_checkpoint(engine, tmp_path):
    source = source_setup(
        engine,
        mode="auto_repair",
        receive={
            "enabled": True,
            "retain_body": True,
            "body_ttl_seconds": 2,
            "allow_history_context": True,
            "model_input": True,
        },
    )
    state = payload_env(engine, tmp_path, source_env=source)
    wait_actual_expiry(engine, "source_ingress_bodies", state.env.input.input_id, "input_id")
    with pytest.raises(DomainError):
        state.journal.get_payload(state.operation, state.kind)
    assert row(state)["ciphertext"] is None and state.cipher.decrypts == 0
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT state FROM app.sa_coding_attempts").fetchone()["state"]
            == "stopped"
        )


@pytest.mark.parametrize("cause", ["cancel", "recovery"])
def test_cancel_or_recovery_payload_erasure_cannot_be_reversed(engine, tmp_path, cause):
    state = payload_env(engine, tmp_path)
    original = row(state)
    env = state.env
    if cause == "cancel":
        state.core.cancel(
            env.cookie,
            CodingCancelRequest(
                request_id=uuid4(),
                coding_id=env.mandate.coding_id,
                run_id=env.target.run_id,
                expected_revision=1,
                expected_epoch=env.run_request.epoch,
                review_ref="SA13-lifecycle-owner-stop",
            ),
        )
    else:
        with engine.transaction() as conn:
            state.core.recover_conn(conn)
    assert row(state)["ciphertext"] is None
    with pytest.raises(DomainError):
        state.journal.get_payload(state.operation, state.kind)
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(
            "UPDATE app.sa_coding_payloads SET ciphertext=%s,unavailable_at=NULL,"
            "reason_code=NULL,revision=revision+1,updated_at=clock_timestamp() WHERE payload_id=%s",
            (original["ciphertext"], original["payload_id"]),
        )
    assert row(state)["ciphertext"] is None and row(state)["revision"] == 2
    assert state.core.maintain()["payloads_erased"] == 0


def test_maintenance_never_claims_rolled_back_erasure_inside_callers_transaction(engine, tmp_path):
    state = payload_env(engine, tmp_path)
    original = row(state)
    with engine.transaction(), pytest.raises(DomainError) as caught:
        state.core.maintain()
    assert caught.value.code == "SOURCE_CODING_MAINTENANCE_OUTSIDE_TRANSACTION_REQUIRED"
    assert row(state)["ciphertext"] == original["ciphertext"]


def test_valid_maintenance_is_idempotent_metadata_only_without_releasing_reservation(
    engine, tmp_path
):
    state = payload_env(engine, tmp_path, model=True)
    original = row(state)
    finances = ledger_rows(state)
    first = state.core.maintain(limit=1)
    assert first["bindings_checked"] == 1 and first["next_after"] == state.env.mandate.coding_id
    assert first["bindings_fenced"] == first["payloads_erased"] == first["operations_unknown"] == 0
    assert state.core.maintain(after=first["next_after"])["bindings_checked"] == 0
    assert row(state) == original and ledger_rows(state) == finances
