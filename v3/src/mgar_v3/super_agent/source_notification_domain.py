"""SA17 owner-reviewed notification contracts; never a publication grant."""

from datetime import timedelta
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from mgar_v3.domain import Model, digest
from mgar_v3.sources.domain import Sha256
from mgar_v3.super_agent.policy_domain import ContentClass, NotificationDestination
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef

NOTIFICATION_VERSION = "source-notification-v1"
ACTION_TYPE = "source_notification"


class NotificationModel(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class NotificationSettings(NotificationModel):
    enabled: bool = False
    policy_id: UUID
    policy_revision: Annotated[int, Field(strict=True, ge=1)]
    policy_sha256: Sha256
    profile_sha256: Sha256
    destination: NotificationDestination
    content_classes: Annotated[tuple[ContentClass, ...], Field(min_length=1, max_length=4)]
    cooldown_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = 300
    aggregation_seconds: Annotated[int, Field(strict=True, ge=0, le=300)] = 5
    ttl_seconds: Annotated[int, Field(strict=True, ge=30, le=86400)] = 3600
    valid_from: AwareDatetime
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if not timedelta(0) < self.valid_until - self.valid_from <= timedelta(days=7):
            raise ValueError("Explicit notification validity is at most seven days")
        if len(set(self.content_classes)) != len(self.content_classes):
            raise ValueError("Content classes must be distinct")
        if self.aggregation_seconds >= self.ttl_seconds:
            raise ValueError("Aggregation must fit the notification TTL")
        return self

    @property
    def settings_sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


class NotificationConfigure(NotificationModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    settings: NotificationSettings
    review_ref: Annotated[str, Field(min_length=1, max_length=200)]


class NotificationPlanRequest(NotificationModel):
    run_id: UUID
    source_input: CorrelationInputRef
    content_class: ContentClass


class NotificationCancelRequest(NotificationModel):
    payload_sha256: Sha256


class NotificationCycleRequest(NotificationModel):
    limit: Annotated[int, Field(strict=True, ge=1, le=64)] = 16


class NotificationFacts(NotificationModel):
    """Server-only, current deterministic facts; HTTP cannot submit this DTO."""

    run_id: UUID
    ticket_id: UUID
    execution_epoch: Annotated[int, Field(strict=True, ge=0)]
    input_revision: Annotated[int, Field(strict=True, ge=0)]
    thread_revision: Annotated[int, Field(strict=True, ge=0)]
    state: str
    content_class: ContentClass
    cause_sha256: Sha256
    verified: bool = False
    reviewable: bool = False
    needs_input: bool = False
    critical: bool = False


class NotificationTemplate(NotificationModel):
    template_version: Literal["source-notification-template-v1"] = "source-notification-template-v1"
    content_class: ContentClass
    data_class: Literal["public_summary"] = "public_summary"
    body: Annotated[str, Field(min_length=1, max_length=512)]
    body_sha256: Sha256
    facts_sha256: Sha256
