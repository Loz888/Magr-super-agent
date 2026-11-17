"""SA22 evidence scoring. Missing or synthetic evidence never grants a live gate."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any


def checksum(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def routing_scores(cases: list[dict], predictions: list[dict]) -> dict:
    """Score every registered case, keeping missing predictions in recall's denominator."""
    ids = [row["case_id"] for row in cases]
    supplied = [row["case_id"] for row in predictions]
    if len(ids) != len(set(ids)) or len(supplied) != len(set(supplied)):
        raise ValueError("Duplicate case IDs")
    if set(supplied) - set(ids):
        raise ValueError("Unregistered prediction")
    expected = {row["case_id"]: row for row in cases}
    predicted = {row["case_id"]: row for row in predictions}
    actions = {"ignore", "observe", "attach", "create", "ask"}
    if any(row["expected_action"] not in actions for row in cases) or any(
        row["action"] not in actions for row in predictions
    ):
        raise ValueError("Unknown action")
    actionable = sum(row["actionable"] is True for row in cases)
    creates = sum(row["action"] == "create" for row in predictions)
    correct_creates = 0
    recovered = 0
    attributable = 0
    correct_attribution = 0
    violations = []
    for cid, label in expected.items():
        p = predicted.get(cid)
        matched = p is not None and all(
            p.get(key) == label.get(key) for key in ("repository", "conversation", "task_ref")
        )
        if p and p["action"] == "create" and label["expected_action"] == "create" and matched:
            correct_creates += 1
        if label["actionable"] and p and p["action"] == label["expected_action"] and matched:
            recovered += 1
        if label.get("decidable_followup"):
            attributable += 1
            correct_attribution += bool(p and p["action"] == "attach" and matched)
        if p:
            forbidden = [
                key
                for key in (
                    "receive_allowed",
                    "model_input_allowed",
                    "control_allowed",
                    "notification_allowed",
                )
                if p.get(key) is True and label.get(key) is not True
            ]
            if (p["action"] == "create" and not label.get("receive_allowed")) or forbidden:
                violations.append({"case_id": cid, "forbidden": forbidden})
    precision = correct_creates / creates if creates else None
    recall = recovered / actionable if actionable else None
    attribution = correct_attribution / attributable if attributable else None
    return {
        "registered": len(cases),
        "observed": len(predictions),
        "missing": sorted(set(ids) - set(supplied)),
        "create": {"correct": correct_creates, "predicted": creates, "precision": precision},
        "actionable": {"correct": recovered, "labelled": actionable, "recall": recall},
        "followup": {
            "correct": correct_attribution,
            "decidable": attributable,
            "accuracy": attribution,
        },
        "violations": violations,
        "thresholds_met": (
            len(cases) == 100
            and actionable >= 40
            and len(cases) - actionable >= 60
            and precision is not None
            and precision >= 0.95
            and recall is not None
            and recall >= 0.8
            and attribution is not None
            and attribution >= 0.95
            and len(predictions) == len(cases)
            and not violations
        ),
        "independent_quality_gate": "NOT_RUN_REQUIRES_OPERATOR_REVIEW_AND_FROZEN_HOLDOUT",
    }


def inspect_journal(records: list[dict], *, minimum_seconds: float = 259200) -> dict:
    """A segment must be contiguous in UTC AND monotonic time, with real work observed."""
    previous = "0" * 64
    problems = []
    for i, row in enumerate(records):
        body = {k: v for k, v in row.items() if k != "sha256"}
        if row.get("previous") != previous or row.get("sha256") != checksum(body):
            problems.append(f"chain:{i}")
        previous = row.get("sha256", "")
        if row.get("sequence") != i or row.get("status") != "healthy":
            problems.append(f"sample:{i}")
        if i:
            before = records[i - 1]
            delta = row["monotonic"] - before["monotonic"]
            wall = (
                datetime.fromisoformat(row["UTC"]) - datetime.fromisoformat(before["UTC"])
            ).total_seconds()
            if not 0 < delta <= 180 or not 0 < wall <= 180 or abs(delta - wall) > 5:
                problems.append(f"gap_or_clock:{i}")
            if (row.get("pid"), row.get("boot_id"), row.get("source_sha256")) != (
                before.get("pid"),
                before.get("boot_id"),
                before.get("source_sha256"),
            ):
                problems.append(f"segment_changed:{i}")
    elapsed = records[-1]["monotonic"] - records[0]["monotonic"] if records else 0
    wall_elapsed = (
        (
            datetime.fromisoformat(records[-1]["UTC"]) - datetime.fromisoformat(records[0]["UTC"])
        ).total_seconds()
        if records
        else 0
    )
    latest = records[-1] if records else {}
    duration_met = min(elapsed, wall_elapsed) >= minimum_seconds
    work_observed = latest.get("cycles", 0) >= 2 and latest.get("sandbox_probes", 0) >= 1
    return {
        "samples": len(records),
        "elapsed_seconds": elapsed,
        "wall_seconds": wall_elapsed,
        "minimum_seconds": minimum_seconds,
        "problems": problems,
        "duration_met": duration_met,
        "work_observed": work_observed,
        "continuous_segment": "PASS"
        if duration_met and work_observed and not problems
        else ("FAIL" if problems else "IN_PROGRESS"),
        "continuous_operation_gate": "NOT_RUN_REQUIRES_FAULT_MATRIX_LIVE_72H_AND_PILOT",
    }


def seal_record(record: dict, records: list[dict]) -> dict:
    row = {
        **record,
        "sequence": len(records),
        "previous": records[-1]["sha256"] if records else "0" * 64,
    }
    return {**row, "sha256": checksum(row)}
