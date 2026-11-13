"""Pure scheduling/fencing rules; the original Engine supplies all current facts."""

from datetime import datetime
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_scheduler_domain import (
    AssignmentFacts,
    PendingWork,
    QueueCandidate,
    QuiescenceFacts,
    ResourceRequest,
    ResourceUsage,
    RunFacts,
    SchedulerAssignmentRequest,
    SchedulerConfigFacts,
    SchedulerLimits,
    ServiceHistory,
    SessionFacts,
    SourceAuthorityFacts,
)


def _error(code: str, status: int = 409) -> DomainError:
    return DomainError(code, "Scheduling requires current original Run resource authority", status)


def _time(at: datetime) -> None:
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise _error("SOURCE_SCHEDULER_TIME_INVALID", 422)


def resource_fit(
    request: ResourceRequest,
    limits: SchedulerLimits,
    usage: ResourceUsage,
    *,
    project_id: str,
    fairness_key: str,
) -> bool:
    """A claim begins preparing; draining usage is retained in every total."""
    source_slots = sum(
        item.slots for item in usage.sources
        if item.project_id == project_id and item.fairness_key == fairness_key
    )
    return (
        usage.slots + 1 <= limits.slots
        and usage.preparing + 1 <= limits.preparing
        and usage.cpu_millis + request.cpu_millis <= limits.cpu_millis
        and usage.memory_mib + request.memory_mib <= limits.memory_mib
        and source_slots + 1 <= limits.source_slots
    )


def choose_candidate(
    candidates: tuple[QueueCandidate, ...],
    history: ServiceHistory,
    *,
    project_id: str,
    limits: SchedulerLimits,
    usage: ResourceUsage,
    at: datetime,
) -> QueueCandidate | None:
    """Alternate eligible classes; source service sequence then age breaks ties.

    The caller rebuilds these metadata facts from PG and rechecks the selected
    Run in its claim transaction. No cached selection itself grants execution.
    """
    _time(at)
    if len(candidates) > 256 or len({item.queue_id for item in candidates}) != len(candidates):
        raise _error("SOURCE_SCHEDULER_CANDIDATES_INVALID", 422)
    eligible = [
        item for item in candidates
        if item.project_id == project_id and item.state == "queued" and item.eligible
        and item.enqueued_at <= at
        and resource_fit(
            item.resources, limits, usage, project_id=project_id, fairness_key=item.fairness_key,
        )
    ]
    if not eligible:
        return None
    classes = {item.classification for item in eligible}
    if len(classes) == 2:
        preferred = "automation" if history.last_class == "human" else "human"
        eligible = [item for item in eligible if item.classification == preferred]
    buckets = {(item.project_id, item.fairness_key): item for item in history.buckets}

    def order(item: QueueCandidate) -> tuple[int, int, datetime, str]:
        bucket = buckets.get((item.project_id, item.fairness_key))
        return (
            bucket.project_sequence if bucket is not None else 0,
            bucket.source_sequence if bucket is not None else 0,
            item.enqueued_at,
            str(item.queue_id),
        )

    return min(eligible, key=order)


def validate_lease(
    configuration: SchedulerConfigFacts,
    session: SessionFacts,
    assignment: AssignmentFacts,
    run: RunFacts,
    source: SourceAuthorityFacts,
    request: SchedulerAssignmentRequest,
    *,
    at: datetime,
    allow_preparing: bool = False,
) -> None:
    """This gate cannot revive an expired lease or mutate a Run/Assignment."""
    _time(at)
    config = configuration.configuration
    if not config.enabled or not config.valid_from <= at < config.valid_until:
        raise _error("SOURCE_SCHEDULER_CONFIG_INACTIVE", 403)
    if (
        assignment.project_id != configuration.project_id
        or session.project_id != assignment.project_id
        or run.project_id != assignment.project_id
    ):
        raise _error("SOURCE_SCHEDULER_PROJECT_MISMATCH", 403)
    if configuration.revision != assignment.config_revision:
        raise _error("SOURCE_SCHEDULER_CONFIG_STALE")
    if request.assignment_id != assignment.assignment_id or (
        request.session_id != assignment.session_id or session.session_id != assignment.session_id
    ):
        raise _error("SOURCE_SCHEDULER_OWNER_MISMATCH", 403)
    if request.expected_revision != assignment.revision:
        raise _error("SOURCE_SCHEDULER_REVISION_CONFLICT")
    if (
        request.epoch != assignment.epoch or run.epoch != assignment.epoch
        or run.run_id != assignment.run_id or run.ticket_id != assignment.ticket_id
    ):
        raise _error("SOURCE_SCHEDULER_EPOCH_FENCED")
    if session.state != "registered" or not session.registered_at <= at < session.expires_at:
        raise _error("SOURCE_SCHEDULER_SESSION_FENCED", 403)
    states = {"active", "preparing"} if allow_preparing else {"active"}
    if (
        assignment.state not in states or at >= assignment.lease_until
        or assignment.fenced_at is not None
    ):
        raise _error("SOURCE_SCHEDULER_LEASE_FENCED", 403)
    if assignment.state == "preparing" and (
        assignment.prepare_until is None or at >= assignment.prepare_until
    ):
        raise _error("SOURCE_SCHEDULER_PREPARATION_EXPIRED", 403)
    if run.status != "running" or not (
        run.ticket_open and run.target_current and run.mandate_current
    ):
        raise _error("SOURCE_SCHEDULER_RUN_FENCED", 403)
    if source.required and (
        not all((
            source.current, source.identity_current, source.receive_allowed,
            source.read_allowed, source.execution_allowed, source.origin_current,
        ))
        or source.expires_at is None or at >= source.expires_at
    ):
        raise _error("SOURCE_SCHEDULER_SOURCE_FENCED", 403)


def validate_release(
    assignment: AssignmentFacts,
    evidence: QuiescenceFacts,
    pending: PendingWork,
    *,
    at: datetime,
) -> None:
    """Expiry and a Worker saying 'done' are not trusted process quiescence.

    Release deliberately does not require a current lease: revoked/expired
    executors must remain cleanable without receiving fresh execution rights.
    """
    _time(at)
    if assignment.state not in {"draining", "released"}:
        raise _error("SOURCE_SCHEDULER_DRAIN_REQUIRED")
    if (
        evidence.assignment_id != assignment.assignment_id
        or evidence.session_id != assignment.session_id
        or evidence.run_id != assignment.run_id
        or evidence.workspace_id != assignment.workspace_id
        or evidence.epoch != assignment.epoch
    ):
        raise _error("SOURCE_SCHEDULER_QUIESCENCE_MISMATCH", 403)
    if (
        assignment.fenced_at is None or evidence.observed_at < assignment.fenced_at
        or evidence.observed_at > at
    ) or not (
        evidence.trusted and evidence.processes_stopped and evidence.workspace_fenced
    ):
        raise _error("SOURCE_SCHEDULER_QUIESCENCE_REQUIRED", 403)
    if any(pending.model_dump().values()):
        raise _error("SOURCE_SCHEDULER_WORK_PENDING")


def queue_quota_key(queue_id: UUID, generation: int) -> str:
    if type(generation) is not int or not 1 <= generation <= 9223372036854775807:
        raise _error("SOURCE_SCHEDULER_GENERATION_INVALID", 422)
    return f"sa12:queue:{queue_id}:{generation}"


def slot_quota_key(assignment_id: UUID) -> str:
    return f"sa12:slot:{assignment_id}"
