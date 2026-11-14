"""First schema/Source-catalogue integrity checks, independent of runtime results."""

import hashlib
import json
from pathlib import Path

import pytest

from mgar_v3.super_agent.source_autonomy_domain import (
    AutonomyCycleRequest,
    AutonomyReopenRequest,
    AutonomyRevisionRequest,
    AutonomyRuleSave,
    AutonomySignal,
)

APP = Path(__file__).resolve().parents[1]
MODELS = {
    "rule-save": AutonomyRuleSave,
    "revision": AutonomyRevisionRequest,
    "reopen": AutonomyReopenRequest,
    "cycle": AutonomyCycleRequest,
    "signal": AutonomySignal,
}


@pytest.mark.parametrize("name", MODELS)
def test_frozen_public_and_internal_schemas_match_strict_versioned_contracts(name):
    schema = json.loads(
        (
            APP / "schemas/super-agent-v1" / ("source-autonomy-" + name + "-v1.schema.json")
        ).read_text()
    )
    assert schema == MODELS[name].model_json_schema(mode="validation")
    assert schema["additionalProperties"] is False
    assert not {"runtime", "factory", "provider", "allow_paid", "success"}.intersection(
        schema["properties"]
    )


def test_source_catalogues_have_exact_current_artifact_bytes():
    checked = 0
    for path in sorted((APP / "config").glob("source-*-v1.json")):
        for ref in json.loads(path.read_text()).get("artifacts", []):
            artifact = APP / ref["path"]
            assert artifact.is_file(), ref
            assert hashlib.sha256(artifact.read_bytes()).hexdigest() == ref["sha256"], ref
            checked += 1
    assert checked >= 479


def test_autonomy_catalogue_declares_no_default_runtime_notification_or_real_activation():
    value = json.loads((APP / "config/source-autonomy-v1.json").read_text())
    assert value["ownership_commit"] == "91c0b75693037bb0d90cb2eccd35356cd2e96135"
    assert value["defaults"] == {
        "rules": 0,
        "profiles": 0,
        "mode": "disabled",
        "timer_work": False,
        "model_calls_per_cycle": 0,
        "notifications": False,
        "publication": False,
    }
    assert value["independent_review"] == "NOT_RUN" and value["SA17"] == "NOT_STARTED"
    assert value["real_source_paid_outbound_publication_longterm"] == "NOT_RUN"
