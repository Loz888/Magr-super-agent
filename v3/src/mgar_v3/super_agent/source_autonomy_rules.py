"""Pure SA16 admission and maintenance planning; no IO, grants or business writes."""

from datetime import datetime, timedelta

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_autonomy_domain import (
    AutonomyAdmissionFacts,
    AutonomyAdmissionPlan,
    AutonomyMaintenancePlan,
    AutonomyRule,
    AutonomyRuntimeFacts,
    AutonomySignal,
)


def _time(at: datetime) -> None:
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise DomainError("AUTONOMY_TIME_INVALID", "Current aware server time required", 422)


def plan_admission(
    rule: AutonomyRule,
    signal: AutonomySignal,
    facts: AutonomyAdmissionFacts,
    *,
    at: datetime,
) -> AutonomyAdmissionPlan:
    _time(at)

    def result(disposition: str, reason: str) -> AutonomyAdmissionPlan:
        return AutonomyAdmissionPlan.model_validate(
            {
                "disposition": disposition,
                "reason_code": "AUTONOMY_" + reason,
            }
        )

    if facts.already_claimed:
        return result("duplicate", "ALREADY_CLAIMED")
    if signal.intended_action != "would_create":
        return result("ignored", "NO_NEW_TASK")
    if rule.mode == "disabled":
        return result("ignored", "DISABLED")
    if not rule.valid_from <= at < min(rule.valid_until, signal.expires_at):
        return result("held", "EXPIRED")
    if any(
        (
            facts.rule_id != signal.rule_id,
            facts.rule_revision != signal.rule_revision,
            facts.definition_sha256 != rule.definition_sha256,
            signal.source_instance_id != rule.source_instance_id,
            signal.policy_id != rule.policy_id,
            signal.policy_revision != rule.policy_revision,
            signal.policy_sha256 != rule.policy_sha256,
            signal.repository != rule.repository,
        )
    ):
        return result("held", "SCOPE_CHANGED")
    if not all(
        (
            facts.source_current,
            facts.identity_current,
            facts.policy_current,
            facts.receive_allowed,
            facts.model_input_allowed,
            facts.admission_allowed,
            facts.execution_allowed,
            facts.rule_basis_current,
            facts.canonical_current,
            facts.resource_claim_current,
            facts.profile_current,
        )
    ):
        return result("held", "AUTHORITY_CHANGED")
    if rule.mode == "shadow":
        return result("shadow", "WOULD_ADMIT")
    if signal.origin == "timer" and (
        rule.schedule is None or not rule.schedule.allow_scheduled_work
    ):
        return result("held", "TIMER_WORK_NOT_AUTHORIZED")
    if facts.actual_model_mode == "live" and not facts.real_quality_authorized:
        return result("held", "REAL_QUALITY_GATE_CLOSED")
    if (rule.mode == "auto_investigate") != (facts.profile_mode == "readonly"):
        return result("held", "PROFILE_MODE_MISMATCH")
    if facts.previously_stopped:
        return result("held", "EXPLICIT_REOPEN_REQUIRED")
    if facts.attempts >= rule.max_attempts:
        return result("held", "ATTEMPT_LIMIT")
    if facts.last_admitted_at is not None and at < (
        facts.last_admitted_at + timedelta(seconds=rule.cooldown_seconds)
    ):
        return result("held", "COOLDOWN")
    if facts.active_count >= rule.max_active:
        return result("held", "ACTIVE_LIMIT")
    if facts.usage_unknown:
        return result("held", "USAGE_UNKNOWN")
    if not facts.budget_current:
        return result("held", "BUDGET_WAIT")
    if not facts.factory_configured:
        return result("held", "EXECUTOR_NOT_CONFIGURED")
    return result("admit", "CURRENT_RULE_ACCEPTED")


def plan_maintenance(
    rule: AutonomyRule,
    facts: AutonomyRuntimeFacts,
    *,
    at: datetime,
) -> AutonomyMaintenancePlan:
    _time(at)
    pending = facts.unknown_usage or facts.resource_unconfirmed
    if not facts.authority_current or rule.mode in {"disabled", "shadow"}:
        return AutonomyMaintenancePlan(control="stop", reason_code="AUTONOMY_AUTHORITY_REVOKED")
    if at >= min(facts.expires_at, rule.valid_until):
        return AutonomyMaintenancePlan(control="stop", reason_code="AUTONOMY_TASK_EXPIRED")
    if facts.original_run_state in {"succeeded", "failed", "cancelled"}:
        return AutonomyMaintenancePlan(
            control="hold" if pending else "release",
            reason_code="AUTONOMY_TERMINAL_PENDING" if pending else "AUTONOMY_ORIGINAL_TERMINAL",
            terminal_success=(
                not pending
                and facts.original_run_state == "succeeded"
                and facts.original_result_known
                and facts.original_result_passed
            ),
            may_release_resource=not pending,
        )
    if pending:
        return AutonomyMaintenancePlan(control="hold", reason_code="AUTONOMY_UNCONFIRMED_RESOURCE")
    if facts.original_run_state in {"waiting_input", "waiting_approval", "paused"}:
        return AutonomyMaintenancePlan(
            control="release", reason_code="AUTONOMY_WAITING", may_release_resource=True
        )
    progress = facts.semantic_progress_at or facts.created_at
    if progress > at:
        return AutonomyMaintenancePlan(control="hold", reason_code="AUTONOMY_PROGRESS_NOT_PROVEN")
    if at >= progress + timedelta(seconds=rule.idle_ttl_seconds):
        return AutonomyMaintenancePlan(control="stop", reason_code="AUTONOMY_NO_SEMANTIC_PROGRESS")
    return AutonomyMaintenancePlan(control="none", reason_code="AUTONOMY_ACTIVE")
