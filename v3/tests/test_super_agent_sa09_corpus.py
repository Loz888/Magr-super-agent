"""Mechanical review-draft integrity; no model predictions or semantic scoring."""

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from mgar_v3.domain import digest
from mgar_v3.super_agent.source_eval_scoring import RoutingCorpus

PATH = Path(__file__).resolve().parents[1] / (
    "fixtures/super-agent-v1/sa09-routing/operator-review-draft-v1.json"
)
RAW = json.loads(PATH.read_text())
CORPUS = RoutingCorpus.model_validate(RAW["corpus"])
SPLITS = ("development", "proposed_holdout")
CATEGORIES = ("explicit", "ambiguous", "quoted", "automation")
UUID_TEXT = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
REPOSITORY_TEXT = re.compile(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b")


def normalized(text):
    text = UUID_TEXT.sub("UUID", text)
    text = REPOSITORY_TEXT.sub("REPOSITORY", text)
    text = re.sub(r"\d+", "NUMBER", text)
    return re.sub(r"\s+", "", text)


def grams(text):
    text = normalized(text)
    return {text[index : index + 3] for index in range(len(text) - 2)}


def test_draft_never_claims_operator_approval_independence_or_model_results():
    assert RAW["status"] == "DRAFT_SYNTHETIC_OPERATOR_REVIEW_REQUIRED"
    assert RAW["synthetic"] is True
    assert RAW["operator_review"] is None and RAW["independent_holdout"] is False
    assert RAW["semantic_evaluation"] == RAW["real_model_runs"] == "NOT_RUN"
    assert CORPUS.operator_review_ref is None
    assert CORPUS.operator_review_evidence_sha256 is None
    assert CORPUS.independent_holdout is False
    assert not {"predictions", "results", "precision", "recall"} & RAW.keys()


def test_complete_two_hundred_case_payload_is_hash_bound_and_has_distinct_content():
    assert len(CORPUS.cases) == 200
    assert len({c.case_id for c in CORPUS.cases}) == 200
    assert CORPUS.case_payload_sha256 == digest(
        [case.model_dump(mode="json") for case in CORPUS.cases]
    )
    assert len({normalized(c.text) for c in CORPUS.cases}) == 200
    assert all(c.synthetic is True and len(c.text) >= 45 for c in CORPUS.cases)


@pytest.mark.parametrize("split", SPLITS)
def test_each_split_has_exact_requested_distributions_and_denominators(split):
    cases = [case for case in CORPUS.cases if case.split == split]
    assert len(cases) == 100
    assert Counter(c.category for c in cases) == dict.fromkeys(CATEGORIES, 25)
    assert Counter(c.expected_action for c in cases) == {
        "create": 20,
        "attach": 10,
        "ask": 10,
        "observe": 30,
        "ignore": 30,
    }
    assert sum(c.actionable for c in cases) == 40
    assert sum(c.expected_action != "create" for c in cases) == 80
    assert sum(not c.actionable for c in cases) == 60
    assert any(not c.receive_allowed for c in cases)
    assert any(c.receive_allowed and not c.model_input_allowed for c in cases)


@pytest.mark.parametrize("split", SPLITS)
@pytest.mark.parametrize("category", CATEGORIES)
def test_category_allocation_keeps_explicit_ambiguous_quoted_and_automation_separate(
    split, category
):
    cases = [c for c in CORPUS.cases if c.split == split and c.category == category]
    expected = {
        "explicit": {"create": 15, "attach": 10},
        "ambiguous": {"ask": 10, "observe": 15},
        "quoted": {"ignore": 20, "observe": 5},
        "automation": {"create": 5, "observe": 10, "ignore": 10},
    }
    assert Counter(c.expected_action for c in cases) == expected[category]


def test_full_trusted_context_is_transitively_bound_without_gold_model_answers():
    contexts = RAW["case_contexts"]
    assert set(contexts) == {case.case_id for case in CORPUS.cases}
    forbidden = {"expected_action", "expected_target_id", "actionable", "gold", "answer"}

    def inspect(value):
        if isinstance(value, dict):
            assert not forbidden & value.keys()
            assert (
                not {"api_key", "access_token", "signing_secret", "cookie", "proof"} & value.keys()
            )
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)

    for case in CORPUS.cases:
        context = contexts[case.case_id]
        inspect(context)
        assert context["synthetic"] is True and context["operator_review"] is None
        assert case.source_case == "synthetic-" + digest(context)
        assert case.topic_group == context["event"]["topic_group"]
        assert case.conversation == context["scope"]["conversation"]
        assert case.actor_role == context["actor"]["recorded_role_class"]
        assert case.receive_allowed == context["source_policy"]["receive_for_fixture"]
        assert case.model_input_allowed == context["source_policy"]["model_input_for_fixture"]
        assert context["source_policy"]["real_signature_or_approval_record"] is False
        assert all(value is False for value in context["runtime"].values())


def test_topics_source_discussions_domains_and_simulated_times_do_not_cross_splits():
    contexts = RAW["case_contexts"]
    axes = {}
    for split in SPLITS:
        cases = [c for c in CORPUS.cases if c.split == split]
        axes[split] = {
            "topics": {c.topic_group for c in cases},
            "source_cases": {c.source_case for c in cases},
            "discussions": {contexts[c.case_id]["event"]["source_discussion_id"] for c in cases},
            "domains": {contexts[c.case_id]["domain"] for c in cases},
        }
        assert all(
            len(axes[split][key]) == 100 for key in ("topics", "source_cases", "discussions")
        )
        start, stop = map(
            datetime.fromisoformat, RAW["split_design"][split]["synthetic_time_window"]
        )
        for case in cases:
            current = datetime.fromisoformat(contexts[case.case_id]["current_time_for_fixture"])
            assert start <= current < stop
    assert all(not axes[SPLITS[0]][axis] & axes[SPLITS[1]][axis] for axis in axes[SPLITS[0]])
    development_end = datetime.fromisoformat(
        RAW["split_design"][SPLITS[0]]["synthetic_time_window"][1]
    )
    proposed_start = datetime.fromisoformat(
        RAW["split_design"][SPLITS[1]]["synthetic_time_window"][0]
    )
    assert development_end < proposed_start


def test_cross_split_text_does_not_become_a_clone_after_ids_and_numbers_are_removed():
    left = [grams(c.text) for c in CORPUS.cases if c.split == SPLITS[0]]
    right = [grams(c.text) for c in CORPUS.cases if c.split == SPLITS[1]]
    # This only catches substantial lexical copying; it cannot establish semantic independence.
    assert all(len(a & b) / len(a | b) < 0.35 for a in left for b in right)


@pytest.mark.parametrize("split,short", [("development", "dev"), ("proposed_holdout", "proposal")])
def test_exact_attachments_only_use_ten_stable_declared_synthetic_references(split, short):
    cases = [c for c in CORPUS.cases if c.split == split and c.expected_action == "attach"]
    declared = [t for t in RAW["declared_synthetic_target_refs"] if t["split"] == split]
    assert len(cases) == len(declared) == 10
    assert {str(c.expected_target_id) for c in cases} == {t["ticket_id"] for t in declared}
    assert {t["ticket_id"] for t in declared} == {
        str(
            uuid5(
                NAMESPACE_URL,
                "mgar-sa09-operator-review-draft-v1:" + short + "-declared-task-" + str(i),
            )
        )
        for i in range(1, 11)
    }
    for case in cases:
        context = RAW["case_contexts"][case.case_id]
        (target,) = context["candidate_target_refs"]
        assert str(case.expected_target_id) in case.text
        assert target["ticket_id"] == str(case.expected_target_id)
        assert target["repository"] == case.repository
        assert target["conversation"] == case.conversation
        assert target["reference_state"] == "DECLARED_SYNTHETIC_REFERENCE_ONLY"
        assert target["persisted_business_object"] is False
        assert target["operator_review"] is None
        assert target["controller_membership_for_fixture"] is True
        assert target["follow_up_admission_for_fixture"] is True
        assert context["actor"]["exact_controller_ticket_ids_for_fixture"] == [target["ticket_id"]]
        assert case.actor_role == "task_controller" and case.control_allowed


@pytest.mark.parametrize("split", SPLITS)
def test_automation_creates_need_separate_synthetic_administrator_rule_assumptions(split):
    cases = [
        c
        for c in CORPUS.cases
        if c.split == split and c.category == "automation" and c.expected_action == "create"
    ]
    assert len(cases) == 5
    for case in cases:
        context = RAW["case_contexts"][case.case_id]
        rule = context["source_policy"]["team_rule"]
        assert context["actor"]["kind"] == "bot" and case.actor_role == "context_provider"
        assert rule["authority"] == "synthetic-administrator-assumption"
        assert rule["assumed_current_for_fixture"] and rule["matches_this_event"]
        assert rule["definition_sha256"] == digest(rule["definition"])
        assert rule["definition"]["scope_repository"] == case.repository
        assert rule["operator_review"] is None
        assert rule["real_signature_or_approval_record"] is False
        assert "team_rule" in context["source_policy"]["admission_request_kinds_for_fixture"]
        assert not case.control_allowed and not case.notification_allowed


def test_all_resources_and_permissions_are_fixture_declarations_without_activation():
    for case in CORPUS.cases:
        context = RAW["case_contexts"][case.case_id]
        assert case.repository is None or case.repository.startswith("synthetic-sa09/")
        assert case.conversation.startswith("synthetic-")
        assert context["scope"]["project_id"].startswith("synthetic-")
        assert context["scope"]["installation_id"].startswith("synthetic-")
        assert context["actor"]["actor_id"].startswith("synthetic-")
        assert case.notification_allowed is False
        assert context["source_policy"]["mode"] == "shadow"
