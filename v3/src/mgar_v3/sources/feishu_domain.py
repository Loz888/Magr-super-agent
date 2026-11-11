"""Feishu v1 configuration is not credential, execution or notification authority."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from mgar_v3.domain import DomainError, Model
from mgar_v3.sources.domain import OpaqueId

VERSION: Literal["feishu-bot-v1"] = "feishu-bot-v1"
RECEIVE: Literal["im.message.receive_v1"] = "im.message.receive_v1"
RECALL: Literal["im.message.recalled_v1"] = "im.message.recalled_v1"
EVENTS = (RECEIVE, RECALL)
AT_SCOPE = "im:message.group_at_msg:readonly"
ALL_SCOPE = "im:message.group_msg"
HISTORY_SCOPE = "im:message:readonly"


class FollowupTopic(Model):
    chat_id: OpaqueId
    thread_id: OpaqueId


class FeishuSettings(Model):
    schema_version: Literal["feishu-bot-v1"] = VERSION
    app_id: OpaqueId
    tenant_key: OpaqueId
    bot_open_id: OpaqueId
    chat_ids: Annotated[tuple[OpaqueId, ...], Field(min_length=1, max_length=100)]
    sender_open_ids: Annotated[tuple[OpaqueId, ...], Field(min_length=1, max_length=100)]
    context_bot_open_ids: Annotated[tuple[OpaqueId, ...], Field(max_length=100)] = ()
    events: Annotated[
        tuple[Literal["im.message.receive_v1", "im.message.recalled_v1"], ...],
        Field(min_length=1, max_length=2),
    ] = (RECEIVE,)
    receive_mode: Literal["mentions_or_topics", "all_selected"] = "mentions_or_topics"
    followup_topics: Annotated[tuple[FollowupTopic, ...], Field(max_length=100)] = ()
    requested_scopes: Annotated[tuple[OpaqueId, ...], Field(min_length=1, max_length=20)] = (
        AT_SCOPE,
    )
    scopes_review_ref: OpaqueId
    history_enabled: bool = Field(default=False, strict=True)
    enabled: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def boundaries(self) -> Self:
        for values in [
            self.chat_ids,
            self.sender_open_ids,
            self.context_bot_open_ids,
            self.events,
            self.requested_scopes,
        ]:
            if len(values) != len(set(values)):
                raise ValueError("Duplicate scope")
        actors = set(self.sender_open_ids) | set(self.context_bot_open_ids)
        if (
            set(self.sender_open_ids) & set(self.context_bot_open_ids)
            or self.bot_open_id in actors
            or self.app_id in actors
        ):
            raise ValueError("Own bot or ambiguous actor cannot be selected")
        if RECEIVE not in self.events or AT_SCOPE not in self.requested_scopes:
            raise ValueError("Explicit receive event and group mention scope required")
        if self.receive_mode == "all_selected" and ALL_SCOPE not in self.requested_scopes:
            raise ValueError("All selected messages require separate sensitive scope review")
        if self.history_enabled and not {HISTORY_SCOPE, ALL_SCOPE}.issubset(self.requested_scopes):
            raise ValueError(
                "Group history requires explicit message read and group message scopes"
            )
        if any(x.chat_id not in self.chat_ids for x in self.followup_topics):
            raise ValueError("Followup topic escapes selected groups")
        if len({(x.chat_id, x.thread_id) for x in self.followup_topics}) != len(
            self.followup_topics
        ):
            raise ValueError("Duplicate followup topic")
        return self


class FeishuConfigure(Model):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    settings: FeishuSettings
    review_ref: OpaqueId


@dataclass(frozen=True)
class FeishuSecrets:
    app_secret: str = field(repr=False)
    verification_token: str = field(repr=False)
    encrypt_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not all(
            isinstance(x, str) and x
            for x in (self.app_secret, self.verification_token, self.encrypt_key)
        ):
            raise ValueError("Explicit server-only Feishu credentials required")


@dataclass(frozen=True)
class FeishuActivation:
    source_id: UUID
    config_revision: int
    generation: int
    expires_at: datetime
    operations: frozenset[str]
    review_ref: str
    mode: Literal["controlled", "live"] = "controlled"

    def check(self, source_id: UUID, revision: int, generation: int, operation: str) -> None:
        if (
            self.source_id != source_id
            or self.config_revision != revision
            or self.generation != generation
            or operation not in self.operations
            or self.expires_at.tzinfo is None
            or self.expires_at <= datetime.now(UTC)
            or not self.review_ref
            or self.mode not in {"controlled", "live"}
        ):
            raise DomainError(
                "FEISHU_ACTIVATION_FENCED", "Feishu activation is absent or stale", 403
            )


class FeishuTransportReceipt(Model):
    """Durable transport-only rejection, never a business Source input or task."""

    receipt_id: UUID
    disposition: Literal["filtered"] = "filtered"
    reason_code: str
    committed: Literal[True] = True


class FeishuHealth(Model):
    connection: Literal["unknown", "disabled", "online", "offline", "degraded"]
    permissions: Literal["unknown", "granted", "insufficient", "revoked"]
    observed_at: AwareDatetime
    mode: Literal["controlled", "live"]
    reason_code: str
