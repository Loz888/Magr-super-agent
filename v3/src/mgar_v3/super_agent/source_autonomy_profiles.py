"""Trusted, inert SA16 fixture profiles. HTTP and model data cannot install one."""

from dataclasses import dataclass
from typing import Literal

from mgar_v3.domain import DomainError, digest
from mgar_v3.super_agent.source_coding import CodingProfile
from mgar_v3.super_agent.source_scheduler_domain import ResourceRequest


@dataclass(frozen=True)
class AutonomyProfile:
    profile_sha256: str
    mode: Literal["readonly", "coding"]
    repository: str
    base_sha: str
    objective: str
    budget_profile_sha256: str
    resources: ResourceRequest
    read_path: str = "fixtures/orders/service.py"
    coding: CodingProfile | None = None

    @property
    def objective_sha256(self) -> str:
        return digest(self.objective)

    def validate(self) -> None:
        import re

        if (
            any(
                not re.fullmatch(r"[a-f0-9]{64}", s)
                for s in (
                    self.profile_sha256,
                    self.budget_profile_sha256,
                )
            )
            or not re.fullmatch(r"[a-f0-9]{40}", self.base_sha)
            or not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", self.repository)
            or not isinstance(self.resources, ResourceRequest)
            or not 1 <= len(self.objective.encode("utf-8")) <= 4096
            or self.mode not in {"readonly", "coding"}
            or (self.mode == "coding") != (self.coding is not None)
            or (self.mode == "readonly" and self.read_path != "fixtures/orders/service.py")
        ):
            raise DomainError(
                "AUTONOMY_TRUSTED_PROFILE_INVALID", "Bounded fixture profile required", 422
            )
        if self.coding is not None and (
            self.profile_sha256 != self.coding.profile_sha256
            or self.repository != self.coding.repository.repository
            or self.base_sha != self.coding.repository.base_commit
            or self.budget_profile_sha256 != self.coding.budget.profile_sha256
            or self.resources != self.coding.grant.resources
        ):
            raise DomainError(
                "AUTONOMY_CODING_PROFILE_MISMATCH", "Original Coding profile required", 422
            )
