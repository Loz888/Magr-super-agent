"""First SA18 delivery/schema/current provenance checks, no old behavior reruns."""

import hashlib
import json
from pathlib import Path

from mgar_v3.super_agent.source_review_domain import (
    ReviewApprove,
    ReviewCycle,
    ReviewExact,
    ReviewNativeLink,
    ReviewPrepare,
)

A = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads((A / path).read_text())


def sha(path):
    return hashlib.sha256((A / path).read_bytes()).hexdigest()


def test_review_contracts_are_bounded_owner_identifiers_and_inert_example():
    schema = load("schemas/super-agent-v1/source-review-v1.json")
    models = (ReviewPrepare, ReviewApprove, ReviewExact, ReviewNativeLink, ReviewCycle)
    assert set(schema["contracts"]) == {m.__name__ for m in models}
    for model in models:
        assert schema["contracts"][model.__name__] == model.model_json_schema()
        assert not {"actor", "diff", "body", "files", "provider", "credential", "approved"} & set(
            schema["contracts"][model.__name__]["properties"]
        )
    example = load("fixtures/super-agent-v1/sa18-review-disabled-v1.json")
    ReviewPrepare.model_validate_json(json.dumps(example["prepare"]))
    assert example["label"] == "SYNTHETIC_CONTRACT_EXAMPLE_NOT_ACTIVATION"
    assert not example["default_live_write"] and example["profiles_configured"] == 0


def test_all_current_source_catalogue_references_match_and_old_bytes_are_preserved():
    count = 0
    for path in sorted((A / "config").glob("source-*-v1.json")):
        for artifact in json.loads(path.read_text()).get("artifacts", []):
            assert sha(artifact["path"]) == artifact["sha256"], (path.name, artifact["path"])
            count += 1
    assert count >= 547
    history = A / "docs/super-agent-v1/accept/SAV1-18/history/catalogues-before-SA18"
    assert len(list(history.glob("*.json"))) == 16
    assert sha("docs/super-agent-v1/accept/SAV1-06/cli-takeover-20261111.json") == (
        "f4adf9605c87d6256bd2ec17b8436da8a7bced763a8e03b185a8cfe19d34a29f"
    )


def test_exact_shared_bytes_confirmed0050_and_forward0051_are_installed(engine):
    base = "docs/super-agent-v1/sa18-proposal/"
    ack = load(base + "ownership-confirmed.json")
    repair = load(base + "async-intake-ownership-confirmed.json")
    assert ack["SQL_sha256"] == sha("migrations/0050_source_reviews.sql")
    assert repair["SQL_sha256"] == sha("migrations/0051_source_review_async_intake.sql")
    for file in ack["files"]:
        assert sha(file["path"].removeprefix("v3/")) == file["after_sha256"]
    with engine.transaction() as conn:
        rows = conn.execute("SELECT name,sha256 FROM app.schema_migrations").fetchall()
        assert len(rows) == 48
        assert all(sha("migrations/" + row["name"]) == row["sha256"] for row in rows)


def test_final_card_registration_includes_scoped_delivery_targets_without_old_cards():
    registry = load("config/verification.json")
    targets = registry["SAV1-18"]["tests"]
    assert len(targets) == 8
    assert "tests/test_source_review_recovery.py" in targets
    assert "tests/test_source_review_browser.py" in targets
    assert "tests/test_source_review_async.py" in targets
    assert "tests/test_source_review_native_index.py" in targets
    assert all(t.startswith("tests/test_source_review_") and (A / t).is_file() for t in targets)
    assert registry["SAV1-18-PREPARE"]["tests"] == ["tests/test_source_review_preparation.py"]


def test_delivery_configuration_keeps_no_live_write_model_merge_or_deployment():
    config = load("config/source-review-v1.json")
    assert config["defaults"] == {
        "profiles": 0,
        "worker_started": False,
        "dispatcher_started": False,
        "model_calls": 0,
        "live_publication": False,
        "merge": False,
        "deploy": False,
    }
    assert config["api_routes"] == 13 and config["console_routes"] == 3
    assert config["independent_review"] == config["real_gates"] == "NOT_RUN"
    assert config["SA19"] == "NOT_STARTED" and "FAIL" in config["H12_fixed_live_agent_mvp"]
    assert "no retries" in config["unknown"]
