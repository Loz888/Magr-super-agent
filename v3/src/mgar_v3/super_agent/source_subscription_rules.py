"""Pure Source routing/fencing rules. No Engine, model, Worker or network port."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef, TargetRef
from mgar_v3.super_agent.source_subscription_domain import (
    MAX_HANDBACK_CYCLES,
    MAX_PAGE_BYTES,
    HandbackDecision,
    HandbackFacts,
    SourceIntakeSnapshot,
    SourceSubscription,
    SubscriptionAckRequest,
    SubscriptionAnchor,
    SubscriptionDecision,
    SubscriptionHeadRef,
    SubscriptionInputFacts,
    SubscriptionMatches,
    SubscriptionRunFacts,
)

TERMINAL = {"succeeded", "failed", "cancelled"}


def _error(code: str, status: int = 409) -> DomainError:
    return DomainError(code, "Source intake requires current exact reference authority", status)


def _time(at: datetime) -> None:
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise _error("SOURCE_SUBSCRIPTION_TIME_INVALID", 422)


def stable_target_identity(target: TargetRef) -> tuple[Any, ...]:
    """Mutable Thread revisions/epochs are compared at intake, not as lease identity."""
    return (target.project_id, target.ticket_id, target.thread_id, target.mandate_id,
            target.mandate_sha256, target.repository)


def selector_identity(anchor: SubscriptionAnchor) -> tuple[Any, ...]:
    """A renewed seed may change; it cannot silently select another problem or chat."""
    return (anchor.source_instance_id, anchor.scope_sha256, anchor.conversation_sha256,
            anchor.topic_sha256, anchor.resource_key, anchor.correlation_id,
            anchor.selector_kind,
            anchor.seed_entity_sha256 if anchor.selector_kind == "reply_chain" else None)


def match_anchor(anchor: SubscriptionAnchor, facts: SubscriptionInputFacts) -> bool:
    if (anchor.source_instance_id != facts.input.source_instance_id
            or anchor.scope_sha256 != facts.scope_sha256
            or anchor.conversation_sha256 != facts.conversation_sha256):
        return False
    if facts.resource_keys and anchor.resource_key not in facts.resource_keys:
        return False
    if anchor.selector_kind == "resource":
        return anchor.resource_key in facts.resource_keys
    if anchor.selector_kind == "native_topic":
        return anchor.topic_sha256 is not None and anchor.topic_sha256 == facts.topic_sha256
    return facts.reply_entity_sha256 == anchor.seed_entity_sha256


def matching(subscriptions: tuple[SourceSubscription, ...], facts: SubscriptionInputFacts,
             *, at: datetime) -> SubscriptionMatches:
    """Callers first filter current signed Source/policy authority, never choose first."""
    _time(at)
    candidates = tuple(item for item in subscriptions if (
        item.state == "active" and item.valid_from <= at < item.expires_at
        and item.project_id == facts.input.project_id and match_anchor(item.anchor, facts)
    ))
    if len({item.subscription_id for item in candidates}) != len(candidates):
        raise _error("SOURCE_SUBSCRIPTION_DUPLICATE_SELECTOR", 422)
    problems = {(item.anchor.correlation_id, stable_target_identity(item.target))
                for item in candidates}
    ambiguous = len(problems) > 1
    return SubscriptionMatches(
        subscription_ids=() if ambiguous else tuple(sorted(
            (item.subscription_id for item in candidates), key=str,
        )), ambiguous=ambiguous,
        reason_code=("SOURCE_SUBSCRIPTION_AMBIGUOUS" if ambiguous else
                     "SOURCE_SUBSCRIPTION_MATCH" if candidates else "SOURCE_SUBSCRIPTION_NO_MATCH"),
    )


def classify(subscription: SourceSubscription, facts: SubscriptionInputFacts,
             run: SubscriptionRunFacts, *, at: datetime) -> SubscriptionDecision:
    _time(at)
    lane = subscription.role
    purpose = "observer" if lane == "observer" else "context"

    def result(outcome: str, reason: str) -> SubscriptionDecision:
        return SubscriptionDecision.model_validate({
            "subscription_id": subscription.subscription_id,
            "subscription_revision": subscription.revision,
            "input_id": facts.input.input_id, "unit_key": facts.unit_key,
            "outcome": outcome, "lane": lane, "purpose": purpose, "reason_code": reason,
        })

    if facts.input.project_id != subscription.project_id or not match_anchor(
        subscription.anchor, facts,
    ):
        return result("no_match", "SOURCE_SUBSCRIPTION_NO_MATCH")
    if subscription.state != "active":
        return result("blocked", "SOURCE_SUBSCRIPTION_INACTIVE")
    if not subscription.valid_from <= at < subscription.expires_at:
        return result("blocked", "SOURCE_SUBSCRIPTION_EXPIRED")
    if not run.authority_current:
        return result("blocked", "SOURCE_SUBSCRIPTION_AUTHORITY_STALE")
    if not facts.receive_allowed or not facts.read_allowed:
        return result("blocked", "SOURCE_SUBSCRIPTION_RECEIVE_DENIED")
    if not facts.is_current:
        return result("blocked", "SOURCE_SUBSCRIPTION_INPUT_STALE")
    if facts.expires_at <= at:
        return result("blocked", "SOURCE_SUBSCRIPTION_INPUT_EXPIRED")
    if (stable_target_identity(subscription.target) != stable_target_identity(run.target)
            or not run.target.allowed):
        return result("blocked", "SOURCE_SUBSCRIPTION_TARGET_STALE")
    if subscription.bound_run_id != run.target.run_id:
        return result("needs_explicit_rebind", "SOURCE_SUBSCRIPTION_REBIND_REQUIRED")
    if run.policy_mode == "disabled":
        return result("blocked", "SOURCE_SUBSCRIPTION_DISABLED")
    if not run.ticket_open or run.status in TERMINAL:
        if facts.purpose == "status_query" and lane != "observer":
            lane, purpose = "context_only", "status_query"
        return result("closed_followup", "SOURCE_SUBSCRIPTION_CLOSED_FOLLOWUP")
    if run.policy_mode == "shadow":
        return result("would_deliver", "SOURCE_SUBSCRIPTION_SHADOW")
    if lane == "observer":
        return result("observe_ref", "SOURCE_SUBSCRIPTION_OBSERVER")
    if not run.original_primary:
        return result("blocked", "SOURCE_SUBSCRIPTION_PRIMARY_LOST")
    directive = (lane == "primary" and facts.actor_kind == "human"
                 and not facts.context_only and not facts.historical
                 and facts.task_controller and facts.follow_up_allowed
                 and facts.purpose in {"follow_up", "control", "new_work"})
    if directive:
        purpose = "directive_proposal"
        return result("deliver_ref", "SOURCE_SUBSCRIPTION_MATCH")
    lane, purpose = "context_only", "context"
    return result("deliver_ref", "SOURCE_SUBSCRIPTION_CONTEXT_ONLY")


def handback(facts: HandbackFacts) -> HandbackDecision:
    if facts.already_handed_back:
        return HandbackDecision(
            input_id=facts.input_id, allowed=False, reason_code="INPUT_ALREADY_HANDED_BACK",
        )
    if facts.cycle_count >= MAX_HANDBACK_CYCLES:
        return HandbackDecision(
            input_id=facts.input_id, allowed=False, reason_code="HANDBACK_CYCLE_LIMIT",
        )
    return HandbackDecision(input_id=facts.input_id, allowed=True, reason_code="NEEDS_ROUTING")


def validate_ack(
    snapshot: SourceIntakeSnapshot, request: SubscriptionAckRequest, *, run_id: UUID,
    epoch: int, cursor: int, subscription_heads: tuple[SubscriptionHeadRef, ...],
    inputs: tuple[CorrelationInputRef, ...], consumer_state_sha256: str,
    at: datetime, already_acknowledged: bool = False,
) -> None:
    """A previous receipt never permits stale epochs, revived content or cursor jumps."""
    _time(at)
    if (snapshot.run_id != run_id or snapshot.epoch != epoch or request.epoch != epoch
            or snapshot.lane != request.lane):
        raise _error("SOURCE_SUBSCRIPTION_RUN_FENCED")
    if (request.snapshot_id != snapshot.snapshot_id
            or request.snapshot_sha256 != snapshot.snapshot_sha256):
        raise _error("SOURCE_SUBSCRIPTION_SNAPSHOT_INVALID")
    if snapshot.expires_at <= at:
        raise _error("SOURCE_SUBSCRIPTION_SNAPSHOT_EXPIRED")
    if consumer_state_sha256 != snapshot.consumer_state_sha256:
        raise _error("SOURCE_SUBSCRIPTION_CONSUMER_STALE")
    supplied = {item.subscription_id: item for item in subscription_heads}
    expected = {item.subscription_id: item for item in snapshot.subscription_heads}
    if len(supplied) != len(subscription_heads) or supplied != expected:
        raise _error("SOURCE_SUBSCRIPTION_HEAD_STALE")
    current = {item.input_id: item for item in inputs}
    expected_inputs = {item.input.input_id: item.input for item in snapshot.items}
    if len(current) != len(inputs) or current != expected_inputs:
        raise _error("SOURCE_SUBSCRIPTION_INPUT_STALE")
    if any(item.disposition == "ready" and item.expires_at <= at for item in snapshot.items):
        raise _error("SOURCE_SUBSCRIPTION_INPUT_EXPIRED")
    if request.expected_cursor != snapshot.from_cursor:
        raise _error("SOURCE_SUBSCRIPTION_CURSOR_STALE")
    if already_acknowledged and cursor >= snapshot.through_cursor:
        return
    if cursor != snapshot.from_cursor:
        raise _error("SOURCE_SUBSCRIPTION_CURSOR_STALE")


def parse_intake(raw: str | bytes) -> SourceIntakeSnapshot:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON field")
            value[key] = item
        return value

    def constant(value: str) -> None:
        raise ValueError("Non-JSON constant")

    try:
        if type(raw) not in {str, bytes}:
            raise ValueError("JSON required")
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(encoded) > MAX_PAGE_BYTES:
            raise ValueError("Snapshot exceeds bound")
        value = json.loads(encoded, object_pairs_hook=unique, parse_constant=constant)
        return SourceIntakeSnapshot.model_validate_json(json.dumps(value))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _error("SOURCE_SUBSCRIPTION_CONTRACT_INVALID", 422) from None
