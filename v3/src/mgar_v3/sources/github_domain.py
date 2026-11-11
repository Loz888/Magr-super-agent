"""GitHub App configuration and explicit snapshot permission; no secrets in SQL DTOs."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from mgar_v3.domain import Model
from mgar_v3.sources.domain import OpaqueId, Revision
from mgar_v3.super_agent.policy_domain import safe_path

VERSION: Literal["github-app-v1"] = "github-app-v1"
GitHubId = Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]
Commit = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
Family = Literal[
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "workflow_run",
    "check_run",
    "check_suite",
]


class GitHubRepository(Model):
    repository_id: GitHubId
    full_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    private: bool = Field(strict=True)


class GitHubSettings(Model):
    schema_version: Literal["github-app-v1"] = VERSION
    app_id: GitHubId
    account_id: GitHubId
    installation_id: GitHubId
    repository: GitHubRepository
    events: Annotated[tuple[Family, ...], Field(min_length=1, max_length=8)]
    sender_ids: Annotated[tuple[GitHubId, ...], Field(min_length=1, max_length=100)]
    context_bot_ids: Annotated[tuple[GitHubId, ...], Field(max_length=100)] = ()
    own_bot_ids: Annotated[tuple[GitHubId, ...], Field(min_length=1, max_length=100)]
    enabled: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def distinct(self) -> Self:
        for values in [self.events, self.sender_ids, self.context_bot_ids, self.own_bot_ids]:
            if len(set(values)) != len(values):
                raise ValueError("Duplicate GitHub selector")
        if set(self.own_bot_ids) & (set(self.sender_ids) | set(self.context_bot_ids)):
            raise ValueError("Own bots cannot be admitted as input actors")
        if set(self.sender_ids) & set(self.context_bot_ids):
            raise ValueError("Human and bot selectors must be separate")
        return self


class GitHubConfigure(Model):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    settings: GitHubSettings
    review_ref: OpaqueId


class GitHubResource(Model):
    repository: GitHubRepository
    kind: Family
    action: Annotated[str, Field(pattern=r"^[a-z_]{1,60}$")]
    number: GitHubId | None = None
    pull_requests: Annotated[tuple[GitHubId, ...], Field(max_length=100)] = ()
    head_sha: Commit | None = None
    base_sha: Commit | None = None
    head_repository_id: GitHubId | None = None
    run_id: GitHubId | None = None
    run_attempt: GitHubId | None = None
    check_run_id: GitHubId | None = None
    check_suite_id: GitHubId | None = None
    producer_app_id: GitHubId | None = None
    author_id: GitHubId | None = None


class SnapshotGrant(Model):
    repository_id: GitHubId
    head_sha: Commit
    paths: Annotated[tuple[str, ...], Field(min_length=1, max_length=100)]
    max_bytes: Annotated[int, Field(strict=True, ge=1, le=1048576)]
    expires_at: AwareDatetime
    policy_id: UUID
    policy_revision: Revision
    identity_id: UUID
    identity_revision: Revision
    pull_request: GitHubId | None = None

    @field_validator("paths")
    @classmethod
    def paths_safe(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("Duplicate snapshot path")
        return tuple(safe_path(value) for value in values)


class SnapshotSave(Model):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    definition: SnapshotGrant
    review_ref: OpaqueId


class GitHubRevoke(Model):
    expected_revision: Revision
    review_ref: OpaqueId


def utc(value: object) -> datetime:
    from datetime import UTC

    if not isinstance(value, str):
        raise ValueError("A GitHub timestamp is required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("A timezone is required")
    return result.astimezone(UTC)
