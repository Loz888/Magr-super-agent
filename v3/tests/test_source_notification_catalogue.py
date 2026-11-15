"""First SA17 delivery consistency checks; no historical behavior targets repeated."""

import hashlib
import json
from pathlib import Path

from mgar_v3.super_agent.source_notification_domain import NotificationConfigure

A = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads((A / path).read_text())


def sha(path):
    return hashlib.sha256((A / path).read_bytes()).hexdigest()


def test_new_schema_and_disabled_example_are_exact_owner_dto_and_not_live_authority():
    schema = load("schemas/super-agent-v1/source-notification-v1.json")
    fixture = load("fixtures/super-agent-v1/sa17-notification-disabled-v1.json")
    request = NotificationConfigure.model_validate_json(json.dumps(fixture["configure"]))
    assert not request.settings.enabled
    assert fixture["label"] == "SYNTHETIC_CONTRACT_EXAMPLE_NOT_ACTIVATION"
    assert schema["contracts"]["NotificationConfigure"] == NotificationConfigure.model_json_schema()
    assert set(schema["contracts"]) == {
        "NotificationConfigure",
        "NotificationPlanRequest",
        "NotificationCancelRequest",
        "NotificationCycleRequest",
    }
    props = schema["contracts"]["NotificationPlanRequest"]["properties"]
    assert set(props) == {"run_id", "source_input", "content_class"}


def test_current_source_catalogues_reference_existing_exact_artifact_bytes():
    count = 0
    for path in sorted((A / "config").glob("source-*-v1.json")):
        data = json.loads(path.read_text())
        for item in data.get("artifacts", []):
            assert (A / item["path"]).is_file(), (path.name, item["path"])
            assert sha(item["path"]) == item["sha256"], (path.name, item["path"])
            count += 1
    assert count >= 500


def test_confirmed_migration_and_engine_recovery_repair_have_complete_provenance():
    p = "docs/super-agent-v1/sa17-proposal/"
    original = load(p + "proposal-manifest.json")
    ack = load(p + "ownership-confirmed.json")
    repair = load(p + "post-integration-repairs.json")
    formatting = load(p + "post-integration-format.json")
    assert ack["commit"] == "a09aef760e4c43ff46595a3c3cc56c503b018e97"
    assert (
        ack["sql_sha256"]
        == sha("migrations/0049_source_notifications.sql")
        == original["migration"]["sha256"]
    )
    assert ack["patch_sha256"] == sha(p + "shared-wiring.patch")
    assert repair["patch_sha256"] == sha(p + repair["patch"])
    before = {r["path"]: r["formatted_after_sha256"] for r in formatting["files"]}
    for f in repair["files"]:
        assert before[f["path"]] == f["before_sha256"]
        assert sha(f["path"].removeprefix("v3/")) == f["after_sha256"]
    assert len(list((A / "migrations").glob("*.sql"))) == 46


def test_registration_keeps_real_first_scopes_and_excludes_historical_card_suites():
    registry = load("config/verification.json")
    assert registry["SAV1-17-PREPARE"]["tests"] == ["tests/test_source_notification_preparation.py"]
    assert len(registry["SAV1-17"]["tests"]) == 8
    assert "tests/test_source_notification_recovery.py" in registry["SAV1-17"]["tests"]
    assert "tests/test_source_notification_review.py" in registry["SAV1-17"]["tests"]
    assert all(
        p.startswith("tests/test_source_notification_") and (A / p).is_file()
        for p in registry["SAV1-17"]["tests"]
    )
    assert registry["SAV1-17-CATALOGUE"]["tests"] == ["tests/test_source_notification_catalogue.py"]


def test_capability_manifest_retains_inert_defaults_and_separate_real_gates():
    data = load("config/source-notification-v1.json")
    assert data["defaults"] == {
        "enabled": False,
        "profiles": 0,
        "dispatcher_started": False,
        "model_calls": 0,
        "live_channel": False,
        "publication": False,
    }
    assert data["channels"] == ["github", "feishu"] and data["api_routes"] == 11
    assert data["independent_review"] == data["real_gates"] == "NOT_RUN"
    assert data["SA18"] == "NOT_STARTED"
    assert "FAIL" in data["H12_fixed_live_agent_mvp"]
