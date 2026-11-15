"""Real bounded local listeners/processes; no desktop, paid model or real source."""

import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.05)
    raise AssertionError("Controlled local condition did not become observable")


@contextmanager
def listener(app, *, certificate=None, private_key=None):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
        proxy_headers=False,
        ssl_certfile=str(certificate) if certificate else None,
        ssl_keyfile=str(private_key) if private_key else None,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started)
        yield f"{'https' if certificate else 'http'}://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(20)
        sock.close()
        assert not thread.is_alive(), "Owned listener leaked"


def certificate(tmp_path):
    cert, key = tmp_path / "tls.crt", tmp_path / "tls.key"
    result = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0
    key.chmod(0o600)
    return cert, key


def environment(settings):
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "MGAR_V3_DATABASE_URL": settings.database_url.get_secret_value(),
        "MGAR_V3_LOCAL_TOKEN": "sa20_controlled_private_controller_token_12345678",
        "MGAR_V3_ARTIFACT_ROOT": str(settings.artifact_root),
        "MGAR_V3_REPOSITORY": settings.repository,
        "MGAR_V3_PROJECT_ID": settings.project_id,
        "MGAR_V3_GRAPH_VERSION": settings.graph_version,
        "MGAR_V3_LOCAL_ACTOR": "owner",
        "MGAR_V3_ALLOWED_ACTORS": '["owner"]',
        "MGAR_V3_ALLOW_PAID": "0",
        "MGAR_V3_ALLOW_EXTERNAL_WRITE": "0",
    }


@contextmanager
def role_process(path, log, *, once=False):
    # Deliberately exclude the controller environment, .env, DB and platform keys.
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    command = [
        sys.executable,
        "-m",
        "mgar_v3.super_agent.source_operations_client",
        "--config",
        str(path),
    ]
    if once:
        command.append("--once")
    with log.open("ab") as stream:
        process = subprocess.Popen(
            command, env=env, cwd=path.parent, stdout=stream, stderr=subprocess.STDOUT
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)
