"""Bounded deployment/observation contracts; operational liveness grants no work."""

import ipaddress
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, Field, SecretStr, field_validator, model_validator

from mgar_v3.domain import Model, digest

VERSION: Literal["source-operations-v1"] = "source-operations-v1"
Role = Literal["source", "scheduler", "worker", "dispatcher", "watchdog"]
Counter = Annotated[int, Field(strict=True, ge=0, le=(1 << 63) - 1)]
Positive = Annotated[int, Field(strict=True, ge=1, le=(1 << 63) - 1)]
Sha256 = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
ReviewRef = Annotated[str, Field(strict=True, min_length=1, max_length=256)]
SignalKind = Literal[
    "component_unavailable",
    "waiting_runner",
    "source_offline",
    "source_permissions",
    "source_gap",
    "input_backlog",
    "semantic_stalled",
    "job_overdue",
    "lease_expired",
    "notification_unknown",
    "usage_unknown",
    "dependency_unavailable",
]


def https_origin(value: str) -> str:
    """One exact HTTPS origin, without path/proxy/header wildcard semantics."""
    if not isinstance(value, str) or len(value) > 255 or any(c.isspace() for c in value):
        raise ValueError("One explicit HTTPS origin is required")
    p = urlsplit(value)
    try:
        port = p.port
    except ValueError:
        raise ValueError("Invalid HTTPS origin") from None
    if (
        p.scheme != "https"
        or not p.hostname
        or p.username is not None
        or p.password is not None
        or p.path
        or p.query
        or p.fragment
        or "*" in p.netloc
        or "\\" in p.netloc
        or p.hostname != p.hostname.lower()
        or p.hostname.endswith(".")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("One explicit HTTPS origin is required")
    host = p.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or any(
            not part or len(part) > 63 or part.startswith("-") or part.endswith("-")
            for part in host.split(".")
        ):
            raise ValueError("A valid exact hostname is required") from None
    # urlsplit normalizes hostname case; demand a canonical authority explicitly.
    authority = f"[{host}]" if ":" in host else host
    authority += f":{port}" if port is not None else ""
    if value != "https://" + authority:
        raise ValueError("Use the canonical exact HTTPS origin")
    return value


def local_api(value: str) -> str:
    p = urlsplit(value)
    try:
        port = p.port
    except ValueError:
        raise ValueError("Invalid local Engine endpoint") from None
    if (
        p.scheme != "http"
        or p.hostname not in {"127.0.0.1", "localhost", "::1"}
        or p.username is not None
        or p.password is not None
        or p.path not in {"", "/"}
        or p.query
        or p.fragment
        or (port is not None and not 1 <= port <= 65535)
        or "\\" in value
        or any(c.isspace() for c in value)
    ):
        raise ValueError("Operational credentials connect only to the local Engine")
    return value.rstrip("/")


class ComponentGrant(Model):
    component_id: UUID
    role: Role
    enabled: bool = Field(default=False, strict=True)
    source_ids: Annotated[tuple[UUID, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def scoped_sources(self) -> Self:
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Duplicate source scope")
        if self.role != "source" and self.source_ids:
            raise ValueError("Only Source relays have a source scope")
        if self.role == "source" and self.enabled and not self.source_ids:
            raise ValueError("An enabled Source relay needs exact sources")
        return self


class OperationsDefinition(Model):
    schema_version: Literal["source-operations-v1"] = VERSION
    enabled: bool = Field(default=False, strict=True)
    valid_until: AwareDatetime
    components: Annotated[tuple[ComponentGrant, ...], Field(max_length=16)] = ()
    lease_seconds: Annotated[int, Field(strict=True, ge=10, le=300)] = 30
    source_stale_seconds: Annotated[int, Field(strict=True, ge=30, le=3600)] = 120
    backlog_threshold: Annotated[int, Field(strict=True, ge=1, le=10000)] = 16
    progress_stale_seconds: Annotated[int, Field(strict=True, ge=30, le=86400)] = 120

    @model_validator(mode="after")
    def distinct_components(self) -> Self:
        if len({c.component_id for c in self.components}) != len(self.components):
            raise ValueError("Duplicate component")
        return self


def valid_definition(value: OperationsDefinition, at: datetime) -> bool:
    return at < value.valid_until <= at + timedelta(days=30)


class OperationsSave(Model):
    expected_revision: Counter
    definition: OperationsDefinition
    review_ref: ReviewRef


class ComponentOpen(Model):
    config_revision: Positive
    expected_generation: Counter
    boot_id: UUID
    request_id: UUID


class ComponentHeartbeat(Model):
    generation: Positive
    boot_id: UUID
    sequence: Positive
    state: Literal["starting", "ready", "degraded", "stopping", "waiting_runner"]


class ComponentTick(Model):
    sequence: Positive
    generation: Positive
    boot_id: UUID
    request_id: UUID


class SignalSubmission(Model):
    kind: SignalKind
    subject_id: UUID | None = None
    observation_key: Sha256
    snapshot_sha256: Sha256


class IncidentDecision(Model):
    expected_revision: Positive
    decision: Literal["wait", "escalate"]
    review_ref: ReviewRef


class ComponentCredential(Model):
    component_id: UUID
    token: SecretStr = Field(min_length=32, max_length=256)

    @field_validator("token")
    @classmethod
    def ascii_token(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value.get_secret_value()):
            raise ValueError("Use a random URL-safe service credential")
        return value


class ServiceSecrets(Model):
    components: Annotated[tuple[ComponentCredential, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def unique_credentials(self) -> Self:
        if len({c.component_id for c in self.components}) != len(self.components) or len(
            {c.token.get_secret_value() for c in self.components}
        ) != len(self.components):
            raise ValueError("Each component needs its own credential")
        return self


class ServiceClient(Model):
    api_url: str
    component_id: UUID
    role: Role
    token: SecretStr = Field(min_length=32, max_length=256)
    interval_seconds: Annotated[int, Field(strict=True, ge=2, le=60)] = 5

    @field_validator("api_url")
    @classmethod
    def local(cls, value: str) -> str:
        return local_api(value)

    @field_validator("token")
    @classmethod
    def credential(cls, value: SecretStr) -> SecretStr:
        return ComponentCredential.ascii_token(value)


class BackupPart(Model):
    path: Annotated[str, Field(strict=True, min_length=1, max_length=240)]
    kind: Literal["database", "cas", "source_key", "console_key"]
    sha256: Sha256
    size: Counter

    @field_validator("path")
    @classmethod
    def relative_file(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or "\x00" in value
            or any(not p or p.startswith(".") for p in value.split("/"))
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
        ):
            raise ValueError("Backup entries must be canonical relative files")
        return value


class BackupManifest(Model):
    schema_version: Literal["source-operations-backup-v1"] = "source-operations-backup-v1"
    backup_id: UUID
    project_id: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    created_at: AwareDatetime
    migration_sha256: Sha256
    parts: Annotated[tuple[BackupPart, ...], Field(min_length=2, max_length=100000)]
    source_body_key_required: bool = Field(default=True, strict=True)
    console_credential_key_required: bool = Field(default=True, strict=True)
    unknown_writes_preserved: Literal[True] = True
    activation_on_restore: Literal[False] = False

    @model_validator(mode="after")
    def complete_inventory(self) -> Self:
        if len({p.path for p in self.parts}) != len(self.parts):
            raise ValueError("Duplicate backup entries")
        if sum(p.kind == "database" for p in self.parts) != 1 or not any(
            p.kind == "cas" for p in self.parts
        ):
            raise ValueError("A backup binds one database dump and its CAS inventory")
        kinds = {p.kind for p in self.parts}
        if (self.source_body_key_required and "source_key" not in kinds) or (
            self.console_credential_key_required and "console_key" not in kinds
        ):
            raise ValueError("Backups of encrypted source state must include its keys")
        return self


class SignalFact(Model):
    kind: SignalKind
    subject_id: UUID | None = None
    cursor: Sha256
    severity: Literal["warning", "critical"] = "warning"

    def key(self, project_id: str) -> str:
        return digest(
            {
                "version": VERSION,
                "project": project_id,
                "kind": self.kind,
                "subject": str(self.subject_id),
                "cursor": self.cursor,
            }
        )


def elapsed_seconds(now: datetime, then: datetime) -> int:
    if then.tzinfo is None or now.tzinfo is None:
        raise ValueError("Operational clocks must be timezone aware")
    return max(0, int((now.astimezone(UTC) - then.astimezone(UTC)).total_seconds()))


class ComponentObservation(Model):
    component_id: UUID
    role: Role
    generation: Positive
    boot_id: UUID
    state: Literal["starting", "ready", "degraded", "stopping", "waiting_runner"]
    expires_at: AwareDatetime
    sequence: Counter


class SourceObservation(Model):
    source_id: UUID
    revision: Positive
    enabled: bool
    connection: Literal["unknown", "disabled", "online", "offline", "degraded"]
    permissions: Literal["unknown", "granted", "insufficient", "revoked"]
    health_at: AwareDatetime | None = None
    open_gaps: Counter = 0
    pending_inputs: Counter = 0
    cursor: Sha256


class RunObservation(Model):
    run_id: UUID
    epoch: Counter
    state: Literal[
        "queued",
        "running",
        "waiting_input",
        "waiting_approval",
        "paused",
        "cancelled",
        "failed",
        "succeeded",
    ]
    progress_cursor: Counter
    progress_at: AwareDatetime
    pending_jobs: Counter = 0
    overdue_jobs: Counter = 0
    expired_assignments: Counter = 0
    unknown_actions: Counter = 0
    unknown_calls: Counter = 0
    job_cursor: Sha256 = digest([])
    lease_cursor: Sha256 = digest([])
    unknown_actions_cursor: Sha256 = digest([])
    unknown_calls_cursor: Sha256 = digest([])


class OperationsSnapshot(Model):
    schema_version: Literal["source-operations-v1"] = VERSION
    project_id: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    observed_at: AwareDatetime
    config_revision: Counter
    components: Annotated[tuple[ComponentObservation, ...], Field(max_length=16)] = ()
    sources: Annotated[tuple[SourceObservation, ...], Field(max_length=100)] = ()
    runs: Annotated[tuple[RunObservation, ...], Field(max_length=256)] = ()
    runner_available: bool = False
    source_backlog: Counter = 0
    notification_unknown: Counter = 0
    usage_unknown: Counter = 0
    redis: Literal["unknown", "ready", "unavailable"] = "unknown"
    dependency_cursor: Sha256 = digest([])
    backlog_cursor: Sha256 = digest([])
    notification_cursor: Sha256 = digest([])
    usage_cursor: Sha256 = digest([])
    partial: bool = False


def signals(
    snapshot: OperationsSnapshot, definition: OperationsDefinition
) -> tuple[SignalFact, ...]:
    """Pure observer; a heartbeat sequence never becomes semantic progress."""
    if not definition.enabled or definition.valid_until <= snapshot.observed_at:
        return ()
    result = []
    components = {c.component_id: c for c in snapshot.components}
    for grant in definition.components:
        if not grant.enabled:
            continue
        item = components.get(grant.component_id)
        if item is None or item.expires_at <= snapshot.observed_at:
            result.append(
                SignalFact(
                    kind="component_unavailable",
                    subject_id=grant.component_id,
                    cursor=digest(
                        [
                            snapshot.config_revision,
                            item.generation if item else 0,
                            item.expires_at.isoformat() if item else None,
                        ]
                    ),
                )
            )
    for source in snapshot.sources:
        if not source.enabled:
            continue
        kind: SignalKind | None = None
        if source.permissions in {"insufficient", "revoked"}:
            kind = "source_permissions"
        elif source.connection in {"offline", "degraded"} or (
            source.health_at is not None
            and elapsed_seconds(snapshot.observed_at, source.health_at)
            >= definition.source_stale_seconds
        ):
            kind = "source_offline"
        if kind:
            result.append(SignalFact(kind=kind, subject_id=source.source_id, cursor=source.cursor))
        if source.open_gaps:
            result.append(
                SignalFact(kind="source_gap", subject_id=source.source_id, cursor=source.cursor)
            )
    for run in snapshot.runs:
        if run.unknown_actions:
            result.append(
                SignalFact(
                    kind="notification_unknown",
                    subject_id=run.run_id,
                    cursor=digest([run.epoch, run.unknown_actions_cursor]),
                    severity="critical",
                )
            )
        if run.unknown_calls:
            result.append(
                SignalFact(
                    kind="usage_unknown",
                    subject_id=run.run_id,
                    cursor=digest([run.epoch, run.unknown_calls_cursor]),
                    severity="critical",
                )
            )
        if run.state in {
            "cancelled",
            "failed",
            "succeeded",
            "paused",
            "waiting_input",
            "waiting_approval",
        }:
            continue
        if run.state == "queued" and not snapshot.runner_available:
            result.append(
                SignalFact(
                    kind="waiting_runner",
                    subject_id=run.run_id,
                    cursor=digest([run.epoch, run.progress_cursor]),
                )
            )
        # An unresolved Job is its own progress boundary, not a stalled LLM loop.
        elif (
            not run.pending_jobs
            and elapsed_seconds(snapshot.observed_at, run.progress_at)
            >= definition.progress_stale_seconds
        ):
            result.append(
                SignalFact(
                    kind="semantic_stalled",
                    subject_id=run.run_id,
                    cursor=digest([run.epoch, run.progress_cursor]),
                )
            )
        if run.overdue_jobs:
            result.append(
                SignalFact(
                    kind="job_overdue",
                    subject_id=run.run_id,
                    cursor=digest([run.epoch, run.job_cursor]),
                )
            )
        if run.expired_assignments:
            result.append(
                SignalFact(
                    kind="lease_expired",
                    subject_id=run.run_id,
                    cursor=digest([run.epoch, run.lease_cursor]),
                )
            )
    if snapshot.source_backlog >= definition.backlog_threshold:
        result.append(
            SignalFact(
                kind="input_backlog",
                cursor=digest([snapshot.config_revision, snapshot.backlog_cursor]),
            )
        )
    if snapshot.notification_unknown:
        result.append(
            SignalFact(
                kind="notification_unknown",
                cursor=snapshot.notification_cursor,
                severity="critical",
            )
        )
    if snapshot.usage_unknown:
        result.append(
            SignalFact(kind="usage_unknown", cursor=snapshot.usage_cursor, severity="critical")
        )
    if snapshot.redis == "unavailable":
        result.append(
            SignalFact(
                kind="dependency_unavailable",
                cursor=digest(["redis-unavailable", snapshot.dependency_cursor]),
            )
        )
    return tuple(result)
