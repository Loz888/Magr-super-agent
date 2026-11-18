"""Author public, synthetic inputs for the frozen SA22 development draft."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DRAFT = ROOT / "fixtures/super-agent-v1/sa22-completion/development-draft.jsonl"
DRAFT_SHA256 = "09bd2e1b19e39c1fff1c104c8a157e1b6ed10278363dfa140df3406098deaf4b"
REPOSITORY = "example/mgar-test"
PUBLIC_FIELDS = (
    "schema", "case_id", "split", "synthetic", "source_kind", "occurred_at",
    "topic_group", "source_case", "mode", "text",
)
DUPLICATE_BASIS = {
    81: "delivery", 82: "canonical_revision", 83: "confirmed_reconnect_delivery",
    84: "workflow_attempt", 85: "own_notification_receipt", 86: "destination_alias",
    87: "consumed_mailbox", 88: "previously_observed_backfill",
    89: "test_receipt_identity", 90: "ack_loss_redelivery",
}


def draft_rows() -> list[dict[str, Any]]:
    data = DRAFT.read_bytes()
    if hashlib.sha256(data).hexdigest() != DRAFT_SHA256:
        raise ValueError("Frozen development draft changed")
    rows = [json.loads(line) for line in data.splitlines()]
    if [row["case_id"] for row in rows] != [f"D{number:03d}" for number in range(1, 101)]:
        raise ValueError("Expected all 100 development cases in frozen order")
    return rows


def task(number: int, source: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticket_id": f"dev-task-{number:02d}",
        "conversation_id": source["topic_group"],
        "installation": f"dev-{source['source_kind']}-installation",
        "repository": REPOSITORY,
        "relation": "native_thread",
        "head_sha": hashlib.sha1(f"synthetic-dev-tree-{number}".encode()).hexdigest(),
        "goal": source["text"],
        "active": True,
    }


def development_inputs() -> list[dict[str, Any]]:
    draft = draft_rows()
    inputs = []
    for number, row in enumerate(draft, start=1):
        case_id = row["case_id"]
        kind = row["source_kind"]
        topic = row["topic_group"]
        arrived = datetime.fromisoformat(row["occurred_at"])
        roles = ["requester", "context_provider"]
        unbound = 77 <= number <= 80 or 92 <= number <= 99
        context_only = 71 <= number <= 76
        own_actor = number == 85
        if unbound:
            roles = []
        elif context_only or own_actor:
            roles = ["context_provider"]
        actor_kind = "bot" if own_actor else "human"
        actor = "context" if context_only else "requester"
        if unbound:
            actor = f"unbound-{case_id}"
        elif own_actor:
            actor = "own-bot"
        addressed = number <= 60 or 71 <= number <= 84 or number >= 86
        # This fixture deliberately has a literal mention without a native mention.
        if number == 79:
            addressed = False

        targets = []
        if 41 <= number <= 60:
            targets = [task(number - 40, draft[number - 41])]
        elif number == 100:
            targets = [
                {**task(n, draft[99]), "goal": "登录失败", "relation": "same_repository"}
                for n in (101, 102)
            ]
        installation = f"dev-{kind}-installation"
        facts = {
            "synthetic_notice": "Developer-authored fixture facts pending operator review.",
            "scope": {
                "tenant": f"dev-{kind}-tenant",
                "installation": installation if number != 99 else "dev-other-installation",
                "conversation_id": topic,
                "native_thread_id": f"thread-{topic}",
                "source_enabled": number != 94,
                "repository_scope": [REPOSITORY],
            },
            "trusted_actor": {
                "actor_id": f"{installation}-{actor}",
                "binding_active": not unbound,
                "binding_revision": 2 if number in (93, 95) else (0 if unbound else 1),
                "current_roles": roles,
                "display_name": "开发组成员" if number != 97 else "群主",
                "identity_origin": "synthetic_platform_fixture",
                "kind": actor_kind,
            },
            "addressing": {
                "verified_direct_or_mention": addressed,
                "quoted_text_is_instruction": False,
            },
            "current_policy": {
                "policy_origin": "synthetic_owner_review_pending",
                "receive_enabled": True,
                "retain_body": True,
                "mode": "shadow",
                "revision": 1,
                "model_input_enabled": not context_only,
                "explicit_human_admission": True,
                "follow_up_admission": True,
                "team_rule": None,
                "execution_grant": None,
                "notification_grant": None,
            },
            "controller_membership": {"active": False, "ticket_id": None},
            "event": {
                "origin": "historical_backfill" if number == 88 else "synthetic_delivery",
                "event_id": f"dev-delivery-{case_id}",
                "canonical_id": f"dev-event-{case_id}",
                "type": "workflow_run.completed" if number in (44, 84) else (
                    "check_run.completed" if number == 52 else (
                        "issue_comment.created" if kind == "github" else "im.message.receive_v1"
                    )
                ),
                "current_revision": 1,
                "revision": 1,
                "deleted": False,
                "already_consumed": number == 87,
                "duplicate_basis": DUPLICATE_BASIS.get(number),
                "processing_time": (
                    arrived + timedelta(seconds=901 if number == 98 else 3)
                ).isoformat(),
                "ttl_valid": number != 98,
            },
            "task_registry": targets,
            "feedback": {
                "own_actor": own_actor,
                "known_success_receipt": "dev-notification-085" if own_actor else None,
            },
            "notification_context": {
                "approved_payload": None,
                "destination_grant": None,
                "router_proposal_grants_control": False,
            },
        }
        if kind == "github":
            facts["native_repository"] = "example/out-of-scope" if number == 92 else REPOSITORY
        inputs.append({**{field: row[field] for field in PUBLIC_FIELDS}, "event_facts": facts})
    return inputs


def received_at(row: dict[str, Any], processing_anchor: datetime) -> datetime:
    """Translate fixture age to an actual processing anchor without changing PG clocks."""
    observed = datetime.fromisoformat(row["occurred_at"])
    fixture_anchor = datetime.fromisoformat(row["event_facts"]["event"]["processing_time"])
    if any(value.utcoffset() is None for value in (observed, fixture_anchor, processing_anchor)):
        raise ValueError("Routing input times must include an offset")
    if observed > fixture_anchor:
        raise ValueError("Fixture event is newer than its processing anchor")
    return processing_anchor - (fixture_anchor - observed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New file for public development input JSONL")
    args = parser.parse_args()
    with args.output.open("x", encoding="utf-8") as stream:
        for row in development_inputs():
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
