"""Draft synthetic rule agreement only; this is not an operator-reviewed semantic holdout."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from mgar_v3.sources.domain import ActorRef, SourceEvent, SourceScope, SourceThreadRef
from mgar_v3.sources.ingress_domain import ItemMetadata
from mgar_v3.super_agent.fast_path_rules import extract_text, group_key, parse_intent

PATH = Path(__file__).resolve().parents[1] / "fixtures/super-agent-v1/sa08-fast-path/rules-v1.json"
BENCHMARK = json.loads(PATH.read_text())


def item(*, platform="feishu", actor="human", addressed="ambient", topic="Topic-A",
         entity="Entity-A", conversation="Chat-A", installation="Installation-A",
         tenant="Tenant-A", version="1", event_kind="message.created"):
    scope = SourceScope(
        source_kind=platform,
        source_instance_id=UUID("00000000-0000-0000-0000-000000000008"),
        installation_id=installation,
        tenant_id=tenant,
    )
    at = datetime(2026, 11, 12, tzinfo=UTC)
    event = SourceEvent(
        scope=scope, project_id="synthetic-sa08", source_revision=1,
        delivery_id="synthetic-delivery", remote_event_id="synthetic-event",
        entity_id=entity, entity_revision={"origin": "native", "value": version},
        event_kind=event_kind,
        source_thread=SourceThreadRef(
            scope=scope, conversation_id=conversation, native_thread_id=topic,
        ),
        sender=ActorRef(
            scope=scope, id_type="synthetic-id", actor_id="Synthetic-actor", actor_kind=actor,
        ),
        addressed=addressed, occurred_at=at, received_at=at,
        verification_ref="SYNTHETIC_RULE_FIXTURE_NOT_A_CREDENTIAL",
        provenance="verified_source",
    )
    return ItemMetadata(
        event=event, entity_namespace="synthetic-events", operation="create", native_order=1,
    )


def content(case):
    if case["container"] == "github":
        return json.dumps({
            "schema_version": "github-content-v1", "resource": {}, "text": case["text"],
        }).encode()
    if case["container"] == "feishu_text":
        return json.dumps({
            "schema_version": "feishu-content-v1", "message_type": "text",
            "content": {"text": case["text"]},
        }).encode()
    if case["container"] == "feishu_post":
        return json.dumps({
            "schema_version": "feishu-content-v1", "message_type": "post",
            "content": {"zh_cn": {"content": [[{"tag": "text", "text": case["text"]}]]}},
        }).encode()
    assert case["container"] == "plain"
    return case["text"].encode()


def test_benchmark_is_complete_synthetic_draft_with_frozen_case_payload():
    assert BENCHMARK["status"] == "DRAFT_SYNTHETIC_NO_OPERATOR_HOLDOUT"
    assert BENCHMARK["synthetic"] is True
    assert BENCHMARK["operator_review"] == "NOT_REVIEWED"
    assert BENCHMARK["independent_holdout"] is False
    assert BENCHMARK["semantic_evaluation"] == "NOT_RUN"
    assert BENCHMARK["expected_actual_model_calls"] == 0
    assert BENCHMARK["expected_actual_model_cost_micros"] == 0
    cases = BENCHMARK["syntax_cases"]
    groups = BENCHMARK["grouping_cases"]
    assert len(cases) == 50 and len(groups) == 8
    identifiers = [case["case_id"] for case in [*cases, *groups]]
    assert len(identifiers) == len(set(identifiers))
    assert all(case["synthetic"] for case in cases)
    frozen = json.dumps(
        {"syntax": cases, "grouping": groups},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode()
    assert hashlib.sha256(frozen).hexdigest() == BENCHMARK["case_payload_sha256"]
    assert {case["source_kind"] for case in cases} == {"feishu", "github"}
    assert {case["actor_kind"] for case in cases} >= {"human", "bot", "service"}


@pytest.mark.parametrize("case", BENCHMARK["syntax_cases"], ids=lambda case: case["case_id"])
def test_draft_synthetic_syntax_rule_agreement(case):
    metadata = item(
        platform=case["source_kind"], actor=case["actor_kind"], addressed=case["addressed"],
        event_kind=case["event_kind"],
    )
    result = parse_intent(metadata, extract_text(content(case)))
    actual = {
        "request_kind": result.request_kind, "command": result.command,
        "control_action": result.control_action,
        "ticket_id": str(result.ticket_id) if result.ticket_id is not None else None,
    }
    assert actual == case["expected"]


@pytest.mark.parametrize("case", BENCHMARK["grouping_cases"], ids=lambda case: case["case_id"])
def test_draft_synthetic_scoped_grouping_agreement(case):
    left, right = item(**case["left"]), item(**case["right"])
    assert (group_key(left) == group_key(right)) is case["expected_equal"]
