from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mgar_v3.quality_eval import harness_v4

ROOT = Path(__file__).resolve().parents[1]
HV4 = ROOT / "docs/harness-v4"


def test_catalog_validates_and_is_frozen() -> None:
    summary = harness_v4.validate_catalog()
    assert summary["tasks"] == 24
    assert summary["per_split"] == {"dev": 12, "holdout": 12}
    assert len(summary["repositories"]) >= 4
    assert {"python", "go"} <= set(summary["languages"])
    assert {"javascript", "typescript"} & set(summary["languages"])


def test_each_split_has_the_frozen_composition() -> None:
    for split, expected in harness_v4.SPLIT_COMPOSITION.items():
        tasks = harness_v4.load_tasks(split)
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task["composition"]] = counts.get(task["composition"], 0) + 1
        assert counts == expected


def test_judge_hashes_are_bound_and_reference_files_match() -> None:
    for split in harness_v4.SPLITS:
        for task in harness_v4.load_tasks(split):
            assert task["judge_sha256"] == harness_v4.judge_sha256(task)
            judge = task["judge"]
            if judge.get("kind") != "v35_reference":
                continue
            for hidden in judge["hidden_tests"]:
                path = ROOT / hidden["path"]
                assert path.is_file(), path
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                assert digest == hidden["sha256"], path


def test_resources_stay_within_the_frozen_ceilings() -> None:
    ceilings = harness_v4.load_protocol()["ceilings"]
    for split in harness_v4.SPLITS:
        for task in harness_v4.load_tasks(split):
            for key, ceiling in ceilings.items():
                assert 0 < task["resources"][key] <= ceiling, (task["task_id"], key)


def test_task_type_contract_covers_every_type() -> None:
    contract = harness_v4.load_task_types()
    assert contract["status"] == "FROZEN"
    assert set(contract["types"]) == set(harness_v4.TYPES)
    for rules in contract["types"].values():
        assert rules["goal"]
        assert rules["success"]
        assert rules["required_receipts"]
        assert rules["default_resources"]


def test_scoring_protocol_freezes_thresholds() -> None:
    protocol = harness_v4.load_protocol()
    assert protocol["status"] == "FROZEN"
    assert protocol["thresholds"]["dev_min_pass"] == 10
    assert protocol["thresholds"]["holdout_min_pass"] == 9
    assert protocol["thresholds"]["max_false_completion"] == 0
    assert protocol["thresholds"]["max_unknown_blind_retries"] == 0


def test_baseline_records_static_cleanup_and_historical_failure() -> None:
    baseline = harness_v4.load_baseline()
    checks = baseline["static_checks"]
    assert checks["ruff_initial"] == 23
    assert checks["ruff_final"] == 0
    assert checks["mypy_initial"] == 16
    assert checks["mypy_final"] == 0
    assert baseline["historical_gates"]["e02_live_agent_gate"] == "FAIL"
    assert baseline["historical_gates"]["live_publication_gate"] == "NOT_RUN"


def test_capability_audit_uses_the_four_required_categories() -> None:
    text = (HV4 / "CAPABILITY_AUDIT.md").read_text(encoding="utf-8")
    for status in ("首页已接入", "已有组件", "未接线", "未实测"):
        assert f"| {status} |" in text, status


def test_baseline_regression_log_records_the_core_commands() -> None:
    text = (HV4 / "BASELINE_REGRESSION.md").read_text(encoding="utf-8")
    assert "ruff check src tests scripts" in text
    assert "mypy" in text
    assert "pytest" in text
    assert "V35-E02" in text


def test_task_catalog_files_are_machine_readable() -> None:
    for name in ("dev", "holdout"):
        path = ROOT / "experiments/harness-v4/tasks" / f"{name}.jsonl"
        rows = harness_v4.load_jsonl(path)
        assert len(rows) == 12
        assert all(json.dumps(row, ensure_ascii=False) for row in rows)
