"""Server-only SA18 ports. Construction does not capture files or start a worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from mgar_v3.domain import digest
from mgar_v3.repair.pr_delivery import ReadTransport, WriteTransport
from mgar_v3.super_agent.source_review_domain import ReviewContext


class ReviewCapture(Protocol):
    def capture(self, context: ReviewContext) -> tuple[dict[str, str], dict[str, str]]:
        """Read actual baseline/tested files outside every business SQL transaction."""
        ...


class ReviewReader(ReadTransport, Protocol):
    def commit_identity(self, repository: str, commit: str) -> tuple[str, tuple[str, ...]] | None:
        """Return full Git tree SHA and all parent SHAs, from the read-only port."""
        ...


@dataclass(frozen=True)
class ReviewProfile:
    """An explicitly injected controlled remote, not a real-publication permit.

    Neither an environment flag, a Coding grant nor an HTTP parameter can make
    this a live profile. Read and write ports remain separate objects.
    """

    name: str
    worker_profile_sha256: str
    repository: str
    repository_id: int
    base_branch: str
    actor: str
    valid_until: datetime
    capture: ReviewCapture = field(repr=False)
    reader: ReviewReader = field(repr=False)
    writer: WriteTransport = field(repr=False)
    write_authorized: bool = False
    mode: Literal["fixture"] = "fixture"

    def __post_init__(self) -> None:
        if (
            self.mode != "fixture"
            or not self.name
            or len(self.name) > 100
            or len(self.worker_profile_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.worker_profile_sha256)
            or self.repository != self.repository.lower()
            or len(self.repository.split("/")) != 2
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or not self.base_branch
            or not self.actor
            or self.valid_until.utcoffset() is None
            or self.reader is self.writer
        ):
            raise ValueError("bounded explicit fixture read/write profiles required")

    @property
    def sha256(self) -> str:
        return digest(
            {
                "schema_version": "source-review-profile-v1",
                "name": self.name,
                "worker_profile_sha256": self.worker_profile_sha256,
                "repository": self.repository,
                "repository_id": self.repository_id,
                "base_branch": self.base_branch,
                "actor": self.actor,
                "valid_until": self.valid_until.isoformat(),
                "write_authorized": self.write_authorized,
                "mode": self.mode,
            }
        )


@dataclass(frozen=True)
class ReviewRemoteBinding:
    review_id: UUID
    action_id: UUID
    lease_token: UUID
