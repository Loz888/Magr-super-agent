"""Actual headless Chromium + original HTTP/Owner/PG controlled review flow."""

import json
import os
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn
from websockets.sync.client import connect

from mgar_v3.api import create_app
from mgar_v3.settings import ROOT
from mgar_v3.web_access import SESSION_COOKIE

CHROME = Path(
    "/home/imini/.cache/ms-playwright/chromium_headless_shell-1254/chrome-headless-shell-linux64/chrome-headless-shell"
)


class CDP:
    def __init__(self, uri):
        self.socket = connect(uri, max_size=8 * 1024 * 1024)
        self.next_id = 0
        self.events = []

    def call(self, method, **params):
        self.next_id += 1
        ident = self.next_id
        self.socket.send(json.dumps({"id": ident, "method": method, "params": params}))
        while True:
            result = json.loads(self.socket.recv(timeout=15))
            if result.get("id") == ident:
                assert "error" not in result, result
                return result.get("result", {})
            self.events.append(result)

    def evaluate(self, expression):
        result = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
            userGesture=True,
        )
        assert "exceptionDetails" not in result, result
        return result.get("result", {}).get("value")

    def wait(self, expression):
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if self.evaluate(expression):
                return
            time.sleep(0.1)
        raise AssertionError(
            {"waiting_for": expression, "body": self.evaluate("document.body.innerText")}
        )


@contextmanager
def browser(engine, tmp_path, cookie, *, feishu_runtime=None):
    assert CHROME.is_file()
    sock = socket.socket()
    # Harness owns 18080. The review app uses a separately bound test socket.
    sock.bind(("127.0.0.1", 18081))
    sock.listen(128)
    app = create_app(engine.settings, engine, run_background=False, feishu_runtime=feishu_runtime)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    profile = tmp_path / "chromium-profile"
    process = subprocess.Popen(
        [
            str(CHROME),
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--no-first-run",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            "--user-data-dir=" + str(profile),
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=(tmp_path / "chromium-stderr.log").open("wb"),
        env={
            **os.environ,
            "LD_LIBRARY_PATH": str(
                ROOT / ".local/sa18-browser-runtime/extracted/usr/lib/x86_64-linux-gnu"
            ),
        },
    )
    client = None
    try:
        end = time.monotonic() + 20
        while (
            not server.started or not (profile / "DevToolsActivePort").exists()
        ) and time.monotonic() < end:
            assert process.poll() is None
            time.sleep(0.1)
        assert server.started
        port = (profile / "DevToolsActivePort").read_text().splitlines()[0]
        pages = httpx.get("http://127.0.0.1:" + port + "/json/list").json()
        page = next(p for p in pages if p["type"] == "page")
        client = CDP(page["webSocketDebuggerUrl"])
        client.call("Page.enable")
        client.call("Runtime.enable")
        client.call("Network.enable")
        assert client.call(
            "Network.setCookie",
            name=SESSION_COOKIE,
            value=cookie,
            url="http://127.0.0.1:18081/",
            httpOnly=True,
            sameSite="Strict",
        )["success"]
        client.call("Page.navigate", url="http://127.0.0.1:18081/source-console")
        client.wait(
            "document.getElementById('source-count') && document.getElementById('source-count').textContent !== '—'"
        )
        yield client
    finally:
        if client:
            client.socket.close()
        process.terminate()
        process.wait(timeout=15)
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        assert not thread.is_alive()
