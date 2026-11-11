import hashlib
import json
from importlib.metadata import version
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sa06_support import ORIGIN, setup
from test_super_agent_sa06 import count

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError
from mgar_v3.sources import feishu_api
from mgar_v3.sources.feishu import Filtered
from mgar_v3.sources.feishu_domain import (
    FeishuConfigure,
    FeishuHealth,
    FeishuSettings,
    FollowupTopic,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "name,model",
    [
        ("settings", FeishuSettings),
        ("configure", FeishuConfigure),
        ("health", FeishuHealth),
    ],
)
def test_published_schema_matches_runtime(name, model):
    schema = ROOT / f"schemas/super-agent-v1/feishu-{name}-v1.schema.json"
    assert json.loads(schema.read_text()) == model.model_json_schema()


def test_frozen_delivery_artifacts_and_official_sdk_version():
    manifest = json.loads((ROOT / "config/source-feishu-v1.json").read_text())
    assert manifest["default_runtime"] is None
    assert manifest["default_listener"] is False
    assert manifest["live_source_channel_gate"] == "NOT_RUN"
    assert version("lark-oapi") == manifest["sdk"]["version"] == "1.7.5"
    assert len(manifest["artifacts"]) >= 15
    for item in manifest["artifacts"]:
        assert hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest() == item["sha256"]


def test_published_recordings_are_synthetic_and_normalize_expected_scope(engine):
    env = setup(
        engine,
        settings_changes={
            "followup_topics": (FollowupTopic(chat_id="Oc_Chat-AbC", thread_id="synthetic-topic"),),
        },
    )
    recording = json.loads((ROOT / "fixtures/super-agent-v1/feishu-v1/recordings.json").read_text())
    assert recording["provenance"].startswith("explicitly_synthetic")
    assert recording["live_source_channel_gate"] == "NOT_RUN"
    for case in recording["cases"]:
        if case["expected"] == "filtered":
            with pytest.raises(Filtered):
                env.adapter.normalize_raw(
                    env.instance, case["event"], case["name"], "installation_proof"
                )
        else:
            item, body = env.adapter.normalize_raw(
                env.instance,
                case["event"],
                case["name"],
                "installation_proof",
            )
            assert item.event.scope == env.instance.scope
            if case["name"] == "recall":
                assert item.operation == "delete" and body is None
            elif case["name"] == "followup":
                assert item.event.source_thread.native_thread_id == "synthetic-topic"
                assert item.event.reply_to_entity_id == "synthetic-parent"
    assert count(env, "source_ingress_inputs") == 0


def test_optional_callback_stream_bound_does_not_invoke_runtime(engine, monkeypatch):
    env = setup(engine)

    def forbidden(_):
        raise AssertionError("Oversized raw stream reached the verifier")

    monkeypatch.setattr(env.runtime, "webhook", forbidden)
    with TestClient(
        create_app(
            engine.settings,
            engine=engine,
            feishu_runtime=env.runtime,
            run_background=False,
        ),
        base_url=ORIGIN,
    ) as client:
        result = client.post("/api/v3/webhooks/feishu", content=(b"x" * 16384 for _ in range(17)))
    # The complete app rejects oversized bodies in its pre-existing global middleware.
    assert result.status_code == 413 and result.json()["code"] == "BODY_TOO_LARGE"
    # The optional registrar must also enforce its own stream limit when composed alone.
    app = FastAPI()

    def authority(request: Request):
        return engine

    feishu_api.register(app, authority, lambda _: None, env.runtime)
    with TestClient(app, base_url=ORIGIN) as client, pytest.raises(DomainError) as error:
        client.post(
            "/api/v3/webhooks/feishu",
            content=(b"x" * 16384 for _ in range(17)),
        )
    assert error.value.code == "INGRESS_ENVELOPE_BOUND"
    assert count(env, "source_ingress_inputs") == count(env, "feishu_transport_filters") == 0
