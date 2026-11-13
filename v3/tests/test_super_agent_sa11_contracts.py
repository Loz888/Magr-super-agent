"""Source subscription confusion, readonly purposes and exact consumption fences.

These tests use synthetic typed facts only. The registered PostgreSQL checks own
current identity/permission lookups, transactions and original Run state.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import SourceScope
from mgar_v3.super_agent.source_correlation_domain import (
    CorrelationInputRef,
    ResourceRef,
    TargetRef,
)
from mgar_v3.super_agent.source_subscription_domain import (
    MAX_HANDBACK_CYCLES,
    MAX_PAGE_BYTES,
    MAX_TTL_SECONDS,
    HandbackFacts,
    MaterializedSourceItem,
    MaterializedSourcePage,
    RefOnlyOutcome,
    SourceConsumptionReceipt,
    SourceDeliveryRef,
    SourceIntakeSnapshot,
    SourceSubscription,
    SubscriptionAckRequest,
    SubscriptionAnchor,
    SubscriptionCloseRequest,
    SubscriptionDecision,
    SubscriptionDispatchRequest,
    SubscriptionHandbackRequest,
    SubscriptionHeadRef,
    SubscriptionInputFacts,
    SubscriptionPrepareRequest,
    SubscriptionRebindRequest,
    SubscriptionRenewRequest,
    SubscriptionRunFacts,
    SubscriptionSaveRequest,
)
from mgar_v3.super_agent.source_subscription_rules import (
    classify,
    handback,
    match_anchor,
    matching,
    parse_intake,
    selector_identity,
    stable_target_identity,
    validate_ack,
)

NOW = datetime(2026, 11, 13, 4, tzinfo=UTC)


def changed(value, **updates):
    return type(value).model_validate({**value.model_dump(), **updates})


def example():
    scope = SourceScope(
        source_kind="github", source_instance_id=uuid4(),
        tenant_id="Synthetic-Tenant", installation_id="Synthetic-Installation",
    )
    resource = ResourceRef(
        project_id="orders-demo", scope=scope, repository_id=900,
        repository="example/mgar-test", kind="issue", resource_id="7",
    )
    target = TargetRef(
        ticket_id=uuid4(), run_id=uuid4(), thread_id=uuid4(), thread_revision=4,
        execution_epoch=2, mandate_id=uuid4(), mandate_sha256="a" * 64,
        project_id="orders-demo", repository=resource.repository, allowed=True,
    )
    source = CorrelationInputRef(
        input_id=uuid4(), source_instance_id=scope.source_instance_id,
        project_id=resource.project_id, version_fingerprint="b" * 64, state_sha256="c" * 64,
    )
    anchor = SubscriptionAnchor(
        source_instance_id=scope.source_instance_id, scope_sha256=scope.key,
        conversation_sha256="d" * 64, topic_sha256="e" * 64,
        resource_key=resource.resource_key, resource=resource, correlation_id=uuid4(),
        correlation_revision=1, selector_kind="native_topic", seed_input_id=source.input_id,
        seed_version_fingerprint=source.version_fingerprint, seed_entity_sha256="f" * 64,
        source_revision=1, generation=1, policy_id=uuid4(), policy_revision=1,
    )
    subscription = SourceSubscription(
        subscription_id=uuid4(), project_id=resource.project_id, revision=1,
        anchor=anchor, target=target, role="primary", state="active",
        authority_sha256="1" * 64, valid_from=NOW - timedelta(seconds=10),
        expires_at=NOW + timedelta(hours=1),
    )
    facts = SubscriptionInputFacts(
        input=source, unit_key="2" * 64, scope_sha256=anchor.scope_sha256,
        conversation_sha256=anchor.conversation_sha256, topic_sha256=anchor.topic_sha256,
        resource_keys=(resource.resource_key,), actor_kind="human", context_only=False,
        historical=False, is_current=True, receive_allowed=True, read_allowed=True,
        task_controller=True, follow_up_allowed=True, purpose="follow_up",
        expires_at=NOW + timedelta(seconds=30),
    )
    run = SubscriptionRunFacts(
        target=target, status="running", ticket_open=True, original_primary=True,
        authority_current=True, policy_mode="auto_investigate",
    )
    return SimpleNamespace(
        scope=scope, resource=resource, target=target, source=source,
        anchor=anchor, subscription=subscription, facts=facts, run=run,
    )


def request_examples():
    env = example()
    close = dict(expected_revision=1, snapshot_sha256="3" * 64, review_ref="owner-review")
    renew = {**close, "seed_input_id": env.source.input_id, "ttl_seconds": 300}
    return (
        SubscriptionSaveRequest(
            correlation_id=env.anchor.correlation_id, expected_correlation_revision=1,
            seed_input_id=env.source.input_id, resource_key=env.resource.resource_key,
            target=env.target, role="primary", selector_kind="native_topic",
            snapshot_sha256="3" * 64, review_ref="owner-review",
        ),
        SubscriptionRenewRequest(**renew),
        SubscriptionRebindRequest(**renew, target=env.target),
        SubscriptionCloseRequest(**close),
        SubscriptionHandbackRequest(**close, input_ids=(env.source.input_id,)),
        SubscriptionDispatchRequest(input_ids=(env.source.input_id,)),
        SubscriptionPrepareRequest(epoch=2, expected_cursor=0, lane="primary"),
        SubscriptionAckRequest(
            epoch=2, expected_cursor=0, lane="primary", snapshot_id=uuid4(),
            snapshot_sha256="3" * 64,
        ),
    )


def page():
    env = example()
    first = SourceDeliveryRef(
        delivery_id=uuid4(), subscription_id=env.subscription.subscription_id,
        subscription_revision=1, project_id=env.source.project_id, run_id=env.target.run_id,
        sequence=11, input=env.source, unit_key=env.facts.unit_key, lane="primary",
        purpose="directive_proposal", disposition="ready", reason_code="SOURCE_AVAILABLE",
        expires_at=NOW + timedelta(seconds=30),
    )
    second = changed(
        first, delivery_id=uuid4(), sequence=13,
        input=changed(first.input, input_id=uuid4(), version_fingerprint="4" * 64),
    )
    head = SubscriptionHeadRef(
        subscription_id=first.subscription_id, revision=1, authority_sha256="1" * 64,
    )
    snapshot = SourceIntakeSnapshot(
        snapshot_id=uuid4(), project_id=first.project_id, run_id=first.run_id,
        epoch=2, lane="primary", from_cursor=5, through_cursor=13,
        items=(first, second), subscription_heads=(head,), consumer_state_sha256="5" * 64,
        expires_at=NOW + timedelta(seconds=30),
    )
    request = SubscriptionAckRequest(
        epoch=2, expected_cursor=5, lane="primary", snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.snapshot_sha256,
    )
    return SimpleNamespace(env=env, first=first, second=second, head=head,
                           snapshot=snapshot, request=request)


def acknowledge(env, **updates):
    values = dict(
        snapshot=env.snapshot, request=env.request, run_id=env.snapshot.run_id,
        epoch=2, cursor=5, subscription_heads=(env.head,),
        inputs=tuple(item.input for item in env.snapshot.items),
        consumer_state_sha256="5" * 64, at=NOW,
    )
    values.update(updates)
    return validate_ack(**values)


@pytest.mark.parametrize("position", range(8))
def test_public_requests_round_trip_through_strict_json(position):
    request = request_examples()[position]
    assert type(request).model_validate_json(request.model_dump_json()) == request


@pytest.mark.parametrize("position", range(8))
@pytest.mark.parametrize("field", [
    "body", "actor", "principal", "factory", "paid", "execution_mode",
])
def test_reference_requests_reject_plaintext_and_authority_assertions(position, field):
    request = request_examples()[position]
    value = {**request.model_dump(mode="json"), field: "untrusted-source-assertion"}
    with pytest.raises(ValidationError):
        type(request).model_validate_json(json.dumps(value))


@pytest.mark.parametrize("ttl", [0, -1, MAX_TTL_SECONDS + 1, True, "300", 300.0])
def test_subscription_lifetime_requires_bounded_exact_integer(ttl):
    with pytest.raises(ValidationError):
        changed(request_examples()[0], ttl_seconds=ttl)


@pytest.mark.parametrize("ttl", [1, MAX_TTL_SECONDS])
def test_lifetime_boundary_is_explicit(ttl):
    assert changed(request_examples()[0], ttl_seconds=ttl).ttl_seconds == ttl


@pytest.mark.parametrize("field,value", [
    ("epoch", True), ("epoch", -1), ("epoch", "2"), ("expected_cursor", -1),
    ("expected_cursor", False), ("limit", 0), ("limit", 17), ("limit", True),
    ("max_bytes", 0), ("max_bytes", MAX_PAGE_BYTES + 1), ("max_bytes", "1024"),
])
def test_intake_bounds_do_not_coerce_counters(field, value):
    with pytest.raises(ValidationError):
        changed(request_examples()[6], **{field: value})


@pytest.mark.parametrize("position", [4, 5])
@pytest.mark.parametrize("count", [0, 17])
def test_dispatch_and_handback_have_bounded_canonical_membership(position, count):
    with pytest.raises(ValidationError):
        changed(request_examples()[position], input_ids=tuple(uuid4() for _ in range(count)))


@pytest.mark.parametrize("position", [4, 5])
def test_dispatch_and_handback_do_not_repeat_canonical_input(position):
    request = request_examples()[position]
    with pytest.raises(ValidationError):
        changed(request, input_ids=(request.input_ids[0], request.input_ids[0]))


@pytest.mark.parametrize("field,value", [
    ("eligible_for_execution", True), ("eligible_for_execution", 0),
    ("calls_model", True), ("calls_model", "false"), ("external_writes", 1),
    ("external_writes", False), ("external_writes", 0.0),
])
def test_reference_outcomes_cannot_promote_execution(field, value):
    with pytest.raises(ValidationError):
        RefOnlyOutcome.model_validate({field: value})


@pytest.mark.parametrize("field", ["source", "scope", "conversation", "project", "resource"])
def test_same_title_or_number_in_another_scope_never_matches(field):
    env = example()
    facts = env.facts
    if field == "source":
        facts = changed(facts, input=changed(facts.input, source_instance_id=uuid4()))
    elif field == "scope":
        facts = changed(facts, scope_sha256="6" * 64)
    elif field == "conversation":
        facts = changed(facts, conversation_sha256="6" * 64)
    elif field == "project":
        facts = changed(facts, input=changed(facts.input, project_id="another-project"))
    else:
        facts = changed(facts, resource_keys=("6" * 64,))
    outcome = matching((env.subscription,), facts, at=NOW)
    assert outcome.subscription_ids == () and not outcome.ambiguous


def test_resource_selector_needs_resource_proof_even_in_same_topic():
    env = example()
    anchor = changed(env.anchor, selector_kind="resource")
    assert not match_anchor(anchor, changed(env.facts, resource_keys=()))
    assert match_anchor(anchor, env.facts)


def test_native_topic_selector_never_falls_back_to_entire_conversation():
    env = example()
    assert not match_anchor(env.anchor, changed(env.facts, topic_sha256=None, resource_keys=()))
    with pytest.raises(ValidationError):
        changed(env.anchor, topic_sha256=None)


def test_reply_chain_requires_the_selected_parent_not_any_message_in_chat():
    env = example()
    anchor = changed(env.anchor, selector_kind="reply_chain", topic_sha256=None)
    assert not match_anchor(anchor, changed(env.facts, resource_keys=()))
    assert match_anchor(anchor, changed(
        env.facts, resource_keys=(), reply_entity_sha256=anchor.seed_entity_sha256,
    ))
    assert not match_anchor(anchor, changed(
        env.facts, resource_keys=(), reply_entity_sha256="7" * 64,
    ))


def test_two_problems_in_one_topic_are_ambiguous_in_both_input_orders():
    env = example()
    other_resource = changed(env.resource, resource_id="8")
    other = changed(
        env.subscription, subscription_id=uuid4(),
        target=changed(env.target, ticket_id=uuid4()),
        anchor=changed(env.anchor, correlation_id=uuid4(), resource=other_resource,
                       resource_key=other_resource.resource_key),
    )
    facts = changed(env.facts, resource_keys=())
    for subscriptions in [(env.subscription, other), (other, env.subscription)]:
        outcome = matching(subscriptions, facts, at=NOW)
        assert outcome.ambiguous and outcome.subscription_ids == ()
        assert outcome.reason_code == "SOURCE_SUBSCRIPTION_AMBIGUOUS"
    exact = matching((other, env.subscription), env.facts, at=NOW)
    assert not exact.ambiguous
    assert exact.subscription_ids == (env.subscription.subscription_id,)


def test_same_task_can_have_multiple_readonly_receivers_without_first_winner():
    env = example()
    observer = changed(env.subscription, subscription_id=uuid4(), role="observer")
    result = matching((observer, env.subscription), env.facts, at=NOW)
    assert not result.ambiguous
    assert set(result.subscription_ids) == {
        observer.subscription_id, env.subscription.subscription_id,
    }


def test_repeated_selector_identity_is_rejected_instead_of_double_delivery():
    env = example()
    with pytest.raises(DomainError) as error:
        matching((env.subscription, env.subscription), env.facts, at=NOW)
    assert error.value.code == "SOURCE_SUBSCRIPTION_DUPLICATE_SELECTOR"


def test_renewed_seed_does_not_change_problem_identity_or_extend_body_readability():
    env = example()
    renewed = changed(
        env.anchor, seed_input_id=uuid4(), seed_version_fingerprint="8" * 64,
        seed_entity_sha256="9" * 64,
    )
    assert selector_identity(renewed) == selector_identity(env.anchor)
    reply = changed(env.anchor, selector_kind="reply_chain")
    assert selector_identity(changed(reply, seed_entity_sha256="9" * 64)) != (
        selector_identity(reply)
    )
    assert env.facts.expires_at < env.subscription.expires_at
    old = classify(env.subscription, env.facts, env.run, at=env.facts.expires_at)
    assert old.outcome == "blocked" and old.reason_code == "SOURCE_SUBSCRIPTION_INPUT_EXPIRED"
    fresh = changed(env.facts, expires_at=NOW + timedelta(minutes=5))
    assert classify(env.subscription, fresh, env.run, at=NOW).outcome == "deliver_ref"


@pytest.mark.parametrize("changes", [
    {"actor_kind": "bot"}, {"actor_kind": "service"}, {"actor_kind": "unknown"},
    {"context_only": True}, {"historical": True}, {"task_controller": False},
    {"follow_up_allowed": False}, {"purpose": "context"}, {"purpose": "status_query"},
])
def test_readonly_or_untrusted_source_never_becomes_primary_directive(changes):
    env = example()
    decision = classify(env.subscription, changed(env.facts, **changes), env.run, at=NOW)
    assert decision.outcome == "deliver_ref"
    assert decision.lane == "context_only" and decision.purpose == "context"
    assert not decision.eligible_for_execution and not decision.calls_model
    assert decision.external_writes == 0


@pytest.mark.parametrize("purpose", ["follow_up", "control", "new_work"])
def test_current_human_controller_only_proposes_input_without_granting_execution(purpose):
    env = example()
    decision = classify(env.subscription, changed(env.facts, purpose=purpose), env.run, at=NOW)
    assert (decision.outcome, decision.lane, decision.purpose) == (
        "deliver_ref", "primary", "directive_proposal",
    )
    assert decision.model_dump()["eligible_for_execution"] is False
    assert decision.model_dump()["calls_model"] is False


def test_observer_remains_outside_worker_lanes_even_when_actor_is_controller():
    env = example()
    observer = changed(env.subscription, role="observer")
    decision = classify(observer, env.facts, changed(env.run, original_primary=False), at=NOW)
    assert (decision.outcome, decision.lane, decision.purpose) == (
        "observe_ref", "observer", "observer",
    )


@pytest.mark.parametrize("role", ["primary", "context_only"])
def test_worker_delivery_requires_current_original_primary(role):
    env = example()
    result = classify(changed(env.subscription, role=role), env.facts,
                      changed(env.run, original_primary=False), at=NOW)
    assert result.outcome == "blocked" and result.reason_code == "SOURCE_SUBSCRIPTION_PRIMARY_LOST"


def test_shadow_and_disabled_never_upgrade_into_real_directives():
    env = example()
    shadow = classify(env.subscription, env.facts, changed(env.run, policy_mode="shadow"), at=NOW)
    assert shadow.outcome == "would_deliver" and shadow.purpose != "directive_proposal"
    disabled = classify(env.subscription, env.facts,
                        changed(env.run, policy_mode="disabled"), at=NOW)
    assert disabled.outcome == "blocked"
    assert not shadow.eligible_for_execution and not disabled.eligible_for_execution


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_finished_tasks_only_report_followup_and_never_reopen(status):
    env = example()
    run = changed(env.run, status=status, ticket_open=False)
    result = classify(env.subscription, changed(env.facts, purpose="status_query"), run, at=NOW)
    assert (result.outcome, result.lane, result.purpose) == (
        "closed_followup", "context_only", "status_query",
    )
    assert result.external_writes == 0 and not result.eligible_for_execution


def test_same_run_new_epoch_and_thread_revision_need_fresh_intake_not_new_subscription():
    env = example()
    target = changed(env.target, execution_epoch=3, thread_revision=5)
    assert stable_target_identity(target) == stable_target_identity(env.target)
    result = classify(env.subscription, env.facts, changed(env.run, target=target), at=NOW)
    assert result.outcome == "deliver_ref"


def test_different_run_needs_explicit_rebind_even_for_identical_task_and_mandate():
    env = example()
    result = classify(env.subscription, env.facts,
                      changed(env.run, target=changed(env.target, run_id=uuid4())), at=NOW)
    assert result.outcome == "needs_explicit_rebind"
    assert result.reason_code == "SOURCE_SUBSCRIPTION_REBIND_REQUIRED"


@pytest.mark.parametrize("field,value", [
    ("mandate_id", "uuid"), ("mandate_sha256", "9" * 64), ("ticket_id", "uuid"),
    ("thread_id", "uuid"), ("repository", "example/another-repository"),
    ("project_id", "another-project"), ("allowed", False),
])
def test_old_target_ref_cannot_grant_current_target_authority(field, value):
    env = example()
    target = changed(env.target, **{field: uuid4() if value == "uuid" else value})
    result = classify(env.subscription, env.facts, changed(env.run, target=target), at=NOW)
    assert result.outcome == "blocked" and result.reason_code == "SOURCE_SUBSCRIPTION_TARGET_STALE"


@pytest.mark.parametrize("field", ["receive_allowed", "read_allowed", "is_current"])
def test_rejected_source_facts_do_not_become_readable_context(field):
    env = example()
    result = classify(env.subscription, changed(env.facts, **{field: False}), env.run, at=NOW)
    assert result.outcome == "blocked"


@pytest.mark.parametrize("at", [NOW - timedelta(seconds=11), NOW + timedelta(hours=1)])
def test_lease_interval_has_no_early_or_expiry_boundary_gap(at):
    env = example()
    result = classify(env.subscription, env.facts, env.run, at=at)
    assert result.outcome == "blocked" and result.reason_code == "SOURCE_SUBSCRIPTION_EXPIRED"


def test_stale_signed_authority_is_checked_even_for_current_readable_input():
    env = example()
    decision = classify(env.subscription, env.facts,
                        changed(env.run, authority_current=False), at=NOW)
    assert decision.outcome == "blocked"
    assert decision.reason_code == "SOURCE_SUBSCRIPTION_AUTHORITY_STALE"


def test_naive_time_is_not_assumed_to_be_the_signed_policy_timezone():
    env = example()
    with pytest.raises(DomainError) as error:
        classify(env.subscription, env.facts, env.run, at=NOW.replace(tzinfo=None))
    assert error.value.code == "SOURCE_SUBSCRIPTION_TIME_INVALID"
    assert error.value.status == 422


@pytest.mark.parametrize("lane,purpose", [
    ("context_only", "directive_proposal"), ("observer", "directive_proposal"),
    ("observer", "context"), ("primary", "observer"),
])
def test_decisions_and_deliveries_keep_lane_purpose_boundaries(lane, purpose):
    env = example()
    with pytest.raises(ValidationError):
        SubscriptionDecision(
            subscription_id=env.subscription.subscription_id, subscription_revision=1,
            input_id=env.source.input_id, unit_key=env.facts.unit_key,
            outcome="deliver_ref", lane=lane, purpose=purpose, reason_code="SOURCE_TEST",
        )
    with pytest.raises(ValidationError):
        changed(page().first, lane=lane, purpose=purpose)


def test_handback_has_exact_canonical_and_bounded_cycle_semantics():
    facts = HandbackFacts(
        input_id=uuid4(), version_fingerprint="a" * 64, problem_key="b" * 64,
        window_key="c" * 64, already_handed_back=False, cycle_count=0,
    )
    assert handback(facts).allowed
    repeated = handback(changed(facts, already_handed_back=True))
    assert not repeated.allowed and repeated.reason_code == "INPUT_ALREADY_HANDED_BACK"
    limited = handback(changed(facts, cycle_count=MAX_HANDBACK_CYCLES))
    assert not limited.allowed and limited.reason_code == "HANDBACK_CYCLE_LIMIT"
    assert limited.semantic_evaluation == "NOT_RUN" and not limited.calls_model


def test_exact_source_page_roundtrip_and_acknowledgement_leave_legacy_state_outside_contract():
    env = page()
    parsed = parse_intake(env.snapshot.model_dump_json())
    assert parsed == env.snapshot
    assert parsed.schema_version == "source-intake-v1"
    assert acknowledge(env) is None
    assert "question" not in parsed.model_dump()
    assert "mailbox_cursor" not in parsed.model_dump()
    assert "input_revision" not in parsed.model_dump()


@pytest.mark.parametrize("change,expected", [
    ("run", "SOURCE_SUBSCRIPTION_RUN_FENCED"),
    ("epoch", "SOURCE_SUBSCRIPTION_RUN_FENCED"),
    ("request_epoch", "SOURCE_SUBSCRIPTION_RUN_FENCED"),
    ("lane", "SOURCE_SUBSCRIPTION_RUN_FENCED"),
    ("snapshot_id", "SOURCE_SUBSCRIPTION_SNAPSHOT_INVALID"),
    ("snapshot_sha", "SOURCE_SUBSCRIPTION_SNAPSHOT_INVALID"),
    ("consumer", "SOURCE_SUBSCRIPTION_CONSUMER_STALE"),
    ("head_revision", "SOURCE_SUBSCRIPTION_HEAD_STALE"),
    ("head_authority", "SOURCE_SUBSCRIPTION_HEAD_STALE"),
    ("head_duplicate", "SOURCE_SUBSCRIPTION_HEAD_STALE"),
    ("input_version", "SOURCE_SUBSCRIPTION_INPUT_STALE"),
    ("input_authority", "SOURCE_SUBSCRIPTION_INPUT_STALE"),
    ("input_duplicate", "SOURCE_SUBSCRIPTION_INPUT_STALE"),
    ("cursor", "SOURCE_SUBSCRIPTION_CURSOR_STALE"),
    ("expected_cursor", "SOURCE_SUBSCRIPTION_CURSOR_STALE"),
    ("expired", "SOURCE_SUBSCRIPTION_SNAPSHOT_EXPIRED"),
])
def test_prepared_page_must_recheck_exact_current_consumer_and_source_authority(change, expected):
    env = page()
    updates = {}
    if change == "run":
        updates["run_id"] = uuid4()
    elif change == "epoch":
        updates["epoch"] = 3
    elif change == "request_epoch":
        updates["request"] = changed(env.request, epoch=3)
    elif change == "lane":
        updates["request"] = changed(env.request, lane="context_only")
    elif change == "snapshot_id":
        updates["request"] = changed(env.request, snapshot_id=uuid4())
    elif change == "snapshot_sha":
        updates["request"] = changed(env.request, snapshot_sha256="6" * 64)
    elif change == "consumer":
        updates["consumer_state_sha256"] = "6" * 64
    elif change == "head_revision":
        updates["subscription_heads"] = (changed(env.head, revision=2),)
    elif change == "head_authority":
        updates["subscription_heads"] = (changed(env.head, authority_sha256="6" * 64),)
    elif change == "head_duplicate":
        updates["subscription_heads"] = (env.head, env.head)
    elif change in {"input_version", "input_authority"}:
        field = "version_fingerprint" if change == "input_version" else "state_sha256"
        updates["inputs"] = (
            changed(env.first.input, **{field: "6" * 64}), env.second.input,
        )
    elif change == "input_duplicate":
        updates["inputs"] = (env.first.input, env.second.input, env.first.input)
    elif change == "cursor":
        updates["cursor"] = 13
    elif change == "expected_cursor":
        updates["request"] = changed(env.request, expected_cursor=13)
    else:
        updates["at"] = env.snapshot.expires_at
    with pytest.raises(DomainError) as error:
        acknowledge(env, **updates)
    assert error.value.code == expected


def test_existing_receipt_does_not_allow_an_old_worker_or_revoked_page():
    env = page()
    assert acknowledge(env, already_acknowledged=True, cursor=13) is None
    with pytest.raises(DomainError) as error:
        acknowledge(env, already_acknowledged=True, cursor=13, epoch=3)
    assert error.value.code == "SOURCE_SUBSCRIPTION_RUN_FENCED"
    with pytest.raises(DomainError) as error:
        acknowledge(env, already_acknowledged=True, cursor=13, inputs=(
            changed(env.first.input, state_sha256="6" * 64), env.second.input,
        ))
    assert error.value.code == "SOURCE_SUBSCRIPTION_INPUT_STALE"


def test_fresh_blocked_metadata_page_can_drain_without_reviving_expired_source_text():
    env = page()
    items = tuple(changed(item, disposition="blocked", expires_at=NOW - timedelta(seconds=1),
                          reason_code="SOURCE_CONTENT_EXPIRED") for item in env.snapshot.items)
    snapshot = changed(env.snapshot, items=items)
    request = changed(env.request, snapshot_sha256=snapshot.snapshot_sha256)
    assert acknowledge(env, snapshot=snapshot, request=request) is None
    assert all(item.disposition == "blocked" and item.expires_at < NOW for item in snapshot.items)


@pytest.mark.parametrize("change", [
    "order", "duplicate_sequence", "duplicate_delivery", "duplicate_input", "from_cursor",
    "through_cursor", "wrong_lane", "wrong_run", "head_missing", "head_extra",
    "head_duplicate", "head_revision",
])
def test_snapshot_membership_is_exact_and_not_an_arbitrary_cursor_jump(change):
    env = page()
    value = env.snapshot.model_dump(mode="json")
    if change == "order":
        value["items"].reverse()
    elif change == "duplicate_sequence":
        value["items"][1]["sequence"] = 11
    elif change == "duplicate_delivery":
        value["items"][1]["delivery_id"] = value["items"][0]["delivery_id"]
    elif change == "duplicate_input":
        value["items"][1]["input"] = value["items"][0]["input"]
    elif change == "from_cursor":
        value["from_cursor"] = 11
    elif change == "through_cursor":
        value["through_cursor"] = 14
    elif change == "wrong_lane":
        value["lane"] = "context_only"
    elif change == "wrong_run":
        value["run_id"] = str(uuid4())
    elif change == "head_missing":
        value["subscription_heads"] = []
    elif change == "head_extra":
        value["subscription_heads"].append({
            **value["subscription_heads"][0], "subscription_id": str(uuid4()),
        })
    elif change == "head_duplicate":
        value["subscription_heads"].append(value["subscription_heads"][0])
    else:
        value["subscription_heads"][0]["revision"] = 2
    with pytest.raises(ValidationError):
        SourceIntakeSnapshot.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("raw", [
    "{}", "[]", b"\xff", '{"a":NaN}', '{"a":1,"a":2}',
    '{"a":Infinity}', '{"a":{"b":1,"b":2}}',
])
def test_invalid_json_never_echoes_or_interprets_external_authority(raw):
    with pytest.raises(DomainError) as error:
        parse_intake(raw)
    assert error.value.code == "SOURCE_SUBSCRIPTION_CONTRACT_INVALID"
    assert error.value.status == 422


def test_snapshot_parser_rejects_oversize_duplicate_fields_and_raw_body_assertions():
    env = page()
    for raw in [
        env.snapshot.model_dump_json() + " " * MAX_PAGE_BYTES,
        env.snapshot.model_dump_json()[:-1] + ',"schema_version":"source-intake-v1"}',
        json.dumps({**env.snapshot.model_dump(mode="json"), "body": "SA11-private-raw-body"}),
    ]:
        with pytest.raises(DomainError) as error:
            parse_intake(raw)
        assert error.value.code == "SOURCE_SUBSCRIPTION_CONTRACT_INVALID"
        assert "SA11-private-raw-body" not in str(error.value)


def test_consumption_receipt_does_not_claim_tool_execution_or_instruction_application():
    env = page()
    receipt = SourceConsumptionReceipt(
        receipt_id=uuid4(), snapshot_id=env.snapshot.snapshot_id,
        snapshot_sha256=env.snapshot.snapshot_sha256, run_id=env.snapshot.run_id,
        epoch=2, lane="primary", from_cursor=5, through_cursor=13,
        delivery_ids=(env.first.delivery_id, env.second.delivery_id),
    )
    assert receipt.status == "acknowledged_refs"
    assert not receipt.eligible_for_execution and not receipt.calls_model
    with pytest.raises(ValidationError):
        changed(receipt, status="instructions_applied")
    with pytest.raises(ValidationError):
        changed(receipt, delivery_ids=(env.first.delivery_id, env.first.delivery_id))


def test_ephemeral_materialized_text_is_not_part_of_repr_or_durable_snapshot():
    env = page()
    marker = "SA11-EPHEMERAL-PRIVATE-CONTENT"
    item = MaterializedSourceItem(delivery_id=env.first.delivery_id,
                                  input_id=env.first.input.input_id, text=marker)
    materialized = MaterializedSourcePage(snapshot=env.snapshot, items=(item,), materialized_at=NOW)
    assert marker not in repr(item) and marker not in repr(materialized)
    assert marker not in materialized.snapshot.model_dump_json()
