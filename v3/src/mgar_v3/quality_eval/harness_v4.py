"""Frozen Harness V4 baseline and task catalogs (HV4-H00).

This module reads and validates planning data only. It does not execute tasks,
grant capabilities, or turn any gate to PASS.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

SCHEMA = "mgar-hv4-task-v1"
TYPES = ("inspect", "bugfix", "feature", "refactor", "run_app")
COMPOSITIONS = ("bugfix", "feature", "refactor", "run_app", "large_multi")
SPLITS = ("dev", "holdout")
SPLIT_SIZES = {"dev": 12, "holdout": 12}
SPLIT_COMPOSITION = {
    "dev": {"bugfix": 4, "feature": 3, "refactor": 2, "run_app": 2, "large_multi": 1},
    "holdout": {"bugfix": 4, "feature": 3, "refactor": 2, "run_app": 2, "large_multi": 1},
}
REQUIRED_TASK_KEYS = (
    "schema",
    "task_id",
    "split",
    "type",
    "composition",
    "repository",
    "source_url",
    "base_commit",
    "fix_commit",
    "language",
    "goal",
    "environment",
    "scope",
    "acceptance",
    "judge",
    "judge_status",
    "resources",
    "judge_sha256",
)
RESOURCE_KEYS = (
    "max_model_calls",
    "max_tool_calls",
    "max_cost_micros",
    "deadline_seconds",
    "max_context_tokens",
    "max_repair_rounds",
)
JUDGE_STATUSES = ("implemented", "spec_frozen_pending_h12")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class CatalogError(ValueError):
    """Raised when frozen catalog data violates the H00 contract."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def judge_sha256(task: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(task["judge"]).encode("utf-8")).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise CatalogError(f"{path.name}:{number}: {error}") from error
        if not isinstance(row, dict):
            raise CatalogError(f"{path.name}:{number}: expected a JSON object")
        rows.append(row)
    return rows


def load_tasks(split: str, root: Path | None = None) -> list[dict[str, Any]]:
    if split not in SPLITS:
        raise CatalogError(f"unknown split: {split}")
    base = root or repo_root()
    return load_jsonl(base / "experiments/harness-v4/tasks" / f"{split}.jsonl")


def load_task_types(root: Path | None = None) -> dict[str, Any]:
    base = root or repo_root()
    data = (base / "experiments/harness-v4/task-types.json").read_text(encoding="utf-8")
    return cast(dict[str, Any], json.loads(data))


def load_protocol(root: Path | None = None) -> dict[str, Any]:
    base = root or repo_root()
    data = (base / "experiments/harness-v4/scoring-v1.json").read_text(encoding="utf-8")
    return cast(dict[str, Any], json.loads(data))


def load_baseline(root: Path | None = None) -> dict[str, Any]:
    base = root or repo_root()
    data = (base / "experiments/harness-v4/baseline.json").read_text(encoding="utf-8")
    return cast(dict[str, Any], json.loads(data))


def validate_task(task: Mapping[str, Any], split: str) -> list[str]:
    errors: list[str] = []
    missing = [key for key in REQUIRED_TASK_KEYS if key not in task]
    if missing:
        return [f"missing keys: {', '.join(missing)}"]
    if task["schema"] != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    if task["split"] != split:
        errors.append(f"split must be {split}")
    if task["type"] not in TYPES:
        errors.append(f"type must be one of {TYPES}")
    if task["composition"] not in COMPOSITIONS:
        errors.append(f"composition must be one of {COMPOSITIONS}")
    if task["judge_status"] not in JUDGE_STATUSES:
        errors.append(f"judge_status must be one of {JUDGE_STATUSES}")
    if not COMMIT_PATTERN.match(str(task["base_commit"])):
        errors.append("base_commit must be a 40-character lowercase hex commit")
    fix_commit = task.get("fix_commit")
    if fix_commit is not None and not COMMIT_PATTERN.match(str(fix_commit)):
        errors.append("fix_commit must be null or a 40-character lowercase hex commit")
    if len(str(task["goal"]).strip()) < 20:
        errors.append("goal must describe the requested behavior")
    if not task["scope"]:
        errors.append("scope must not be empty")

    environment = task["environment"]
    if not isinstance(environment, Mapping):
        errors.append("environment must be an object")
    else:
        for key in ("platform", "runtime", "prepare", "verify", "network"):
            if key not in environment:
                errors.append(f"environment.{key} is required")

    acceptance = task["acceptance"]
    if not isinstance(acceptance, Mapping):
        errors.append("acceptance must be an object")
    else:
        for key in ("baseline", "required", "regression"):
            if not acceptance.get(key):
                errors.append(f"acceptance.{key} must not be empty")

    judge = task["judge"]
    if not isinstance(judge, Mapping):
        errors.append("judge must be an object")
    elif not judge.get("pass_rule") or not judge.get("version"):
        errors.append("judge.version and judge.pass_rule are required")

    resources = task["resources"]
    if not isinstance(resources, Mapping):
        errors.append("resources must be an object")
    else:
        for key in RESOURCE_KEYS:
            value = resources.get(key)
            if not isinstance(value, int) or value <= 0:
                errors.append(f"resources.{key} must be a positive integer")

    expected = judge_sha256(task) if isinstance(judge, Mapping) else None
    if expected is not None and task["judge_sha256"] != expected:
        errors.append("judge_sha256 does not match the canonical judge object")
    return errors


def validate_catalog(root: Path | None = None) -> dict[str, Any]:
    base = root or repo_root()
    all_tasks: list[dict[str, Any]] = []
    per_split: dict[str, int] = {}
    for split in SPLITS:
        tasks = load_tasks(split, base)
        per_split[split] = len(tasks)
        for task in tasks:
            errors = validate_task(task, split)
            if errors:
                raise CatalogError(f"{task.get('task_id', '<unknown>')}: {'; '.join(errors)}")
        all_tasks.extend(tasks)
        if len(tasks) != SPLIT_SIZES[split]:
            raise CatalogError(
                f"{split} must contain {SPLIT_SIZES[split]} tasks, found {len(tasks)}"
            )
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task["composition"]] = counts.get(task["composition"], 0) + 1
        if counts != SPLIT_COMPOSITION[split]:
            raise CatalogError(f"{split} composition mismatch: {counts}")

    ids = [task["task_id"] for task in all_tasks]
    if len(ids) != len(set(ids)):
        raise CatalogError("task_id values must be unique across splits")
    repositories = sorted({task["repository"] for task in all_tasks})
    if len(repositories) < 4:
        raise CatalogError("at least four real projects are required")
    languages = {task["language"] for task in all_tasks}
    if "python" not in languages or not languages & {"javascript", "typescript"}:
        raise CatalogError("python and JS/TS coverage are required")
    if "go" not in languages:
        raise CatalogError("the Go compatibility regression must be preserved")

    return {
        "tasks": len(all_tasks),
        "per_split": per_split,
        "repositories": repositories,
        "languages": sorted(languages),
    }
