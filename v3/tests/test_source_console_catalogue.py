"""First completed SA19 schema, delivery inventory and ownership artifact checks."""

import hashlib
import json
from pathlib import Path

from mgar_v3.super_agent.source_console_domain import (
    ConsoleCredentialSave,
    ConsoleDisableSources,
    ConsoleSourceRevoke,
    ConsoleSourceSave,
    ConsoleTaskStop,
)

A = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads((A / path).read_text())


def sha(path):
    return hashlib.sha256((A / path).read_bytes()).hexdigest()


def test_published_console_schema_and_inert_example_match_actual_input_contracts():
    schema = load("schemas/super-agent-v1/source-console-v1.json")
    models = (
        ConsoleSourceSave,
        ConsoleCredentialSave,
        ConsoleSourceRevoke,
        ConsoleDisableSources,
        ConsoleTaskStop,
    )
    assert set(schema["contracts"]) == {m.__name__ for m in models}
    for model in models:
        assert schema["contracts"][model.__name__] == model.model_json_schema()
    example = load("fixtures/super-agent-v1/sa19-console-disabled-v1.json")
    value = ConsoleSourceSave.model_validate(example["source"])
    assert value.credentials is None and not value.configuration.settings.enabled
    assert example["label"] == "SYNTHETIC_CONTRACT_EXAMPLE_NOT_ACTIVATION"
    assert example["model_calls"] == example["external_writes"] == 0
    assert not example["worker_started"]


def test_all_eighteen_source_catalogues_current_references_and_seventeen_history_files_match():
    catalogues = sorted((A / "config").glob("source-*-v1.json"))
    assert len(catalogues) == 18
    refs = 0
    for p in catalogues:
        for ref in json.loads(p.read_text()).get("artifacts", []):
            assert sha(ref["path"]) == ref["sha256"], (p.name, ref["path"])
            refs += 1
    assert refs >= 580
    history = A / "docs/super-agent-v1/accept/SAV1-19/history/catalogues-before-SA19"
    assert len(list(history.glob("*.json"))) == 17
    manifest = load("docs/super-agent-v1/accept/SAV1-19/history/manifest.json")
    for ref in manifest["artifacts"]:
        assert sha(ref["path"]) == ref["sha256"]
    assert (
        sha("docs/super-agent-v1/accept/SAV1-06/cli-takeover-20261111.json")
        == "f4adf9605c87d6256bd2ec17b8436da8a7bced763a8e03b185a8cfe19d34a29f"
    )


def test_completed_registry_covers_backend_http_browser_boundaries_and_delivery_without_preparation():
    registry = load("config/verification.json")
    assert set(registry["SAV1-19"]["tests"]) == {
        "tests/test_source_console_backend.py",
        "tests/test_source_console_http.py",
        "tests/test_source_console_browser.py",
        "tests/test_source_console_boundaries.py",
        "tests/test_source_console_catalogue.py",
    }
    assert registry["SAV1-19-PREPARE"]["tests"] == ["tests/test_source_console_preparation.py"]
    assert registry["SAV1-19-DELIVERY"]["tests"] == ["tests/test_source_console_catalogue.py"]
    assert registry["SAV1-19"]["components"]["SA20"] == "NOT_STARTED"


def test_final_console_catalogue_has_no_runtime_execution_or_publication_activation():
    c = load("config/source-console-v1.json")
    assert c["ownership_commit"] == "8d19308a585e80885fc4f0bfeec4853395236ab8"
    assert c["api_routes"] == 12 and c["console_routes"] == 3
    assert c["defaults"] == {
        "default_mode": "shadow",
        "automatic_repair": False,
        "worker_started": False,
        "dispatcher_started": False,
        "model_calls": 0,
        "external_writes": 0,
        "runtime_started": False,
    }
    assert c["independent_review"] == c["real_gates"] == "NOT_RUN"
    assert c["SA20"] == "NOT_STARTED" and "FAIL" in c["H12_fixed_live_agent_mvp"]
    assert c["list_limit"] == 50 and c["identity_limit"] == 100


def test_delivery_ownership_bytes_and_operation_guidance_remain_exact():
    base = "docs/super-agent-v1/sa19-proposal/"
    ack = load(base + "ownership-confirmed.json")
    assert (
        ack["SQL_sha256"]
        == sha("migrations/0052_source_console.sql")
        == sha(base + "0052_source_console.sql")
    )
    assert ack["patch_sha256"] == sha(base + "shared-wiring.patch")
    for ref in ack["files"]:
        assert sha(ref["path"].removeprefix("v3/")) == ref["after_sha256"]
    guide = (A / "docs/super-agent-v1/SA19_CONSOLE.md").read_text()
    for term in [
        "Origin/CSRF",
        "unknown",
        "组织",
        "关闭浏览器",
        "消息权限",
        "0052",
        "密钥",
        "SA20",
        "独立验收",
    ]:
        assert term in guide
