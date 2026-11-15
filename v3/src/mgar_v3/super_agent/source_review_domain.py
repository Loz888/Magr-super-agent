"""SA18 private contracts. These values describe evidence, never confer authority.

The HTTP commands deliberately contain identifiers and exact digests only. A
caller cannot supply a diff, test success, credential, actor, or remote receipt.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mgar_v3.domain import digest
from mgar_v3.super_agent.source_coding_domain import (
    CodingModel,
    CodingOrigin,
    CodingTarget,
    Commit,
    EngineActor,
)
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef
from mgar_v3.super_agent.source_scheduler_domain import SHA256, Counter, Revision, UTCTime

REVIEW_VERSION = "source-review-v1"
MAX_REVIEW_BYTES = 1_048_576
MAX_REVIEW_SECONDS = 3600
PRESENTATION_SECONDS = 120


class ReviewPrepare(CodingModel):
    request_id: UUID
    coding_id: UUID
    verification_id: UUID
    expected_epoch: Revision
    expected_input_revision: Revision
    profile_sha256: SHA256


class ReviewExact(CodingModel):
    bundle_sha256: SHA256
    action_sha256: SHA256


class ReviewApprove(ReviewExact):
    presentation_id: UUID


class ReviewNativeLink(ReviewExact):
    source_input: CorrelationInputRef


class ReviewCycle(CodingModel):
    limit: Annotated[int, Field(strict=True, ge=1, le=32)] = 8


class ReviewTarget(CodingModel):
    repository: Annotated[str, Field(pattern=r"^[a-z0-9_.-]+/[a-z0-9_.-]+$")]
    repository_id: Revision
    base_branch: Annotated[
        str, Field(pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){0,3}$", max_length=200)
    ]
    base_sha: Commit
    branch: Annotated[str, Field(pattern=r"^sa18/[a-f0-9]{32}$")]
    mode: Literal["fixture"] = "fixture"
    source_kind: Literal["fixture_git"] = "fixture_git"

    @model_validator(mode="after")
    def separate_branch(self) -> Self:
        if self.base_branch == self.branch:
            raise ValueError("publication branch must differ from the base branch")
        return self


class ReviewVerification(CodingModel):
    """Projection of one original Verifier row and its trusted execution receipt."""

    verification_id: UUID
    operation_id: UUID
    execution_operation_id: UUID
    attempt_id: UUID
    coding_id: UUID
    mandate_sha256: SHA256
    receipt_sha256: SHA256
    tree_sha256: SHA256
    environment_sha256: SHA256
    profile_sha256: SHA256
    acceptance_sha256: SHA256
    provenance_sha256: SHA256
    result: Literal["passed"]
    tests_run: Annotated[int, Field(strict=True, gt=0, le=1_000_000)]
    tests_failed: Literal[0] = 0
    observed_at: UTCTime


class ReviewContext(CodingModel):
    review_id: UUID
    coding_id: UUID
    target: CodingTarget
    origin: CodingOrigin
    attempt_id: UUID
    assignment_id: UUID
    session_id: UUID
    workspace_id: UUID
    epoch: Revision
    input_revision: Revision
    async_revision: Counter
    async_input_revision: Counter
    mandate_sha256: SHA256
    coding_revision: Revision
    worker_profile_sha256: SHA256
    profile_sha256: SHA256
    environment_sha256: SHA256
    acceptance_sha256: SHA256
    baseline_tree_sha256: SHA256
    registration_sha256: SHA256
    context_sha256: SHA256
    publication: ReviewTarget
    verification: ReviewVerification
    actor: EngineActor
    prepared_by: UUID
    prepared_at: UTCTime
    expires_at: UTCTime

    @model_validator(mode="after")
    def one_result(self) -> Self:
        v = self.verification
        if (
            v.coding_id != self.coding_id
            or v.attempt_id != self.attempt_id
            or v.mandate_sha256 != self.mandate_sha256
            or v.environment_sha256 != self.environment_sha256
            or v.profile_sha256 != self.worker_profile_sha256
            or v.acceptance_sha256 != self.acceptance_sha256
            or v.observed_at > self.prepared_at
            or not 0 < (self.expires_at - self.prepared_at).total_seconds() <= MAX_REVIEW_SECONDS
            or self.expires_at > self.origin.expires_at
        ):
            raise ValueError("review must freeze one current verified result and a bounded expiry")
        return self

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


class ReviewCurrent(CodingModel):
    """Only the Engine may build this current SQL authority projection."""

    context_sha256: SHA256
    epoch: Revision
    input_revision: Revision
    async_revision: Counter
    async_input_revision: Counter
    tree_sha256: SHA256
    profile_sha256: SHA256
    user_id: UUID
    actor: EngineActor
    source_current: bool = False
    roles_current: bool = False
    policy_current: bool = False
    target_current: bool = False
    verification_current: bool = False
    primary_current: bool = False
    write_grant_current: bool = False
    no_running_jobs: bool = False
    owner_current: bool = False
    mode: Literal["fixture", "live"] = "fixture"
    run_state: Literal["running", "waiting_approval", "succeeded", "failed", "cancelled"]


class ReviewPresentation(CodingModel):
    presentation_id: UUID
    review_id: UUID
    user_id: UUID
    session_sha256: SHA256
    context_sha256: SHA256
    bundle_sha256: SHA256
    action_sha256: SHA256
    presented_at: UTCTime
    expires_at: UTCTime

    @model_validator(mode="after")
    def short_lived(self) -> Self:
        if not 0 < (self.expires_at - self.presented_at).total_seconds() <= PRESENTATION_SECONDS:
            raise ValueError("full bundle presentation must be short lived")
        return self
