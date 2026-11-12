"""Source correlation confusion, immutable contracts and exact code boundaries.

Pure synthetic facts only: these cases never request Engine, PG, model or network.
The service separately owns current authority and real transaction/claim tests.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import SourceScope
from mgar_v3.super_agent.source_correlation_domain import (
    MAX_CORRELATION_BYTES,
    CodeRef,
    CorrelationCorrectRequest,
    CorrelationHead,
    CorrelationInputRef,
    CorrelationLinkRequest,
    CorrelationRemoveRequest,
    CorrelationResolution,
    CorrelationResolveRequest,
    CorrelationSnapshot,
    InputUnit,
    ResourceEvidence,
    ResourceRef,
    TargetRef,
    input_unit_key,
    snapshot_hash,
    verification_reusable,
)
from mgar_v3.super_agent.source_correlation_rules import (
    exact_url_resources,
    parse_snapshot,
    resolve,
)
from mgar_v3.super_agent.source_eval_domain import SourceDecision, SourceTargetRef

NOW = datetime(2026, 11, 12, 14, tzinfo=UTC)


def example():
    scope = SourceScope(
        source_kind="github", source_instance_id=uuid4(),
        tenant_id="Synthetic-Tenant", installation_id="Synthetic-Installation",
    )
    source = CorrelationInputRef(
        input_id=uuid4(), source_instance_id=scope.source_instance_id,
        project_id="orders-demo", version_fingerprint="a" * 64, state_sha256="b" * 64,
    )
    resource = ResourceRef(
        project_id=source.project_id, scope=scope, repository_id=123,
        repository="example/mgar-test", kind="pull_request", resource_id="12",
    )
    target = TargetRef(
        ticket_id=uuid4(), run_id=uuid4(), thread_id=uuid4(), thread_revision=4,
        execution_epoch=2, mandate_id=uuid4(), mandate_sha256="c" * 64,
        project_id=source.project_id, repository=resource.repository, allowed=True,
    )
    unit = InputUnit(
        input_id=source.input_id, unit_key=input_unit_key(source, resource.resource_key),
        resource_keys=(resource.resource_key,), origin="verified_resource",
    )
    return CorrelationSnapshot(
        project_id=source.project_id, inputs=(source,), units=(unit,), resources=(
            ResourceEvidence(input_id=source.input_id, resource=resource,
                             proof_kind="verified_resource", proof_sha256="d" * 64),
        ), heads=(CorrelationHead(
            resource_key=resource.resource_key, revision=1, head_sha256="e" * 64,
            state="bound", target=target, link_id=uuid4(),
        ),), targets=(target,), expires_at=NOW + timedelta(seconds=60),
    )


def altered(model, **changes):
    value = model.model_dump()
    value.update(changes)
    return type(model).model_validate(value)


def code_ref():
    return CodeRef(
        resource_key=example().resources[0].resource.resource_key, proof_sha256="d" * 64,
        head_repository_id=456, head_sha="a" * 40, base_sha="b" * 40,
        tree_sha="c" * 40, run_id=50, run_attempt=1, check_run_id=60, check_suite_id=70,
    )


@pytest.mark.parametrize("field", [
    "project_id", "repository_id", "kind", "resource_id", "instance", "tenant", "installation",
])
def test_same_number_different_owner_or_resource_does_not_merge(field):
    resource = example().resources[0].resource
    if field in {"instance", "tenant", "installation"}:
        scopes = {"instance": {"source_instance_id": uuid4()},
                  "tenant": {"tenant_id": "synthetic-tenant"},
                  "installation": {"installation_id": "synthetic-installation"}}
        other = altered(resource, scope=altered(resource.scope, **scopes[field]))
    else:
        changes = {"project_id": "other-project", "repository_id": 124,
                   "kind": "issue", "resource_id": "13"}
        other = altered(resource, **{field: changes[field]})
    assert other.resource_key != resource.resource_key


def test_repository_display_rename_does_not_change_immutable_resource_id():
    resource = example().resources[0].resource
    assert altered(resource, repository="OTHER/RENAMED").resource_key == resource.resource_key
    assert altered(resource, repository="EXAMPLE/MGAR-TEST").repository == resource.repository


@pytest.mark.parametrize("field,value", [
    ("resource_id", "012"), ("resource_id", "0"), ("resource_id", 12),
    ("resource_id", "9223372036854775808"), ("repository_id", True),
    ("repository_id", "123"), ("repository_id", 123.0), ("kind", "topic"),
])
def test_resource_identifiers_do_not_coerce_or_guess(field, value):
    with pytest.raises(ValidationError):
        altered(example().resources[0].resource, **{field: value})


def test_im_message_cannot_be_promoted_to_a_native_github_issue():
    resource = example().resources[0].resource
    with pytest.raises(ValidationError):
        altered(resource, scope=altered(resource.scope, source_kind="feishu"))


@pytest.mark.parametrize("field,value", [
    ("resource_key", "f" * 64), ("head_repository_id", 457), ("head_sha", "f" * 40),
    ("base_sha", "f" * 40), ("tree_sha", "f" * 40), ("run_id", 51),
    ("run_attempt", 2), ("check_run_id", 61), ("check_suite_id", 71),
])
def test_pr_force_push_or_distinct_ci_observation_cannot_reuse_tree(field, value):
    old = code_ref()
    current = altered(old, **{field: value})
    assert current.code_version_key != old.code_version_key
    assert verification_reusable(old, current) is False


@pytest.mark.parametrize("field", ["head_repository_id", "head_sha", "base_sha", "tree_sha"])
def test_missing_code_identity_is_unknown_not_verified(field):
    old = code_ref()
    assert verification_reusable(old, altered(old, **{field: None})) is False
    assert verification_reusable(None, old) is False


def test_fresh_proof_of_same_tree_preserves_code_identity_without_granting_permission():
    old = code_ref()
    current = altered(old, proof_sha256="f" * 64)
    assert verification_reusable(old, current) is True
    assert verification_reusable(old, old) is True
    with pytest.raises(ValidationError):
        altered(old, run_id=None)


@pytest.mark.parametrize("field", ["body", "actor", "factory", "execute", "grant", "authorization"])
def test_http_relation_request_never_accepts_text_or_factory_authority(field):
    with pytest.raises(ValidationError):
        CorrelationResolveRequest.model_validate({
            "input_ids": (uuid4(),), field: "synthetic-private-request",
        })


def test_api_requests_require_exact_target_snapshot_and_current_revision():
    value = example()
    request = CorrelationLinkRequest(
        input_ids=(value.inputs[0].input_id,), resource_key=value.heads[0].resource_key,
        snapshot_sha256=snapshot_hash(value), expected_revision=0, target=value.targets[0],
        review_ref="synthetic-owner-review",
    )
    assert request.target == value.targets[0]
    with pytest.raises(ValidationError):
        altered(request, expected_revision=True)
    with pytest.raises(ValidationError):
        altered(request, input_ids=request.input_ids * 2)
    for model, extra in [(CorrelationCorrectRequest, {"target": value.targets[0]}),
                         (CorrelationRemoveRequest, {})]:
        with pytest.raises(ValidationError):
            model(snapshot_sha256=snapshot_hash(value), expected_revision=0,
                  review_ref="synthetic-review", **extra)


def test_contracts_are_frozen_and_roundtrip_json_uses_uuid_boundary():
    value = example()
    assert parse_snapshot(value.model_dump_json()) == value
    with pytest.raises(ValidationError):
        value.project_id = "other-project"
    with pytest.raises(ValidationError):
        value.targets[0].allowed = False
    with pytest.raises(ValidationError):
        CorrelationSnapshot.model_validate_json(value.model_dump_json().replace(
            "source-correlation-snapshot-v1", "source-router-snapshot-v1"
        ))


@pytest.mark.parametrize("case", [
    "duplicate-input", "missing-unit", "duplicate-unit", "missing-proof", "duplicate-proof",
    "foreign-native-installation", "foreign-project", "duplicate-target", "unknown-head",
])
def test_snapshot_binds_exact_input_unit_proof_and_project_membership(case):
    value = example()
    data = value.model_dump()
    if case == "duplicate-input":
        data["inputs"] *= 2
    elif case == "missing-unit":
        data["units"] = ()
    elif case == "duplicate-unit":
        data["units"] *= 2
    elif case == "missing-proof":
        data["resources"] = ()
    elif case == "duplicate-proof":
        data["resources"] *= 2
    elif case == "foreign-native-installation":
        data["inputs"][0]["source_instance_id"] = uuid4()
    elif case == "foreign-project":
        data["targets"][0]["project_id"] = "other-project"
    elif case == "duplicate-target":
        data["targets"] *= 2
    else:
        data["heads"][0]["resource_key"] = "f" * 64
    with pytest.raises(ValidationError):
        CorrelationSnapshot.model_validate(data)


def test_alias_requires_signed_identity_and_revision_and_does_not_fake_native_owner():
    value = example()
    proof = value.resources[0]
    with pytest.raises(ValidationError):
        altered(proof, proof_kind="signed_alias")
    alias = altered(proof, proof_kind="signed_alias", alias_id=uuid4(), alias_revision=2)
    foreign = altered(value.inputs[0], source_instance_id=uuid4())
    current = altered(value, inputs=(foreign,), resources=(alias,))
    assert resolve(current, at=NOW)[0].outcome == "matched"
    with pytest.raises(ValidationError):
        altered(alias, proof_kind="verified_resource")


@pytest.mark.parametrize("payload", [
    b"\xff", b"[]", b"null", b"{}", b'{"project_id":"a","project_id":"b"}',
    b'{"expires_at":NaN}', b"[" * 2000 + b"]" * 2000,
    "界" * 21846, "\ud800", b" " * (MAX_CORRELATION_BYTES + 1),
])
def test_malformed_or_unbounded_snapshot_has_fixed_error_without_private_echo(payload):
    with pytest.raises(DomainError) as error:
        parse_snapshot(payload)
    assert error.value.code == "SOURCE_CORRELATION_CONTRACT_INVALID"
    assert "project_id" not in str(error.value)
    assert "界" not in str(error.value)


@pytest.mark.parametrize("url", [
    "http://github.com/example/mgar-test/pull/12",
    "https://github.com.evil/example/mgar-test/pull/12",
    "https://github.com@evil/example/mgar-test/pull/12",
    "https://user@github.com/example/mgar-test/pull/12",
    "https://github.com:443/example/mgar-test/pull/12",
    "https://github.com/example/other/pull/12",
    "https://github.com/example/mgar-test/issues/12",
    "https://github.com/example/mgar-test/pull/012",
    "https://github.com/example/mgar-test/pull/12?target=13",
    "https://github.com/example/mgar-test/pull/12/../13",
    "same title #12",
])
def test_exact_url_matching_never_guesses_scope_or_follows_untrusted_urls(url):
    assert exact_url_resources(url, (example().resources[0].resource,)) == ()


def test_two_installations_same_visible_url_remain_ambiguous_resource_references():
    resource = example().resources[0].resource
    other = altered(resource, scope=altered(resource.scope, source_instance_id=uuid4()))
    refs = exact_url_resources(
        "(https://github.com/EXAMPLE/MGAR-TEST/pull/12#issuecomment-30)", (resource, other),
    )
    assert {ref.resource_key for ref in refs} == {resource.resource_key, other.resource_key}


def test_exact_match_is_observation_only_with_full_current_target_binding():
    value = example()
    result, = resolve(value, at=NOW)
    assert result.outcome == "matched"
    assert result.reason_code == "EXACT_RESOURCE"
    assert result.candidates == value.targets
    assert result.snapshot_sha256 == snapshot_hash(value)
    assert result.eligible_for_execution is False
    assert result.executes_tasks is False
    assert result.calls_model is False
    assert result.external_writes == 0
    assert resolve(value, at=NOW) == (result,)


@pytest.mark.parametrize("case,reason", [
    ("no-head", "RESOURCE_UNCLAIMED"), ("unclaimed", "RESOURCE_UNCLAIMED"),
    ("removed", "RESOURCE_REMOVED"), ("denied", "TARGET_DENIED"),
    ("missing-current", "TARGET_UNAVAILABLE"), ("epoch", "TARGET_UNAVAILABLE"),
    ("thread-revision", "TARGET_UNAVAILABLE"), ("mandate", "TARGET_UNAVAILABLE"),
    ("repository", "RESOURCE_CONFLICT"), ("suggestion-proof", "SUGGESTION_ONLY"),
])
def test_uncertain_denied_or_stale_relation_does_not_expose_a_target(case, reason):
    value = example()
    head = value.heads[0]
    if case == "no-head":
        value = altered(value, heads=())
    elif case == "unclaimed":
        value = altered(value, heads=(altered(
            head, state="unclaimed", revision=0, target=None, link_id=None,
        ),))
    elif case == "removed":
        value = altered(value, heads=(altered(head, state="removed", target=None),))
    elif case == "denied":
        denied = altered(value.targets[0], allowed=False)
        value = altered(value, targets=(denied,), heads=(altered(head, target=denied),))
    elif case == "missing-current":
        value = altered(value, targets=())
    elif case in {"epoch", "thread-revision", "mandate"}:
        updates = {"epoch": {"execution_epoch": 3},
                   "thread-revision": {"thread_revision": 5}, "mandate": {"mandate_id": uuid4()}}
        value = altered(value, targets=(altered(value.targets[0], **updates[case]),))
    elif case == "repository":
        other = altered(value.targets[0], repository="example/other")
        value = altered(value, targets=(other,), heads=(altered(head, target=other),))
    else:
        value = altered(value, resources=(altered(value.resources[0], proof_kind="suggestion"),))
    result, = resolve(value, at=NOW)
    assert result.outcome != "matched"
    assert result.reason_code == reason
    assert result.candidates == ()


def two_problems(value, one_unit=False, same_target=False):
    source = value.inputs[0]
    other = altered(value.resources[0].resource, resource_id="13")
    target = value.targets[0] if same_target else altered(value.targets[0], ticket_id=uuid4())
    evidence = ResourceEvidence(
        input_id=source.input_id, resource=other, proof_kind="verified_resource",
        proof_sha256="f" * 64,
    )
    unit = InputUnit(
        input_id=source.input_id, unit_key=input_unit_key(source, other.resource_key),
        resource_keys=(other.resource_key,), origin="verified_resource",
    )
    units = (altered(value.units[0], resource_keys=(
        value.resources[0].resource.resource_key, other.resource_key,
    )),) if one_unit else (*value.units, unit)
    return altered(
        value, units=units, resources=(*value.resources, evidence),
        heads=(*value.heads, altered(value.heads[0], resource_key=other.resource_key,
                                   target=target, link_id=uuid4())),
        targets=value.targets if same_target else (*value.targets, target),
    )


def test_one_message_two_questions_has_separate_units_and_existing_tasks():
    value = two_problems(example())
    results = resolve(value, at=NOW)
    assert len(results) == 2
    assert len({result.input_id for result in results}) == 1
    assert len({result.candidates[0].ticket_id for result in results}) == 2
    assert all(result.outcome == "matched" for result in results)


@pytest.mark.parametrize("same_target", [False, True])
def test_multiple_resources_in_one_unit_never_select_first_or_silently_merge(same_target):
    value = two_problems(example(), one_unit=True, same_target=same_target)
    result, = resolve(value, at=NOW)
    assert result.outcome == "ambiguous" and result.reason_code == "RESOURCE_AMBIGUOUS"
    assert result.candidates == ()


def test_unknown_topic_has_no_chat_or_latest_task_fallback():
    value = example()
    source = value.inputs[0]
    unresolved = InputUnit(input_id=source.input_id, unit_key=input_unit_key(source))
    current = altered(value, units=(unresolved,))
    result, = resolve(current, at=NOW)
    assert result.reason_code == "NO_EXPLICIT_RESOURCE"
    assert result.candidates == ()


def test_model_suggestion_cannot_supply_a_candidate_or_break_ambiguity():
    value = two_problems(example(), one_unit=True)
    suggestion = SourceDecision(
        decision_key="a" * 64, batch_id=uuid4(), group_id=uuid4(), snapshot_sha256="b" * 64,
        fast_batch_id=uuid4(), fast_batch_revision=1,
        input_ids=(value.inputs[0].input_id,),
        input_version_fingerprints=(value.inputs[0].version_fingerprint,),
        intended_action="would_attach", reason_code="EXISTING_TASK",
        target=SourceTargetRef.model_validate(value.targets[0].model_dump()),
        expires_at=value.expires_at,
    )
    result, = resolve(value, at=NOW, suggestion=suggestion)
    assert result.suggestion_decision_key == suggestion.decision_key
    assert result.outcome == "ambiguous" and result.candidates == ()
    empty = altered(value, heads=(), targets=())
    result, = resolve(empty, at=NOW, suggestion=suggestion)
    assert result.candidates == () and result.eligible_for_execution is False


@pytest.mark.parametrize("field", ["version", "state", "relation", "epoch", "proof", "expiry"])
def test_snapshot_hash_binds_current_input_authority_relationship_and_target(field):
    old = example()
    if field in {"version", "state"}:
        key = "version_fingerprint" if field == "version" else "state_sha256"
        current = altered(old, inputs=(altered(old.inputs[0], **{key: "f" * 64}),))
    elif field == "relation":
        current = altered(old, heads=(altered(old.heads[0], revision=2),))
    elif field == "epoch":
        target = altered(old.targets[0], execution_epoch=3)
        current = altered(old, targets=(target,), heads=(altered(old.heads[0], target=target),))
    elif field == "proof":
        current = altered(old, resources=(altered(old.resources[0], proof_sha256="f" * 64),))
    else:
        current = altered(old, expires_at=old.expires_at + timedelta(seconds=1))
    assert snapshot_hash(current) != snapshot_hash(old)


@pytest.mark.parametrize("when,code", [
    (NOW.replace(tzinfo=None), "SOURCE_CORRELATION_TIME_INVALID"),
    (NOW + timedelta(seconds=60), "SOURCE_CORRELATION_SNAPSHOT_EXPIRED"),
])
def test_expired_or_naive_snapshot_cannot_be_resolved_as_current(when, code):
    with pytest.raises(DomainError) as error:
        resolve(example(), at=when)
    assert error.value.code == code


@pytest.mark.parametrize("field,value", [
    ("eligible_for_execution", True), ("eligible_for_execution", 0),
    ("executes_tasks", True), ("calls_model", "false"),
    ("external_writes", 1), ("external_writes", False),
])
def test_resolution_cannot_be_turned_into_execution_or_notification(field, value):
    result, = resolve(example(), at=NOW)
    with pytest.raises(ValidationError):
        CorrelationResolution.model_validate({**result.model_dump(), field: value})


def test_malformed_private_extra_field_is_not_echoed_by_parser():
    value = example().model_dump(mode="json")
    value["body"] = "SYNTHETIC_PRIVATE_TEXT_MUST_NOT_ESCAPE"
    with pytest.raises(DomainError) as error:
        parse_snapshot(json.dumps(value))
    assert "SYNTHETIC_PRIVATE" not in str(error.value)
