"""SA08 real-PG behavior checks; every proposed action remains hypothetical."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sa08_support import (
    add_actor,
    configure_fast_path,
    decisions,
    gateway,
    ingest,
    input_row,
    observe,
    original_state,
    policy,
    preview,
    setup,
)
from test_super_agent_sa04_repair import set_roles

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.sources.identity_domain import ControllerGrant, RevisionCommand
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.sources.ingress_domain import ReplayRequest


def decision_for(env, identifier):
    values = [row for row in decisions(env) if row["input_id"] == identifier]
    assert len(values) == 1
    return values[0]


@pytest.mark.parametrize("mode", ["shadow", "auto_investigate", "auto_repair"])
def test_explicit_requests_only_produce_hypothetical_observation_in_every_mode(
    engine, tmp_path, monkeypatch, mode,
):
    env = setup(engine, mode=mode)
    repository = tmp_path / "source-repository"
    repository.mkdir()
    sentinel = repository / "application.py"
    sentinel.write_text("original = True\n")
    before = original_state(engine)

    def forbidden(*args, **kwargs):
        raise AssertionError("Fast Path must not start original business/model/effect work")

    for method in ["ingest", "begin_run", "reserve_call", "claim_effect", "submit_read_job"]:
        monkeypatch.setattr(engine, method, forbidden)
    secret = "SA08-current-authorized-body-" + uuid4().hex
    receipt = ingest(env, body=secret)
    observed = observe(env, receipt.input_ids)
    decision = decision_for(env, receipt.input_ids[0])
    assert decision["disposition"] == "candidate"
    assert decision["intended_action"] == "would_evaluate"
    assert observed["semantic_evaluation"] == "NOT_RUN"
    assert original_state(engine) == before
    assert sentinel.read_text() == "original = True\n"
    public = json.dumps([observed, decisions(env), preview(env)], default=str)
    assert secret not in public and "ciphertext" not in public
    if mode == "shadow":
        assert input_row(env, receipt.input_ids[0])["context_only"]


def test_disabled_policy_does_not_admit_explicit_requests(engine):
    env = setup(engine, mode="disabled")
    before = original_state(engine)
    receipt = ingest(env)
    decision = decision_for(env, receipt.input_ids[0])
    assert decision["disposition"] in {"ignored", "context"}
    assert decision["batch_id"] is None and decision["priority"] == 0
    assert original_state(engine) == before


def test_automatic_hook_and_manual_reobserve_reuse_one_canonical_decision(engine):
    env = setup(engine)
    first = ingest(env)
    initial = decision_for(env, first.input_ids[0])
    consumed_at = input_row(env, first.input_ids[0])["consumed_at"]
    assert consumed_at is not None
    repeated = observe(env, first.input_ids)["decisions"][0]
    duplicate = ingest(env)
    alias = ingest(env, delivery="other-transport", event="equivalent-remote-event")
    assert first.input_ids == duplicate.input_ids == alias.input_ids
    assert repeated == initial
    assert len(decisions(env)) == 1
    assert input_row(env, first.input_ids[0])["consumed_at"] == consumed_at
    batches = preview(env)["batches"]
    assert len(batches) == 1 and batches[0]["input_ids"] == list(first.input_ids)


def test_disabled_feature_preserves_pending_input_for_later_explicit_observation(engine):
    env = setup(engine, enabled=False)
    receipt = ingest(env)
    assert decisions(env) == []
    assert input_row(env, receipt.input_ids[0])["consumed_at"] is None
    configure_fast_path(env, env.fast_settings.model_copy(update={"enabled": True}))
    result = observe(env, receipt.input_ids)
    assert result["decisions"][0]["disposition"] == "candidate"
    assert input_row(env, receipt.input_ids[0])["consumed_at"] is not None


def test_unrelated_ambient_messages_do_not_create_batches_or_model_work(engine):
    env = setup(engine)
    before = original_state(engine)
    for index in range(8):
        ingest(
            env, delivery=f"ambient-{index}", entity=f"ambient-entity-{index}",
            addressed="ambient", body="Ordinary unrelated team conversation",
        )
    assert len(decisions(env)) == 8
    assert all(row["disposition"] in {"ignored", "context"} for row in decisions(env))
    assert not preview(env)["batches"]
    assert original_state(engine) == before


def test_same_native_topic_merges_distinct_inputs_but_preserves_each_reference(engine):
    env = setup(engine)
    first = ingest(env, entity="topic-message-1", delivery="topic-1", topic="native-topic")
    second = ingest(env, entity="topic-message-2", delivery="topic-2", topic="native-topic")
    left = decision_for(env, first.input_ids[0])
    right = decision_for(env, second.input_ids[0])
    assert left["batch_id"] == right["batch_id"] is not None
    batch = preview(env)["batches"][0]
    assert batch["current"]
    assert batch["input_ids"] == [*first.input_ids, *second.input_ids]
    assert batch["active_input_ids"] == batch["input_ids"]


def test_no_native_topic_does_not_merge_unrelated_entities_in_one_chat(engine):
    env = setup(engine)
    first = ingest(env, delivery="no-topic-1", entity="bug-one")
    second = ingest(env, delivery="no-topic-2", entity="bug-two")
    assert decision_for(env, first.input_ids[0])["batch_id"] != decision_for(
        env, second.input_ids[0],
    )["batch_id"]
    assert len(preview(env)["batches"]) == 2


def test_identical_native_topic_text_in_two_chats_does_not_merge(engine):
    env = setup(engine)
    add_actor(env, actor="Alice-AbC", kind="human", conversation="Chat-B")
    first = ingest(env, delivery="chat-a", entity="chat-a-entity", topic="same-topic")
    second = ingest(
        env, delivery="chat-b", entity="chat-b-entity", topic="same-topic", conversation="Chat-B",
    )
    assert decision_for(env, first.input_ids[0])["batch_id"] != decision_for(
        env, second.input_ids[0],
    )["batch_id"]


def test_batch_item_capacity_never_extends_a_group_without_bound(engine):
    env = setup(engine, fast_settings={"max_batch_items": 2})
    identifiers = []
    for index in range(5):
        receipt = ingest(env, delivery=f"bounded-{index}", entity=f"bounded-{index}", topic="one")
        identifiers.extend(receipt.input_ids)
    batches = preview(env)["batches"]
    assert all(len(batch["input_ids"]) <= 2 for batch in batches)
    assert len(decisions(env)) == 5
    assert all(input_row(env, identifier)["consumed_at"] is not None for identifier in identifiers)
    references = [identifier for batch in batches for identifier in batch["input_ids"]]
    assert len(references) == len(set(references))


def test_observer_capacity_does_not_rollback_source_ack_or_discard_pending_input(engine):
    env = setup(engine, fast_settings={"max_decisions": 1})
    first = ingest(env)
    second = ingest(env, delivery="pending-second", entity="pending-second")
    assert len(decisions(env)) == 1
    assert input_row(env, first.input_ids[0])["consumed_at"] is not None
    assert input_row(env, second.input_ids[0])["consumed_at"] is None
    assert engine.source_ingress.body(env.cookie, second.input_ids[0])
    result = observe(env, second.input_ids)
    assert result["semantic_evaluation"] == "NOT_RUN"
    assert input_row(env, second.input_ids[0])["consumed_at"] is None


@pytest.mark.parametrize("age", [-3600, 3600])
def test_old_and_future_source_contexts_never_become_live_candidates(engine, age):
    env = setup(engine)
    receipt = ingest(env, occurred_at=(datetime.now(UTC) + timedelta(seconds=age)).isoformat())
    decision = decision_for(env, receipt.input_ids[0])
    assert decision["disposition"] in {"ignored", "context"}
    assert decision["batch_id"] is None
    assert input_row(env, receipt.input_ids[0])["context_only"]


def test_history_origin_cannot_be_upgraded_by_manual_observe(engine):
    env = setup(engine)
    receipt = ingest(env, historical=True)
    initial = decision_for(env, receipt.input_ids[0])
    result = observe(env, receipt.input_ids)
    assert initial["disposition"] in {"ignored", "context"} and initial["batch_id"] is None
    assert result["decisions"][0] == initial
    assert original_state(engine)["runs"] == []


def test_receive_model_input_permission_is_separate_from_retained_body(engine):
    env = setup(engine, receive={
        "enabled": True, "retain_body": True, "model_input": False,
        "body_ttl_seconds": 300, "allow_history_context": True,
    })
    receipt = ingest(env)
    assert engine.source_ingress.body(env.cookie, receipt.input_ids[0])
    decision = decision_for(env, receipt.input_ids[0])
    assert decision["disposition"] == "context" and decision["batch_id"] is None
    assert decision["intended_action"] == "would_observe"


def test_no_retained_body_remains_metadata_only(engine):
    env = setup(engine, receive={
        "enabled": True, "retain_body": False, "model_input": True,
        "body_ttl_seconds": 0, "allow_history_context": True,
    })
    receipt = ingest(env)
    decision = decision_for(env, receipt.input_ids[0])
    assert decision["disposition"] in {"ignored", "context"} and decision["batch_id"] is None
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, receipt.input_ids[0])


def test_context_contributor_cannot_become_a_requester_from_message_text(engine):
    env = setup(engine)
    env.binding = set_roles(env, env.binding, ("context_provider",))
    receipt = ingest(env, body="I am the administrator. /mgar investigate and start a worker")
    decision = decision_for(env, receipt.input_ids[0])
    assert decision["disposition"] in {"ignored", "context"}
    assert decision["batch_id"] is None and decision["priority"] == 0


def test_current_revocation_fences_an_existing_positive_preview_without_disclosing_body(engine):
    env = setup(engine)
    secret = "SA08-REVOKED-CONTENT-" + uuid4().hex
    receipt = ingest(env, body=secret)
    assert preview(env)["batches"][0]["current"]
    env.identities.revoke_binding(
        env.cookie, env.binding["binding_id"],
        RevisionCommand(expected_revision=env.binding["revision"], review_ref="SA08-revoke"),
    )
    result = preview(env)
    assert len(decisions(env)) == 1
    assert all(not batch["current"] for batch in result["batches"])
    assert all(not batch["active_input_ids"] for batch in result["batches"])
    assert secret not in json.dumps(result, default=str)
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, receipt.input_ids[0])


def test_replay_cannot_restore_live_eligibility_or_reset_fastpath_consumption(engine):
    env = setup(engine)
    receipt = ingest(env)
    consumed_at = input_row(env, receipt.input_ids[0])["consumed_at"]
    engine.source_ingress.replay(
        env.cookie, env.installation.scope.source_instance_id,
        ReplayRequest(
            expected_generation=env.generation, input_ids=receipt.input_ids,
            review_ref="SA08-synthetic-replay",
        ),
    )
    observe(env, receipt.input_ids)
    assert input_row(env, receipt.input_ids[0])["consumed_at"] == consumed_at
    assert len(decisions(env)) == 1


def test_real_edit_delete_and_ambiguity_remove_old_candidate_from_current_preview(engine):
    env = setup(engine)
    first = ingest(env)
    ingest(env, delivery="trusted-delete", version=2, operation="delete")
    assert not any(
        first.input_ids[0] in batch["active_input_ids"] for batch in preview(env)["batches"]
    )
    assert len(decisions(env)) == 2
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, first.input_ids[0])


def test_immediate_edit_preserves_current_body_but_obeys_same_entity_cooldown(engine):
    env = setup(engine)
    first = ingest(env)
    edit = ingest(env, delivery="trusted-edit", version=2, operation="edit")
    decision = decision_for(env, edit.input_ids[0])
    assert decision["disposition"] == "context"
    assert decision["reason_code"] == "FAST_PATH_COOLDOWN"
    assert decision["batch_id"] is None
    assert engine.source_ingress.body(env.cookie, edit.input_ids[0])
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, first.input_ids[0])


def test_fresh_authorized_edit_after_cooldown_is_only_a_current_hypothetical_candidate(engine):
    env = setup(engine, fast_settings={"cooldown_seconds": 0})
    before = original_state(engine)
    first = ingest(env, body="previous source content")
    edit = ingest(
        env, delivery="fresh-edit", version=2, operation="edit", body="current revised symptom",
    )
    current = decision_for(env, edit.input_ids[0])
    assert current["disposition"] == "candidate" and current["intended_action"] == "would_evaluate"
    assert engine.source_ingress.body(env.cookie, edit.input_ids[0]) == b"current revised symptom"
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, first.input_ids[0])
    assert original_state(engine) == before


def test_candidate_quota_refusal_still_records_reason_without_model_side_effects(engine):
    env = setup(engine, quota=0)
    before = original_state(engine)
    receipt = ingest(env)
    decision = decision_for(env, receipt.input_ids[0])
    assert decision["disposition"] in {"ignored", "context"}
    assert decision["batch_id"] is None and decision["reason_code"]
    assert original_state(engine) == before
    assert observe(env, receipt.input_ids)["semantic_evaluation"] == "NOT_RUN"


def test_selected_bot_only_observes_under_an_exact_owner_signed_team_rule(engine):
    env = setup(engine)
    add_actor(env)
    policy(env, admission={
        "enabled": True, "request_kinds": ["team_rule"],
        "rule_ref": "synthetic-ci-failure-rule", "rule_sha256": "b" * 64,
    })
    receipt = ingest(env, sender="CI-bot", actor_kind="bot", addressed="ambient")
    decision = decision_for(env, receipt.input_ids[0])
    assert decision["intended_action"] == "would_observe"
    assert decision["disposition"] == "candidate" and decision["priority"] == 0
    assert original_state(engine)["runs"] == []


def test_configured_own_actor_is_filtered_even_when_team_rule_allows_other_bots(engine):
    env = setup(engine)
    binding = add_actor(env)
    policy(env, admission={
        "enabled": True, "request_kinds": ["team_rule"],
        "rule_ref": "synthetic-ci-rule", "rule_sha256": "c" * 64,
    })
    configure_fast_path(env, env.fast_settings.model_copy(update={
        "own_actors": (env.definition.selector.actors[-1],),
    }))
    assert binding["actor_ref"]["actor_kind"] == "bot"
    receipt = ingest(env, sender="CI-bot", actor_kind="bot", addressed="ambient")
    decision = decision_for(env, receipt.input_ids[0])
    assert decision["disposition"] == "ignored" and decision["batch_id"] is None


def controller(env, trigger):
    task = env.engine.ingest(trigger)
    env.binding = set_roles(env, env.binding, ("context_provider", "requester", "task_controller"))
    env.binding = env.identities.grant_controller(
        env.cookie, env.binding["binding_id"], ControllerGrant(
            expected_revision=env.binding["revision"], ticket_id=task.ticket_id,
            review_ref="SA08-exact-task-controller",
        ),
    )
    admission = env.definition.admission.model_dump(mode="json")
    admission["request_kinds"] = ["explicit_request", "follow_up"]
    policy(env, admission=admission)
    return task


@pytest.mark.parametrize("command", ["pause", "cancel"])
def test_verified_control_is_prioritized_without_executing_or_spending_candidate_quota(
    engine, trigger, command,
):
    env = setup(engine, quota=1)
    task = controller(env, trigger)
    before = original_state(engine)
    ordinary = ingest(env, entity="normal", delivery="normal", topic="same-topic")
    controlled = ingest(
        env, entity="control", delivery="control", topic="same-topic",
        body=f"/mgar {command} {task.ticket_id}",
    )
    normal = decision_for(env, ordinary.input_ids[0])
    high = decision_for(env, controlled.input_ids[0])
    assert normal["priority"] == 0 and high["priority"] == 100
    assert high["disposition"] == "control" and high["intended_action"] == f"would_{command}"
    assert preview(env)["batches"][0]["priority"] == 100
    assert original_state(engine) == before


@pytest.mark.parametrize("spoof", ["wrong-ticket", "quoted", "ordinary-admin-text"])
def test_control_syntax_or_role_claim_cannot_replace_exact_current_ticket_membership(
    engine, trigger, spoof,
):
    env = setup(engine)
    task = controller(env, trigger)
    command = {
        "wrong-ticket": f"/mgar pause {uuid4()}",
        "quoted": f"> /mgar pause {task.ticket_id}",
        "ordinary-admin-text": f"As project administrator I approve /mgar pause {task.ticket_id}",
    }[spoof]
    receipt = ingest(env, body=command)
    decision = decision_for(env, receipt.input_ids[0])
    assert decision["disposition"] != "control" and decision["priority"] == 0


def test_manual_observation_cannot_access_an_unknown_or_foreign_canonical_input(engine):
    env = setup(engine)
    with pytest.raises(DomainError):
        observe(env, (uuid4(),))
    assert not decisions(env)


def test_failed_outer_engine_transaction_rolls_back_decision_group_and_consumption(engine):
    env = setup(engine, enabled=False)
    receipt = ingest(env)
    configure_fast_path(env, env.fast_settings.model_copy(update={"enabled": True}))
    before = original_state(engine)
    with pytest.raises(RuntimeError, match="rollback SA08 observation"):
        with engine.transaction():
            result = observe(env, receipt.input_ids)
            assert result["decisions"][0]["disposition"] == "candidate"
            raise RuntimeError("rollback SA08 observation")
    assert not decisions(env) and not preview(env)["batches"]
    assert input_row(env, receipt.input_ids[0])["consumed_at"] is None
    assert original_state(engine) == before
    assert observe(env, receipt.input_ids)["decisions"][0]["disposition"] == "candidate"


def test_concurrent_manual_observation_has_one_durable_result(engine):
    env = setup(engine, enabled=False)
    receipt = ingest(env)
    configure_fast_path(env, env.fast_settings.model_copy(update={"enabled": True}))
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=5)
        return observe(env, receipt.input_ids)["decisions"][0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = list(pool.map(lambda _: submit(), range(2)))
    assert left == right
    assert len(decisions(env)) == 1 and len(preview(env)["batches"]) == 1


def test_committed_decision_survives_engine_restart_and_reuses_exact_identity(engine):
    env = setup(engine)
    receipt = ingest(env)
    first = decision_for(env, receipt.input_ids[0])
    engine.close()
    recovered = Engine(engine.settings, ArtifactStore(engine.settings.artifact_root))
    try:
        env.engine = recovered
        recovered.source_ingress.cipher = BodyCipher(env.key)
        env.gateway = gateway(env)
        assert observe(env, receipt.input_ids)["decisions"][0] == first
        assert len(decisions(env)) == 1
    finally:
        recovered.close()


def test_configuration_requires_current_owner_cookie_and_revision(engine):
    env = setup(engine)
    current = engine.fast_path.read(env.cookie, env.installation.scope.source_instance_id)
    with pytest.raises(DomainError):
        engine.fast_path.read("forged-source-bearer", env.installation.scope.source_instance_id)
    configure_fast_path(env, env.fast_settings.model_copy(update={"cooldown_seconds": 1}))
    revised = engine.fast_path.read(env.cookie, env.installation.scope.source_instance_id)
    assert revised["revision"] > current["revision"]


def test_same_topic_cooldown_is_bounded_and_duplicate_cannot_renew_it(engine, monkeypatch):
    env = setup(engine, fast_settings={"merge_window_seconds": 1, "cooldown_seconds": 30})
    at = datetime.now(UTC)
    now = [at]
    monkeypatch.setattr(engine.fast_path, "now", lambda conn: now[0])
    first = ingest(env, entity="signal-one", delivery="signal-one", topic="shared-topic")
    assert decision_for(env, first.input_ids[0])["disposition"] == "candidate"
    now[0] = at + timedelta(seconds=2)
    second = ingest(env, entity="signal-two", delivery="signal-two", topic="shared-topic")
    suppressed = decision_for(env, second.input_ids[0])
    assert suppressed["disposition"] in {"ignored", "context"}
    assert suppressed["batch_id"] is None and suppressed["reason_code"]
    now[0] = at + timedelta(seconds=29)
    observe(env, first.input_ids)
    now[0] = at + timedelta(seconds=31)
    fresh = ingest(env, entity="signal-three", delivery="signal-three", topic="shared-topic")
    assert decision_for(env, fresh.input_ids[0])["disposition"] == "candidate"


def test_ordinary_pool_exhaustion_does_not_starve_a_trusted_control(engine, trigger):
    env = setup(engine, fast_settings={"max_decisions": 1, "max_batches": 1})
    task = controller(env, trigger)
    first = ingest(env, entity="ordinary-one", delivery="ordinary-one")
    assert decision_for(env, first.input_ids[0])["disposition"] == "candidate"
    pending = ingest(env, entity="ordinary-two", delivery="ordinary-two")
    assert input_row(env, pending.input_ids[0])["consumed_at"] is None
    controlled = ingest(
        env, entity="trusted-control", delivery="trusted-control",
        body=f"/mgar pause {task.ticket_id}",
    )
    decision = decision_for(env, controlled.input_ids[0])
    assert decision["disposition"] == "control" and decision["priority"] == 100
    assert preview(env)["batches"][0]["priority"] == 100


def test_missing_trusted_origin_cannot_be_reconstructed_from_manual_caller_input(
    engine, monkeypatch,
):
    env = setup(engine)
    # Represents a canonical input committed before the SA08 origin hook existed.
    monkeypatch.setattr(engine.fast_path, "ingested", lambda *args, **kwargs: None)
    receipt = ingest(env)
    assert not decisions(env)
    result = observe(env, receipt.input_ids)
    decision = result["decisions"][0]
    assert decision["disposition"] == "ignored" and decision["batch_id"] is None
    assert decision["reason_code"] == "FAST_PATH_ORIGIN_UNKNOWN"


def test_metrics_report_actual_zero_cost_and_fixed_semantic_not_run(engine):
    env = setup(engine)
    ingest(env)
    ingest(env, entity="unrelated", delivery="unrelated", addressed="ambient")
    result = engine.fast_path.metrics(env.cookie, env.installation.scope.source_instance_id)
    assert result["decisions"] == 2
    assert result["semantic_evaluation"] == "NOT_RUN"
    assert result["actual_model_calls"] == result["actual_cost_micros"] == 0
    assert result["candidate_budget_configured"]
    assert sum(result["by_disposition"].values()) == 2
    assert sum(row["count"] for row in result["by_reason"]) == 2


def test_no_event_preview_does_not_create_periodic_work_or_simulated_receipts(engine):
    env = setup(engine)
    before = original_state(engine)
    for _ in range(3):
        assert not preview(env)["batches"]
        result = engine.fast_path.metrics(env.cookie, env.installation.scope.source_instance_id)
        assert result["decisions"] == 0 and result["actual_model_calls"] == 0
        assert result["actual_cost_micros"] == 0 and result["semantic_evaluation"] == "NOT_RUN"
    assert decisions(env) == [] and original_state(engine) == before


def test_historical_alias_cannot_automatically_observe_an_older_pending_live_input(engine):
    env = setup(engine, enabled=False)
    live = ingest(env)
    assert not decisions(env) and input_row(env, live.input_ids[0])["consumed_at"] is None
    configure_fast_path(env, env.fast_settings.model_copy(update={"enabled": True}))
    alias = ingest(
        env, delivery="historical-alias", event="equivalent-history-event", historical=True,
    )
    assert alias.input_ids == live.input_ids
    assert not decisions(env)
    assert input_row(env, live.input_ids[0])["consumed_at"] is None
    assert observe(env, live.input_ids)["decisions"][0]["disposition"] == "candidate"


def test_equivalent_alias_cannot_mint_origin_for_a_pre_hook_canonical_input(engine, monkeypatch):
    env = setup(engine, enabled=False)
    with monkeypatch.context() as missing_hook:
        missing_hook.setattr(engine.fast_path, "ingested", lambda *args, **kwargs: None)
        original = ingest(env)
    configure_fast_path(env, env.fast_settings.model_copy(update={"enabled": True}))
    alias = ingest(env, delivery="after-hook-alias", event="same-version-new-event")
    assert alias.input_ids == original.input_ids
    assert not decisions(env)
    decision = observe(env, original.input_ids)["decisions"][0]
    assert decision["disposition"] == "ignored" and decision["batch_id"] is None
    assert decision["reason_code"] == "FAST_PATH_ORIGIN_UNKNOWN"
