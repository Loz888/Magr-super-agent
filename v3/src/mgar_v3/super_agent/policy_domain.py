"""Independent, owner-signed policy layers; proposals and previews are not grants."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator, model_validator

from mgar_v3.domain import Model, digest
from mgar_v3.sources.domain import (
    ActorRef,
    OpaqueId,
    Revision,
    Sha256,
    SourceScope,
    SourceThreadRef,
)

POLICY_VERSION: Literal["source-policy-v1"] = "source-policy-v1"
Mode = Literal["disabled", "shadow", "auto_investigate", "auto_repair"]
Layer = Literal["receive", "admission", "execution", "notification"]
Capability = Literal[
    "read_repository",
    "read_knowledge",
    "search_knowledge",
    "recall_memory",
    "describe_tables",
    "query_readonly",
    "edit_files",
    "run_tests",
    "network_access",
    "model_call",
]
RequestKind = Literal["explicit_request", "team_rule", "follow_up"]
ContentClass = Literal[
    "verified_result", "necessary_question", "review_available", "critical_fault"
]
DataClass = Literal["public_summary", "private_summary", "private_log"]
Nonnegative = Annotated[int, Field(strict=True, ge=0)]


def safe_path(value: str) -> str:
    if (
        not value
        or len(value) > 256
        or value.startswith("/")
        or "\\" in value
        or any(p in {"", ".", ".."} for p in value.split("/"))
        or any(c in value for c in "\x00:*?[]%")
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("A bounded relative POSIX path without traversal or globs is required")
    return value


class SourceSelector(Model):
    scope: SourceScope
    source_revision: Revision
    conversations: Annotated[tuple[OpaqueId, ...], Field(min_length=1, max_length=100)]
    actors: Annotated[tuple[ActorRef, ...], Field(min_length=1, max_length=100)]
    native_threads: Annotated[tuple[OpaqueId, ...], Field(max_length=100)] = ()
    event_kinds: Annotated[tuple[OpaqueId, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def exact_scope(self) -> Self:
        if any(actor.scope != self.scope or actor.actor_kind == "unknown" for actor in self.actors):
            raise ValueError("Policy actors must be known kinds in the exact source installation")
        for items in [
            self.conversations,
            self.event_kinds,
            self.native_threads,
            tuple(actor.key for actor in self.actors),
        ]:
            if len(set(items)) != len(items):
                raise ValueError("Duplicate source selector")
        return self


class ReceivePolicy(Model):
    enabled: bool = Field(default=False, strict=True)
    retain_body: bool = Field(default=False, strict=True)
    model_input: bool = Field(default=False, strict=True)
    body_ttl_seconds: Annotated[int, Field(strict=True, ge=0, le=604800)] = 0
    allow_history_context: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def independent(self) -> Self:
        if not self.enabled and (
            self.retain_body or self.model_input or self.allow_history_context
        ):
            raise ValueError("Disabled Receive cannot retain or disclose content")
        if self.retain_body != (self.body_ttl_seconds > 0):
            raise ValueError("Body retention requires an explicit positive TTL")
        return self


class AdmissionPolicy(Model):
    enabled: bool = Field(default=False, strict=True)
    request_kinds: Annotated[tuple[RequestKind, ...], Field(max_length=3)] = ()
    rule_ref: OpaqueId | None = None
    rule_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def signed_basis(self) -> Self:
        if self.enabled != bool(self.request_kinds) or len(set(self.request_kinds)) != len(
            self.request_kinds
        ):
            raise ValueError("Admission needs explicit, distinct request kinds")
        if "team_rule" in self.request_kinds and (
            self.rule_ref is None or self.rule_sha256 is None
        ):
            raise ValueError(
                "Autonomous admission requires an exact signed rule reference and hash"
            )
        return self


class RepositoryBoundary(Model):
    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    base_sha: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    paths: Annotated[tuple[str, ...], Field(min_length=1, max_length=100)]

    @field_validator("paths")
    @classmethod
    def paths_are_safe(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("Duplicate path")
        return tuple(safe_path(value) for value in values)


class ExecutionGrant(Model):
    enabled: bool = Field(default=False, strict=True)
    repositories: Annotated[tuple[RepositoryBoundary, ...], Field(max_length=10)] = ()
    capabilities: Annotated[tuple[Capability, ...], Field(max_length=10)] = ()
    network_hosts: Annotated[
        tuple[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$")], ...],
        Field(max_length=30),
    ] = ()

    @model_validator(mode="after")
    def explicit_boundary(self) -> Self:
        if self.enabled != bool(self.repositories and self.capabilities):
            raise ValueError("Execution needs explicit repository, base, paths and capabilities")
        if not self.enabled and (self.repositories or self.capabilities or self.network_hosts):
            raise ValueError("Disabled execution has no grants")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Duplicate capability")
        if len({r.repository.lower() for r in self.repositories}) != len(self.repositories):
            raise ValueError("Duplicate repository")
        if bool(self.network_hosts) != ("network_access" in self.capabilities):
            raise ValueError("Network capability and explicit hosts must be granted together")
        return self


class BudgetBoundary(Model):
    total_limit_micros: Nonnegative = 0
    router_limit_micros: Nonnegative = 0
    accounting_ref: OpaqueId | None = None
    timezone: str = "Asia/Shanghai"

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown IANA timezone") from None
        return value

    @model_validator(mode="after")
    def no_implicit_budget(self) -> Self:
        if self.router_limit_micros > self.total_limit_micros:
            raise ValueError("Router budget must be inside the total boundary")
        if self.total_limit_micros > 0 and self.accounting_ref is None:
            raise ValueError(
                "Nonzero planned budget needs an accounting reference; not payment authority"
            )
        return self


class QuietHours(Model):
    timezone: str
    start: Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
    end: Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
    weekdays: Annotated[
        tuple[Annotated[int, Field(strict=True, ge=0, le=6)], ...],
        Field(min_length=1, max_length=7),
    ] = (0, 1, 2, 3, 4, 5, 6)

    @model_validator(mode="after")
    def valid(self) -> Self:
        BudgetBoundary.valid_zone(self.timezone)
        if self.start == self.end or len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("Quiet hours need a nonempty interval and distinct weekdays")
        return self

    def contains(self, at: datetime) -> bool:
        local = at.astimezone(ZoneInfo(self.timezone))
        minute = local.hour * 60 + local.minute
        start, end = (int(value[:2]) * 60 + int(value[3:]) for value in (self.start, self.end))
        if start < end:
            return local.weekday() in self.weekdays and start <= minute < end
        if minute >= start:
            return local.weekday() in self.weekdays
        return minute < end and (local.weekday() - 1) % 7 in self.weekdays


class NotificationDestination(Model):
    thread: SourceThreadRef
    target: OpaqueId
    visibility: Literal["public", "private"]

    @model_validator(mode="after")
    def channel_kind(self) -> Self:
        if not self.target.startswith(self.thread.scope.source_kind + ":"):
            raise ValueError("Notification target must belong to its declared source channel")
        return self


class NotificationGrant(Model):
    enabled: bool = Field(default=False, strict=True)
    destinations: Annotated[tuple[NotificationDestination, ...], Field(max_length=20)] = ()
    content_classes: Annotated[tuple[ContentClass, ...], Field(max_length=4)] = ()
    data_classes: Annotated[tuple[DataClass, ...], Field(max_length=3)] = ()
    max_bytes: Annotated[int, Field(strict=True, ge=0, le=65536)] = 0
    max_per_hour: Annotated[int, Field(strict=True, ge=0, le=100)] = 0
    quiet_hours: Annotated[tuple[QuietHours, ...], Field(max_length=10)] = ()

    @model_validator(mode="after")
    def explicit_disclosure(self) -> Self:
        complete = bool(
            self.destinations
            and self.content_classes
            and self.data_classes
            and self.max_bytes
            and self.max_per_hour
        )
        if self.enabled != complete or (
            not self.enabled
            and (
                self.destinations
                or self.content_classes
                or self.data_classes
                or self.max_bytes
                or self.max_per_hour
            )
        ):
            raise ValueError(
                "Notification needs an explicit destination/content/size/rate boundary"
            )
        if len({digest(d.model_dump(mode="json")) for d in self.destinations}) != len(
            self.destinations
        ):
            raise ValueError("Duplicate destination")
        return self


class PolicyDefinition(Model):
    schema_version: Literal["source-policy-v1"] = POLICY_VERSION
    mode: Mode = "shadow"
    selector: SourceSelector
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    receive: ReceivePolicy
    admission: AdmissionPolicy
    execution: ExecutionGrant
    notification: NotificationGrant
    budget: BudgetBoundary

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.valid_until <= self.valid_from:
            raise ValueError("Policy expiry must follow its start")
        if self.mode == "auto_investigate" and set(self.execution.capabilities) & {
            "edit_files",
            "run_tests",
        }:
            raise ValueError("Investigation cannot grant code writes or execution")
        for destination in self.notification.destinations:
            if destination.thread.scope != self.selector.scope:
                raise ValueError(
                    "Cross-installation notification needs a separately reviewed policy"
                )
        return self


class PolicySave(Model):
    expected_revision: Nonnegative
    definition: PolicyDefinition
    review_ref: OpaqueId


class PolicyRevoke(Model):
    expected_revision: Revision
    review_ref: OpaqueId


class PolicyFacts(Model):
    """Server-derived candidate metadata. Console preview input is never an execution grant."""

    event_kind: OpaqueId
    request_kind: RequestKind = "explicit_request"
    historical: bool = Field(default=False, strict=True)
    repository: (
        Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)] | None
    ) = None
    base_sha: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")] | None = None
    path: str | None = None
    capability: Capability | None = None
    network_host: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$")] | None = None
    ticket_id: UUID | None = None

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str | None) -> str | None:
        return safe_path(value) if value is not None else None


class PolicyPreview(Model):
    expected_revision: Nonnegative
    definition: PolicyDefinition
    actor: ActorRef
    source_thread: SourceThreadRef
    facts: PolicyFacts


class LayerDecision(Model):
    layer: Layer
    allowed: bool
    reason_code: str
    would_allow: bool = False
    retain_body: bool = False
    model_input: bool = False
    semantic_evaluation: Literal["NOT_RUN"] = "NOT_RUN"


class NotificationRequest(Model):
    action_id: UUID
    destination: NotificationDestination
    content_class: ContentClass
    data_class: DataClass
    body_sha256: Sha256
