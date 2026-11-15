"""SA17 first formal original-Engine/Dispatcher checks on isolated real PG."""

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from sa08_support import policy
from sa17_support import plan, row, setup

from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.sources.ingress_domain import ItemMetadata
from mgar_v3.sources.ports import DeliveryReceipt
from mgar_v3.super_agent.source_notifications import SourceNotifications


def test_formal0049_exact_checksum_original_composition_and_shared_bytes(engine):
    a = Path(__file__).resolve().parents[1]
    p = a / "docs/super-agent-v1/sa17-proposal"
    m = json.loads((p / "proposal-manifest.json").read_text())
    formatting = json.loads((p / "post-integration-format.json").read_text())
    after = {r["path"]: r["formatted_after_sha256"] for r in formatting["files"]}
    repairs = json.loads((p / "post-integration-repairs.json").read_text())
    after.update({f["path"]: f["after_sha256"] for f in repairs["files"]})
    expected = {
        r.name: hashlib.sha256(r.read_bytes()).hexdigest() for r in (a / "migrations").glob("*.sql")
    }
    assert len(expected) == 46
    assert expected["0049_source_notifications.sql"] == m["migration"]["sha256"]
    assert (a / "migrations/0049_source_notifications.sql").read_bytes() == (
        p / "0049_source_notifications.sql"
    ).read_bytes()
    for f in m["patch"]["files"]:
        assert hashlib.sha256((a.parent / f["path"]).read_bytes()).hexdigest() == after.get(
            f["path"], f["after_sha256"]
        )
    assert isinstance(engine.source_notifications, SourceNotifications)
    assert (
        engine.source_notifications.engine is engine and engine.source_notifications._profiles == {}
    )
    assert engine.source_coding.runtime is None and engine.source_async.runtime is None
    with engine.transaction() as conn:
        assert {
            r["name"]: r["sha256"]
            for r in conn.execute("SELECT name,sha256 FROM app.schema_migrations")
        } == expected


def test_actual_dispatcher_records_delivery_without_advancing_failed_task(engine, tmp_path):
    e = setup(engine, tmp_path)
    p = plan(e)
    original = engine.get_run(e.work.run_id)
    dispatched = e.service.tick(e.cookie)
    assert dispatched["dispatched"] == [p["action_id"]]
    assert len(e.channel.sends) == 1
    result = e.service.get(e.cookie, p["action_id"])
    assert result["status"] == "succeeded" and result["receipts"][0]["outcome"] == "accepted"
    assert engine.get_run(e.work.run_id) == original
    assert not e.service.tick(e.cookie)["dispatched"]
    assert not plan(e)["created"] and len(e.channel.sends) == 1


def test_verified_original_readonly_graph_result_notifies_without_publication(engine, tmp_path):
    from sa12_support import claim, ready, session
    from test_super_agent_sa16_boundaries import ExactFixtureReader

    from mgar_v3.graph.runner import Runner
    from mgar_v3.settings import ROOT
    from mgar_v3.super_agent.source_scheduler_domain import SchedulerAssignmentRequest

    e = setup(engine, tmp_path, kind="verified_result", fail=False)
    scheduler = e.source.scheduler
    worker = session(scheduler)
    active = ready(scheduler, worker, claim(scheduler, worker))
    request = SchedulerAssignmentRequest(
        assignment_id=active["assignment_id"],
        session_id=worker["session_id"],
        epoch=active["execution_epoch"],
        expected_revision=active["revision"],
    )
    with engine.source_scheduler.worker_context(worker["bearer"], request):
        Runner(engine, ExactFixtureReader(ROOT / e.source.profile.read_path)).execute(e.work.run_id)
    before = engine.get_run(e.work.run_id)
    assert before["status"] == "succeeded" and before["outcome"] != "published"
    p = plan(e)
    assert e.service.tick(e.cookie)["dispatched"] == [p["action_id"]]
    assert engine.get_run(e.work.run_id) == before
    assert e.channel.sends[0][2].startswith("核验结果已记录")


def test_remote_acceptance_response_loss_is_unknown_then_query_only(engine, tmp_path):
    e = setup(engine, tmp_path)
    e.channel.outcome = "lost"
    p = plan(e)
    original = engine.get_run(e.work.run_id)
    assert e.service.tick(e.cookie)["dispatched"] == [p["action_id"]]
    assert e.service.get(e.cookie, p["action_id"])["status"] == "outcome_unknown"
    assert not e.service.tick(e.cookie)["dispatched"] and len(e.channel.sends) == 1
    assert e.service.reconcile(e.cookie, p["action_id"])["resolved"]
    assert len(e.channel.sends) == len(e.channel.queries) == 1
    assert engine.get_run(e.work.run_id) == original
    assert e.service.get(e.cookie, p["action_id"])["status"] == "succeeded"


@pytest.mark.parametrize("capability", ["unsupported", "unknown"])
def test_no_reliable_query_retains_unknown_and_waits_manual(capability, engine, tmp_path):
    e = setup(engine, tmp_path, query=capability)
    e.channel.outcome = "lost"
    p = plan(e)
    e.service.tick(e.cookie)
    result = e.service.reconcile(e.cookie, p["action_id"])
    assert not result["resolved"] and not result["resent"]
    assert len(e.channel.sends) == 1 and not e.channel.queries
    assert e.service.get(e.cookie, p["action_id"])["status"] == "outcome_unknown"
    assert not e.service.tick(e.cookie)["dispatched"]


def test_negative_query_does_not_prove_write_absent_or_allow_retry(engine, tmp_path):
    e = setup(engine, tmp_path)
    e.channel.outcome = "lost"
    p = plan(e)
    e.service.tick(e.cookie)

    def absent(instance, lookup):
        e.channel.queries.append(lookup)
        return DeliveryReceipt(
            scope=instance.scope, idempotency_key=lookup.idempotency_key, outcome="not_found"
        )

    e.channel.query = absent
    assert not e.service.reconcile(e.cookie, p["action_id"])["resolved"]
    assert e.service.get(e.cookie, p["action_id"])["status"] == "outcome_unknown"
    assert not e.service.tick(e.cookie)["dispatched"] and len(e.channel.sends) == 1


def test_definitive_rejection_does_not_retry_or_change_business_task(engine, tmp_path):
    e = setup(engine, tmp_path)
    e.channel.outcome = "rejected"
    p = plan(e)
    before = engine.get_run(e.work.run_id)
    e.service.tick(e.cookie)
    assert row(e, p["action_id"])[0]["status"] == "failed"
    assert engine.get_run(e.work.run_id) == before
    assert not e.service.tick(e.cookie)["dispatched"] and len(e.channel.sends) == 1


@pytest.mark.parametrize("fault", ["scope", "key", "no_remote_id"])
def test_mismatched_or_incomplete_receipt_never_becomes_known(fault, engine, tmp_path):
    e = setup(engine, tmp_path)
    p = plan(e)

    def wrong(instance, message, body):
        e.channel.sends.append(message)
        return DeliveryReceipt(
            scope=instance.scope.model_copy(update={"tenant_id": "other"})
            if fault == "scope"
            else instance.scope,
            idempotency_key="wrong" if fault == "key" else message.idempotency_key,
            outcome="accepted",
            remote_message_id=None if fault == "no_remote_id" else "remote-1",
        )

    e.channel.send = wrong
    e.service.tick(e.cookie)
    assert row(e, p["action_id"])[0]["status"] == "outcome_unknown"
    assert len(e.channel.sends) == 1 and not e.service.tick(e.cookie)["dispatched"]


@pytest.mark.parametrize("boundary", ["policy", "role"])
def test_revocation_committed_after_quota_before_final_send_blocks_actual_channel(
    boundary, engine, tmp_path, monkeypatch
):
    e = setup(engine, tmp_path)
    p = plan(e)
    original_transaction = engine.transaction
    quota = engine.project_budgets.notification_quota
    armed, committed = [], []

    def reserve(conn, action_id):
        quota(conn, action_id)
        armed.append(True)

    @contextmanager
    def transaction():
        with original_transaction() as conn:
            yield conn
        if armed and not committed:
            assert engine.connection.info.transaction_status.name == "IDLE"
            committed.append(True)
            if boundary == "policy":
                policy(e.source, mode="disabled")
            else:
                b = e.source.binding
                e.source.identities.roles(
                    e.cookie,
                    b["binding_id"],
                    RoleChange(
                        expected_revision=b["revision"],
                        roles=("requester",),
                        review_ref="SA17-committed-before-send",
                    ),
                )

    monkeypatch.setattr(engine.project_budgets, "notification_quota", reserve)
    monkeypatch.setattr(engine, "transaction", transaction)
    before = engine.get_run(e.work.run_id)
    e.service.tick(e.cookie)
    assert len(committed) == 1 and not e.channel.sends
    assert e.service.get(e.cookie, p["action_id"])["status"] == "cancelled"
    assert engine.get_run(e.work.run_id)["outcome"] == before["outcome"]


def test_final_quota_exhaustion_is_not_unknown_and_never_reaches_channel(
    engine, tmp_path, monkeypatch
):
    from mgar_v3.domain import DomainError

    e = setup(engine, tmp_path)
    p = plan(e)

    def exhausted(conn, action_id):
        raise DomainError("PROJECT_QUOTA_EXHAUSTED", "Controlled exhausted quota", 429)

    monkeypatch.setattr(engine.project_budgets, "notification_quota", exhausted)
    e.service.tick(e.cookie)
    assert not e.channel.sends
    assert e.service.get(e.cookie, p["action_id"])["status"] == "cancelled"


def test_trusted_receipt_causality_needs_exact_sender_scope_thread_and_entity(engine, tmp_path):
    e = setup(engine, tmp_path)
    plan(e)
    e.service.tick(e.cookie)
    with engine.transaction() as conn:
        saved = conn.execute(
            "SELECT metadata FROM app.source_ingress_inputs WHERE input_id=%s", (e.input.input_id,)
        ).fetchone()
        item = ItemMetadata.model_validate(saved["metadata"])
        event = item.event.model_copy(
            update={"sender": e.profile.sender, "entity_id": "notification-remote-1"}
        )
        own = item.model_copy(update={"event": event})
        assert e.service.own_result(conn, own)
        assert engine.fast_path._own_result(conn, own, b"not-a-marker")
        for changed in (
            event.model_copy(update={"entity_id": "other"}),
            event.model_copy(
                update={"sender": event.sender.model_copy(update={"actor_id": "Other"})}
            ),
            event.model_copy(
                update={
                    "source_thread": event.source_thread.model_copy(
                        update={"native_thread_id": "Other"}
                    )
                }
            ),
            event.model_copy(
                update={"sender": event.sender.model_copy(update={"actor_kind": "human"})}
            ),
        ):
            assert not e.service.own_result(conn, own.model_copy(update={"event": changed}))


def test_parallel_original_dispatchers_send_once_under_same_writer(engine, tmp_path):
    import threading

    e = setup(engine, tmp_path)
    p = plan(e)
    results, failures = [], []

    def send():
        try:
            results.append(e.service.tick(e.cookie, 1))
        except Exception as exc:
            failures.append(type(exc).__name__)

    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads) and not failures
    assert sum(len(r["dispatched"]) for r in results) == 1 and len(e.channel.sends) == 1
    assert e.service.get(e.cookie, p["action_id"])["status"] == "succeeded"


def test_original_comment_and_draft_routes_are_not_claimed_by_notification_transport(
    engine, tmp_path
):
    e = setup(engine, tmp_path)
    for kind in ("issue_comment", "draft_pr"):
        assert not e.service.handles({"action_type": kind})
    assert e.service.tick(e.cookie)["dispatched"] == []
    assert not e.channel.sends
