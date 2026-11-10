"""Thin compatibility adapters for the legacy read-only and repair services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from mgar_v3.domain import Model
from mgar_v3.repair.repair_agent import RepairProvider, RepairReport, RepairRequest


class InspectionView(Model):
    run_id: UUID
    read_only: bool = True
    report: dict[str, Any]
    evidence_ids: tuple[str, ...] = ()


class LegacyAnalysisAdapter:
    """Expose an existing read-only analysis run without granting writes.

    The composition root supplies the legacy runner.  This adapter intentionally
    has no edit, command or approval methods.
    """

    def __init__(self, inspect_run: Callable[[UUID], dict[str, Any]]) -> None:
        self._inspect_run = inspect_run

    def inspect(self, run_id: UUID) -> InspectionView:
        report = self._inspect_run(run_id)
        evidence = report.get("evidence_ids", ())
        evidence_ids = tuple(item for item in evidence if isinstance(item, str))
        return InspectionView(run_id=run_id, report=report, evidence_ids=evidence_ids)


class RepairRunner(Protocol):
    def run(self, request: RepairRequest, provider: RepairProvider) -> RepairReport: ...


class LegacyRepairAdapter:
    """Delegate to the existing RepairAgent without adding a second authority."""

    def __init__(self, runner: RepairRunner) -> None:
        self._runner = runner

    def repair(self, request: RepairRequest, provider: RepairProvider) -> RepairReport:
        return self._runner.run(request, provider)
