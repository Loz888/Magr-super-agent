"""New composition checks across original receive, Recall/Memory/CAS and Outbox."""

import json
import time
from pathlib import Path

import jsonschema
import pytest
from sa04_support import envelope, rows
from sa04_support import setup as ingress_setup
from sa15_support import legacy_memory, path, ref, setup
from sa17_support import plan, row
from sa17_support import setup as notification_setup
from sa21_support import cleartext_in_database, observation, withdraw

from mgar_v3.domain import DomainError
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.super_agent.source_recall_access import RecallCaller
from mgar_v3.super_agent.source_recall_domain import RecallPageRequest
from mgar_v3.super_agent.source_security import SourceSecurity


@pytest.mark.parametrize("operation", ["delete", "edit", "identity", "policy"])
def test_committed_withdrawal_blocks_source_memory_derived_export_and_saved_cursor(
    engine, tmp_path, operation
):
    env = setup(engine, tmp_path)
    memory, _ = legacy_memory(env)
    paths = [path(env.source_ref), env.report_path, path(env.run_ref, memory)]
    requests = [RecallPageRequest(path=p, max_bytes=128) for p in paths]
    first = [env.recall.page(env.owner, r) for r in requests]
    assert all(r["content"] for r in first) and all(r["cursor"] for r in first[:2])
    inspect = SourceSecurity(engine)
    before = inspect.inspect(env.cookie)
    withdraw(env.source, env.input_id, operation)
    for request, page in zip(requests, first, strict=True):
        with pytest.raises(DomainError):
            env.recall.page(env.owner, request)
        if page["cursor"]:
            with pytest.raises(DomainError):
                env.recall.page(env.owner, request.model_copy(update={"cursor": page["cursor"]}))
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, env.input_id)
    with engine.transaction() as conn:
        assert not conn.execute(
            "SELECT 1 FROM app.source_ingress_bodies WHERE input_id=%s", (env.input_id,)
        ).fetchone()
        assert conn.execute(
            "SELECT unavailable_at FROM app.source_ingress_inputs WHERE input_id=%s",
            (env.input_id,),
        ).fetchone()["unavailable_at"]
    after = inspect.inspect(env.cookie)
    assert all(r["unavailable_ciphertexts"] == 0 for r in after["sources"])
    assert before["security_gate"] == after["security_gate"] == "NOT_ESTABLISHED_BY_INSPECTION"
    observation("source_memory_cas_withdrawal", operation=operation, blocked_reads=5)


@pytest.mark.parametrize("operation", ["delete", "identity", "policy"])
@pytest.mark.parametrize("already_sent", [False, True])
def test_same_withdrawal_cancels_pending_but_preserves_unknown_without_releasing_content(
    engine, tmp_path, operation, already_sent
):
    env = notification_setup(engine, tmp_path, query="unsupported")
    source_path = path(ref("source", env.input.input_id))
    caller = RecallCaller("owner", env.cookie)
    request = RecallPageRequest(path=source_path)
    assert engine.source_recall.page(caller, request)["content"]
    planned = plan(env)
    if already_sent:
        env.channel.outcome = "lost"
        env.service.tick(env.cookie)
        assert len(env.channel.sends) == 1
    withdraw(env.source, env.input.input_id, operation)
    with pytest.raises(DomainError):
        engine.source_recall.page(caller, request)
    action, _ = row(env, planned["action_id"])
    expected = "outcome_unknown" if already_sent else "cancelled"
    assert action["status"] == expected
    for _ in range(2):
        assert not env.service.tick(env.cookie)["dispatched"]
    assert len(env.channel.sends) == int(already_sent)
    if already_sent:
        assert not env.service.reconcile(env.cookie, planned["action_id"])["resolved"]
        assert not env.channel.queries
    observation(
        "withdrawal_outbox", operation=operation, state=expected, sends=len(env.channel.sends)
    )


@pytest.mark.parametrize("fault", ["signature", "conversation", "sender"])
def test_untrusted_injected_body_is_not_retained_exported_or_decrypted(engine, fault, caplog):
    env = ingress_setup(engine)
    secret = "SA21_UNAUTHORIZED_PRIVATE_" + fault
    changes = {"conversation": "Private-Foreign"} if fault == "conversation" else {}
    if fault == "sender":
        changes["sender"] = "Unbound-Alice"
    incoming = envelope(env, body=secret, **changes)
    if fault == "signature":
        incoming = TransportEnvelope(payload=incoming.payload, headers={"signature": "0" * 64})
        with pytest.raises(DomainError):
            env.gateway.receive(incoming)
    else:
        receipt = env.gateway.receive(incoming)
        assert receipt.disposition == "rejected" and not receipt.input_ids
    assert not rows(env, "bodies") and not rows(env, "inputs")
    assert cleartext_in_database(engine, secret) == []
    assert secret not in caplog.text
    inspection = SourceSecurity(engine).inspect(env.cookie)
    assert secret not in json.dumps(inspection, default=str)
    observation("untrusted_ingress", fault=fault, retained_bodies=0, cleartext_tables=[])


def test_expired_body_is_purged_through_original_owner_command_without_erasing_minimal_facts(
    engine,
):
    env = ingress_setup(
        engine,
        receive={"enabled": True, "retain_body": True, "body_ttl_seconds": 1},
    )
    receipt = env.gateway.receive(envelope(env, body="SA21 short lived synthetic body"))
    assert engine.source_ingress.body(env.cookie, receipt.input_ids[0])
    time.sleep(1.1)
    before = SourceSecurity(engine).inspect(env.cookie)
    assert before["sources"][0]["ciphertexts_due_for_purge"] == 1
    result = engine.source_ingress.purge(env.cookie, env.installation.scope.source_instance_id)
    assert result["expired_bodies"] == 1
    assert not rows(env, "bodies") and len(rows(env, "inputs")) == len(rows(env, "deliveries")) == 1
    after = SourceSecurity(engine).inspect(env.cookie)
    assert after["sources"][0]["retained_ciphertexts"] == 0
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, receipt.input_ids[0])
    observation("retention_sweep", purged=1, input_facts_preserved=1, delivery_facts_preserved=1)


def test_inspection_requires_current_owner_and_never_calls_body_decryption(engine, monkeypatch):
    env = ingress_setup(engine)
    env.gateway.receive(envelope(env, body="SA21 inspection must never release body"))
    service = SourceSecurity(engine)
    with pytest.raises(DomainError):
        service.inspect("untrusted-owner-cookie")

    def forbidden(*args, **kwargs):
        raise AssertionError("Retention inspection must not decrypt or read CAS")

    monkeypatch.setattr(engine.source_ingress.cipher, "decrypt", forbidden)
    monkeypatch.setattr(engine.artifacts, "read", forbidden)
    before = rows(env, "inputs")
    result = service.inspect(env.cookie)
    assert result["read_only"] and result["sources"][0]["retained_ciphertexts"] == 1
    assert rows(env, "inputs") == before
    schema = Path("schemas/super-agent-v1/source-security-inspection-v1.schema.json")
    jsonschema.validate(json.loads(json.dumps(result, default=str)), json.loads(schema.read_text()))
