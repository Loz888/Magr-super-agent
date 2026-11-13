"""Synthetic current-fact checks; these do not establish any PG/H11/H12 gate."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_coding_domain import (
    CODING_VERSION,
    CodingAdmitRequest,
    CodingBudgetRef,
    CodingCancelRequest,
    CodingCurrentFacts,
    CodingGrant,
    CodingHarnessGate,
    CodingHarnessGates,
    CodingInspectRequest,
    CodingMandate,
    CodingOrigin,
    CodingReceiptFacts,
    CodingRepository,
    CodingRunRequest,
    CodingTarget,
)
from mgar_v3.super_agent.source_coding_rules import (
    validate_coding_current,
    validate_coding_receipt,
    validate_coding_tool,
)
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef
from mgar_v3.super_agent.source_scheduler_domain import ResourceRequest

NOW = datetime(2026, 11, 13, 10, tzinfo=UTC)
HASH = "a" * 64
CURRENT_FLAGS = (
    "source_current",
    "identity_current",
    "policy_current",
    "canonical_current",
    "admission_allowed",
    "model_input_allowed",
    "execution_allowed",
    "budget_current",
    "snapshot_current",
    "target_current",
    "runtime_profile_current",
    "assignment_current",
    "session_current",
)


def changed(value, **updates):
    return type(value).model_validate({**value.model_dump(), **updates})


def frozen_scope():
    target = CodingTarget(
        project_id="orders-demo",
        thread_id=uuid4(),
        ticket_id=uuid4(),
        run_id=uuid4(),
        mandate_id=uuid4(),
    )
    ref = CorrelationInputRef(
        input_id=uuid4(),
        source_instance_id=uuid4(),
        project_id=target.project_id,
        version_fingerprint=HASH,
        state_sha256=HASH,
    )
    origin = CodingOrigin(
        input=ref,
        identity_id=uuid4(),
        identity_revision=1,
        principal_id=uuid4(),
        engine_actor="sa_" + uuid4().hex,
        policy_id=uuid4(),
        policy_revision=1,
        policy_sha256=HASH,
        expires_at=NOW + timedelta(hours=1),
    )
    gates = CodingHarnessGates(
        **{
            name: CodingHarnessGate(
                card=card,
                commit="a" * 40,
                source_sha256=HASH,
                evidence_sha256=HASH,
                engineering="PASS",
                recovery="PASS" if name == "h11" else "NOT_RUN",
            )
            for name, card in (("h11", "HV4-H11"), ("h12", "HV4-H12"))
        }
    )
    grant = CodingGrant(
        read_paths=("src", "tests"),
        write_paths=("src",),
        allowed_commands=("python3",),
        resources=ResourceRequest(cpu_millis=1000, memory_mib=256),
        environment_sha256=HASH,
    )
    repository = CodingRepository(
        repository="example/mgar-test",
        repository_id=900,
        source_kind="fixture_git",
        base_commit="a" * 40,
        upstream_tree="b" * 40,
        materialized_commit="c" * 40,
        materialized_tree_sha256=HASH,
        registration_sha256=HASH,
        snapshot_sha256=HASH,
        private=True,
        model_disclosure="controlled_fixture",
    )
    budget = CodingBudgetRef(
        organization_id="bounded-organization",
        organization_revision=1,
        project_revision=1,
        profile_sha256=HASH,
        price_version="fixed-synthetic-price-v1",
        price_sha256=HASH,
        limits_sha256=HASH,
    )
    mandate = CodingMandate(
        coding_id=uuid4(),
        revision=1,
        target=target,
        origin=origin,
        repository=repository,
        grant=grant,
        budget=budget,
        harness=gates,
        context_refs=(ref,),
        context_sha256=HASH,
        acceptance_sha256=HASH,
        worker_profile_sha256=HASH,
        review_sha256=HASH,
        signed_by=uuid4(),
        valid_from=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=30),
    )
    assignment, session_id, workspace = uuid4(), uuid4(), uuid4()
    facts = CodingCurrentFacts(
        target=target,
        origin=origin,
        repository=repository,
        budget=budget,
        mandate_sha256=mandate.mandate_sha256,
        coding_revision=1,
        profile_sha256=HASH,
        context_sha256=HASH,
        harness=gates,
        assignment_id=assignment,
        session_id=session_id,
        workspace_id=workspace,
        epoch=7,
        run_epoch=7,
        assignment_epoch=7,
        assignment_created_at=NOW - timedelta(seconds=5),
        lease_until=NOW + timedelta(seconds=30),
        session_expires_at=NOW + timedelta(hours=1),
        configuration_expires_at=NOW + timedelta(hours=1),
        current_tree_sha256=HASH,
        environment_sha256=HASH,
        original_scope_kind=CODING_VERSION,
        original_read_only=False,
        source_mode="auto_repair",
        run_state="running",
        assignment_state="active",
        policy_read_paths=("src", "tests"),
        policy_write_paths=("src",),
        policy_commands=("python3",),
        **{field: True for field in CURRENT_FLAGS},
    )
    request = CodingRunRequest(
        request_id=uuid4(),
        coding_id=mandate.coding_id,
        run_id=target.run_id,
        assignment_id=assignment,
        session_id=session_id,
        epoch=7,
        expected_revision=1,
    )
    return mandate, facts, request


def receipt(mandate, facts, **updates):
    return CodingReceiptFacts.model_validate(
        {
            "coding_id": mandate.coding_id,
            "coding_revision": mandate.revision,
            "mandate_sha256": mandate.mandate_sha256,
            "target": mandate.target,
            "assignment_id": facts.assignment_id,
            "session_id": facts.session_id,
            "workspace_id": facts.workspace_id,
            "epoch": facts.epoch,
            "operation_id": uuid4(),
            "operation_sha256": HASH,
            "receipt_sha256": HASH,
            "tree_sha256": facts.current_tree_sha256,
            "environment_sha256": HASH,
            "profile_sha256": HASH,
            "acceptance_sha256": HASH,
            "observed_at": NOW,
            "provenance": "broker_verifier",
            "journaled": True,
            "result": "passed",
            "tests_run": 1,
            **updates,
        }
    )


def owner_requests():
    mandate, _, request = frozen_scope()
    return (
        CodingAdmitRequest(
            request_id=uuid4(),
            run_id=mandate.target.run_id,
            expected_epoch=0,
            source_input=mandate.origin.input,
            expected_policy_revision=1,
            expected_revision=0,
            target_sha256=HASH,
            profile_sha256=HASH,
            review_ref="exact-owner-review",
        ),
        request,
        CodingInspectRequest(coding_id=mandate.coding_id, run_id=mandate.target.run_id),
        CodingCancelRequest(
            request_id=uuid4(),
            coding_id=mandate.coding_id,
            run_id=mandate.target.run_id,
            expected_epoch=7,
            expected_revision=1,
            review_ref="exact-owner-cancel",
        ),
    )


@pytest.mark.parametrize(
    "field",
    [
        "body",
        "question",
        "path",
        "host_path",
        "argv",
        "command",
        "factory",
        "paid",
        "grant",
        "approved",
        "quiescent",
        "actor",
        "principal",
        "network",
        "model",
        "bearer",
    ],
)
def test_owner_metadata_cannot_submit_content_factories_or_self_authorization(field):
    for request in owner_requests():
        with pytest.raises(ValidationError):
            type(request).model_validate({**request.model_dump(), field: "untrusted"})


def test_metadata_json_roundtrip_and_strict_cas_do_not_coerce_authority():
    for request in owner_requests():
        assert type(request).model_validate_json(request.model_dump_json()) == request
        if hasattr(request, "expected_revision"):
            for invalid in (True, "1", 1.0, -1, 2**63):
                with pytest.raises(ValidationError):
                    changed(request, expected_revision=invalid)


def test_owner_can_cancel_a_queued_coding_admission_before_its_first_epoch():
    request = owner_requests()[-1]
    queued = changed(request, expected_epoch=0)
    assert queued.expected_epoch == 0
    for invalid in (True, "0", 0.0, -1):
        with pytest.raises(ValidationError):
            changed(request, expected_epoch=invalid)


def test_upstream_and_materialized_identity_are_distinct_and_bound_without_source_text():
    mandate, facts, request = frozen_scope()
    assert mandate.repository.base_commit != mandate.repository.materialized_commit
    before = mandate.model_dump_json()
    validate_coding_current(mandate, facts, request, at=NOW)
    assert mandate.model_dump_json() == before
    assert not ({"body", "question", "objective", "text"} & json.loads(before).keys())


@pytest.mark.parametrize("field", CURRENT_FLAGS)
def test_each_current_source_budget_target_and_assignment_fact_is_required(field):
    mandate, facts, request = frozen_scope()
    with pytest.raises(DomainError):
        validate_coding_current(
            mandate, changed(facts, **{field: False}), request, at=NOW
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"original_scope_kind": "legacy"},
        {"original_read_only": True},
        {"source_mode": "auto_investigate"},
        {"source_mode": "shadow"},
        {"source_mode": "disabled"},
        {"run_state": "paused"},
        {"run_state": "succeeded"},
        {"assignment_state": "draining"},
        {"assignment_state": "released"},
        {"epoch": 8},
        {"run_epoch": 8},
        {"assignment_epoch": 8},
        {"lease_until": NOW},
        {"session_expires_at": NOW},
        {"configuration_expires_at": NOW},
        {"mandate_sha256": "b" * 64},
        {"context_sha256": "b" * 64},
        {"profile_sha256": "b" * 64},
        {"environment_sha256": "b" * 64},
    ],
)
def test_old_namespaces_readonly_epochs_and_expiry_never_upgrade(updates):
    mandate, facts, request = frozen_scope()
    with pytest.raises(DomainError):
        validate_coding_current(mandate, changed(facts, **updates), request, at=NOW)


@pytest.mark.parametrize(
    "part,updates",
    [
        ("target", {"run_id": uuid4()}),
        ("target", {"project_id": "other-project"}),
        ("origin", {"identity_revision": 2}),
        ("origin", {"policy_revision": 2}),
        ("repository", {"base_commit": "e" * 40}),
        ("repository", {"repository_id": 901}),
        ("budget", {"project_revision": 2}),
        ("budget", {"price_version": "other-price"}),
    ],
)
def test_exact_current_canonical_original_target_repository_and_budget_refs_are_frozen(
    part, updates
):
    mandate, facts, request = frozen_scope()
    with pytest.raises(DomainError):
        validate_coding_current(
            mandate,
            changed(
                facts,
                **{
                    part: changed(getattr(facts, part), **updates),
                },
            ),
            request,
            at=NOW,
        )


@pytest.mark.parametrize(
    "card,gate,status",
    [
        (card, gate, status)
        for card, gate in (
            ("h11", "engineering"),
            ("h11", "recovery"),
            ("h12", "engineering"),
            ("h12", "live_agent"),
            ("h12", "mvp"),
        )
        for status in ("FAIL", "NOT_RUN")
    ],
)
def test_live_coding_requires_each_fixed_harness_gate_not_just_environment(
    card, gate, status
):
    mandate, facts, request = frozen_scope()
    gates = changed(
        mandate.harness,
        **{
            name: changed(getattr(mandate.harness, name), live_agent="PASS", mvp="PASS")
            for name in ("h11", "h12")
        },
    )
    gates = changed(gates, **{card: changed(getattr(gates, card), **{gate: status})})
    mandate = changed(
        mandate,
        harness=gates,
        repository=changed(mandate.repository, model_disclosure="approved_live"),
    )
    facts = changed(
        facts,
        harness=gates,
        repository=mandate.repository,
        mandate_sha256=mandate.mandate_sha256,
        mode="live",
        live_source_authorized=True,
        live_model_authorized=True,
        live_execution_authorized=True,
    )
    with pytest.raises(DomainError):
        validate_coding_current(mandate, facts, request, at=NOW)


@pytest.mark.parametrize(
    "capability,path,command",
    [
        ("read", "src/module.py", None),
        ("write", "src/module.py", None),
        ("execute", None, "python3"),
    ],
)
def test_tool_requires_current_mandate_and_policy_intersection(
    capability, path, command
):
    mandate, facts, request = frozen_scope()
    validate_coding_tool(
        mandate,
        facts,
        request,
        at=NOW,
        capability=capability,
        path=path,
        command=command,
        expected_tree_sha256=HASH,
    )


@pytest.mark.parametrize(
    "path", ["../src/file.py", "/etc/passwd", "src/../secret", ".env", ".git/config"]
)
def test_traversal_absolute_and_protected_paths_are_denied(path):
    mandate, facts, request = frozen_scope()
    with pytest.raises(DomainError):
        validate_coding_tool(
            mandate,
            facts,
            request,
            at=NOW,
            capability="write",
            path=path,
            expected_tree_sha256=HASH,
        )


def test_narrower_policy_or_stale_tree_blocks_a_previous_grant():
    mandate, facts, request = frozen_scope()
    for changed_facts, tree in (
        (changed(facts, policy_write_paths=("src/other",)), HASH),
        (facts, "b" * 64),
        (facts, None),
    ):
        with pytest.raises(DomainError):
            validate_coding_tool(
                mandate,
                changed_facts,
                request,
                at=NOW,
                capability="write",
                path="src/module.py",
                expected_tree_sha256=tree,
            )


@pytest.mark.parametrize(
    "updates",
    [
        {"network": True},
        {"network": 0},
        {"write_paths": ("",)},
        {"write_paths": (".git",)},
        {"read_paths": ("tests",)},
        {"allowed_commands": ("/bin/sh",)},
        {"allowed_commands": ("..",)},
        {"resources": {"cpu_millis": True, "memory_mib": 256}},
    ],
)
def test_frozen_grant_does_not_expand_paths_process_or_resource_bounds(updates):
    mandate, _, _ = frozen_scope()
    with pytest.raises(ValidationError):
        changed(mandate.grant, **updates)


def test_current_trusted_receipt_and_exact_actual_tree_are_required():
    mandate, facts, request = frozen_scope()
    result = receipt(mandate, facts)
    validate_coding_receipt(
        mandate,
        facts,
        request,
        result,
        at=NOW,
        operation_id=result.operation_id,
        operation_sha256=HASH,
    )
    assert result.target.run_id == request.run_id


@pytest.mark.parametrize(
    "updates",
    [
        {"coding_id": uuid4()},
        {"assignment_id": uuid4()},
        {"session_id": uuid4()},
        {"workspace_id": uuid4()},
        {"epoch": 8},
        {"provenance": "untrusted"},
        {"journaled": False},
        {"result": "missing"},
        {"result": "failed"},
        {"tests_run": 0},
        {"tree_sha256": "b" * 64},
        {"environment_sha256": "b" * 64},
        {"acceptance_sha256": "b" * 64},
        {"observed_at": NOW + timedelta(seconds=1)},
    ],
)
def test_foreign_untrusted_empty_and_stale_receipts_cannot_promote_success(updates):
    mandate, facts, request = frozen_scope()
    result = receipt(mandate, facts, **updates)
    with pytest.raises(DomainError):
        validate_coding_receipt(
            mandate,
            facts,
            request,
            result,
            at=NOW,
            operation_id=result.operation_id,
            operation_sha256=HASH,
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"coding_id": uuid4()},
        {"run_id": uuid4()},
        {"assignment_id": uuid4()},
        {"session_id": uuid4()},
        {"epoch": 8},
        {"expected_revision": 2},
    ],
)
def test_worker_metadata_cannot_select_another_current_assignment_or_revision(updates):
    mandate, facts, request = frozen_scope()
    with pytest.raises(DomainError):
        validate_coding_current(mandate, facts, changed(request, **updates), at=NOW)


def test_current_projection_defaults_grant_no_coding_authority():
    mandate, facts, request = frozen_scope()
    values = facts.model_dump()
    for field in (
        *CURRENT_FLAGS,
        "original_scope_kind",
        "original_read_only",
        "source_mode",
        "run_state",
        "assignment_state",
        "policy_read_paths",
        "policy_write_paths",
        "policy_commands",
    ):
        values.pop(field)
    unverified = CodingCurrentFacts.model_validate(values)
    assert unverified.original_read_only and unverified.original_scope_kind == "legacy"
    assert all(getattr(unverified, field) is False for field in CURRENT_FLAGS)
    with pytest.raises(DomainError):
        validate_coding_current(mandate, unverified, request, at=NOW)


def test_h12_engineering_pass_and_failed_quality_allow_only_controlled_fixture():
    mandate, facts, request = frozen_scope()
    gates = changed(
        mandate.harness,
        h12=changed(mandate.harness.h12, live_agent="FAIL", mvp="FAIL"),
    )
    mandate = changed(mandate, harness=gates)
    facts = changed(facts, harness=gates, mandate_sha256=mandate.mandate_sha256)
    validate_coding_current(mandate, facts, request, at=NOW)
    live = changed(
        facts,
        mode="live",
        live_source_authorized=True,
        live_model_authorized=True,
        live_execution_authorized=True,
    )
    with pytest.raises(DomainError) as error:
        validate_coding_current(mandate, live, request, at=NOW)
    assert error.value.code == "SOURCE_CODING_HARNESS_LIVE_REQUIRED"


@pytest.mark.parametrize(
    "card,gate,status",
    [
        (card, gate, status)
        for card, gate in (
            ("h11", "engineering"),
            ("h11", "recovery"),
            ("h12", "engineering"),
        )
        for status in ("FAIL", "NOT_RUN")
    ],
)
def test_fixture_requires_fixed_h11_recovery_and_both_engineering_gates(
    card, gate, status
):
    mandate, facts, request = frozen_scope()
    gates = changed(
        mandate.harness,
        **{card: changed(getattr(mandate.harness, card), **{gate: status})},
    )
    mandate = changed(mandate, harness=gates)
    facts = changed(facts, harness=gates, mandate_sha256=mandate.mandate_sha256)
    with pytest.raises(DomainError) as error:
        validate_coding_current(mandate, facts, request, at=NOW)
    assert error.value.code == "SOURCE_CODING_HARNESS_ENGINEERING_REQUIRED"


def test_synthetic_live_shape_uses_h11_recovery_and_h12_release_responsibilities():
    """Synthetic projections do not establish any actual platform or Coding gate."""
    mandate, facts, request = frozen_scope()
    gates = changed(
        mandate.harness,
        h12=changed(mandate.harness.h12, live_agent="PASS", mvp="PASS"),
    )
    assert gates.h11.live_agent == gates.h11.mvp == "NOT_RUN"
    mandate = changed(
        mandate,
        harness=gates,
        repository=changed(mandate.repository, model_disclosure="approved_live"),
    )
    facts = changed(
        facts,
        harness=gates,
        repository=mandate.repository,
        mandate_sha256=mandate.mandate_sha256,
        mode="live",
        live_source_authorized=True,
        live_model_authorized=True,
        live_execution_authorized=True,
    )
    validate_coding_current(mandate, facts, request, at=NOW)


@pytest.mark.parametrize("field", ["version_fingerprint", "state_sha256"])
def test_current_source_canonical_version_changes_invalidate_old_coding_context(field):
    mandate, facts, request = frozen_scope()
    reference = changed(facts.origin.input, **{field: "b" * 64})
    current = changed(facts, origin=changed(facts.origin, input=reference))
    with pytest.raises(DomainError):
        validate_coding_current(mandate, current, request, at=NOW)


@pytest.mark.parametrize("kind", ["missing_origin", "duplicate", "other_project"])
def test_context_references_cannot_drop_the_origin_duplicate_or_cross_project(kind):
    mandate, _, _ = frozen_scope()
    reference = mandate.origin.input
    if kind == "missing_origin":
        refs = (changed(reference, input_id=uuid4()),)
    elif kind == "duplicate":
        refs = (reference, reference)
    else:
        refs = (
            reference,
            changed(reference, input_id=uuid4(), project_id="other-project"),
        )
    with pytest.raises(ValidationError):
        changed(mandate, context_refs=refs)


@pytest.mark.parametrize("at", [NOW.replace(tzinfo=None), None, True])
def test_current_time_must_be_an_explicit_aware_clock_observation(at):
    mandate, facts, request = frozen_scope()
    with pytest.raises(DomainError):
        validate_coding_current(mandate, facts, request, at=at)


@pytest.mark.parametrize("capability", ["write", "execute"])
def test_every_mutation_or_command_requires_the_exact_actual_tree(capability):
    mandate, facts, request = frozen_scope()
    arguments = (
        {"path": "src/module.py"} if capability == "write" else {"command": "python3"}
    )
    with pytest.raises(DomainError) as error:
        validate_coding_tool(
            mandate,
            facts,
            request,
            at=NOW,
            capability=capability,
            **arguments,
        )
    assert error.value.code == "SOURCE_CODING_TREE_REQUIRED"


@pytest.mark.parametrize("command", ["sh", "python3 -c", "bin/python3"])
def test_command_names_do_not_gain_shell_or_path_authority(command):
    mandate, facts, request = frozen_scope()
    with pytest.raises(DomainError):
        validate_coding_tool(
            mandate,
            facts,
            request,
            at=NOW,
            capability="execute",
            command=command,
            expected_tree_sha256=HASH,
        )


def test_command_policy_revocation_blocks_an_unchanged_mandate():
    mandate, facts, request = frozen_scope()
    with pytest.raises(DomainError):
        validate_coding_tool(
            mandate,
            changed(facts, policy_commands=()),
            request,
            at=NOW,
            capability="execute",
            command="python3",
            expected_tree_sha256=HASH,
        )


@pytest.mark.parametrize(
    "part", ["operation_id", "operation_sha256", "target", "profile"]
)
def test_receipt_must_match_the_requested_journal_operation_and_original_target(part):
    mandate, facts, request = frozen_scope()
    result = receipt(mandate, facts)
    expected_operation, expected_hash = result.operation_id, result.operation_sha256
    if part == "operation_id":
        result = changed(result, operation_id=uuid4())
    elif part == "operation_sha256":
        result = changed(result, operation_sha256="b" * 64)
    elif part == "target":
        result = changed(result, target=changed(result.target, ticket_id=uuid4()))
    else:
        result = changed(result, profile_sha256="b" * 64)
    with pytest.raises(DomainError):
        validate_coding_receipt(
            mandate,
            facts,
            request,
            result,
            at=NOW,
            operation_id=expected_operation,
            operation_sha256=expected_hash,
        )


@pytest.mark.parametrize("minimum", [0, True, "1", 2])
def test_receipt_cannot_substitute_invalid_or_insufficient_test_counts(minimum):
    mandate, facts, request = frozen_scope()
    result = receipt(mandate, facts)
    with pytest.raises(DomainError):
        validate_coding_receipt(
            mandate,
            facts,
            request,
            result,
            at=NOW,
            operation_id=result.operation_id,
            operation_sha256=HASH,
            minimum_tests=minimum,
        )


def test_pre_assignment_receipt_cannot_prove_current_attempt_verification():
    mandate, facts, request = frozen_scope()
    result = receipt(
        mandate, facts, observed_at=facts.assignment_created_at - timedelta(seconds=1)
    )
    with pytest.raises(DomainError):
        validate_coding_receipt(
            mandate,
            facts,
            request,
            result,
            at=NOW,
            operation_id=result.operation_id,
            operation_sha256=HASH,
        )
