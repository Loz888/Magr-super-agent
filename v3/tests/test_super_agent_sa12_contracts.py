"""SA12 metadata-only contracts and current-fact scheduling safety boundaries.

These synthetic facts do not establish PG/Provider or process integration.
The owning Engine must obtain and recheck each fact in its original transaction.
"""

import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef
from mgar_v3.super_agent.source_scheduler_domain import (
    AssignmentFacts,
    PendingWork,
    QueueCandidate,
    QuiescenceFacts,
    ResourceRequest,
    ResourceUsage,
    RunFacts,
    SchedulerAssignmentRequest,
    SchedulerClaimRequest,
    SchedulerConfigFacts,
    SchedulerConfiguration,
    SchedulerConfigureRequest,
    SchedulerDrainRequest,
    SchedulerEnqueueRequest,
    SchedulerHeartbeatRequest,
    SchedulerLimits,
    SchedulerOperationRequest,
    SchedulerOwnerDrainRequest,
    SchedulerQueueCancelRequest,
    SchedulerReadyRequest,
    SchedulerReconcileRequest,
    SchedulerReleaseRequest,
    SchedulerReportRequest,
    SchedulerSessionRegisterRequest,
    SchedulerSessionRevokeRequest,
    ServiceBucket,
    ServiceHistory,
    SessionFacts,
    SourceAuthorityFacts,
    SourceOccupancy,
)
from mgar_v3.super_agent.source_scheduler_rules import (
    choose_candidate,
    queue_quota_key,
    resource_fit,
    slot_quota_key,
    validate_lease,
    validate_release,
)

NOW = datetime(2026, 11, 13, 7, tzinfo=UTC)
PROJECT = "orders-demo"
HASH = "a" * 64


def changed(value, **updates):
    return type(value).model_validate({**value.model_dump(), **updates})


def limits(**updates):
    return SchedulerLimits.model_validate({
        "slots": 2, "preparing": 2, "cpu_millis": 4000, "memory_mib": 4096,
        "queue": 256, "source_slots": 2, "lease_seconds": 60,
        "session_ttl_seconds": 3600, **updates,
    })


def resources(**updates):
    return ResourceRequest.model_validate({"cpu_millis": 1000, "memory_mib": 512, **updates})


def config(**updates):
    return SchedulerConfiguration.model_validate({
        "enabled": True, "limits": limits(), "valid_from": NOW - timedelta(hours=1),
        "valid_until": NOW + timedelta(hours=1), **updates,
    })


def fence():
    session = SessionFacts(
        session_id=uuid4(), project_id=PROJECT, revision=1, profile_sha256=HASH,
        state="registered", registered_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    assignment = AssignmentFacts(
        assignment_id=uuid4(), queue_id=uuid4(), run_id=uuid4(), ticket_id=uuid4(),
        session_id=session.session_id, workspace_id=uuid4(), project_id=PROJECT,
        epoch=3, config_revision=2, revision=4, state="active",
        lease_until=NOW + timedelta(seconds=30), resources=resources(),
        prepare_until=NOW + timedelta(seconds=30),
    )
    run = RunFacts(
        run_id=assignment.run_id, ticket_id=assignment.ticket_id, project_id=PROJECT,
        epoch=3, status="running", ticket_open=True, target_current=True, mandate_current=True,
    )
    source = SourceAuthorityFacts(
        current=True, identity_current=True, receive_allowed=True, read_allowed=True,
        execution_allowed=True, origin_current=True, expires_at=NOW + timedelta(minutes=1),
    )
    request = SchedulerAssignmentRequest(
        assignment_id=assignment.assignment_id, session_id=session.session_id,
        epoch=3, expected_revision=4,
    )
    configuration = SchedulerConfigFacts(
        project_id=PROJECT, revision=2, configuration_sha256=HASH, configuration=config(),
    )
    return configuration, session, assignment, run, source, request


def candidate(**updates):
    return QueueCandidate.model_validate({
        "queue_id": uuid4(), "run_id": uuid4(), "project_id": PROJECT,
        "fairness_key": HASH, "classification": "human",
        "enqueued_at": NOW - timedelta(minutes=1), "resources": resources(),
        "generation": 1, "eligible": True, **updates,
    })


def choose(candidates, history=None, **updates):
    return choose_candidate(
        tuple(candidates), history or ServiceHistory(), project_id=PROJECT,
        limits=updates.get("limits", limits()), usage=updates.get("usage", ResourceUsage()),
        at=updates.get("at", NOW),
    )


def evidence(assignment, **updates):
    return QuiescenceFacts.model_validate({
        "assignment_id": assignment.assignment_id, "session_id": assignment.session_id,
        "run_id": assignment.run_id, "workspace_id": assignment.workspace_id,
        "epoch": assignment.epoch, "provider_receipt_sha256": HASH, "observed_at": NOW,
        "trusted": True, "processes_stopped": True, "workspace_fenced": True, **updates,
    })


@pytest.mark.parametrize("field,value", [
    ("cpu_millis", 99), ("cpu_millis", 2001), ("cpu_millis", True),
    ("cpu_millis", "100"), ("cpu_millis", 100.0),
    ("memory_mib", 63), ("memory_mib", 2049), ("memory_mib", False),
    ("memory_mib", "64"), ("memory_mib", 64.0),
])
def test_resource_bounds_reject_coercion_and_per_assignment_overcommit(field, value):
    with pytest.raises(ValidationError):
        ResourceRequest.model_validate({"cpu_millis": 100, "memory_mib": 64, field: value})


@pytest.mark.parametrize("updates", [
    {"slots": 3}, {"slots": True}, {"slots": "2"}, {"preparing": 3},
    {"slots": 1, "preparing": 2}, {"slots": 1, "preparing": 1, "source_slots": 2},
    {"cpu_millis": 4001}, {"memory_mib": 4097}, {"queue": 257}, {"queue": -1},
    {"source_slots": 3}, {"lease_seconds": 29}, {"lease_seconds": 121},
    {"preparation_seconds": 0}, {"preparation_seconds": 601},
    {"preparation_seconds": "120"}, {"preparation_seconds": True},
    {"session_ttl_seconds": 0}, {"session_ttl_seconds": 86401},
])
def test_signed_limit_hard_bounds_cannot_be_expanded_by_json(updates):
    with pytest.raises(ValidationError):
        limits(**updates)


def test_defaults_are_disabled_with_no_implicit_execution_capacity():
    value = SchedulerConfiguration(valid_from=NOW, valid_until=NOW + timedelta(hours=1))
    assert value.enabled is False
    assert value.limits.slots == value.limits.preparing == value.limits.cpu_millis == 0
    assert value.limits.memory_mib == value.limits.queue == value.limits.source_slots == 0
    assert value.limits.preparation_seconds == 120
    with pytest.raises(ValidationError):
        value.enabled = True


@pytest.mark.parametrize("updates", [
    {"enabled": 1}, {"enabled": "true"}, {"valid_until": NOW - timedelta(hours=1)},
    {"valid_until": NOW}, {"valid_from": NOW.replace(tzinfo=None)},
    {"valid_until": NOW.replace(tzinfo=None)},
])
def test_configuration_requires_explicit_aware_nonempty_window(updates):
    data = {"valid_from": NOW, "valid_until": NOW + timedelta(hours=1), **updates}
    with pytest.raises(ValidationError):
        SchedulerConfiguration.model_validate(data)


def test_json_roundtrip_keeps_explicit_integer_resources_and_utc_time():
    offset = timezone(timedelta(hours=8))
    value = config(valid_from=NOW.astimezone(offset))
    assert value.valid_from == NOW
    assert value.valid_from.utcoffset() == timedelta(0)
    serialized = value.model_dump_json()
    assert SchedulerConfiguration.model_validate_json(serialized) == value
    payload = json.loads(serialized)
    payload["limits"]["slots"] = "2"
    with pytest.raises(ValidationError):
        SchedulerConfiguration.model_validate_json(json.dumps(payload))


def enqueue():
    return SchedulerEnqueueRequest(
        request_id=uuid4(), run_id=uuid4(), expected_epoch=0, target_sha256=HASH,
        resources=resources(), review_ref="owner-review-1",
    )


@pytest.mark.parametrize("field", [
    "body", "question", "classification", "human", "actor_kind", "priority", "paid",
    "factory", "command", "argv", "path", "workspace_id", "create_run", "resume",
    "tool", "network", "task_outcome", "approved", "target", "bearer",
])
def test_owner_enqueue_cannot_smuggle_content_privilege_or_new_business_object(field):
    with pytest.raises(ValidationError):
        SchedulerEnqueueRequest.model_validate({**enqueue().model_dump(), field: "untrusted"})


def test_enqueue_source_reference_preserves_exact_canonical_version_without_body():
    origin = CorrelationInputRef(
        input_id=uuid4(), source_instance_id=uuid4(), project_id=PROJECT,
        version_fingerprint="b" * 64, state_sha256="c" * 64,
    )
    request = changed(enqueue(), source_input=origin)
    assert SchedulerEnqueueRequest.model_validate_json(request.model_dump_json()) == request
    altered = origin.model_dump()
    altered["body"] = "Do not treat this as a scheduler instruction"
    with pytest.raises(ValidationError):
        changed(request, source_input=altered)
    with pytest.raises(ValidationError):
        changed(request, target_sha256="A" * 64)


def worker_requests():
    request = fence()[-1]
    operation = {**request.model_dump(), "operation_id": uuid4(), "request_sha256": HASH}
    return [
        SchedulerClaimRequest(request_id=uuid4(), session_id=request.session_id,
                              expected_config_revision=1),
        request, SchedulerHeartbeatRequest(**request.model_dump()),
        SchedulerOperationRequest(**operation), SchedulerReadyRequest(**operation),
        SchedulerReleaseRequest(**operation),
        SchedulerReportRequest(**operation, status="finished"),
        SchedulerDrainRequest(**request.model_dump(), reason_code="WAITING_INPUT"),
    ]


@pytest.mark.parametrize("field", [
    "bearer", "session_token", "body", "command", "path", "priority", "resources",
    "target_sha256", "trusted", "quiescent", "task_outcome", "eligible_for_execution",
])
def test_worker_metadata_requests_accept_neither_secret_nor_self_authorization(field):
    for request in worker_requests():
        with pytest.raises(ValidationError):
            type(request).model_validate({**request.model_dump(), field: "forged"})


def test_all_control_requests_keep_cas_and_review_without_worker_secret_fields():
    requests = [
        SchedulerConfigureRequest(expected_revision=0, configuration=config(), review_ref="r1"),
        enqueue(), SchedulerSessionRegisterRequest(
            request_id=uuid4(), profile_sha256=HASH, ttl_seconds=3600, review_ref="r1",
        ),
        SchedulerSessionRevokeRequest(session_id=uuid4(), expected_revision=1, review_ref="r1"),
        SchedulerQueueCancelRequest(queue_id=uuid4(), expected_revision=1, review_ref="r1"),
        SchedulerOwnerDrainRequest(assignment_id=uuid4(), expected_revision=1,
                                   reason_code="OWNER_DRAIN", review_ref="r1"),
        SchedulerReconcileRequest(assignment_id=uuid4(), expected_revision=1,
                                  operation_id=uuid4(), request_sha256=HASH, review_ref="r1"),
        *worker_requests(),
    ]
    for request in requests:
        assert type(request).model_validate_json(request.model_dump_json()) == request
        assert not (
            {"bearer", "token", "session_token", "body"} & type(request).model_fields.keys()
        )
        if hasattr(request, "expected_revision"):
            with pytest.raises(ValidationError):
                changed(request, expected_revision=True)


@pytest.mark.parametrize("status", ["succeeded", "published", "approved", "running", True])
def test_worker_report_accepts_resource_suggestions_without_business_success_states(status):
    request = fence()[-1]
    with pytest.raises(ValidationError):
        SchedulerReportRequest.model_validate({
            **request.model_dump(), "operation_id": uuid4(), "request_sha256": HASH,
            "status": status,
        })


@pytest.mark.parametrize("updates", [
    {"expires_at": NOW - timedelta(minutes=1)},
    {"expires_at": NOW + timedelta(days=1)},
    {"expires_at": NOW.replace(tzinfo=None)},
])
def test_session_identity_has_bounded_nonrenewable_lifetime(updates):
    session = fence()[1]
    with pytest.raises(ValidationError):
        changed(session, **updates)


def test_current_exact_lease_and_explicit_owner_origin_pass_without_mutation():
    facts = fence()
    before = tuple(item.model_dump_json() for item in facts)
    validate_lease(*facts, at=NOW)
    owner = SourceAuthorityFacts(required=False)
    validate_lease(*facts[:4], owner, facts[-1], at=NOW)
    assert tuple(item.model_dump_json() for item in facts) == before


@pytest.mark.parametrize("position,updates", [
    (0, {"revision": 3}), (0, {"project_id": "other-project"}),
    (1, {"state": "revoked"}), (1, {"expires_at": NOW}),
    (1, {"session_id": UUID(int=1)}), (1, {"project_id": "other-project"}),
    (2, {"state": "draining"}), (2, {"state": "released"}),
    (2, {"lease_until": NOW}), (2, {"lease_until": NOW - timedelta(microseconds=1)}),
    (3, {"epoch": 4}), (3, {"run_id": UUID(int=2)}), (3, {"ticket_id": UUID(int=3)}),
    (3, {"project_id": "other-project"}), (3, {"ticket_open": False}),
    (3, {"target_current": False}), (3, {"mandate_current": False}),
    (5, {"expected_revision": 5}), (5, {"epoch": 2}),
    (5, {"assignment_id": UUID(int=4)}), (5, {"session_id": UUID(int=5)}),
])
def test_stale_lease_cas_epoch_owner_project_and_authority_never_resume(position, updates):
    facts = list(fence())
    facts[position] = changed(facts[position], **updates)
    with pytest.raises(DomainError):
        validate_lease(*facts, at=NOW)


@pytest.mark.parametrize("status", [
    "queued", "waiting_input", "waiting_approval", "paused", "succeeded", "failed", "cancelled",
])
def test_only_running_original_run_can_hold_execution_authority(status):
    configuration, session, assignment, run, source, request = fence()
    with pytest.raises(DomainError) as error:
        validate_lease(configuration, session, assignment, changed(run, status=status),
                       source, request, at=NOW)
    assert error.value.code == "SOURCE_SCHEDULER_RUN_FENCED"


@pytest.mark.parametrize("field", [
    "current", "identity_current", "receive_allowed", "read_allowed", "execution_allowed",
    "origin_current",
])
def test_each_current_source_check_is_required_even_after_claim_was_accepted(field):
    configuration, session, assignment, run, source, request = fence()
    with pytest.raises(DomainError) as error:
        validate_lease(configuration, session, assignment, run,
                       changed(source, **{field: False}), request, at=NOW)
    assert error.value.code == "SOURCE_SCHEDULER_SOURCE_FENCED"


@pytest.mark.parametrize("expiry", [None, NOW, NOW - timedelta(microseconds=1)])
def test_source_origin_expiry_blocks_old_lease_without_body_materialization(expiry):
    configuration, session, assignment, run, source, request = fence()
    with pytest.raises(DomainError):
        validate_lease(configuration, session, assignment, run,
                       changed(source, expires_at=expiry), request, at=NOW)


@pytest.mark.parametrize("updates", [
    {"enabled": False}, {"valid_until": NOW},
    {"valid_from": NOW + timedelta(microseconds=1)},
])
def test_committed_configuration_disable_or_validity_end_fences_a_previous_claim(updates):
    configuration, session, assignment, run, source, request = fence()
    configuration = changed(configuration, configuration=changed(config(), **updates))
    with pytest.raises(DomainError):
        validate_lease(configuration, session, assignment, run, source, request, at=NOW)


def test_preparing_can_heartbeat_only_through_explicit_gate_and_never_after_expiry():
    configuration, session, assignment, run, source, request = fence()
    assignment = changed(assignment, state="preparing")
    with pytest.raises(DomainError):
        validate_lease(configuration, session, assignment, run, source, request, at=NOW)
    validate_lease(configuration, session, assignment, run, source, request,
                   at=NOW, allow_preparing=True)
    with pytest.raises(DomainError):
        validate_lease(configuration, session, assignment, run, source, request,
                       at=assignment.lease_until, allow_preparing=True)


def test_preparation_deadline_is_not_extended_by_a_later_heartbeat_lease():
    configuration, session, assignment, run, source, request = fence()
    assignment = changed(assignment, state="preparing", prepare_until=NOW,
                         lease_until=NOW + timedelta(minutes=1))
    with pytest.raises(DomainError) as error:
        validate_lease(configuration, session, assignment, run, source, request,
                       at=NOW, allow_preparing=True)
    assert error.value.code == "SOURCE_SCHEDULER_PREPARATION_EXPIRED"
    validate_lease(configuration, session, changed(assignment, state="active"), run, source,
                   request, at=NOW)


def test_untrusted_default_source_facts_or_missing_preparation_deadline_fail_closed():
    configuration, session, assignment, run, source, request = fence()
    with pytest.raises(DomainError):
        validate_lease(configuration, session, assignment, run,
                       SourceAuthorityFacts(), request, at=NOW)
    with pytest.raises(DomainError):
        validate_lease(configuration, session,
                       changed(assignment, state="preparing", prepare_until=None),
                       run, source, request, at=NOW, allow_preparing=True)


@pytest.mark.parametrize("updates,limit_updates", [
    ({"slots": 2}, {}), ({"slots": 2, "preparing": 2}, {}),
    ({"slots": 1, "cpu_millis": 2000}, {"cpu_millis": 2500}),
    ({"slots": 1, "memory_mib": 2048}, {"memory_mib": 2500}),
    ({"slots": 2, "sources": (SourceOccupancy(
        project_id=PROJECT, fairness_key=HASH, slots=2,
    ),)}, {}),
])
def test_occupied_or_draining_capacity_never_becomes_available_just_by_selection(
    updates, limit_updates,
):
    usage = ResourceUsage.model_validate(updates)
    configured = limits(**limit_updates)
    assert not resource_fit(resources(), configured, usage, project_id=PROJECT, fairness_key=HASH)
    assert choose([candidate()], limits=configured, usage=usage) is None


def test_preparation_and_per_source_capacity_apply_independently():
    assert choose([candidate()], limits=limits(preparing=0)) is None
    usage = ResourceUsage(slots=1, sources=(SourceOccupancy(
        project_id=PROJECT, fairness_key=HASH, slots=1,
    ),))
    assert choose([candidate()], limits=limits(source_slots=1), usage=usage) is None
    other = candidate(fairness_key="b" * 64)
    assert choose([other], limits=limits(source_slots=1), usage=usage) == other


def test_draining_preparation_keeps_its_preparation_capacity_until_trusted_release():
    usage = ResourceUsage(slots=1, preparing=1, cpu_millis=1000, memory_mib=512)
    assert choose([candidate()], limits=limits(preparing=1), usage=usage) is None
    released_usage = ResourceUsage()
    assert choose([candidate()], limits=limits(preparing=1), usage=released_usage) is not None


def test_exact_resource_boundaries_fit_without_granting_extra_capacity():
    request = ResourceRequest(cpu_millis=2000, memory_mib=2048)
    usage = ResourceUsage(slots=1, preparing=1, cpu_millis=2000, memory_mib=2048)
    assert resource_fit(request, limits(), usage, project_id=PROJECT, fairness_key=HASH)
    assert not resource_fit(request, limits(cpu_millis=3999), usage,
                            project_id=PROJECT, fairness_key=HASH)


def test_ci_flood_cannot_take_two_claim_turns_while_eligible_human_waits():
    human = candidate(enqueued_at=NOW - timedelta(hours=1))
    automated = [candidate(classification="automation", fairness_key=f"{i:064x}")
                 for i in range(1, 129)]
    first = choose([*automated, human], ServiceHistory(last_class="human"))
    assert first is not None and first.classification == "automation"
    second = choose([*automated, human], ServiceHistory(last_class=first.classification))
    assert second == human


def test_human_flood_preserves_automation_opportunity_and_rebuilt_history():
    automated = candidate(classification="automation", fairness_key="b" * 64)
    humans = [candidate() for _ in range(128)]
    initial = choose([automated, *humans])
    assert initial is not None and initial.classification == "human"
    persisted = ServiceHistory(last_class="human").model_dump_json()
    rebuilt = ServiceHistory.model_validate_json(persisted)
    assert choose([automated, *humans], rebuilt) == automated


def test_fair_order_then_age_and_uuid_is_independent_of_process_input_order():
    recent_source = candidate(fairness_key="b" * 64,
                              enqueued_at=NOW - timedelta(minutes=30))
    underserved = candidate(fairness_key="c" * 64)
    history = ServiceHistory(buckets=(
        ServiceBucket(project_id=PROJECT, fairness_key="b" * 64,
                      project_sequence=9, source_sequence=8),
        ServiceBucket(project_id=PROJECT, fairness_key="c" * 64,
                      project_sequence=9, source_sequence=2),
    ))
    assert choose([recent_source, underserved], history) == underserved
    older = candidate(queue_id=UUID(int=2), enqueued_at=NOW - timedelta(hours=1))
    younger = candidate(queue_id=UUID(int=1))
    assert choose([younger, older]) == choose([older, younger]) == older
    same_age = changed(younger, enqueued_at=older.enqueued_at)
    assert choose([older, same_age]) == same_age


def test_ineligible_future_other_project_or_unfitting_human_does_not_block_usable_ci():
    automated = candidate(classification="automation")
    excluded = [candidate(eligible=False), candidate(project_id="other-project"),
                candidate(state="claimed"), candidate(state="closed"),
                candidate(enqueued_at=NOW + timedelta(microseconds=1)),
                candidate(resources=ResourceRequest(cpu_millis=2000, memory_mib=2048))]
    assert choose([*excluded, automated], limits=limits(memory_mib=1024)) == automated
    assert choose(excluded, limits=limits(memory_mib=1024)) is None


def test_ambiguous_service_history_and_duplicate_queue_ids_are_rejected():
    bucket = ServiceBucket(project_id=PROJECT, fairness_key=HASH)
    with pytest.raises(ValidationError):
        ServiceHistory(buckets=(bucket, bucket))
    queue = candidate()
    with pytest.raises(DomainError):
        choose([queue, changed(queue, resources=ResourceRequest(cpu_millis=100, memory_mib=64))])
    with pytest.raises(DomainError):
        choose([candidate() for _ in range(257)])


@pytest.mark.parametrize("field", [
    "requested_operations", "running_operations", "unknown_operations",
    "queued_read_jobs", "running_read_jobs",
])
def test_waiting_release_is_blocked_by_every_live_or_unknown_operation_and_job(field):
    assignment = changed(fence()[2], state="draining", fenced_at=NOW)
    with pytest.raises(DomainError) as error:
        validate_release(assignment, evidence(assignment),
                         PendingWork.model_validate({field: 1}), at=NOW)
    assert error.value.code == "SOURCE_SCHEDULER_WORK_PENDING"


@pytest.mark.parametrize("updates", [
    {"trusted": False}, {"processes_stopped": False}, {"workspace_fenced": False},
    {"assignment_id": UUID(int=1)}, {"session_id": UUID(int=2)},
    {"run_id": UUID(int=3)}, {"workspace_id": UUID(int=4)}, {"epoch": 2},
    {"observed_at": NOW - timedelta(microseconds=1)},
    {"observed_at": NOW + timedelta(microseconds=1)},
])
def test_worker_self_report_or_another_execution_receipt_cannot_release_slot(updates):
    assignment = changed(fence()[2], state="draining", fenced_at=NOW)
    with pytest.raises(DomainError):
        validate_release(assignment, evidence(assignment, **updates), PendingWork(), at=NOW)


@pytest.mark.parametrize("state", ["active", "preparing"])
def test_execution_must_be_drained_before_slot_release(state):
    assignment = changed(fence()[2], state=state)
    with pytest.raises(DomainError):
        validate_release(assignment, evidence(assignment), PendingWork(), at=NOW)


def test_expired_drained_assignment_can_be_cleaned_without_resurrecting_lease():
    facts = list(fence())
    facts[2] = changed(facts[2], state="draining", lease_until=NOW - timedelta(seconds=1),
                       fenced_at=NOW)
    validate_release(facts[2], evidence(facts[2]), PendingWork(), at=NOW)
    validate_release(changed(facts[2], state="released"), evidence(facts[2]), PendingWork(), at=NOW)
    with pytest.raises(DomainError):
        validate_lease(*facts, at=NOW, allow_preparing=True)


def test_quota_identity_retries_same_generation_but_never_reuses_released_generation():
    queue_id, assignment_id = uuid4(), uuid4()
    assert queue_quota_key(queue_id, 1) == queue_quota_key(queue_id, 1)
    assert queue_quota_key(queue_id, 1) != queue_quota_key(queue_id, 2)
    assert slot_quota_key(assignment_id) != slot_quota_key(uuid4())
    assert queue_quota_key(queue_id, 1) != slot_quota_key(assignment_id)


@pytest.mark.parametrize("generation", [0, -1, True, "1", 1.0, 9223372036854775808])
def test_invalid_queue_generation_never_produces_fresh_quota_identity(generation):
    with pytest.raises(DomainError):
        queue_quota_key(uuid4(), generation)


def test_all_pure_gates_require_explicit_current_time():
    invalid = NOW.replace(tzinfo=None)
    facts = fence()
    with pytest.raises(DomainError):
        validate_lease(*facts, at=invalid)
    assignment = changed(facts[2], state="draining", fenced_at=NOW)
    with pytest.raises(DomainError):
        validate_release(assignment, evidence(assignment), PendingWork(), at=invalid)
    with pytest.raises(DomainError):
        choose([candidate()], at=invalid)
