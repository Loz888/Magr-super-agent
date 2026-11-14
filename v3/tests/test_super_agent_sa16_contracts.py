"""First SA16 rule/planner/DST checks. These do not create tasks or grant authority."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_autonomy_domain import (
    AutonomyAdmissionFacts,
    AutonomyRule,
    AutonomyRuleSave,
    AutonomyRuntimeFacts,
    AutonomySignal,
    DailySignal,
)
from mgar_v3.super_agent.source_autonomy_rules import plan_admission, plan_maintenance
from mgar_v3.super_agent.source_autonomy_schedule import latest_due
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
SOURCE, RULE, POLICY = uuid4(), uuid4(), uuid4()
INPUT = CorrelationInputRef(
    input_id=uuid4(),
    source_instance_id=SOURCE,
    project_id="orders-demo",
    version_fingerprint="a" * 64,
    state_sha256="b" * 64,
)


def rule(**updates):
    values = dict(
        source_instance_id=SOURCE,
        policy_id=POLICY,
        policy_revision=1,
        policy_sha256="c" * 64,
        rule_ref="owner-reviewed-team-rule",
        rule_sha256="d" * 64,
        repository="example/mgar-test",
        mode="auto_repair",
        profile_sha256="e" * 64,
        objective_sha256="f" * 64,
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=6),
    )
    values.update(updates)
    return AutonomyRule(**values)


def signal(**updates):
    values = dict(
        key="a" * 64,
        subject_key="b" * 64,
        rule_id=RULE,
        rule_revision=1,
        source_instance_id=SOURCE,
        policy_id=POLICY,
        policy_revision=1,
        policy_sha256="c" * 64,
        repository="example/mgar-test",
        intended_action="would_create",
        inputs=(INPUT,),
        expires_at=NOW + timedelta(minutes=1),
    )
    values.update(updates)
    return AutonomySignal(**values)


def facts(current_rule=None, **updates):
    r = current_rule or rule()
    values = dict(
        rule_id=RULE,
        rule_revision=1,
        definition_sha256=r.definition_sha256,
        source_current=True,
        identity_current=True,
        policy_current=True,
        receive_allowed=True,
        model_input_allowed=True,
        admission_allowed=True,
        execution_allowed=True,
        rule_basis_current=True,
        canonical_current=True,
        resource_claim_current=True,
        profile_current=True,
        profile_mode="coding",
        actual_model_mode="fixture",
        budget_current=True,
        factory_configured=True,
    )
    values.update(updates)
    return AutonomyAdmissionFacts(**values)


@pytest.mark.parametrize(
    "mode,profile,expected",
    [
        ("disabled", "coding", "ignored"),
        ("shadow", "coding", "shadow"),
        ("auto_repair", "coding", "admit"),
        ("auto_investigate", "readonly", "admit"),
        ("auto_investigate", "coding", "held"),
        ("auto_repair", "readonly", "held"),
    ],
)
def test_modes_never_expand_profile_or_send_messages(mode, profile, expected):
    r = rule(mode=mode)
    p = plan_admission(r, signal(), facts(r, profile_mode=profile), at=NOW)
    assert p.disposition == expected
    assert p.creates_authority is False and p.sends_notification is False


@pytest.mark.parametrize(
    "field",
    [
        "source_current",
        "identity_current",
        "policy_current",
        "receive_allowed",
        "model_input_allowed",
        "admission_allowed",
        "execution_allowed",
        "rule_basis_current",
        "canonical_current",
        "resource_claim_current",
        "profile_current",
    ],
)
def test_each_current_authority_fact_is_required(field):
    p = plan_admission(rule(), signal(), facts(**{field: False}), at=NOW)
    assert p.disposition == "held" and p.reason_code == "AUTONOMY_AUTHORITY_CHANGED"


@pytest.mark.parametrize(
    "field,value", [("rule_id", uuid4()), ("rule_revision", 2), ("definition_sha256", "1" * 64)]
)
def test_facts_cannot_switch_signed_rule(field, value):
    assert (
        plan_admission(rule(), signal(), facts(**{field: value}), at=NOW).reason_code
        == "AUTONOMY_SCOPE_CHANGED"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_instance_id", uuid4()),
        ("policy_id", uuid4()),
        ("policy_revision", 2),
        ("policy_sha256", "1" * 64),
        ("repository", "other/private"),
    ],
)
def test_signal_scope_must_match_rule(field, value):
    s = signal().model_copy(update={field: value})
    assert plan_admission(rule(), s, facts(), at=NOW).reason_code == "AUTONOMY_SCOPE_CHANGED"


@pytest.mark.parametrize("action", ["would_ignore", "would_observe", "would_attach", "would_ask"])
def test_non_create_decisions_never_allocate_a_worker(action):
    assert (
        plan_admission(rule(), signal(intended_action=action), facts(), at=NOW).disposition
        == "ignored"
    )


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"already_claimed": True}, "ALREADY_CLAIMED"),
        ({"previously_stopped": True}, "EXPLICIT_REOPEN_REQUIRED"),
        ({"attempts": 1}, "ATTEMPT_LIMIT"),
        ({"last_admitted_at": NOW - timedelta(seconds=1)}, "COOLDOWN"),
        ({"active_count": 1}, "ACTIVE_LIMIT"),
        ({"usage_unknown": True}, "USAGE_UNKNOWN"),
        ({"budget_current": False}, "BUDGET_WAIT"),
        ({"factory_configured": False}, "EXECUTOR_NOT_CONFIGURED"),
        ({"actual_model_mode": "live"}, "REAL_QUALITY_GATE_CLOSED"),
    ],
)
def test_bounded_retries_cooldown_unknown_budget_and_executor_wait(updates, reason):
    p = plan_admission(rule(), signal(), facts(**updates), at=NOW)
    assert p.reason_code == "AUTONOMY_" + reason and p.disposition != "admit"


def test_exact_ttl_and_cooldown_boundaries():
    r = rule()
    assert (
        plan_admission(
            r, signal(), facts(last_admitted_at=NOW - timedelta(seconds=300)), at=NOW
        ).disposition
        == "admit"
    )
    assert (
        plan_admission(r, signal(expires_at=NOW), facts(), at=NOW).reason_code == "AUTONOMY_EXPIRED"
    )
    with pytest.raises(DomainError):
        plan_admission(r, signal(), facts(), at=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "field",
    [
        "actor",
        "provider",
        "token",
        "trusted",
        "budget_reset",
        "notification",
        "paid",
        "execution_mode",
        "task_succeeded",
        "factory",
        "mandate",
    ],
)
def test_rule_json_rejects_runtime_or_authority_fields(field):
    value = rule().model_dump(mode="json")
    value[field] = True
    with pytest.raises(ValidationError):
        AutonomyRule.model_validate_json(__import__("json").dumps(value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("cooldown_seconds", 0),
        ("cooldown_seconds", True),
        ("task_ttl_seconds", 29),
        ("task_ttl_seconds", 86401),
        ("idle_ttl_seconds", 0),
        ("max_active", 0),
        ("max_active", 17),
        ("max_attempts", 4),
        ("repository", "Other/Private"),
        ("policy_revision", True),
        ("valid_until", NOW - timedelta(days=2)),
    ],
)
def test_rule_limits_and_aware_expiry_are_strict(field, value):
    with pytest.raises(ValidationError):
        rule(**{field: value})


def test_invalid_rule_duration_idle_ttl_source_and_schedule():
    with pytest.raises(ValidationError):
        rule(valid_until=NOW + timedelta(days=8))
    with pytest.raises(ValidationError):
        rule(task_ttl_seconds=30, idle_ttl_seconds=31)
    with pytest.raises(ValidationError):
        DailySignal(timezone="invalid/zone", at="12:00")
    with pytest.raises(ValidationError):
        DailySignal(timezone="UTC", at="12:00", weekdays=(True,))
    with pytest.raises(ValidationError):
        DailySignal(timezone="UTC", at="12:00", allow_scheduled_work=True)
    other = INPUT.model_copy(update={"source_instance_id": uuid4()})
    with pytest.raises(ValidationError):
        rule(schedule=DailySignal(timezone="UTC", at="12:00", seed_input=other))


def test_bodyless_rule_review_is_explicit_and_immutable():
    r = rule()
    request = AutonomyRuleSave(expected_revision=0, definition=r, review_ref="exact-rule-review")
    assert request.definition.definition_sha256 == r.definition_sha256
    with pytest.raises(ValidationError):
        request.expected_revision = 1
    with pytest.raises(ValidationError):
        AutonomyRuleSave(expected_revision=-1, definition=r, review_ref="x")


def runtime(**updates):
    values = dict(
        original_run_state="running",
        authority_current=True,
        created_at=NOW - timedelta(seconds=30),
        expires_at=NOW + timedelta(minutes=5),
    )
    values.update(updates)
    return AutonomyRuntimeFacts(**values)


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
def test_unknown_work_or_usage_never_releases_terminal_resource(state):
    p = plan_maintenance(
        rule(), runtime(original_run_state=state, resource_unconfirmed=True), at=NOW
    )
    assert p.control == "hold" and not p.may_release_resource and not p.discard_unknown_or_refund


@pytest.mark.parametrize("state", ["waiting_input", "waiting_approval", "paused"])
def test_waiting_releases_only_proven_quiet_resources(state):
    p = plan_maintenance(rule(), runtime(original_run_state=state), at=NOW)
    assert p.control == "release" and p.may_release_resource
    assert (
        plan_maintenance(
            rule(), runtime(original_run_state=state, unknown_usage=True), at=NOW
        ).control
        == "hold"
    )


@pytest.mark.parametrize(
    "known,passed,success", [(False, False, False), (True, False, False), (True, True, True)]
)
def test_success_requires_original_receipt_not_summary(known, passed, success):
    p = plan_maintenance(
        rule(),
        runtime(
            original_run_state="succeeded",
            original_result_known=known,
            original_result_passed=passed,
        ),
        at=NOW,
    )
    assert p.terminal_success is success


def test_rule_disable_expiry_and_semantic_idle_stop_without_resetting_unknown():
    for r, f in [
        (rule(mode="disabled"), runtime()),
        (rule(), runtime(authority_current=False)),
        (rule(), runtime(expires_at=NOW)),
        (rule(), runtime(created_at=NOW - timedelta(seconds=300))),
    ]:
        p = plan_maintenance(r, f, at=NOW)
        assert p.control == "stop" and not p.discard_unknown_or_refund
    assert (
        plan_maintenance(
            rule(), runtime(semantic_progress_at=NOW + timedelta(seconds=1)), at=NOW
        ).control
        == "hold"
    )
    assert plan_maintenance(rule(), runtime(semantic_progress_at=NOW), at=NOW).control == "none"


def test_timer_defaults_to_signal_without_worker_authority():
    r = rule(schedule=DailySignal(timezone="UTC", at="11:00"))
    s = signal(origin="timer")
    assert (
        plan_admission(r, s, facts(r), at=NOW).reason_code == "AUTONOMY_TIMER_WORK_NOT_AUTHORIZED"
    )
    r = rule(
        schedule=DailySignal(
            timezone="UTC", at="11:00", allow_scheduled_work=True, seed_input=INPUT
        )
    )
    assert plan_admission(r, s, facts(r), at=NOW).disposition == "admit"


def test_due_tick_once_daily_and_bounded_restart_catchup():
    r = rule(
        valid_from=NOW - timedelta(days=2),
        valid_until=NOW + timedelta(days=5),
        schedule=DailySignal(timezone="UTC", at="11:00"),
    )
    due = latest_due(
        r, rule_id=str(RULE), signed_at=NOW - timedelta(days=2), now=NOW, last_local_date=None
    )
    assert due.local_date == date(2026, 11, 14) and due.missed_coalesced == 1
    assert (
        latest_due(
            r,
            rule_id=str(RULE),
            signed_at=NOW - timedelta(days=2),
            now=NOW,
            last_local_date=due.local_date,
        )
        is None
    )
    assert (
        latest_due(rule(), rule_id=str(RULE), signed_at=NOW, now=NOW, last_local_date=None) is None
    )
    assert latest_due(r, rule_id=str(RULE), signed_at=NOW, now=NOW, last_local_date=None) is None


def test_fall_back_repeated_hour_is_one_local_day_signal():
    now = datetime(2026, 11, 1, 6, 45, tzinfo=UTC)
    r = rule(
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=1),
        schedule=DailySignal(timezone="America/New_York", at="01:30"),
    )
    due = latest_due(
        r,
        rule_id=str(RULE),
        signed_at=now - timedelta(days=1),
        now=now,
        last_local_date=date(2026, 10, 31),
    )
    assert due.scheduled_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert (
        latest_due(
            r,
            rule_id=str(RULE),
            signed_at=now - timedelta(days=1),
            now=now,
            last_local_date=due.local_date,
        )
        is None
    )


def test_spring_forward_nonexistent_local_time_is_not_invented():
    now = datetime(2026, 3, 8, 8, tzinfo=UTC)
    r = rule(
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=1),
        schedule=DailySignal(timezone="America/New_York", at="02:30"),
    )
    assert (
        latest_due(
            r,
            rule_id=str(RULE),
            signed_at=now - timedelta(days=1),
            now=now,
            last_local_date=date(2026, 3, 7),
        )
        is None
    )
