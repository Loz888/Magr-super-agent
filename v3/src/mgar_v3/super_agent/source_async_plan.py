"""Build immutable metadata safe points; the original Engine applies them in PG.

This planner neither reads Source text nor performs authorization. Its facts must
be assembled by the Engine from its held connection inside the committing txn.
"""

from datetime import datetime, timedelta
from uuid import UUID

from mgar_v3.domain import DomainError, digest
from mgar_v3.super_agent.source_async_domain import (
    AsyncCheckpoint,
    AsyncCurrentFacts,
    AsyncInputFacts,
    AsyncInputReceipt,
    AsyncModel,
    SafePointKind,
)
from mgar_v3.super_agent.source_async_rules import classify_input
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef


class PendingInput(AsyncModel):
    receipt: AsyncInputReceipt
    facts: AsyncInputFacts


class BlockedInput(AsyncModel):
    input_id: UUID
    reason_code: str


class SafePointPlan(AsyncModel):
    checkpoint: AsyncCheckpoint
    applied_ids: tuple[UUID, ...]
    blocked: tuple[BlockedInput, ...]


def _deny(code: str) -> DomainError:
    return DomainError(code, "A complete current original input snapshot is required", 409)


def plan_safe_point(
    *,
    checkpoint_id: UUID,
    current: AsyncCurrentFacts,
    stage: SafePointKind,
    at: datetime,
    received_sequence: int,
    pending: tuple[PendingInput, ...],
    previous: AsyncCheckpoint | None = None,
    retained_facts: tuple[AsyncInputFacts, ...] = (),
) -> SafePointPlan:
    """Require a complete contiguous tail and revalidate every retained Source ref.

    Persistence must CAS the same binding revision and input revision, insert the
    checkpoint and mark these dispositions in one original Engine transaction.
    Model/tool dispatch must still re-read current facts at the actual boundary.
    """
    if at.utcoffset() is None or not all(
        (
            current.original_coding_current,
            current.assignment_current,
            current.session_current,
            current.sources_current,
            current.budget_current,
            current.active,
            not current.pending_invalidation,
        )
    ):
        raise _deny("SOURCE_ASYNC_AUTHORITY_FENCED")
    if current.expires_at <= at:
        raise _deny("SOURCE_ASYNC_VIEW_EXPIRED")
    if (
        type(received_sequence) is not int
        or received_sequence < 0
        or len(pending) > 256
        or len(retained_facts) > 64
    ):
        raise _deny("SOURCE_ASYNC_INPUT_SNAPSHOT_INVALID")
    through = 0
    restriction = current.restriction
    if previous is not None:
        if (
            previous.async_id != current.async_id
            or previous.scope != current.scope
            or previous.input_revision > current.input_revision
            or previous.restriction.narrow(restriction) != restriction
        ):
            raise _deny("SOURCE_ASYNC_SCOPE_FENCED")
        through = previous.through_sequence
    if received_sequence != through + len(pending):
        raise _deny("SOURCE_ASYNC_INPUT_GAP")
    if any(item.receipt.sequence != through + index + 1 for index, item in enumerate(pending)):
        raise _deny("SOURCE_ASYNC_INPUT_GAP")
    identifiers = [item.receipt.input_id for item in pending]
    if len(identifiers) != len(set(identifiers)):
        raise _deny("SOURCE_ASYNC_INPUT_DUPLICATE")
    dependencies: dict[UUID, CorrelationInputRef] = {}
    expires_at = current.expires_at
    expected_refs = {ref.input_id: ref for ref in previous.source_refs} if previous else {}
    if len(retained_facts) != len(expected_refs):
        raise _deny("SOURCE_ASYNC_CONTEXT_RECHECK_REQUIRED")
    for facts in retained_facts:
        ref = facts.source_input
        if ref is None or expected_refs.pop(ref.input_id, None) != ref:
            raise _deny("SOURCE_ASYNC_CONTEXT_RECHECK_REQUIRED")
        if facts.kind != "context" or classify_input(facts, at=at) != "context":
            raise _deny("SOURCE_ASYNC_CONTEXT_REVOKED")
        dependencies[ref.input_id] = ref
        expires_at = min(expires_at, facts.expires_at)
    applied: list[UUID] = []
    blocked: list[BlockedInput] = []
    for item in pending:
        receipt, facts = item.receipt, item.facts
        if (
            receipt.state != "received"
            or receipt.async_id != current.async_id
            or receipt.scope_sha256 != current.scope.scope_sha256
            or receipt.input_revision > current.input_revision
            or receipt.kind != facts.kind
            or receipt.source_input != facts.source_input
            or receipt.restriction != facts.restriction
        ):
            raise _deny("SOURCE_ASYNC_INPUT_SCOPE_FENCED")
        disposition = classify_input(facts, at=at)
        if disposition == "invalidation":
            raise _deny("SOURCE_ASYNC_AUTHORITY_FENCED")
        if disposition == "blocked":
            blocked.append(
                BlockedInput(input_id=receipt.input_id, reason_code="SOURCE_ASYNC_INPUT_DENIED")
            )
            continue
        if disposition == "restriction":
            assert facts.restriction is not None
            restriction = restriction.narrow(facts.restriction)
        if facts.source_input is not None:
            ref = facts.source_input
            if ref.input_id in dependencies:
                raise _deny("SOURCE_ASYNC_INPUT_DUPLICATE")
            if ref.project_id != current.scope.target.project_id:
                raise _deny("SOURCE_ASYNC_SCOPE_FENCED")
            dependencies[ref.input_id] = ref
        expires_at = min(expires_at, facts.expires_at)
        applied.append(receipt.input_id)
    if len(dependencies) > 64:
        raise _deny("SOURCE_ASYNC_CONTEXT_LIMIT")
    expires_at = min(expires_at, at + timedelta(seconds=1800))
    refs = tuple(sorted(dependencies.values(), key=lambda ref: str(ref.input_id)))
    context_sha256 = digest(
        {
            "version": "source-async-context-v1",
            "scope_sha256": current.scope.scope_sha256,
            "initial_context_sha256": current.scope.initial_context_sha256,
            "input_revision": current.input_revision,
            "restriction": restriction.model_dump(mode="json"),
            "source_refs": [ref.model_dump(mode="json") for ref in refs],
        }
    )
    checkpoint = AsyncCheckpoint(
        checkpoint_id=checkpoint_id,
        async_id=current.async_id,
        scope=current.scope,
        stage=stage,
        input_revision=current.input_revision,
        through_sequence=received_sequence,
        restriction=restriction,
        source_refs=refs,
        context_sha256=context_sha256,
        tree_sha256=current.tree_sha256,
        environment_sha256=current.environment_sha256,
        created_at=at,
        expires_at=expires_at,
    )
    return SafePointPlan(checkpoint=checkpoint, applied_ids=tuple(applied), blocked=tuple(blocked))
