"""Formal Owner/CSRF/HTTPS and local service-role boundaries on real PG."""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sa20_support import configured

from mgar_v3.api import create_app
from mgar_v3.super_agent.source_operations_domain import ComponentOpen
from mgar_v3.web_access import SESSION_COOKIE

P = "/api/v3/source-operations"


def app(env, **changes):
    return create_app(
        env.settings.model_copy(update=changes), engine=env.engine, run_background=False
    )


def headers(env, role="worker"):
    return {"X-MGAR-Operations-Token": env.tokens[role]}


def path(env, operation, role="worker"):
    return f"{P}/components/{env.ids[role]}/{operation}"


def test_all_eleven_formal_routes_registered_on_original_app(engine, tmp_path):
    env = configured(engine, tmp_path)
    routes = {
        (m, r.path)
        for r in app(env).routes
        for m in getattr(r, "methods", [])
        if r.path == P or r.path.startswith(P + "/")
    }
    expected = json.loads(
        (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "docs/super-agent-v1/sa20-proposal/interfaces.json"
        ).read_text()
    )
    assert {m + " " + p for m, p in routes} == set(expected)


def test_owner_cookie_csrf_cannot_be_replaced_by_cli_or_service_token(engine, tmp_path):
    env = configured(engine, tmp_path)
    with TestClient(app(env), base_url="http://127.0.0.1:18080") as client:
        for supplied in (
            {},
            {"X-MGAR-Token": engine.settings.local_token.get_secret_value()},
            headers(env),
        ):
            result = client.get(P, headers=supplied)
            assert result.status_code == 401
            assert result.headers["cache-control"] == "no-store"
        client.cookies.set(SESSION_COOKIE, env.cookie)
        assert client.get(P).status_code == 200
        raw = {
            "expected_revision": 1,
            "definition": env.definition.model_dump(mode="json"),
            "review_ref": "owner",
        }
        assert (
            client.put(P, json=raw, headers={"Origin": "http://127.0.0.1:18080"}).status_code == 403
        )
        user = env.accounts.session(env.cookie)
        result = client.put(
            P,
            json=raw,
            headers={"Origin": "http://127.0.0.1:18080", "X-CSRF-Token": user.csrf_token},
        )
        assert result.status_code == 200 and result.json()["revision"] == 2


@pytest.mark.parametrize(
    "attack", ["missing", "other_component", "duplicate", "cookie", "origin", "remote"]
)
def test_machine_auth_separates_components_browser_and_actual_peer(engine, tmp_path, attack):
    env = configured(engine, tmp_path)
    supplied = headers(env)
    if attack == "missing":
        supplied = {}
    if attack == "other_component":
        supplied = headers(env, "scheduler")
    if attack == "duplicate":
        supplied = list(supplied.items()) + list(supplied.items())
    if attack == "origin":
        supplied.update(Origin="http://127.0.0.1:18080")
    peer = ("203.0.113.5", 1234) if attack == "remote" else ("127.0.0.1", 1234)
    with TestClient(app(env), base_url="http://127.0.0.1:18080", client=peer) as client:
        if attack == "cookie":
            client.cookies.set(SESSION_COOKIE, env.cookie)
        if attack == "remote":
            supplied.update({"X-Forwarded-For": "127.0.0.1"})
        result = client.get(path(env, "snapshot"), headers=supplied)
        assert result.status_code in {401, 403}
        assert result.headers["cache-control"] == "no-store"


def test_machine_activation_heartbeat_tick_and_rotation_do_not_leak_credentials(engine, tmp_path):
    env = configured(engine, tmp_path)
    with TestClient(
        app(env), base_url="http://127.0.0.1:18080", client=("127.0.0.1", 1234)
    ) as client:
        snapshot = client.get(path(env, "snapshot"), headers=headers(env))
        assert snapshot.status_code == 200, snapshot.text
        assert all(token not in snapshot.text for token in env.tokens.values())
        raw = ComponentOpen(
            config_revision=1, expected_generation=0, boot_id=uuid4(), request_id=uuid4()
        ).model_dump(mode="json")
        opened = client.post(path(env, "open"), json=raw, headers=headers(env))
        assert opened.status_code == 200, opened.text
        beat = {
            "generation": 1,
            "boot_id": raw["boot_id"],
            "sequence": 1,
            "state": "waiting_runner",
        }
        assert (
            client.post(path(env, "heartbeat"), json=beat, headers=headers(env)).status_code == 200
        )
        tick = {
            "generation": 1,
            "boot_id": raw["boot_id"],
            "sequence": 1,
            "request_id": str(uuid4()),
        }
        result = client.post(path(env, "tick"), json=tick, headers=headers(env))
        assert result.status_code == 200 and result.json()["waiting_runner"]
        env.secrets_path.write_text('{"components":[]}')
        assert client.get(path(env, "snapshot"), headers=headers(env)).status_code == 401


def test_nonexecuting_role_cannot_tick_even_with_valid_machine_token(engine, tmp_path):
    env = configured(engine, tmp_path)
    selected = "watchdog"
    with TestClient(
        app(env), base_url="http://127.0.0.1:18080", client=("127.0.0.1", 1234)
    ) as client:
        raw = ComponentOpen(
            config_revision=1, expected_generation=0, boot_id=uuid4(), request_id=uuid4()
        ).model_dump(mode="json")
        assert (
            client.post(
                path(env, "open", selected), json=raw, headers=headers(env, selected)
            ).status_code
            == 200
        )
        result = client.post(
            path(env, "tick", selected),
            json={
                "generation": 1,
                "boot_id": raw["boot_id"],
                "sequence": 1,
                "request_id": str(uuid4()),
            },
            headers=headers(env, selected),
        )
        assert result.status_code == 403


def test_public_https_login_exact_origin_secure_cookie_and_no_remote_first_owner(engine, tmp_path):
    env = configured(engine, tmp_path)
    origin = "https://magr.example.test"
    with TestClient(app(env, web_public_origin=origin), base_url=origin) as client:
        credentials = {"username": "console-owner", "password": "sa19-fixture-password"}
        assert (
            client.post(
                "/api/v3/auth/setup", json=credentials, headers={"Origin": origin}
            ).status_code
            == 403
        )
        login = client.post("/api/v3/auth/login", json=credentials, headers={"Origin": origin})
        assert login.status_code == 200, login.text
        cookie = login.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert client.get(P).status_code == 200
        body = {
            "expected_revision": 1,
            "definition": env.definition.model_dump(mode="json"),
            "review_ref": "https",
        }
        csrf = client.get("/api/v3/auth/session").json()["csrf_token"]
        assert (
            client.put(
                P, json=body, headers={"Origin": "https://other.test", "X-CSRF-Token": csrf}
            ).status_code
            == 403
        )
        assert (
            client.put(P, json=body, headers={"Origin": origin, "X-CSRF-Token": csrf}).status_code
            == 200
        )
        assert client.get(P, headers={"Host": "evil.test"}).status_code == 400


def test_mount_prefix_keeps_operations_private_and_no_store(engine, tmp_path):
    env = configured(engine, tmp_path)
    application = app(env)
    application.root_path = "/mounted"
    with TestClient(application, base_url="http://127.0.0.1:18080/mounted") as client:
        result = client.get(P)
        assert result.status_code == 401
        assert result.headers["cache-control"] == "no-store"


def test_raw_json_duplicates_and_unknown_commands_are_rejected_without_side_effects(
    engine, tmp_path
):
    env = configured(engine, tmp_path)
    with TestClient(app(env), base_url="http://127.0.0.1:18080") as client:
        result = client.post(
            path(env, "open"),
            content='{"config_revision":1,"config_revision":2}',
            headers={**headers(env), "content-type": "application/json"},
        )
        assert result.status_code == 422
        with engine.transaction() as conn:
            assert not conn.execute("SELECT 1 FROM app.sa_operations_components").fetchone()
