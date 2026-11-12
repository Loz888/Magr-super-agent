"""Independent hand-counted scoring examples, not a model quality experiment."""

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError, digest
from mgar_v3.super_agent.source_eval_scoring import (
    RoutingCase,
    RoutingCorpus,
    RoutingPrediction,
    score,
)

REPOSITORY = "synthetic/router-example"
TARGET = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TARGET = UUID("22222222-2222-4222-8222-222222222222")


def corpus():
    definitions = [
        ("a", "explicit", "create", "Please investigate a new login error.", None, True),
        ("b", "explicit", "create", "Please fix duplicate quantities in the cart.", None, True),
        ("c", "ambiguous", "ask", "Help me with this.", None, True),
        ("d", "quoted", "ignore", "The manual quotes /mgar repair as an example.", None, True),
        ("e", "automation", "observe", "Build passed; no new failure was observed.", None, True),
        ("f", "explicit", "attach", "Add this log to the exact existing task.", TARGET, True),
        ("g", "ambiguous", "attach", "Yesterday's problem has another symptom.", None, False),
        ("h", "ambiguous", "ask", "Can this be handled? The target repository is unclear.",
         None, True),
    ]
    cases = tuple(RoutingCase(
        case_id=key, category=category, split="development", topic_group="topic-" + key,
        source_case="source-" + key, text=text, synthetic=True,
        receive_allowed=key != "d", model_input_allowed=key != "d",
        actionable=action in {"create", "attach", "ask"}, expected_action=action,
        repository=REPOSITORY if action in {"create", "attach"} else None,
        conversation="synthetic-chat", expected_target_id=target,
        attribution_decidable=decidable, actor_role="requester",
    ) for key, category, action, text, target, decidable in definitions)
    return RoutingCorpus(
        schema_version="source-router-evaluation-corpus-v1", cases=cases,
        case_payload_sha256=digest([c.model_dump(mode="json") for c in cases]),
    )


def predictions():
    completed = {"a": "create", "c": "create", "d": "create", "e": "observe",
                 "f": "attach", "h": "ask"}
    rows = tuple(RoutingPrediction(
        case_id=key, status="completed", action=action, repository=REPOSITORY,
        target_id=OTHER_TARGET if key == "f" else None,
        model_called=True, settled_cost_micros=2,
    ) for key, action in completed.items())
    return (*rows, RoutingPrediction(case_id="g", status="unknown", model_called=True,
                                    unknown_reservation_micros=5))


def report(value=None, rows=None):
    return score(value or corpus(), predictions() if rows is None else rows,
                 run_kind="controlled", source_sha256="a" * 64, prompt_sha256="b" * 64)


def test_hand_counted_false_positives_missing_attempt_and_wrong_target_keep_denominators():
    result = report()
    total = result["all"]
    assert total["cases"] == 8
    assert total["status"] == {"completed": 6, "not_run": 1, "unknown": 1}
    assert total["create_precision"]["numerator"] == 1
    assert total["create_precision"]["denominator"] == 3
    assert total["create_precision"]["value"] == pytest.approx(1 / 3)
    assert total["actionable_recall"]["numerator"] == 2
    assert total["actionable_recall"]["denominator"] == 6
    assert total["actionable_recall"]["value"] == pytest.approx(2 / 6)
    assert total["followup_attribution"]["numerator"] == 0
    assert total["followup_attribution"]["denominator"] == 1
    assert total["undecidable_followups"] == 1
    assert total["unauthorized_model_inputs"] == 1
    assert total["model_attempts"] == 7
    assert total["settled_cost_micros"] == 12
    assert total["unknown_reservation_micros"] == 5
    assert total["shadow_side_effects"] == {
        "created_tasks": 0, "sent_messages": 0, "publications": 0,
    }
    assert result["by_category"]["explicit"]["cases"] == 3
    assert result["by_category"]["ambiguous"]["cases"] == 3
    assert result["by_category"]["quoted"]["cases"] == 1
    assert result["by_category"]["automation"]["cases"] == 1
    assert sum(g["cases"] for g in result["by_category"].values()) == total["cases"]


def test_zero_predictions_is_undefined_precision_and_zero_recall_not_perfect_score():
    result = report(rows=())["all"]
    assert result["status"] == {"not_run": 8}
    assert result["create_precision"] == {
        "numerator": 0, "denominator": 0, "value": None, "wilson95": None,
    }
    assert result["actionable_recall"]["value"] == 0
    assert result["actionable_recall"]["denominator"] == 6


def test_wrong_repository_is_not_counted_as_a_correct_creation():
    rows = tuple(p.model_copy(update={"repository": "other/repository"})
                 if p.case_id == "a" else p for p in predictions())
    result = report(rows=rows)["all"]
    assert result["create_precision"]["numerator"] == 0
    assert result["create_precision"]["denominator"] == 3
    assert result["actionable_recall"]["numerator"] == 1
    assert result["actionable_recall"]["denominator"] == 6


def test_shadow_effects_and_unauthorized_inputs_are_measured_separately_from_accuracy():
    rows = tuple(p.model_copy(update={"created_tasks": 1, "sent_messages": 2,
                                      "publications": 1})
                 if p.case_id == "a" else p for p in predictions())
    total = report(rows=rows)["all"]
    assert total["create_precision"]["numerator"] == 1
    assert total["shadow_side_effects"] == {
        "created_tasks": 1, "sent_messages": 2, "publications": 1,
    }
    assert total["unauthorized_model_inputs"] == 1


def test_small_sample_interval_keeps_visible_uncertainty():
    rate = report()["all"]["create_precision"]
    low, high = rate["wilson95"]
    assert 0 < low < rate["value"] < high < 1
    assert high - low > 0.5


def test_controlled_results_never_claim_real_semantic_or_holdout_acceptance():
    result = report()
    assert result["run_kind"] == "controlled"
    assert result["semantic_quality_gate"] == "NOT_RUN"
    assert result["independent_holdout"] is False
    assert result["operator_review_ref"] is None
    assert not result["sa00_case_counts_met"]
    assert result["protocol_counts"]["development"] == {
        "cases": 8, "actionable": 6, "not_new_task": 6,
    }


@pytest.mark.parametrize("case", ["duplicate", "unknown"])
def test_best_of_or_unknown_case_results_are_rejected(case):
    rows = predictions()
    rows += (rows[0] if case == "duplicate" else rows[0].model_copy(
        update={"case_id": "unregistered"},
    ),)
    with pytest.raises(DomainError) as error:
        report(rows=rows)
    assert error.value.code == "SOURCE_EVAL_SCORE_CASES"


@pytest.mark.parametrize("field", ["source_sha256", "prompt_sha256"])
def test_unbound_source_or_prompt_report_is_rejected(field):
    kwargs = {"source_sha256": "a" * 64, "prompt_sha256": "b" * 64}
    kwargs[field] = "unknown"
    with pytest.raises(DomainError) as error:
        score(corpus(), predictions(), run_kind="controlled", **kwargs)
    assert error.value.code == "SOURCE_EVAL_SCORE_VERSION"


@pytest.mark.parametrize("change", [
    {"status": "unknown", "action": "create"}, {"status": "completed"},
    {"status": "failed", "action": "ignore"},
    {"status": "not_run", "unknown_reservation_micros": 1},
    {"status": "unknown", "unknown_reservation_micros": 1},
    {"status": "completed", "action": "create", "settled_cost_micros": 1},
    {"status": "unknown", "settled_cost_micros": -1},
    {"status": "unknown", "model_called": 1},
])
def test_unknown_nonattempts_and_financial_types_cannot_claim_known_success(change):
    with pytest.raises(ValidationError):
        RoutingPrediction(case_id="case", **change)


def test_frozen_corpus_binds_actual_text_and_full_labels():
    data = corpus().model_dump(mode="json")
    data["cases"][0]["text"] = "changed source case"
    with pytest.raises(ValidationError):
        RoutingCorpus.model_validate(data)


@pytest.mark.parametrize("axis", ["topic_group", "source_case"])
def test_renamed_case_id_cannot_reuse_a_development_discussion_in_holdout(axis):
    value = corpus()
    changed = value.cases[0].model_copy(update={
        "case_id": "holdout-new-id", "split": "proposed_holdout",
        "topic_group": "different-topic", "source_case": "different-source",
        axis: getattr(value.cases[0], axis),
    })
    cases = (*value.cases, changed)
    with pytest.raises(ValidationError):
        RoutingCorpus(
            schema_version="source-router-evaluation-corpus-v1", cases=cases,
            case_payload_sha256=digest([c.model_dump(mode="json") for c in cases]),
        )


def test_independence_cannot_be_declared_without_operator_evidence():
    value = corpus()
    data = value.model_dump()
    data["independent_holdout"] = True
    with pytest.raises(ValidationError):
        RoutingCorpus.model_validate(data)


def test_checked_in_examples_are_explicitly_draft_and_match_complete_case_hash():
    path = Path(__file__).resolve().parents[1] / (
        "fixtures/super-agent-v1/sa09-routing/draft-cases-v1.json"
    )
    raw = json.loads(path.read_text())
    assert raw["status"] == "DRAFT_SYNTHETIC_OPERATOR_REVIEW_REQUIRED"
    value = RoutingCorpus.model_validate(raw["corpus"])
    assert value.case_payload_sha256 == corpus().case_payload_sha256
    assert value.operator_review_ref is None and not value.independent_holdout
