"""First limited operational client checks; synthetic loopback transport only."""

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from mgar_v3.super_agent.source_operations_client import (
    LocalOperationsClient,
    PrivateConfigurationError,
    ServiceUnavailable,
    private_model,
)
from mgar_v3.super_agent.source_operations_domain import ServiceClient

TOKEN = "sa20_synthetic_private_" + "x" * 32


def config(role="worker"):
    return {
        "api_url": "http://127.0.0.1:18081",
        "component_id": str(uuid4()),
        "role": role,
        "token": TOKEN,
    }


def test_private_configuration_round_trips_without_reading_process_environment(
    tmp_path, monkeypatch
):
    path = tmp_path / "client.json"
    raw = config()
    path.write_text(json.dumps(raw))
    path.chmod(0o600)
    monkeypatch.setenv("MGAR_V3_DATABASE_URL", "must-not-be-loaded")
    monkeypatch.setenv("HTTP_PROXY", "http://external.invalid")
    value = private_model(path, ServiceClient)
    assert value.token.get_secret_value() == TOKEN
    assert TOKEN not in value.model_dump_json() and TOKEN not in repr(value)
    assert not {"database_url", "local_token", "allow_paid"} & set(type(value).model_fields)


@pytest.mark.parametrize(
    "unsafe",
    ["mode", "symlink", "parent-symlink", "hardlink", "fifo", "too-large", "duplicate-key"],
)
def test_private_file_rejects_aliases_special_files_bounds_and_duplicate_fields(tmp_path, unsafe):
    directory = tmp_path / "real"
    directory.mkdir()
    path = directory / "client.json"
    path.write_text(json.dumps(config()))
    path.chmod(0o600)
    if unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    elif unsafe == "parent-symlink":
        link = tmp_path / "link"
        link.symlink_to(directory, target_is_directory=True)
        path = link / path.name
    elif unsafe == "hardlink":
        os.link(path, tmp_path / "hardlink.json")
    elif unsafe == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif unsafe == "too-large":
        path.write_text("x" * 65537)
    else:
        path.write_text('{"token":"' + TOKEN + '","token":"duplicate"}')
    with pytest.raises(PrivateConfigurationError) as error:
        private_model(path, ServiceClient)
    assert TOKEN not in str(error.value)


def transport_state(role="worker", *, enabled=True, unknown=False):
    raw = config(role)
    c = ServiceClient.model_validate(raw)
    e = SimpleNamespace(
        config=c, calls=[], component=None, tick={"state": "idle", "sequence": 0}, ticks=0
    )
    until = datetime.now(UTC) + timedelta(hours=1)

    def handler(request):
        assert request.url.host == "127.0.0.1" and request.url.port == 18081
        assert request.headers["X-MGAR-Operations-Token"] == TOKEN
        operation = request.url.path.rsplit("/", 1)[-1]
        e.calls.append((request.method, operation))
        body = json.loads(request.content) if request.content else {}
        now = datetime.now(UTC)
        if operation == "snapshot":
            return httpx.Response(
                200,
                json={
                    "definition": {
                        "enabled": enabled,
                        "valid_until": until.isoformat(),
                        "components": [
                            {"component_id": str(c.component_id), "role": role, "enabled": enabled}
                        ],
                    },
                    "snapshot": {
                        "project_id": "fixture",
                        "observed_at": now.isoformat(),
                        "config_revision": 1,
                        "components": [e.component] if e.component else [],
                        "redis": "unavailable" if role == "watchdog" else "unknown",
                    },
                    "snapshot_sha256": "a" * 64,
                    "tick": e.tick,
                    "driver_available": True,
                },
            )
        if operation == "open":
            assert body["expected_generation"] == 0
            e.component = {
                "component_id": str(c.component_id),
                "role": role,
                "generation": 1,
                "boot_id": body["boot_id"],
                "state": "starting",
                "expires_at": (now + timedelta(seconds=30)).isoformat(),
                "sequence": 0,
            }
            return httpx.Response(200, json={"generation": 1})
        if operation == "heartbeat":
            assert body["generation"] == 1 and body["boot_id"] == e.component["boot_id"]
            assert body["sequence"] == e.component["sequence"] + 1
            e.component.update(sequence=body["sequence"], state=body["state"])
            return httpx.Response(200, json={"accepted": True})
        if operation == "observations":
            assert role == "watchdog" and body["kind"] == "dependency_unavailable"
            assert body["snapshot_sha256"] == "a" * 64 and len(body["observation_key"]) == 64
            assert not {"reset_budget", "run_id", "command", "body"} & set(body)
            return httpx.Response(200, json={"created": True})
        assert operation == "tick" and role in {"worker", "scheduler", "dispatcher"}
        e.ticks += 1
        e.tick = {
            "state": "unknown" if unknown else "known",
            "sequence": body["sequence"],
            "request_id": body["request_id"],
            "processed": 1,
        }
        if unknown:
            raise httpx.ReadTimeout("synthetic response lost after acceptance")
        return httpx.Response(200, json=e.tick)

    e.transport = httpx.MockTransport(handler)
    return e


@pytest.mark.parametrize("role", ["worker", "scheduler", "dispatcher"])
def test_separate_role_process_opens_fenced_lease_before_bounded_tick(role):
    e = transport_state(role)
    client = LocalOperationsClient(e.config, transport=e.transport)
    try:
        result = client.poll()
        assert result == {"status": "observed", "processed": 1, "tick_state": "known"}
        assert e.calls == [
            ("GET", "snapshot"),
            ("POST", "open"),
            ("GET", "snapshot"),
            ("POST", "heartbeat"),
            ("POST", "tick"),
        ]
        assert e.ticks == 1
    finally:
        client.close()


def test_disabled_control_causes_only_snapshot_without_lease_or_work():
    e = transport_state(enabled=False)
    client = LocalOperationsClient(e.config, transport=e.transport)
    try:
        assert client.poll() == {"status": "inactive", "model_calls": 0}
        assert e.calls == [("GET", "snapshot")] and e.ticks == 0
    finally:
        client.close()


def test_unknown_tick_reply_is_read_only_polled_and_process_restart_cannot_reset_it():
    e = transport_state(unknown=True)
    client = LocalOperationsClient(e.config, transport=e.transport)
    try:
        assert client.poll()["status"] == "awaiting_reconciliation"
        before = len(e.calls)
        assert client.poll()["resubmitted"] is False
        assert e.calls[before:] == [("GET", "snapshot")]
    finally:
        client.close()
    replacement = LocalOperationsClient(e.config, transport=e.transport)
    try:
        before = len(e.calls)
        assert replacement.poll()["status"] == "awaiting_reconciliation"
        assert e.calls[before:] == [("GET", "snapshot")] and e.ticks == 1
    finally:
        replacement.close()


def test_watchdog_only_posts_observation_without_tick_or_control():
    e = transport_state("watchdog")
    client = LocalOperationsClient(e.config, transport=e.transport)
    try:
        assert client.poll() == {"status": "observed", "signals": 1, "partial": False}
        assert e.calls[-1] == ("POST", "observations") and e.ticks == 0
        assert not any(op in {"tick", "cancel", "approve"} for _, op in e.calls)
    finally:
        client.close()


def test_source_heartbeat_never_claims_real_platform_connection():
    e = transport_state("source")
    # Source role in a valid control has an exact source selection.
    handler = e.transport.handler

    def with_scope(request):
        response = handler(request)
        if request.url.path.endswith("/snapshot"):
            raw = response.json()
            raw["definition"]["components"][0]["source_ids"] = [str(uuid4())]
            return httpx.Response(200, json=raw)
        return response

    client = LocalOperationsClient(e.config, transport=httpx.MockTransport(with_scope))
    try:
        result = client.poll()
        assert result["platform_connection_confirmed"] is False and e.ticks == 0
    finally:
        client.close()


@pytest.mark.parametrize("response", ["redirect", "oversize", "duplicate-json"])
def test_client_rejects_redirect_or_unbounded_ambiguous_response_without_echo(response):
    calls = []

    def handler(request):
        calls.append(request)
        if response == "redirect":
            return httpx.Response(307, headers={"Location": "https://external.invalid/"})
        if response == "oversize":
            return httpx.Response(200, content=b"x" * 65537)
        return httpx.Response(200, content=('{"value":"' + TOKEN + '","value":0}').encode())

    client = LocalOperationsClient(
        ServiceClient.model_validate(config()), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ServiceUnavailable) as error:
            client.request("snapshot")
        assert len(calls) == 1 and calls[0].url.host == "127.0.0.1"
        assert TOKEN not in str(error.value)
        with pytest.raises(ValueError):
            client.request("../../auth/setup")
        assert len(calls) == 1
    finally:
        client.close()
