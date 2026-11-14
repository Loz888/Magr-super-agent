"""First private-contract checks; not PG/Job/Worker or full-card acceptance."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError, digest
from mgar_v3.super_agent.source_async_domain import (
    AsyncAdmitRequest,
    AsyncCheckpoint,
    AsyncCurrentFacts,
    AsyncInputFacts,
    AsyncInputReceipt,
    AsyncJob,
    AsyncJobCancelRequest,
    AsyncRestriction,
    AsyncRestrictRequest,
    AsyncScope,
    AsyncWorkerJobCancelRequest,
    AsyncWorkerRequest,
)
from mgar_v3.super_agent.source_async_plan import PendingInput, plan_safe_point
from mgar_v3.super_agent.source_async_rules import (
    classify_input,
    validate_job_transition,
    validate_mutation,
    validate_view,
)
from mgar_v3.super_agent.source_coding_domain import CodingTarget
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef

AT = datetime(2026, 11, 14, tzinfo=UTC)
HASH = digest("SA14 explicit synthetic contract fixture")


@pytest.fixture
def scope():
    return AsyncScope(
        coding_id=uuid4(),
        coding_revision=1,
        target=CodingTarget(
            project_id="sa14-controlled",
            thread_id=uuid4(),
            ticket_id=uuid4(),
            run_id=uuid4(),
            mandate_id=uuid4(),
        ),
        assignment_id=uuid4(),
        session_id=uuid4(),
        workspace_id=uuid4(),
        epoch=1,
        mandate_sha256=HASH,
        profile_sha256=HASH,
        initial_context_sha256=HASH,
    )


@pytest.fixture
def pair(scope):
    async_id = uuid4()
    restriction = AsyncRestriction(deny_write_paths=("src/db",), deny_commands=("psql",))
    view = AsyncCheckpoint(
        checkpoint_id=uuid4(),
        async_id=async_id,
        scope=scope,
        stage="after_model",
        input_revision=3,
        through_sequence=5,
        restriction=restriction,
        context_sha256=HASH,
        tree_sha256=HASH,
        environment_sha256=HASH,
        created_at=AT - timedelta(seconds=1),
        expires_at=AT + timedelta(minutes=1),
    )
    current = AsyncCurrentFacts(
        async_id=async_id,
        scope=scope,
        revision=4,
        input_revision=3,
        restriction=restriction,
        tree_sha256=HASH,
        environment_sha256=HASH,
        expires_at=AT + timedelta(minutes=2),
        original_coding_current=True,
        assignment_current=True,
        session_current=True,
        sources_current=True,
        budget_current=True,
        active=True,
        pending_invalidation=False,
    )
    return view, current


def source_facts(**changes):
    values = dict(
        kind="context",
        source="source",
        source_input=CorrelationInputRef(
            input_id=uuid4(),
            source_instance_id=uuid4(),
            project_id="sa14-controlled",
            version_fingerprint=HASH,
            state_sha256=HASH,
        ),
        expires_at=AT + timedelta(minutes=1),
        receive_current=True,
        identity_current=True,
        policy_current=True,
        canonical_current=True,
        exact_target=True,
        read_allowed=True,
    )
    values.update(changes)
    return AsyncInputFacts(**values)


def test_context_permission_never_becomes_control_authority():
    context = source_facts()
    assert classify_input(context, at=AT) == "context"
    command = context.model_copy(
        update={
            "kind": "restriction",
            "restriction": AsyncRestriction(deny_write_paths=("src/db",)),
        }
    )
    assert classify_input(command, at=AT) == "blocked"


def test_source_control_requires_current_human_exact_ticket_membership():
    control = source_facts(
        kind="restriction",
        context_only=False,
        human=True,
        task_controller=True,
        ticket_membership=True,
        restriction=AsyncRestriction(deny_write_paths=("src/db",)),
    )
    assert classify_input(control, at=AT) == "restriction"
    for field in ("human", "task_controller", "ticket_membership", "exact_target"):
        assert classify_input(control.model_copy(update={field: False}), at=AT) == "blocked"
    assert classify_input(control.model_copy(update={"context_only": True}), at=AT) == "blocked"


@pytest.mark.parametrize(
    "field",
    [
        "receive_current",
        "identity_current",
        "policy_current",
        "canonical_current",
        "read_allowed",
    ],
)
def test_a_prepared_context_is_unreadable_after_any_current_permission_loss(field):
    facts = source_facts()
    assert classify_input(facts.model_copy(update={field: False}), at=AT) == "blocked"


def test_context_expiry_and_missing_canonical_reference_are_not_instructions():
    assert classify_input(source_facts(expires_at=AT), at=AT) == "blocked"
    assert classify_input(source_facts(source_input=None), at=AT) == "blocked"


@pytest.mark.parametrize("origin,kind", [("system", "invalidation"), ("job", "job_progress")])
def test_observation_is_data_and_requires_original_trusted_journal(origin, kind):
    value = source_facts(source=origin, kind=kind, source_input=None)
    assert classify_input(value, at=AT) == "blocked"
    assert classify_input(value.model_copy(update={"trusted_observation": True}), at=AT) == kind
    forged = value.model_copy(
        update={
            "kind": "restriction",
            "trusted_observation": True,
            "restriction": AsyncRestriction(deny_commands=("psql",)),
        }
    )
    assert classify_input(forged, at=AT) == "blocked"


def test_owner_cookie_does_not_replace_current_owner_session():
    value = source_facts(
        source="owner",
        kind="restriction",
        source_input=None,
        restriction=AsyncRestriction(deny_write_paths=("src/db",)),
    )
    assert classify_input(value, at=AT) == "blocked"
    assert (
        classify_input(value.model_copy(update={"owner_session_current": True}), at=AT)
        == "restriction"
    )


def test_restrictions_only_accumulate_and_replaying_is_idempotent():
    first = AsyncRestriction(deny_write_paths=("src/db",))
    extra = AsyncRestriction(deny_write_paths=("migrations",), deny_commands=("psql",))
    merged = first.narrow(extra)
    assert merged.deny_write_paths == ("migrations", "src/db")
    assert merged.deny_commands == ("psql",)
    assert merged.narrow(AsyncRestriction()) == merged.narrow(first) == merged
    assert merged.restriction_sha256 != first.restriction_sha256


@pytest.mark.parametrize(
    "path", ["", ".", "../src", "src/../db", "/etc", "src//db", "src\\db", "src/*", "src/%2e"]
)
def test_deny_sets_require_precise_bounded_paths(path):
    with pytest.raises(ValidationError):
        AsyncRestriction(deny_write_paths=(path,))


@pytest.mark.parametrize("values", [("src", "src"), ("z", "a")])
def test_noncanonical_restriction_sets_cannot_create_ambiguous_hashes(values):
    with pytest.raises(ValidationError):
        AsyncRestriction(deny_write_paths=values)


def test_safe_point_revision_rejects_the_entire_old_model_view(pair):
    view, current = pair
    validate_view(view, current, at=AT)
    changed = current.model_copy(update={"input_revision": 4})
    with pytest.raises(DomainError) as error:
        validate_mutation(view, changed, at=AT, capability="write", paths=("src/calc.py",))
    assert error.value.code == "SOURCE_ASYNC_INPUT_STALE"


@pytest.mark.parametrize(
    "field",
    [
        "coding_id",
        "assignment_id",
        "session_id",
        "workspace_id",
        "epoch",
        "mandate_sha256",
        "profile_sha256",
        "initial_context_sha256",
    ],
)
def test_scope_cannot_be_reused_for_another_original_assignment(pair, field):
    view, current = pair
    value = 2 if field == "epoch" else "0" * 64 if field.endswith("sha256") else uuid4()
    changed = current.model_copy(update={"scope": current.scope.model_copy(update={field: value})})
    with pytest.raises(DomainError) as error:
        validate_view(view, changed, at=AT)
    assert error.value.code == "SOURCE_ASYNC_SCOPE_FENCED"


def test_other_async_id_and_other_task_are_not_the_same_scope(pair):
    view, current = pair
    for changed in [
        current.model_copy(update={"async_id": uuid4()}),
        current.model_copy(
            update={
                "scope": current.scope.model_copy(
                    update={
                        "target": current.scope.target.model_copy(update={"ticket_id": uuid4()})
                    }
                )
            }
        ),
    ]:
        with pytest.raises(DomainError):
            validate_view(view, changed, at=AT)


@pytest.mark.parametrize(
    "field",
    [
        "original_coding_current",
        "assignment_current",
        "session_current",
        "sources_current",
        "budget_current",
        "active",
    ],
)
def test_view_never_defaults_to_current_authority(pair, field):
    view, current = pair
    with pytest.raises(DomainError) as error:
        validate_view(view, current.model_copy(update={field: False}), at=AT)
    assert error.value.code == "SOURCE_ASYNC_AUTHORITY_FENCED"


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"pending_invalidation": True}, "SOURCE_ASYNC_AUTHORITY_FENCED"),
        ({"tree_sha256": "0" * 64}, "SOURCE_ASYNC_TREE_STALE"),
        ({"environment_sha256": "0" * 64}, "SOURCE_ASYNC_ENVIRONMENT_STALE"),
        ({"expires_at": AT}, "SOURCE_ASYNC_VIEW_EXPIRED"),
        ({"restriction": AsyncRestriction()}, "SOURCE_ASYNC_INPUT_STALE"),
    ],
)
def test_mutation_is_blocked_at_changed_boundary(pair, changes, code):
    view, current = pair
    with pytest.raises(DomainError) as error:
        validate_mutation(
            view, current.model_copy(update=changes), at=AT, capability="execute", command="python3"
        )
    assert error.value.code == code


def test_database_restriction_blocks_subtrees_without_blocking_similarly_named_files(pair):
    view, current = pair
    for path in ("src/db", "src/db/query.py"):
        with pytest.raises(DomainError) as error:
            validate_mutation(view, current, at=AT, capability="write", paths=(path,))
        assert error.value.code == "SOURCE_ASYNC_PATH_DENIED"
    validate_mutation(view, current, at=AT, capability="write", paths=("src/db_readme.py",))
    validate_mutation(view, current, at=AT, capability="execute", command="python3")
    with pytest.raises(DomainError):
        validate_mutation(view, current, at=AT, capability="execute", command="psql")


def test_received_and_blocked_are_never_misreported_as_applied():
    base = dict(
        input_id=uuid4(),
        async_id=uuid4(),
        scope_sha256=HASH,
        sequence=1,
        kind="context",
        input_revision=1,
        request_sha256=HASH,
    )
    received = AsyncInputReceipt(**base, state="received")
    assert received.applied_checkpoint_id is None
    for extras in [
        {"state": "applied"},
        {"state": "blocked"},
        {"state": "received", "applied_checkpoint_id": uuid4()},
    ]:
        with pytest.raises(ValidationError):
            AsyncInputReceipt(**base, **extras)
    assert AsyncInputReceipt(**base, state="blocked", reason_code="ROLE_DENIED").state == "blocked"


@pytest.mark.parametrize("state", ["unknown", "stop_requested", "known"])
def test_jobs_never_restart_after_unknown_or_stop(scope, state):
    data = dict(
        job_id=uuid4(),
        async_id=uuid4(),
        scope=scope,
        checkpoint_id=uuid4(),
        coding_operation_id=uuid4(),
        resource_operation_id=uuid4(),
        request_sha256=HASH,
        tree_sha256=HASH,
        environment_sha256=HASH,
        input_revision=1,
        revision=1,
        state=state,
    )
    if state == "known":
        data.update(result_code="succeeded", receipt_sha256=HASH, observed_at=AT)
    job = AsyncJob(**data)
    with pytest.raises(DomainError) as error:
        validate_job_transition(job, "running")
    assert error.value.code == "SOURCE_ASYNC_JOB_TRANSITION_DENIED"
    if state == "unknown":
        for trusted, exact in [(False, False), (True, False), (False, True)]:
            with pytest.raises(DomainError):
                validate_job_transition(
                    job, "known", trusted_receipt=trusted, exact_original_resource=exact
                )
        validate_job_transition(job, "known", trusted_receipt=True, exact_original_resource=True)


def test_http_models_do_not_accept_command_provider_authority_or_success_claims(scope):
    requests = [
        AsyncAdmitRequest(
            request_id=uuid4(),
            coding_id=scope.coding_id,
            run_id=scope.target.run_id,
            expected_coding_revision=1,
            expected_epoch=1,
            review_ref="explicit-owner-review",
        ),
        AsyncRestrictRequest(
            request_id=uuid4(),
            coding_id=scope.coding_id,
            run_id=scope.target.run_id,
            expected_epoch=1,
            expected_revision=1,
            restriction=AsyncRestriction(deny_commands=("psql",)),
            review_ref="explicit-narrowing",
        ),
        AsyncWorkerRequest(
            request_id=uuid4(),
            coding_id=scope.coding_id,
            run_id=scope.target.run_id,
            assignment_id=scope.assignment_id,
            session_id=scope.session_id,
            epoch=1,
            expected_revision=1,
        ),
        AsyncJobCancelRequest(
            request_id=uuid4(),
            coding_id=scope.coding_id,
            run_id=scope.target.run_id,
            job_id=uuid4(),
            expected_revision=1,
            review_ref="explicit-resource-stop",
        ),
        AsyncWorkerJobCancelRequest(
            request_id=uuid4(),
            coding_id=scope.coding_id,
            run_id=scope.target.run_id,
            assignment_id=scope.assignment_id,
            session_id=scope.session_id,
            epoch=1,
            expected_revision=1,
            job_id=uuid4(),
        ),
    ]
    for request in requests:
        for key in ("argv", "provider", "token", "paid", "trusted", "task_succeeded", "read_paths"):
            data = request.model_dump(mode="json")
            data[key] = None
            with pytest.raises(ValidationError):
                type(request).model_validate_json(json.dumps(data))
        assert type(request).model_validate_json(request.model_dump_json()) == request


def pending_input(current, *, sequence, facts, input_revision=3):
    return PendingInput(
        receipt=AsyncInputReceipt(
            input_id=uuid4(),
            async_id=current.async_id,
            scope_sha256=current.scope.scope_sha256,
            sequence=sequence,
            kind=facts.kind,
            state="received",
            input_revision=input_revision,
            request_sha256=digest({"sequence": sequence}),
            source_input=facts.source_input,
            restriction=facts.restriction,
        ),
        facts=facts,
    )


def plan(current, pending, **changes):
    values = dict(
        checkpoint_id=uuid4(),
        current=current,
        stage="after_model",
        at=AT,
        received_sequence=len(pending),
        pending=pending,
    )
    values.update(changes)
    return plan_safe_point(**values)


def test_safe_point_atomically_plans_control_context_and_blocked_dispositions(pair):
    _, current = pair
    context = source_facts(expires_at=AT + timedelta(seconds=10))
    owner = AsyncInputFacts(
        source="owner",
        kind="restriction",
        exact_target=True,
        owner_session_current=True,
        expires_at=AT + timedelta(seconds=30),
        restriction=AsyncRestriction(deny_write_paths=("src/models",)),
    )
    denied = source_facts(identity_current=False)
    pending = tuple(
        pending_input(current, sequence=index + 1, facts=facts)
        for index, facts in enumerate((context, owner, denied))
    )
    result = plan(current, pending)
    assert result.applied_ids == tuple(item.receipt.input_id for item in pending[:2])
    assert [item.input_id for item in result.blocked] == [pending[2].receipt.input_id]
    assert result.checkpoint.source_refs == (context.source_input,)
    assert result.checkpoint.expires_at == context.expires_at
    assert result.checkpoint.restriction.deny_write_paths == ("src/db", "src/models")
    assert result.checkpoint.restriction.deny_commands == ("psql",)
    assert all(item.receipt.state == "received" for item in pending)


@pytest.mark.parametrize(
    "changes", [{"received_sequence": 2}, {"received_sequence": -1}, {"received_sequence": True}]
)
def test_safe_point_cannot_silently_skip_unread_input(pair, changes):
    _, current = pair
    pending = (pending_input(current, sequence=1, facts=source_facts()),)
    with pytest.raises(DomainError):
        plan(current, pending, **changes)


@pytest.mark.parametrize("sequences", [(2,), (1, 1), (2, 1), (1, 3)])
def test_safe_point_rejects_out_of_order_or_duplicate_sequences(pair, sequences):
    _, current = pair
    pending = tuple(pending_input(current, sequence=n, facts=source_facts()) for n in sequences)
    with pytest.raises(DomainError):
        plan(current, pending)


@pytest.mark.parametrize(
    "field,value",
    [
        ("async_id", uuid4()),
        ("scope_sha256", "0" * 64),
        ("input_revision", 4),
        ("kind", "job_progress"),
    ],
)
def test_safe_point_cannot_consume_a_substituted_receipt(pair, field, value):
    _, current = pair
    pending = pending_input(current, sequence=1, facts=source_facts())
    changed = pending.model_copy(
        update={"receipt": pending.receipt.model_copy(update={field: value})}
    )
    with pytest.raises(DomainError):
        plan(current, (changed,))


def test_safe_point_requires_current_source_facts_for_every_retained_dependency(pair):
    _, current = pair
    facts = source_facts(expires_at=AT + timedelta(seconds=20))
    previous = plan(current, (pending_input(current, sequence=1, facts=facts),)).checkpoint
    with pytest.raises(DomainError):
        plan(current, (), previous=previous, received_sequence=1)
    with pytest.raises(DomainError):
        plan(
            current,
            (),
            previous=previous,
            received_sequence=1,
            retained_facts=(facts.model_copy(update={"policy_current": False}),),
        )
    replay = plan(current, (), previous=previous, received_sequence=1, retained_facts=(facts,))
    assert replay.applied_ids == ()
    assert replay.blocked == ()
    assert replay.checkpoint.context_sha256 == previous.context_sha256
    assert replay.checkpoint.source_refs == previous.source_refs
    assert replay.checkpoint.expires_at == facts.expires_at


def test_progress_cannot_revive_a_fenced_scope_or_turn_invalidation_into_context(pair):
    _, current = pair
    facts = AsyncInputFacts(
        source="system",
        kind="invalidation",
        exact_target=True,
        trusted_observation=True,
        expires_at=AT + timedelta(seconds=1),
    )
    with pytest.raises(DomainError):
        plan(current, (pending_input(current, sequence=1, facts=facts),))
    with pytest.raises(DomainError):
        plan(current.model_copy(update={"pending_invalidation": True}), ())


def test_plaintext_and_task_success_do_not_exist_in_safe_point_plan(pair):
    _, current = pair
    result = plan(current, (pending_input(current, sequence=1, facts=source_facts()),))
    value = result.model_dump(mode="json")
    assert set(value) == {"checkpoint", "applied_ids", "blocked"}
    assert set(value["checkpoint"]["source_refs"][0]) == {
        "input_id",
        "source_instance_id",
        "project_id",
        "version_fingerprint",
        "state_sha256",
    }
