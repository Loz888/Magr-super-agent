"""Strict proposal parsing and program-owned decision binding; no I/O or authority.

Create/attach with missing repository/context is rejected, leaving the caller's
candidate pending. An explicit ask proposal can be accepted when currently allowed.
The caller must independently recheck current PG authority before and after a model
call; a valid frozen snapshot or a wrapped would_* decision grants no execution.
"""

import json
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from mgar_v3.domain import DomainError, digest
from mgar_v3.super_agent.source_eval_domain import (
    MAX_EVAL_BYTES,
    SOURCE_DECISION_VERSION,
    SourceAction,
    SourceDecision,
    SourceIntendedAction,
    SourceProposals,
    SourceSnapshot,
)

INTENDED_ACTIONS: dict[SourceAction, SourceIntendedAction] = {
    "ignore": "would_ignore", "observe": "would_observe", "attach": "would_attach",
    "create": "would_create", "ask": "would_ask",
}


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate proposal field")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def parse_proposals(raw: bytes | str) -> SourceProposals:
    """Reject ambiguous JSON before Pydantic can accept a last duplicate field."""
    try:
        if type(raw) not in {bytes, str}:
            raise ValueError("Proposal bytes or text required")
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(encoded) > MAX_EVAL_BYTES:
            raise ValueError("Proposal response exceeds its bound")
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=_constant)
        return SourceProposals.model_validate_json(json.dumps(value))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise DomainError(
            "SOURCE_EVAL_PROPOSALS_INVALID", "Router proposals are invalid", 422
        ) from None


def snapshot_hash(snapshot: SourceSnapshot) -> str:
    return digest(snapshot.model_dump(mode="json"))


def _checked(snapshot: SourceSnapshot, proposals: SourceProposals,
             at: datetime) -> tuple[SourceSnapshot, SourceProposals]:
    try:
        snapshot = SourceSnapshot.model_validate(snapshot.model_dump())
        proposals = SourceProposals.model_validate(proposals.model_dump())
    except ValidationError:
        raise DomainError(
            "SOURCE_EVAL_CONTRACT_INVALID", "Router contract is invalid", 422
        ) from None
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise DomainError("SOURCE_EVAL_TIME_INVALID", "Current time must be timezone-aware", 422)
    if snapshot.expires_at <= at:
        raise DomainError("SOURCE_EVAL_SNAPSHOT_EXPIRED", "Router snapshot has expired")
    groups = {g.group_id: g for g in snapshot.groups}
    if set(groups) != {p.group_id for p in proposals.proposals}:
        raise DomainError(
            "SOURCE_EVAL_BATCH_INCOMPLETE", "One proposal per frozen group is required"
        )
    for proposal in proposals.proposals:
        group = groups[proposal.group_id]
        if proposal.action not in group.allowed_actions:
            raise DomainError(
                "SOURCE_EVAL_ACTION_DENIED", "Action is outside the frozen allowance", 403
            )
        if proposal.action in {"create", "attach"} and (
            group.repository is None or not group.context_complete
        ):
            raise DomainError("SOURCE_EVAL_CONTEXT_REQUIRED", "Repository and context are required")
        if proposal.action == "attach":
            target = next((t for t in group.targets if t.ticket_id == proposal.target_id), None)
            if target is None or not target.allowed:
                raise DomainError(
                    "SOURCE_EVAL_TARGET_DENIED", "Target is outside allowed candidates", 403
                )
        elif proposal.target_id is not None:
            raise DomainError(
                "SOURCE_EVAL_TARGET_DENIED", "This action cannot select a target", 403
            )
    return snapshot, proposals


def validate(snapshot: SourceSnapshot, proposals: SourceProposals, *, at: datetime) -> None:
    _checked(snapshot, proposals, at)


def wrap(snapshot: SourceSnapshot, proposals: SourceProposals, *, at: datetime
         ) -> tuple[SourceDecision, ...]:
    snapshot, proposals = _checked(snapshot, proposals, at)
    proposals_by_group = {p.group_id: p for p in proposals.proposals}
    fingerprint = snapshot_hash(snapshot)
    return tuple(
        SourceDecision(
            decision_key=digest([SOURCE_DECISION_VERSION, str(snapshot.batch_id), str(g.group_id)]),
            batch_id=snapshot.batch_id, group_id=g.group_id, snapshot_sha256=fingerprint,
            fast_batch_id=g.fast_batch_id, fast_batch_revision=g.fast_batch_revision,
            input_ids=tuple(i.input_id for i in g.inputs),
            input_version_fingerprints=tuple(i.version_fingerprint for i in g.inputs),
            intended_action=INTENDED_ACTIONS[proposals_by_group[g.group_id].action],
            reason_code=proposals_by_group[g.group_id].reason_code,
            target=next((t for t in g.targets
                         if t.ticket_id == proposals_by_group[g.group_id].target_id), None),
            expires_at=snapshot.expires_at,
        ) for g in snapshot.groups
    )
