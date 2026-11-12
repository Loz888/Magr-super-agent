"""Source namespace isolation and original RouterWorker composition.

Tagged batch/call rows below are explicitly synthetic malformed namespace probes.
They are never Source evaluation results, real provider calls, or fabricated Runs.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_routing import cold
from test_routing import routed as routed

from mgar_v3.domain import DomainError, digest
from mgar_v3.routing.domain import Proposal, Proposals, RouterDecision
from mgar_v3.routing.worker import FixtureRouter, RouterWorker

SOURCE_VERSION = "source-router-snapshot-v1"


def tagged_probe(engine, *, status="prepared", mode="fixture", with_call=False):
    """An adversarial tag must be refused before any legacy payload interpretation."""
    batch_id = uuid4()
    value = {
        "schema_version": SOURCE_VERSION,
        "batch_id": str(batch_id),
        "synthetic_namespace_probe": True,
        "threads": ["not a legacy thread; do not inspect a Source batch"],
    }
    with engine.transaction() as conn:
        row = conn.execute(
            "INSERT INTO app.router_batches(batch_id,project_id,policy_version,context_hash,"
            "snapshot,mode,status,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                batch_id,
                engine.settings.project_id,
                engine.settings.policy_version,
                digest(value),
                Jsonb(value),
                mode,
                status,
                datetime.now(UTC) + timedelta(minutes=5),
            ),
        ).fetchone()
        if with_call:
            profile = "synthetic-namespace-" + uuid4().hex
            conn.execute(
                "INSERT INTO app.authorization_budgets(profile_hash,limit_micros,call_limit) "
                "VALUES(%s,1,1)",
                (profile,),
            )
            conn.execute(
                "INSERT INTO app.router_calls(call_id,batch_id,budget_profile_hash,"
                "router_profile_hash,status,reserved_micros,settled_micros,result) "
                "VALUES(%s,%s,%s,%s,'known',1,0,%s)",
                (uuid4(), batch_id, profile, profile, Jsonb({"synthetic_namespace_probe": True})),
            )
    assert row is not None
    return row


def probe_state(engine, batch_id):
    with engine.transaction() as conn:
        return (
            conn.execute(
                "SELECT * FROM app.router_batches WHERE batch_id=%s", (batch_id,)
            ).fetchone(),
            conn.execute(
                "SELECT * FROM app.router_calls WHERE batch_id=%s", (batch_id,)
            ).fetchall(),
            conn.execute(
                "SELECT * FROM app.router_audit WHERE batch_id=%s", (batch_id,)
            ).fetchall(),
        )


@pytest.mark.parametrize("mode", ["fixture", "live"])
@pytest.mark.parametrize(
    "status",
    [
        "prepared",
        "ready",
        "model_running",
        "failed",
        "rejected",
        "usage_unknown",
        "applied",
    ],
)
def test_legacy_prepare_never_claims_or_interprets_tagged_source_batches(routed, mode, status):
    row = tagged_probe(routed, status=status, mode=mode, with_call=True)
    before = probe_state(routed, row["batch_id"])
    assert routed.routing.prepare(mode) is None
    assert probe_state(routed, row["batch_id"]) == before
    assert routed.list_runs() == []


@pytest.mark.parametrize("status", ["prepared", "model_running", "failed", "usage_unknown"])
def test_tagged_source_prior_failure_and_known_cache_do_not_block_legacy_input(
    routed,
    trigger,
    status,
):
    source = tagged_probe(routed, status=status, mode="live", with_call=True)
    before = probe_state(routed, source["batch_id"])
    event = cold(routed, trigger)
    batch = routed.routing.prepare("live")
    assert batch is not None and batch["batch_id"] != source["batch_id"]
    assert (
        batch["status"] == "prepared" and batch["snapshot"].get("schema_version") != SOURCE_VERSION
    )
    assert batch["snapshot"]["threads"][0]["thread_id"] == str(event.thread_id)
    assert probe_state(routed, source["batch_id"]) == before


@pytest.mark.parametrize("operation", ["wrap", "validate"])
def test_legacy_wrapping_and_validation_refuse_the_source_namespace(routed, operation):
    row = tagged_probe(routed)
    before = probe_state(routed, row["batch_id"])
    with pytest.raises(DomainError) as error:
        if operation == "wrap":
            routed.routing.wrap(
                row,
                Proposals(
                    proposals=[
                        Proposal(
                            thread_id=uuid4(),
                            action="ignore",
                        )
                    ]
                ),
            )
        else:
            with routed.transaction() as conn:
                routed.routing._validate(conn, row, [], False)
    assert error.value.code == "ROUTER_PIPELINE_MISMATCH"
    assert probe_state(routed, row["batch_id"]) == before


@pytest.mark.parametrize("status", ["prepared", "ready", "applied"])
def test_legacy_commit_refuses_source_before_even_writing_a_template_artifact(
    routed,
    monkeypatch,
    status,
):
    row = tagged_probe(routed, status=status)
    before = probe_state(routed, row["batch_id"])
    writes = []

    def unexpected(content):
        writes.append(content)
        raise AssertionError("Legacy template CAS must not see a Source batch")

    monkeypatch.setattr(routed.artifacts, "put", unexpected)
    suggestion = RouterDecision(
        thread_id=uuid4(),
        action="request_clarification",
        message_code="clarification",
        decision_key="synthetic-source-namespace-refusal",
        input_event_ids=[uuid4()],
        expected_thread_revision=1,
        policy_version=routed.settings.policy_version,
        context_snapshot_hash=row["context_hash"],
        expires_at=row["expires_at"],
    )
    with pytest.raises(DomainError) as error:
        routed.routing.commit(row["batch_id"], [suggestion])
    assert error.value.code == "ROUTER_PIPELINE_MISMATCH" and writes == []
    assert probe_state(routed, row["batch_id"]) == before


@pytest.mark.parametrize("status", ["prepared", "ready", "model_running", "usage_unknown"])
def test_legacy_save_and_failure_cannot_rewrite_source_state(routed, status):
    row = tagged_probe(routed, status=status)
    before = probe_state(routed, row["batch_id"])
    with pytest.raises(DomainError) as error:
        routed.routing.save(row["batch_id"], [])
    assert error.value.code == "ROUTER_BATCH_STATE"
    routed.routing.failure(row["batch_id"], "SYNTHETIC_NAMESPACE_FAILURE")
    assert probe_state(routed, row["batch_id"]) == before


def test_default_router_worker_keeps_the_original_routing_behavior(routed, trigger):
    source = tagged_probe(routed)
    cold(routed, trigger)

    class Counting(FixtureRouter):
        calls = 0

        def propose(self, batch):
            self.calls += 1
            return super().propose(batch)

    provider = Counting()
    worker = RouterWorker(routed, provider)
    assert worker.tick() != source["batch_id"]
    assert len(routed.list_runs()) == 1 and provider.calls == 1
    assert worker.tick() is None and provider.calls == 1
    assert probe_state(routed, source["batch_id"])[0]["status"] == "prepared"


@pytest.mark.parametrize("only", ["pipeline", "decoder"])
def test_router_pipeline_and_decoder_must_be_supplied_together(only):
    engine = SimpleNamespace(routing=object())
    kwargs = (
        {"pipeline": object()} if only == "pipeline" else {"decision_decoder": lambda value: value}
    )
    with pytest.raises(DomainError) as error:
        RouterWorker(engine, FixtureRouter(), **kwargs)
    assert error.value.code == "ROUTER_PIPELINE_CONFIGURATION"


@pytest.mark.parametrize("status", ["prepared", "ready"])
def test_injected_pipeline_decodes_ready_evidence_without_repeating_provider(status):
    identifier = uuid4()
    calls = []
    decoded = {"safe": "source-only typed decision"}

    class Pipeline:
        def prepare(self, mode):
            calls.append(("prepare", mode))
            return {"batch_id": identifier, "status": status, "decisions": [{"stored": True}]}

        def wrap(self, batch, proposals):
            calls.append(("wrap", proposals))
            return [decoded]

        def save(self, batch_id, decisions):
            calls.append(("save", batch_id, decisions))

        def commit(self, batch_id, decisions, *, permit=None):
            calls.append(("commit", batch_id, decisions, permit))

        def failure(self, batch_id, reason):
            pytest.fail("Injected pipeline should succeed without touching legacy failure")

    class Provider:
        mode = "fixture"

        def propose(self, batch):
            calls.append(("provider", batch["batch_id"]))
            assert status == "prepared", "Ready recovery must not repeat a model/provider"
            return "source-only proposals"

    def decode(value):
        calls.append(("decode", value))
        return decoded

    engine = SimpleNamespace(routing=object())
    worker = RouterWorker(engine, Provider(), pipeline=Pipeline(), decision_decoder=decode)
    assert worker.tick() == identifier
    assert calls[-1] == ("commit", identifier, [decoded], None)
    assert sum(call[0] == "provider" for call in calls) == (status == "prepared")
    assert sum(call[0] == "decode" for call in calls) == (status == "ready")
    assert sum(call[0] == "save" for call in calls) == (status == "prepared")
