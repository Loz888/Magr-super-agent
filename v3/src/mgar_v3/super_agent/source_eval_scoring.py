"""Offline routing accounting, never an authorization or independent quality gate.

Every frozen case remains in the denominator, including unknown and not-run
attempts. Operator review and real-provider evidence are reported separately;
controlled agreement cannot establish semantic quality.
"""

import math
from collections import Counter
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.sources.domain import OpaqueId
from mgar_v3.super_agent.source_eval_domain import SHA256, SourceAction

Category = Literal["explicit", "ambiguous", "quoted", "automation"]
Nonnegative = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class RoutingCase(Model):
    case_id: OpaqueId
    category: Category
    split: Literal["development", "proposed_holdout"]
    topic_group: OpaqueId
    source_case: OpaqueId
    text: Annotated[str, Field(min_length=1, max_length=8000)]
    synthetic: bool = Field(strict=True)
    receive_allowed: bool = Field(strict=True)
    model_input_allowed: bool = Field(strict=True)
    actionable: bool = Field(strict=True)
    expected_action: SourceAction
    repository: str | None = None
    conversation: OpaqueId
    expected_target_id: UUID | None = None
    attribution_decidable: bool = Field(default=True, strict=True)
    actor_role: Literal["context_provider", "requester", "task_controller"]
    control_allowed: bool = Field(default=False, strict=True)
    notification_allowed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def coherent_labels(self) -> Self:
        if self.model_input_allowed and not self.receive_allowed:
            raise ValueError("Model input requires receive permission")
        if self.actionable != (self.expected_action in {"create", "attach", "ask"}):
            raise ValueError("Actionability must agree with the expected action")
        if self.expected_action == "attach":
            if self.attribution_decidable and self.expected_target_id is None:
                raise ValueError("Decidable attachment requires an exact target")
        elif self.expected_target_id is not None:
            raise ValueError("Only attachment may name a target")
        if self.expected_action in {"create", "attach"} and self.repository is None:
            raise ValueError("Creation and attachment require a repository label")
        return self


class RoutingCorpus(Model):
    schema_version: Literal["source-router-evaluation-corpus-v1"]
    cases: Annotated[tuple[RoutingCase, ...], Field(min_length=1, max_length=10000)]
    case_payload_sha256: SHA256
    operator_review_ref: OpaqueId | None = None
    operator_review_evidence_sha256: SHA256 | None = None
    independent_holdout: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def fixed_complete_cases(self) -> Self:
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("Case identifiers must be distinct")
        if digest([c.model_dump(mode="json") for c in self.cases]) != self.case_payload_sha256:
            raise ValueError("The complete case payload changed")
        if (self.operator_review_ref is None) != (self.operator_review_evidence_sha256 is None):
            raise ValueError("Operator review requires its evidence hash")
        splits: dict[str, set[tuple[str, str]]] = {}
        for case in self.cases:
            splits.setdefault(case.split, set()).add((case.topic_group, case.source_case))
        dev, holdout = splits.get("development", set()), splits.get("proposed_holdout", set())
        if {x[0] for x in dev} & {x[0] for x in holdout}:
            raise ValueError("A topic cannot appear in both splits")
        if {x[1] for x in dev} & {x[1] for x in holdout}:
            raise ValueError("A source case cannot appear in both splits")
        if self.independent_holdout and self.operator_review_ref is None:
            raise ValueError("An independent holdout requires operator review evidence")
        return self


class RoutingPrediction(Model):
    case_id: OpaqueId
    status: Literal["completed", "failed", "unknown", "not_run"]
    action: SourceAction | None = None
    target_id: UUID | None = None
    repository: str | None = None
    model_called: bool = Field(default=False, strict=True)
    settled_cost_micros: Nonnegative = 0
    unknown_reservation_micros: Nonnegative = 0
    created_tasks: Nonnegative = 0
    sent_messages: Nonnegative = 0
    publications: Nonnegative = 0

    @model_validator(mode="after")
    def observed_only(self) -> Self:
        if (self.status == "completed") != (self.action is not None):
            raise ValueError("Only a completed prediction may claim an action")
        if self.target_id is not None and self.action != "attach":
            raise ValueError("Only an attachment may claim a target")
        if self.status != "unknown" and self.unknown_reservation_micros:
            raise ValueError("Only unknown attempts may carry an unknown reservation")
        if not self.model_called and (self.settled_cost_micros or self.unknown_reservation_micros):
            raise ValueError("Model financial facts require an observed model attempt")
        return self


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    if denominator == 0:
        return {"numerator": numerator, "denominator": 0, "value": None, "wilson95": None}
    value, z = numerator / denominator, 1.96
    divisor = 1 + z * z / denominator
    center = (value + z * z / (2 * denominator)) / divisor
    margin = z * math.sqrt(
        value * (1 - value) / denominator + z * z / (4 * denominator * denominator)
    ) / divisor
    return {"numerator": numerator, "denominator": denominator, "value": value,
            "wilson95": [max(0.0, center - margin), min(1.0, center + margin)]}


def _correct(case: RoutingCase, prediction: RoutingPrediction) -> bool:
    return (
        prediction.status == "completed" and prediction.action == case.expected_action
        and (case.expected_action not in {"create", "attach"}
             or prediction.repository == case.repository)
        and (case.expected_action != "attach"
             or (case.attribution_decidable and prediction.target_id == case.expected_target_id))
    )


def _statistics(cases: tuple[RoutingCase, ...],
                predictions: dict[str, RoutingPrediction]) -> dict[str, Any]:
    rows = [(case, predictions.get(case.case_id, RoutingPrediction(
        case_id=case.case_id, status="not_run",
    ))) for case in cases]
    create = [(c, p) for c, p in rows if p.action == "create"]
    actionable = [(c, p) for c, p in rows if c.actionable]
    followup = [(c, p) for c, p in rows
                if c.expected_action == "attach" and c.attribution_decidable]
    return {
        "cases": len(rows), "status": dict(Counter(p.status for _, p in rows)),
        "create_precision": _rate(sum(_correct(c, p) for c, p in create), len(create)),
        "actionable_recall": _rate(sum(_correct(c, p) for c, p in actionable), len(actionable)),
        "followup_attribution": _rate(sum(_correct(c, p) for c, p in followup), len(followup)),
        "undecidable_followups": sum(
            c.expected_action == "attach" and not c.attribution_decidable for c, _ in rows
        ),
        "unauthorized_model_inputs": sum(
            p.model_called and not c.model_input_allowed for c, p in rows
        ),
        "shadow_side_effects": {
            "created_tasks": sum(p.created_tasks for _, p in rows),
            "sent_messages": sum(p.sent_messages for _, p in rows),
            "publications": sum(p.publications for _, p in rows),
        },
        "model_attempts": sum(p.model_called for _, p in rows),
        "settled_cost_micros": sum(p.settled_cost_micros for _, p in rows),
        "unknown_reservation_micros": sum(p.unknown_reservation_micros for _, p in rows),
    }


def score(corpus: RoutingCorpus, results: tuple[RoutingPrediction, ...], *,
          run_kind: Literal["controlled", "real_model"], source_sha256: str,
          prompt_sha256: str) -> dict[str, Any]:
    """Produce denominators and uncertainty; independent reviewers decide gates."""
    corpus = RoutingCorpus.model_validate(corpus.model_dump())
    results = tuple(RoutingPrediction.model_validate(p.model_dump()) for p in results)
    if run_kind not in {"controlled", "real_model"}:
        raise DomainError("SOURCE_EVAL_SCORE_MODE", "An explicit evaluation mode is required", 422)
    if any(len(h) != 64 or any(c not in "0123456789abcdef" for c in h)
           for h in (source_sha256, prompt_sha256)):
        raise DomainError("SOURCE_EVAL_SCORE_VERSION", "Source and prompt hashes are required", 422)
    predictions = {p.case_id: p for p in results}
    if (
        len(predictions) != len(results)
        or not set(predictions) <= {c.case_id for c in corpus.cases}
    ):
        raise DomainError("SOURCE_EVAL_SCORE_CASES", "Unknown or repeated case results", 422)
    protocol_counts = {}
    for split in ("development", "proposed_holdout"):
        cases = [c for c in corpus.cases if c.split == split]
        protocol_counts[split] = {
            "cases": len(cases), "actionable": sum(c.actionable for c in cases),
            "not_new_task": sum(c.expected_action != "create" for c in cases),
        }
    return {
        "schema_version": "source-router-evaluation-result-v1", "run_kind": run_kind,
        "source_sha256": source_sha256, "prompt_sha256": prompt_sha256,
        "case_payload_sha256": corpus.case_payload_sha256,
        "operator_review_ref": corpus.operator_review_ref,
        "operator_review_evidence_sha256": corpus.operator_review_evidence_sha256,
        "independent_holdout": corpus.independent_holdout,
        "protocol_counts": protocol_counts,
        "sa00_case_counts_met": all(
            v["cases"] >= 100 and v["actionable"] >= 40 and v["not_new_task"] >= 60
            for v in protocol_counts.values()
        ),
        "all": _statistics(corpus.cases, predictions),
        "by_category": {kind: _statistics(
            tuple(c for c in corpus.cases if c.category == kind), predictions,
        ) for kind in ("explicit", "ambiguous", "quoted", "automation")},
        "semantic_quality_gate": "NOT_RUN",
        "gate_note": (
            "Counts and review references are evidence claims, not authorization or "
            "an independent semantic verdict; controlled outputs are not real-model quality."
        ),
    }
