"""Real HTTPS termination, Engine HTTP and deployment credential/network contracts."""

import configparser
import json
import ssl
import subprocess
from pathlib import Path

import httpx
import pytest
from sa20_process_support import certificate, listener
from sa20_support import configured

from mgar_v3.api import create_app
from mgar_v3.super_agent.source_operations_gateway import GatewayConfiguration, create_gateway

ROOT = Path(__file__).resolve().parents[1]


def test_compose_renders_private_pinned_dependencies_without_starting_any_service():
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            str(ROOT / "deploy/super-agent-v1/compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert set(config["services"]) == {"postgres", "redis"}
    assert config["networks"]["private"]["internal"] is True
    for service in config["services"].values():
        assert "@sha256:" in service["image"]
        assert all(port["host_ip"] == "127.0.0.1" for port in service["ports"])
    assert "POSTGRES_PASSWORD" not in config["services"]["postgres"]["environment"]


def test_system_services_are_login_independent_and_role_environment_is_minimal():
    folder = ROOT / "deploy/super-agent-v1/systemd"
    for role in ["source", "scheduler", "worker", "dispatcher", "watchdog", "https"]:
        config = configparser.ConfigParser(interpolation=None)
        config.read(folder / f"mgar-sa20-{role}.service")
        command = config["Service"]["ExecStart"]
        assert "/usr/bin/env -i " in command
        assert config["Service"]["User"] == "mgar-sa20-" + role
        assert "EnvironmentFile" not in config["Service"]
        assert "Settings" not in command and "database" not in command
        assert config["Install"]["WantedBy"] == "multi-user.target"
        assert config["Service"]["NoNewPrivileges"] == "true"
    api = (folder / "mgar-sa20-api.service").read_text()
    assert "--workers 1" in api and "--no-proxy-headers" in api
    assert "--host 127.0.0.1" in api and "source_operations_runtime:create_application" in api


def test_real_https_login_secure_cookie_origin_and_private_route_blocking(engine, tmp_path):
    env = configured(engine, tmp_path)
    cert, key = certificate(tmp_path)
    # Reserve gateway port first so API and TLS gateway freeze the same exact Origin.
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    gateway_port = sock.getsockname()[1]
    sock.close()
    public = f"https://127.0.0.1:{gateway_port}"
    settings = env.settings.model_copy(update={"web_public_origin": public})
    with listener(create_app(settings, engine=engine, run_background=False)) as upstream:
        configuration = GatewayConfiguration(
            public_origin=public, api_url=upstream, certificate=cert, private_key=key
        )
        # Use a separately bound real TLS listener and preserve its declared Host.
        with listener(create_gateway(configuration), certificate=cert, private_key=key) as gateway:
            context = ssl.create_default_context(cafile=str(cert))
            with httpx.Client(
                base_url=gateway,
                verify=context,
                trust_env=False,
                headers={"Host": f"127.0.0.1:{gateway_port}"},
            ) as client:
                credentials = {"username": "console-owner", "password": "sa19-fixture-password"}
                login = client.post(
                    "/api/v3/auth/login", json=credentials, headers={"Origin": public}
                )
                assert login.status_code == 200, login.text
                assert "Secure" in login.headers["set-cookie"]
                assert client.get("/api/v3/source-operations").status_code == 200
                assert client.get("/api/v3/auth/session").json()["authenticated"]
                assert (
                    client.post(
                        "/api/v3/auth/setup", json=credentials, headers={"Origin": public}
                    ).status_code
                    == 403
                )
                csrf = login.json()["csrf_token"]
                body = {
                    "expected_revision": 1,
                    "definition": env.definition.model_dump(mode="json"),
                    "review_ref": "TLS",
                }
                assert (
                    client.put(
                        "/api/v3/source-operations",
                        json=body,
                        headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf},
                    ).status_code
                    == 403
                )
                for path in [
                    "/api/v3/source-operations/components/" + str(env.ids["worker"]) + "/snapshot",
                    "/api/v3/watchdog/snapshot",
                    "/mcp/data",
                    "/api/v3/source-scheduler/worker/claim",
                    "/api/v3/%73ource-operations/components/"
                    + str(env.ids["worker"])
                    + "/snapshot",
                    "/api/v3//source-operations/components/ignored/snapshot",
                ]:
                    result = client.get(
                        path, headers={"X-MGAR-Operations-Token": env.tokens["worker"]}
                    )
                    assert result.status_code == 404, (path, result.status_code)
                    assert result.headers["cache-control"] == "no-store"
                    assert "strict-transport-security" in result.headers


@pytest.mark.parametrize("scheme,host", [("http", "127.0.0.1:8443"), ("https", "evil.test")])
def test_gateway_rejects_plaintext_or_host_mismatch_before_contacting_upstream(
    tmp_path, scheme, host
):
    from fastapi.testclient import TestClient

    config = GatewayConfiguration(
        public_origin="https://127.0.0.1:8443",
        certificate=tmp_path / "cert",
        private_key=tmp_path / "key",
    )
    with TestClient(create_gateway(config), base_url=scheme + "://127.0.0.1:8443") as client:
        result = client.get("/", headers={"Host": host})
        assert result.status_code == 403 and result.headers["cache-control"] == "no-store"
