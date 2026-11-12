"""Strict Source proposals and bodyless snapshots; no database or model invocation."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import ActorRef, SourceScope
from mgar_v3.super_agent.budget_domain import PriceQuote
from mgar_v3.super_agent.source_eval_domain import (
    MAX_EVAL_BYTES,
    REASONS,
    SOURCE_PROPOSALS_VERSION,
    CanonicalInputRef,
    ModelContext,
    ModelContextGroup,
    SourceDecision,
    SourceEvalConfigure,
    SourceEvalProfile,
    SourceEvalSettings,
    SourceEvaluateRequest,
    SourceProposal,
    SourceProposals,
    SourceSnapshot,
    SourceSnapshotGroup,
    SourceTargetRef,
)
from mgar_v3.super_agent.source_eval_rules import parse_proposals, snapshot_hash, validate, wrap

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)


def snapshot():
    scope = SourceScope(
        source_kind="feishu", source_instance_id=uuid4(),
        tenant_id="Synthetic-tenant", installation_id="Synthetic-installation",
    )
    actor = ActorRef(scope=scope, id_type="open_id", actor_id="Alice", actor_kind="human")
    return SourceSnapshot(
        batch_id=uuid4(), project_id="orders-demo", scope=scope,
        source_revision=1, generation=1, fast_config_revision=1, eval_config_revision=1,
        policy_id=uuid4(), policy_revision=1, prompt_version="synthetic-router-v1",
        prompt_sha256="a" * 64, profile_sha256="b" * 64, price_sha256="c" * 64,
        budget_snapshot_sha256="9" * 64,
        expires_at=NOW + timedelta(seconds=60), groups=(SourceSnapshotGroup(
            group_id=uuid4(), fast_batch_id=uuid4(), fast_batch_revision=1,
            topic_key="d" * 64, actors=(actor,), inputs=(CanonicalInputRef(
                input_id=uuid4(), version_fingerprint="e" * 64,
                identity_id=uuid4(), identity_revision=1, actor_key=actor.key,
            ),), targets=(SourceTargetRef(
                ticket_id=uuid4(), run_id=uuid4(), thread_id=uuid4(), thread_revision=2,
                execution_epoch=3, mandate_id=uuid4(), mandate_sha256="f" * 64,
                project_id="orders-demo", repository="example/mgar-test", allowed=True,
            ),), repository="example/mgar-test", context_complete=True,
            allowed_actions=("ignore", "observe", "attach", "create", "ask"),
        ),),
    )


def proposal(value, action="create", **changes):
    group = value.groups[0]
    return SourceProposal(
        group_id=group.group_id, action=action,
        target_id=changes.pop(
            "target_id", group.targets[0].ticket_id if action == "attach" else None
        ),
        reason_code=REASONS[action], **changes,
    )


def proposals(value, action="create", **changes):
    return SourceProposals(
        schema_version=SOURCE_PROPOSALS_VERSION, proposals=(proposal(value, action, **changes),),
    )


def raw(value, action="create"):
    return proposals(value, action).model_dump(mode="json")


@pytest.mark.parametrize("action", ["ignore", "observe", "attach", "create", "ask"])
def test_supported_actions_wrap_as_bound_hypothetical_decisions(action):
    value = snapshot()
    original = value.model_dump_json()
    parsed = parse_proposals(json.dumps(raw(value, action)))
    validate(value, parsed, at=NOW)
    decision, = wrap(value, parsed, at=NOW)
    group = value.groups[0]
    assert decision.intended_action == "would_" + action
    assert decision.reason_code == REASONS[action]
    assert decision.snapshot_sha256 == snapshot_hash(value)
    assert decision.input_ids == (group.inputs[0].input_id,)
    assert decision.input_version_fingerprints == (group.inputs[0].version_fingerprint,)
    assert decision.fast_batch_id == group.fast_batch_id
    assert decision.fast_batch_revision == group.fast_batch_revision
    assert decision.target == (group.targets[0] if action == "attach" else None)
    assert decision.eligible_for_execution is False
    assert wrap(value, parsed, at=NOW) == (decision,)
    assert value.model_dump_json() == original


@pytest.mark.parametrize("field", [
    "body", "rationale", "actor", "policy_revision", "budget", "execution_epoch",
    "eligible_for_execution", "prompt_version", "mandate_id",
])
def test_model_cannot_supply_body_or_authority_fields(field):
    data = raw(snapshot())
    data["proposals"][0][field] = "a private echo must not appear in the error"
    with pytest.raises(DomainError) as error:
        parse_proposals(json.dumps(data))
    assert error.value.code == "SOURCE_EVAL_PROPOSALS_INVALID"
    assert "private echo" not in str(error.value)


@pytest.mark.parametrize("action", ["bash", "repair", "send", "cancel", "approve", "publish"])
def test_model_cannot_request_execution_control_or_publication(action):
    data = raw(snapshot())
    data["proposals"][0]["action"] = action
    with pytest.raises(DomainError, match="Router proposals are invalid"):
        parse_proposals(json.dumps(data))


@pytest.mark.parametrize("change", ["missing-version", "other-version", "extra-version-fields"])
def test_explicit_exact_contract_version_is_required(change):
    data = raw(snapshot())
    if change == "missing-version":
        del data["schema_version"]
    elif change == "other-version":
        data["schema_version"] = "router-legacy-v1"
    else:
        data["actor"] = "administrator"
    with pytest.raises(DomainError):
        parse_proposals(json.dumps(data))


@pytest.mark.parametrize("payload", [
    b"\xff", b"[]", b"null", b"{}", b"```json\n{}\n```",
    b'{"schema_version":"other","schema_version":"source-router-proposals-v1"}',
    b'{"schema_version":"source-router-proposals-v1","proposals":NaN}',
    b"[" * 2000 + b"]" * 2000,
])
def test_ambiguous_or_malformed_json_cannot_become_a_proposal(payload):
    with pytest.raises(DomainError) as error:
        parse_proposals(payload)
    assert error.value.code == "SOURCE_EVAL_PROPOSALS_INVALID"


@pytest.mark.parametrize("case", ["duplicate-action", "duplicate-group", "reason", "empty", "many"])
def test_proposals_reject_ambiguous_actions_duplicate_groups_and_unbounded_lists(case):
    data = raw(snapshot())
    if case == "duplicate-action":
        payload = json.dumps(data).replace(
            '"action": "create"', '"action":"ignore","action":"create"'
        )
    else:
        if case == "duplicate-group":
            data["proposals"] *= 2
        elif case == "reason":
            data["proposals"][0]["reason_code"] = "IRRELEVANT"
        elif case == "empty":
            data["proposals"] = []
        else:
            data["proposals"] *= 17
        payload = json.dumps(data)
    with pytest.raises(DomainError):
        parse_proposals(payload)


@pytest.mark.parametrize("payload", [b" " * (MAX_EVAL_BYTES + 1), "界" * 21846, "\ud800"])
def test_response_bound_uses_utf8_bytes_and_rejects_invalid_unicode(payload):
    with pytest.raises(DomainError):
        parse_proposals(payload)


@pytest.mark.parametrize("case", ["unknown", "missing", "extra"])
def test_complete_exact_frozen_group_membership_is_required(case):
    value = snapshot()
    p = proposal(value)
    if case == "extra":
        supplied = (p, p.model_copy(update={"group_id": uuid4()}))
    else:
        supplied = (p.model_copy(update={"group_id": uuid4()}),)
        if case == "missing":
            other = value.groups[0].model_copy(update={
                "group_id": uuid4(), "fast_batch_id": uuid4(),
                "inputs": (value.groups[0].inputs[0].model_copy(update={"input_id": uuid4()}),),
            })
            value = value.model_copy(update={"groups": (*value.groups, other)})
            supplied = (p,)
    with pytest.raises(DomainError) as error:
        wrap(value, SourceProposals(
            schema_version=SOURCE_PROPOSALS_VERSION, proposals=supplied,
        ), at=NOW)
    assert error.value.code == "SOURCE_EVAL_BATCH_INCOMPLETE"


@pytest.mark.parametrize("case", ["unknown", "none", "denied"])
def test_attach_requires_one_supplied_allowed_target(case):
    value = snapshot()
    target_id = value.groups[0].targets[0].ticket_id
    if case == "denied":
        group = value.groups[0]
        value = value.model_copy(update={"groups": (group.model_copy(update={
            "targets": (group.targets[0].model_copy(update={"allowed": False}),),
        }),)})
    else:
        target_id = uuid4() if case == "unknown" else None
    with pytest.raises(DomainError) as error:
        wrap(value, proposals(value, "attach", target_id=target_id), at=NOW)
    assert error.value.code == "SOURCE_EVAL_TARGET_DENIED"


@pytest.mark.parametrize("action", ["ignore", "observe", "create", "ask"])
def test_other_actions_cannot_borrow_a_target_reference(action):
    value = snapshot()
    with pytest.raises(DomainError) as error:
        wrap(value, proposals(
            value, action, target_id=value.groups[0].targets[0].ticket_id,
        ), at=NOW)
    assert error.value.code == "SOURCE_EVAL_TARGET_DENIED"


@pytest.mark.parametrize("action", ["ignore", "attach", "create", "ask"])
def test_program_frozen_action_allowance_is_rechecked(action):
    value = snapshot()
    value = value.model_copy(update={"groups": (
        value.groups[0].model_copy(update={"allowed_actions": ("observe",)}),
    )})
    with pytest.raises(DomainError) as error:
        wrap(value, proposals(value, action), at=NOW)
    assert error.value.code == "SOURCE_EVAL_ACTION_DENIED"


@pytest.mark.parametrize("action", ["create", "attach"])
@pytest.mark.parametrize("missing", ["repository", "context"])
def test_missing_context_is_rejected_without_silently_rewriting_the_action(action, missing):
    value = snapshot()
    original = proposals(value, action)
    group = value.groups[0].model_copy(update={
        "repository": None, "targets": (),
    } if missing == "repository" else {"context_complete": False})
    value = value.model_copy(update={"groups": (group,)})
    with pytest.raises(DomainError) as error:
        wrap(value, original, at=NOW)
    assert error.value.code == "SOURCE_EVAL_CONTEXT_REQUIRED"
    assert original.proposals[0].action == action


def test_explicit_ask_can_remain_when_repository_and_context_are_missing():
    value = snapshot()
    value = value.model_copy(update={"groups": (value.groups[0].model_copy(update={
        "repository": None, "context_complete": False, "targets": (),
    }),)})
    assert wrap(value, proposals(value, "ask"), at=NOW)[0].intended_action == "would_ask"


@pytest.mark.parametrize("offset", [0, 1])
def test_expired_snapshot_is_rejected_at_the_half_open_boundary(offset):
    value = snapshot()
    with pytest.raises(DomainError) as error:
        wrap(value, proposals(value), at=value.expires_at + timedelta(seconds=offset))
    assert error.value.code == "SOURCE_EVAL_SNAPSHOT_EXPIRED"


def test_current_time_must_be_timezone_aware():
    value = snapshot()
    with pytest.raises(DomainError) as error:
        wrap(value, proposals(value), at=NOW.replace(tzinfo=None))
    assert error.value.code == "SOURCE_EVAL_TIME_INVALID"


@pytest.mark.parametrize("case", [
    "source-scope", "project", "actor", "unknown-author", "duplicate-input", "duplicate-batch",
    "duplicate-group", "target-project", "target-repository", "rule-reference", "rule-hash",
])
def test_program_snapshots_reject_cross_scope_and_ambiguous_authority(case):
    value = snapshot()
    data = value.model_dump(mode="json")
    group = data["groups"][0]
    if case == "source-scope":
        data["scope"]["installation_id"] = "Other-installation"
    elif case == "project":
        data["project_id"] = "other-project"
    elif case == "actor":
        group["actors"].append(group["actors"][0])
    elif case == "unknown-author":
        group["inputs"][0]["actor_key"] = "0" * 64
    elif case == "duplicate-input":
        group["inputs"].append(group["inputs"][0])
    elif case in {"duplicate-batch", "duplicate-group"}:
        other = json.loads(json.dumps(group))
        other["inputs"][0]["input_id"] = str(uuid4())
        other["group_id"] = str(uuid4()) if case == "duplicate-batch" else group["group_id"]
        other["fast_batch_id"] = (
            group["fast_batch_id"] if case == "duplicate-batch" else str(uuid4())
        )
        data["groups"].append(other)
    elif case == "target-project":
        group["targets"][0]["project_id"] = "foreign-project"
    elif case == "target-repository":
        group["targets"][0]["repository"] = "foreign/repository"
    elif case == "rule-reference":
        data["rule_ref"] = "signed-rule"
    else:
        data["rule_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        SourceSnapshot.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("change", [
    {"source_revision": True}, {"generation": "1"}, {"policy_revision": 0},
    {"fast_config_revision": -1}, {"eval_config_revision": 1.5},
    {"prompt_sha256": "A" * 64}, {"expires_at": 12345}, {"expires_at": "2026-11-12T12:00:00"},
    {"body": "source body"}, {"budget": 1},
])
def test_snapshot_revisions_times_hashes_and_bodyless_contract_are_strict(change):
    data = snapshot().model_dump(mode="json")
    data.update(change)
    with pytest.raises(ValidationError):
        SourceSnapshot.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("field", [
    "source_revision", "generation", "fast_config_revision", "eval_config_revision",
    "policy_revision", "prompt_version", "prompt_sha256", "profile_sha256", "price_sha256",
    "budget_snapshot_sha256",
    "input_version", "identity_revision", "target_epoch", "target_thread_revision",
])
def test_decision_snapshot_hash_binds_all_relevant_versions(field):
    value = snapshot()
    data = value.model_dump(mode="json")
    if field == "input_version":
        data["groups"][0]["inputs"][0]["version_fingerprint"] = "0" * 64
    elif field == "identity_revision":
        data["groups"][0]["inputs"][0]["identity_revision"] += 1
    elif field.startswith("target_"):
        key = "execution_epoch" if field == "target_epoch" else "thread_revision"
        data["groups"][0]["targets"][0][key] += 1
    elif field.endswith("sha256"):
        data[field] = "0" * 64
    elif field == "prompt_version":
        data[field] = "other-prompt"
    else:
        data[field] += 1
    changed = SourceSnapshot.model_validate_json(json.dumps(data))
    assert snapshot_hash(changed) != snapshot_hash(value)
    assert wrap(changed, proposals(changed), at=NOW)[0].snapshot_sha256 == snapshot_hash(changed)


def test_context_is_separate_from_persistable_snapshot_and_bounded_across_groups():
    value = snapshot()
    secret = "ephemeral authorized source body"
    context = ModelContext(groups=(ModelContextGroup(
        group_id=value.groups[0].group_id, text=secret,
    ),))
    assert context.groups[0].text == secret
    assert secret not in value.model_dump_json()
    assert secret not in wrap(value, proposals(value), at=NOW)[0].model_dump_json()
    assert ModelContext(groups=(ModelContextGroup(group_id=uuid4(), text="x" * 65536),))
    with pytest.raises(ValidationError):
        ModelContext(groups=(ModelContextGroup(group_id=uuid4(), text="x" * 32769),
                             ModelContextGroup(group_id=uuid4(), text="x" * 32768)))


@pytest.mark.parametrize("text", ["界" * 21846, "\ud800"])
def test_ephemeral_context_checks_utf8_bytes_instead_of_only_character_count(text):
    with pytest.raises(ValidationError):
        ModelContext(groups=(ModelContextGroup(group_id=uuid4(), text=text),))


def test_duplicate_context_groups_are_not_accepted():
    group = ModelContextGroup(group_id=uuid4(), text="data")
    with pytest.raises(ValidationError):
        ModelContext(groups=(group, group))


@pytest.mark.parametrize("value", [True, 0, 1, "false"])
def test_a_decision_cannot_claim_execution_eligibility(value):
    s = snapshot()
    decision = wrap(s, proposals(s), at=NOW)[0].model_dump()
    decision["eligible_for_execution"] = value
    with pytest.raises(ValidationError):
        SourceDecision.model_validate(decision)


@pytest.mark.parametrize("change", [
    {"enabled": 1}, {"enabled": True}, {"decision_ttl_seconds": 0},
    {"decision_ttl_seconds": 301}, {"max_context_bytes": 65537},
    {"max_output_bytes": True}, {"max_pending": 10001}, {"allow_paid": True},
])
def test_owner_settings_are_default_disabled_and_bounded_without_payment_authority(change):
    assert SourceEvalSettings().enabled is False
    with pytest.raises(ValidationError):
        SourceEvalSettings(**change)


def test_owner_configuration_binds_explicit_profile_price_prompt_and_cas():
    profile = SourceEvalProfile(
        provider_id="controlled", profile_ref="synthetic-only",
        price=PriceQuote(model_id="fixture-model", price_version="fixture-v1",
                         input_micros_per_million=0, output_micros_per_million=0),
        prompt_version="source-prompt-v1", prompt_sha256="a" * 64,
    )
    configured = SourceEvalConfigure(
        expected_revision=0, settings=SourceEvalSettings(enabled=True, profile=profile),
        review_ref="synthetic-owner-review",
    )
    assert configured.settings.profile.price.price_version == "fixture-v1"
    with pytest.raises(ValidationError):
        SourceEvalConfigure(
            expected_revision=True, settings=SourceEvalSettings(), review_ref="test",
        )


@pytest.mark.parametrize("case", ["body", "actor", "duplicate", "empty", "too-many"])
def test_evaluation_request_only_accepts_distinct_bounded_existing_batch_ids(case):
    data = {"fast_batch_ids": [str(uuid4())]}
    if case in {"body", "actor"}:
        data[case] = "untrusted caller assertion"
    elif case == "duplicate":
        data["fast_batch_ids"] *= 2
    elif case == "empty":
        data["fast_batch_ids"] = []
    else:
        data["fast_batch_ids"] = [str(uuid4()) for _ in range(17)]
    with pytest.raises(ValidationError):
        SourceEvaluateRequest.model_validate_json(json.dumps(data))


def test_program_copy_bypass_cannot_skip_revalidation_at_wrap_boundary():
    value = snapshot().model_copy(update={"policy_revision": True})
    with pytest.raises(DomainError) as error:
        wrap(value, proposals(value), at=NOW)
    assert error.value.code == "SOURCE_EVAL_CONTRACT_INVALID"
