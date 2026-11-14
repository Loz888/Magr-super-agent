"""Pure SA14 validation, with no SQL, IO, lease issuance or business state writes."""

from datetime import datetime
from typing import Literal

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.policy_domain import safe_path
from mgar_v3.super_agent.source_async_domain import (
    AsyncCheckpoint,
    AsyncCurrentFacts,
    AsyncInputFacts,
    AsyncJob,
    JobState,
)
from mgar_v3.super_agent.source_coding_domain import within


def _deny(code: str) -> DomainError:
    return DomainError(code, "Async work requires current original scope and observed facts", 403)


def _time(at: datetime) -> None:
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise _deny("SOURCE_ASYNC_TIME_INVALID")


def classify_input(
    facts: AsyncInputFacts, *, at: datetime
) -> Literal["context", "restriction", "invalidation", "job_progress", "blocked"]:
    _time(at)
    if not facts.exact_target:
        return "blocked"
    if facts.source == "system":
        return (
            "invalidation"
            if facts.kind == "invalidation" and facts.trusted_observation
            else "blocked"
        )
    if facts.source == "job":
        return (
            "job_progress"
            if facts.kind == "job_progress" and facts.trusted_observation
            else "blocked"
        )
    if at >= facts.expires_at:
        return "blocked"
    if facts.source == "owner":
        return (
            "restriction"
            if (
                facts.owner_session_current
                and facts.kind == "restriction"
                and facts.restriction is not None
            )
            else "blocked"
        )
    if not all(
        (
            facts.source_input is not None,
            facts.receive_current,
            facts.identity_current,
            facts.policy_current,
            facts.canonical_current,
            facts.read_allowed,
        )
    ):
        return "blocked"
    if facts.kind == "context":
        return "context"
    if facts.kind == "restriction" and all(
        (
            not facts.context_only,
            facts.human,
            facts.task_controller,
            facts.ticket_membership,
            facts.restriction is not None,
        )
    ):
        return "restriction"
    return "blocked"


def validate_view(view: AsyncCheckpoint, facts: AsyncCurrentFacts, *, at: datetime) -> None:
    _time(at)
    if (
        not all(
            (
                facts.original_coding_current,
                facts.assignment_current,
                facts.session_current,
                facts.sources_current,
                facts.budget_current,
                facts.active,
            )
        )
        or facts.pending_invalidation
    ):
        raise _deny("SOURCE_ASYNC_AUTHORITY_FENCED")
    if view.async_id != facts.async_id or view.scope != facts.scope:
        raise _deny("SOURCE_ASYNC_SCOPE_FENCED")
    if not view.created_at <= at < min(view.expires_at, facts.expires_at):
        raise _deny("SOURCE_ASYNC_VIEW_EXPIRED")
    if view.input_revision != facts.input_revision or view.restriction != facts.restriction:
        raise _deny("SOURCE_ASYNC_INPUT_STALE")
    if view.tree_sha256 != facts.tree_sha256:
        raise _deny("SOURCE_ASYNC_TREE_STALE")
    if view.environment_sha256 != facts.environment_sha256:
        raise _deny("SOURCE_ASYNC_ENVIRONMENT_STALE")


def validate_mutation(
    view: AsyncCheckpoint,
    facts: AsyncCurrentFacts,
    *,
    at: datetime,
    capability: Literal["write", "execute"],
    paths: tuple[str, ...] = (),
    command: str | None = None,
) -> None:
    """Only further denies; the caller must separately validate the original grant."""
    validate_view(view, facts, at=at)
    if capability == "write":
        if not paths or command is not None:
            raise _deny("SOURCE_ASYNC_OPERATION_SCOPE")
        try:
            normalized = tuple(safe_path(path) for path in paths)
        except ValueError:
            raise _deny("SOURCE_ASYNC_PATH_DENIED") from None
        if any(within(path, facts.restriction.deny_write_paths) for path in normalized):
            raise _deny("SOURCE_ASYNC_PATH_DENIED")
    elif capability == "execute":
        if paths or not command or command in facts.restriction.deny_commands:
            raise _deny("SOURCE_ASYNC_COMMAND_DENIED")
    else:
        raise _deny("SOURCE_ASYNC_OPERATION_SCOPE")


def validate_job_transition(
    job: AsyncJob,
    next_state: JobState,
    *,
    trusted_receipt: bool = False,
    exact_original_resource: bool = False,
) -> None:
    allowed: dict[str, set[str]] = {
        "prepared": {"running", "stop_requested", "unknown", "known"},
        "running": {"stop_requested", "unknown", "known"},
        "stop_requested": {"unknown", "known"},
        "unknown": {"known"},
        "known": set(),
    }
    if next_state not in allowed[job.state]:
        raise _deny("SOURCE_ASYNC_JOB_TRANSITION_DENIED")
    if next_state == "known" and not (trusted_receipt and exact_original_resource):
        raise _deny("SOURCE_ASYNC_JOB_RECEIPT_REQUIRED")
