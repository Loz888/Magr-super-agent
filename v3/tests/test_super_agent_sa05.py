"""SA05 engineering acceptance: synthetic signed GitHub + real disposable PostgreSQL."""

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import ValidationError
from sa05_support import BASE, HEAD, KEY, grant, payload, setup, signed

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError
from mgar_v3.sources.github_auth import GitHubActivation, app_jwt, verify_signature
from mgar_v3.sources.github_broker import GitHubHistory, GitHubReadBroker
from mgar_v3.sources.github_domain import GitHubConfigure, GitHubRevoke
from mgar_v3.sources.github_runtime import GitHubRuntime
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.sources.ingress_domain import HistoryRequest, IngressConfigure
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.store.migrate import migrate
from mgar_v3.web_access import SESSION_COOKIE

ORIGIN = "http://127.0.0.1:18080"


@pytest.fixture
def env(engine):
    return setup(engine)


@pytest.mark.parametrize(
    "family,kind,number",
    [
        ("issues", "issue", 7),
        ("issue_comment", "issue", 7),
        ("pull_request", "pull_request", 7),
        ("pull_request_review", "pull_request", 7),
        ("pull_request_review_comment", "pull_request", 7),
        ("workflow_run", "workflow", 7),
        ("check_run", "check", 7),
        ("check_suite", "check", 7),
    ],
)
def test_event_families_keep_authenticated_resources_and_id_case(env, family, kind, number):
    delivery = env.adapter.verify_ingress(env.instance, signed(payload(family), family))
    item = delivery.metadata.items[0]
    event = item.event
    content = json.loads(delivery.bodies[0])
    assert event.sender.actor_id == "42" and event.sender.id_type == "github_id"
    assert event.repository_hint.resource_kind == kind and event.repository_hint.number == number
    assert event.source_thread.conversation_id == "repository:900"
    assert item.native_order is None and event.entity_revision.origin == "derived"
    assert content["resource"]["repository"]["repository_id"] == 900
    assert content["resource"]["repository"]["private"] is True
    if family == "workflow_run":
        assert content["resource"]["run_id"] == 500
        assert content["resource"]["run_attempt"] == 2
        assert content["resource"]["head_sha"] == HEAD
    if family.startswith("pull_request"):
        assert content["resource"]["head_repository_id"] == 900
        assert content["resource"]["base_sha"] == BASE
        assert event.source_thread.native_thread_id == "pr:7"
    verified = env.adapter.verify(env.instance, signed(payload(family), family))
    assert env.adapter.normalize(env.instance, verified)[0].sender.actor_id == "42"


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("bad_signature", "SIGNATURE_INVALID"),
        ("installation", "GITHUB_INSTALLATION_DENIED"),
        ("tenant", "GITHUB_INSTALLATION_DENIED"),
        ("repository", "GITHUB_INSTALLATION_DENIED"),
        ("visibility", "GITHUB_INSTALLATION_DENIED"),
        ("same_name", "GITHUB_SENDER_DENIED"),
        ("bot_impersonation", "GITHUB_SENDER_DENIED"),
        ("own_bot", "GITHUB_SELF_RESULT"),
    ],
)
def test_scope_signature_and_sender_negatives_never_persist_content(env, mutation, code):
    raw = payload()
    headers = {}
    if mutation == "bad_signature":
        headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    elif mutation == "installation":
        raw["installation"]["id"] = 201
    elif mutation == "tenant":
        raw["repository"]["owner"]["id"] = 101
    elif mutation == "repository":
        raw["repository"]["id"] = 901
    elif mutation == "visibility":
        raw["repository"]["private"] = False
    elif mutation == "same_name":
        raw["sender"]["id"] = 43
        raw["sender"]["login"] = "MiXeD-Login"
    elif mutation == "bot_impersonation":
        raw["sender"]["type"] = "Bot"
    else:
        raw["sender"] = {"id": 99, "login": "look-like-human", "type": "User"}
    raw["comment"]["body"] = "DO_NOT_STORE_THIS_UNAPPROVED_BODY"
    with pytest.raises(DomainError) as exc:
        env.runtime.receive(signed(raw, headers=headers), 1)
    assert exc.value.code == code
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_inputs").fetchone()["n"] == 0
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_bodies").fetchone()["n"] == 0
        )


def test_header_and_json_duplicates_are_explicitly_invalid(env):
    candidate = signed(payload())
    headers = {**candidate.headers, "x-github-event": "check_run"}
    with pytest.raises(DomainError):
        env.adapter.verify_ingress(env.instance, TransportEnvelope(headers, candidate.payload))
    raw = b'{"installation":1,"installation":2}'
    candidate = signed(payload())
    import hmac

    headers = {
        **candidate.headers,
        "X-Hub-Signature-256": "sha256=" + hmac.new(KEY, raw, hashlib.sha256).hexdigest(),
    }
    with pytest.raises(DomainError):
        env.adapter.verify_ingress(env.instance, TransportEnvelope(headers, raw))


def test_pr_issue_comment_is_normalized_and_persisted_without_fake_issue(env):
    raw = payload()
    raw["issue"]["pull_request"] = {"url": "https://untrusted.invalid/ignored"}
    receipt = env.runtime.receive(signed(raw), 1)
    assert receipt.disposition == "accepted" and len(receipt.input_ids) == 1
    body = env.engine.source_ingress.body(env.cookie, receipt.input_ids[0])
    resource = json.loads(body)["resource"]
    assert resource["pull_requests"] == [7] and resource["head_sha"] is None
    with env.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.trigger_events").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 0


def test_workflow_without_pr_has_no_synthetic_issue_and_run_attempts_stay_distinct(env):
    raw = payload("workflow_run")
    raw["workflow_run"]["pull_requests"] = []
    first = env.adapter.verify_ingress(env.instance, signed(raw, "workflow_run"))
    assert first.metadata.items[0].event.repository_hint.number is None
    assert first.metadata.items[0].event.source_thread.native_thread_id is None
    raw["workflow_run"]["run_attempt"] += 1
    second = env.adapter.verify_ingress(env.instance, signed(raw, "workflow_run"))
    assert first.metadata.items[0].entity_key != second.metadata.items[0].entity_key


def test_multiple_ci_pr_links_preserved_without_guessing_one(env):
    raw = payload("check_suite")
    raw["check_suite"]["pull_requests"] = [{"number": 7}, {"number": 8}]
    delivery = env.adapter.verify_ingress(env.instance, signed(raw, "check_suite"))
    assert delivery.metadata.items[0].event.repository_hint.number is None
    assert json.loads(delivery.bodies[0])["resource"]["pull_requests"] == [7, 8]


def test_duplicate_ci_concurrent_delivery_commits_one_input(env):
    candidate = signed(payload("workflow_run"), "workflow_run", delivery="ci-concurrent")
    # Identity already exists; SA02 strict proof replay is not weakened for transport retries.
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: env.runtime.receive(candidate, 1), range(8)))
    assert len({r.input_ids for r in results}) == 1
    assert sum(r.disposition == "accepted" for r in results) == 1
    assert all(r.committed and r.disposition in {"accepted", "duplicate"} for r in results)
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_inputs").fetchone()["n"] == 1
        )


def test_same_delivery_different_body_is_conflict_not_a_second_business_input(env):
    raw = payload()
    env.runtime.receive(signed(raw, delivery="conflict"), 1)
    raw["comment"]["body"] = "different authenticated body"
    with pytest.raises(DomainError) as exc:
        env.runtime.receive(signed(raw, delivery="conflict"), 1)
    assert exc.value.code == "INGRESS_DELIVERY_CONFLICT"
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_inputs").fetchone()["n"] == 1
        )


def test_same_ci_event_alternate_delivery_is_canonical_duplicate(env):
    raw = payload("check_run")
    a = env.runtime.receive(signed(raw, "check_run", delivery="ci-a"), 1)
    b = env.runtime.receive(signed(raw, "check_run", delivery="ci-b"), 1)
    assert a.input_ids == b.input_ids


def test_own_check_producer_is_rejected_even_with_allowed_sender(env):
    raw = payload("check_run")
    raw["check_run"]["app"]["id"] = 10
    with pytest.raises(DomainError) as exc:
        env.runtime.receive(signed(raw, "check_run"), 1)
    assert exc.value.code == "GITHUB_SELF_RESULT"


def test_pr_head_change_keeps_new_sha_and_invalidates_unordered_old_content(env):
    raw = payload("pull_request")
    original = env.runtime.receive(signed(raw, "pull_request"), 1)
    assert (
        json.loads(env.engine.source_ingress.body(env.cookie, original.input_ids[0]))["resource"][
            "head_sha"
        ]
        == HEAD
    )
    raw["action"] = "synchronize"
    raw["pull_request"]["head"]["sha"] = "c" * 40
    newer = env.runtime.receive(signed(raw, "pull_request"), 1)
    assert newer.input_ids != original.input_ids
    for identifier in [original.input_ids[0], newer.input_ids[0]]:
        with pytest.raises(DomainError):
            env.engine.source_ingress.body(env.cookie, identifier)


def test_receive_private_event_does_not_grant_any_snapshot(env):
    env.runtime.receive(signed(payload()), 1)
    with pytest.raises(DomainError) as exc:
        env.broker.snapshot(uuid4(), 1, ("src/a.py",))
    assert exc.value.code == "GITHUB_SNAPSHOT_GRANT_REQUIRED" and env.calls == []


def test_explicit_snapshot_grant_reads_exact_blob_without_exposing_credentials(env):
    identifier = grant(env, pull_request=7)
    result = env.broker.snapshot(identifier, 1, ("src/a.py",))
    assert result == {"src/a.py": b"synthetic source file\n"}
    for req in env.calls:
        assert req.url.host == "api.github.com" and "untrusted" not in str(req.url)
        if req.method == "POST":
            body = json.loads(req.content)
            assert body["repository_ids"] == [900]
            assert set(body["permissions"].values()) == {"read"}
        if "/contents/" in req.url.path:
            assert req.url.params["ref"] == HEAD
    report = env.sources.inspect(env.cookie, env.source_id)
    encoded = json.dumps(report, default=str)
    assert (
        "ghs_" not in encoded and "PRIVATE KEY" not in encoded and "webhook_secret" not in encoded
    )


@pytest.mark.parametrize(
    "path",
    ["../secret", "src/../.env", "/src/a.py", "src\\a.py", "src/%2e%2e/.env", "outside/a.py"],
)
def test_snapshot_path_escape_rejected_before_network(env, path):
    identifier = grant(env)
    with pytest.raises(DomainError):
        env.broker.snapshot(identifier, 1, (path,))
    assert env.calls == []


@pytest.mark.parametrize("change", ["fork", "sha", "base"])
def test_snapshot_pr_current_head_or_fork_cannot_inherit_old_grant(env, change):
    identifier = grant(env, pull_request=7)

    def response(request):
        if "/pulls/" in request.url.path:
            raw = payload("pull_request")["pull_request"]
            if change == "fork":
                raw["head"]["repo"]["id"] = 901
            elif change == "sha":
                raw["head"]["sha"] = "c" * 40
            else:
                raw["base"]["repo"]["id"] = 901
            return httpx.Response(200, json=raw)

    env.response = response
    with pytest.raises(DomainError) as exc:
        env.broker.snapshot(identifier, 1, ("src/a.py",))
    assert exc.value.code == "GITHUB_PR_HEAD_CHANGED"
    assert not any("/contents/" in r.url.path for r in env.calls)


@pytest.mark.parametrize("change", ["revoke", "identity", "policy", "settings", "owner"])
def test_snapshot_current_authority_and_post_network_recheck(env, change):
    identifier = grant(env)
    changed = False

    def response(request):
        nonlocal changed
        if request.method != "GET" or changed:
            return
        changed = True
        if change == "revoke":
            env.sources.revoke(
                env.cookie,
                env.source_id,
                identifier,
                GitHubRevoke(expected_revision=1, review_ref="operator-revoked"),
            )
        elif change == "identity":
            env.identities.roles(
                env.cookie,
                env.binding["binding_id"],
                RoleChange(expected_revision=1, roles=("requester",), review_ref="remove-context"),
            )
        elif change == "policy":
            from mgar_v3.super_agent.policy_domain import PolicyRevoke

            env.engine.source_policies.revoke(
                env.cookie, env.policy_id, PolicyRevoke(expected_revision=1, review_ref="stop")
            )
        elif change == "settings":
            env.sources.configure(
                env.cookie,
                env.source_id,
                GitHubConfigure(
                    expected_revision=1, settings=env.settings, review_ref="new-version"
                ),
            )
        else:
            env.engine.source_ingress.configure(
                env.cookie,
                env.source_id,
                IngressConfigure(
                    expected_generation=1,
                    settings=env.control.model_copy(update={"owner": "disabled"}),
                    review_ref="disable-owner",
                ),
            )

    env.response = response
    with pytest.raises(DomainError):
        env.broker.snapshot(identifier, 1, ("src/a.py",))


@pytest.mark.parametrize(
    "fault",
    [
        "wide_repo",
        "write_permission",
        "expired",
        "redirect",
        "rate",
        "blob",
        "symlink",
        "oversize",
        "lost",
    ],
)
def test_broker_response_fail_closed_no_retry_or_credential_leak(env, fault):
    identifier = grant(env)

    def response(request):
        if request.method == "POST" and fault in {"wide_repo", "write_permission", "expired"}:
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "token": "ghs_SYNTHETIC_SECRET",
                    "expires_at": (
                        datetime.now(UTC) + timedelta(minutes=-1 if fault == "expired" else 59)
                    ).isoformat(),
                    "repositories": [
                        {
                            "id": 901 if fault == "wide_repo" else 900,
                            "full_name": "example/mgar-test",
                        }
                    ],
                    "permissions": {"contents": "write", "metadata": "read"}
                    if fault == "write_permission"
                    else body["permissions"],
                },
            )
        if request.method != "GET":
            return
        if fault == "redirect":
            return httpx.Response(302, headers={"Location": "https://evil.invalid/credential"})
        if fault == "rate":
            return httpx.Response(429, headers={"Retry-After": "60"})
        if fault == "lost":
            raise httpx.ReadTimeout("secret might be in remote exception", request=request)
        if fault == "oversize":
            return httpx.Response(200, content=b"x" * 2097153)
        if fault == "blob" and "/git/blobs/" not in request.url.path:
            return
        return httpx.Response(
            200,
            json={
                "type": "symlink" if fault == "symlink" else "file",
                "path": "src/a.py",
                "encoding": "base64",
                "content": base64.b64encode(b"a").decode(),
                "size": 1,
                "sha": "0" * 40,
            },
        )

    env.response = response
    with pytest.raises(DomainError) as exc:
        env.broker.snapshot(identifier, 1, ("src/a.py",))
    assert "secret might" not in str(exc.value) and "ghs_" not in str(exc.value)
    assert len(env.calls) <= 8
    if fault == "rate":
        before = len(env.calls)
        with pytest.raises(DomainError):
            env.broker.snapshot(identifier, 1, ("src/a.py",))
        assert len(env.calls) == before


def test_jwt_is_real_rs256_bounded_and_secrets_are_not_represented(env):
    encoded = app_jwt(10, env.secrets.private_key)
    header, claims, signature = encoded.split(".")
    parsed = json.loads(base64.urlsafe_b64decode(claims + "=="))
    assert parsed["iss"] == "10" and parsed["exp"] - parsed["iat"] == 600
    key = serialization.load_pem_private_key(env.secrets.private_key, password=None)
    key.public_key().verify(
        base64.urlsafe_b64decode(signature + "=="),
        (header + "." + claims).encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert "PRIVATE KEY" not in repr(env.secrets) and KEY.decode() not in repr(env.secrets)
    with pytest.raises(DomainError):
        verify_signature(KEY, b"body", "sha256=非ASCII")


def test_default_and_expired_activation_deny_network_and_webhooks(env):
    broker = GitHubReadBroker(env.sources, env.source_id, 1, env.secrets)
    with pytest.raises(DomainError):
        GitHubRuntime(broker).receive(signed(payload()), 1)
    with pytest.raises(ValueError):
        GitHubReadBroker(env.sources, env.source_id, 1, env.secrets, env.activation)
    expired = GitHubActivation(
        env.source_id,
        1,
        900,
        datetime.now(UTC) - timedelta(seconds=1),
        frozenset({"webhook"}),
        "expired",
    )
    env.broker.activation = expired
    with pytest.raises(DomainError):
        env.runtime.receive(signed(payload()), 1)
    assert env.calls == []


def test_gateway_history_is_bounded_current_and_context_only(env):
    raw = payload()
    env.response = lambda request: (
        httpx.Response(200, json=[raw["comment"]]) if request.method == "GET" else None
    )
    from mgar_v3.sources.gateway import SourceGateway

    gateway = SourceGateway(env.engine.source_ingress, env.source_id, 1)
    subject = env.adapter.verify_identity(env.instance, signed(raw))
    request = HistoryRequest(
        source_thread=subject.source_thread.model_copy(update={"native_thread_id": "issue:7"}),
        limit=1,
    )
    result = gateway.history(GitHubHistory(env.broker), request)
    assert result.next_cursor == "2" and result.complete is False
    with env.engine.transaction() as conn:
        rows = conn.execute(
            "SELECT context_only,metadata FROM app.source_ingress_inputs"
        ).fetchall()
        assert len(rows) == 1 and rows[0]["context_only"]
    env.identities.roles(
        env.cookie,
        env.binding["binding_id"],
        RoleChange(expected_revision=1, roles=("requester",), review_ref="no-history"),
    )
    before = len(env.calls)
    with pytest.raises(DomainError):
        gateway.history(GitHubHistory(env.broker), request)
    assert len(env.calls) == before


def test_settings_concurrent_cas_fences_old_source_and_rolls_back_atomically(env):
    command = GitHubConfigure(expected_revision=1, settings=env.settings, review_ref="CAS")

    def change(_):
        try:
            env.sources.configure(env.cookie, env.source_id, command)
            return "saved"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(change, range(2)))
    assert sorted(result) == ["GITHUB_REVISION_CONFLICT", "saved"]
    with pytest.raises(DomainError):
        env.runtime.receive(signed(payload()), 1)
    before = env.sources.current(env.source_id)[0]["revision"]
    with pytest.raises(RuntimeError), env.engine.transaction():
        env.sources.configure(
            env.cookie,
            env.source_id,
            GitHubConfigure(expected_revision=before, settings=env.settings, review_ref="rollback"),
        )
        raise RuntimeError("force rollback")
    assert env.sources.current(env.source_id)[0]["revision"] == before


def test_settings_and_grant_admin_require_cookie_origin_csrf_and_hide_tokens(env):
    app = create_app(env.engine.settings, env.engine, run_background=False)
    with TestClient(app, base_url=ORIGIN) as client:
        path = "/api/v3/github-sources/" + str(env.source_id)
        assert client.get(path).status_code == 401
        client.cookies.set(SESSION_COOKIE, env.cookie)
        result = client.get(path)
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        body = {
            "expected_revision": 1,
            "settings": env.settings.model_dump(mode="json"),
            "review_ref": "API",
        }
        assert client.put(path, json=body).status_code == 403
        from mgar_v3.web_access import WebAccounts

        session = WebAccounts(env.engine).session(env.cookie)
        assert session
        assert (
            client.put(
                path, json=body, headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"}
            ).status_code
            == 403
        )
        good = client.put(
            path, json=body, headers={"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token}
        )
        assert good.status_code == 200

        assert client.post(path + "/snapshot-grants/" + str(uuid4()) + "/token").status_code == 404


def test_new_owner_api_does_not_fall_through_without_runtime(env):
    candidate = signed(payload())
    with TestClient(
        create_app(env.engine.settings, env.engine, run_background=False), base_url=ORIGIN
    ) as client:
        result = client.post(
            "/api/v3/webhooks/github", content=candidate.payload, headers=candidate.headers
        )
        assert (
            result.status_code == 503 and result.json()["code"] == "GITHUB_RUNTIME_NOT_CONFIGURED"
        )


def test_api_gateway_ack_after_durable_receipt_and_old_owner_replay_blocked(env):
    candidate = signed(payload("workflow_run"), "workflow_run", delivery="cutover-workflow")
    app = create_app(
        env.engine.settings, env.engine, run_background=False, github_runtime=env.runtime
    )
    with TestClient(app, base_url=ORIGIN) as client:
        result = client.post(
            "/api/v3/webhooks/github", content=candidate.payload, headers=candidate.headers
        )
        assert result.status_code == 202 and result.json()["committed"]
        env.engine.source_ingress.configure(
            env.cookie,
            env.source_id,
            IngressConfigure(
                expected_generation=1,
                settings=env.control.model_copy(update={"owner": "legacy"}),
                review_ref="gray-cutback",
            ),
        )
        second = client.post(
            "/api/v3/webhooks/github", content=candidate.payload, headers=candidate.headers
        )
        assert (
            second.status_code == 409 and second.json()["code"] == "INGRESS_DELIVERY_ALREADY_OWNED"
        )


def test_unknown_event_scope_and_raw_diff_do_not_change_configuration(env):
    with pytest.raises(ValidationError):
        GitHubConfigure.model_validate(
            {
                "expected_revision": 1,
                "settings": env.settings.model_dump(),
                "review_ref": "fake",
                "api_key": "not-accepted",
            }
        )
    raw = payload()
    raw["action"] = "unknown_action"
    with pytest.raises(DomainError):
        env.runtime.receive(signed(raw), 1)
    assert env.sources.current(env.source_id)[0]["revision"] == 1


def test_real_33_to_34_upgrade_preserves_every_legacy_checksum_and_row(database, tmp_path):
    from shutil import copyfile

    from mgar_v3.coordination.engine import Engine
    from mgar_v3.domain import TriggerInput
    from mgar_v3.evidence.artifacts import ArtifactStore
    from mgar_v3.settings import ROOT, Settings

    name = "sa05_upgrade_" + uuid4().hex
    with psycopg.connect(database, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(database, dbname=name)
    older = tmp_path / "migrations"
    older.mkdir()
    paths = sorted((ROOT / "migrations").glob("*.sql"))
    for path in paths:
        if path.name < "0034":
            copyfile(path, older / path.name)
    try:
        assert len(migrate(dsn, older)) == 33
        value = Settings(
            database_url=dsn,
            artifact_root=tmp_path / "artifacts",
            repository="example/mgar-test",
            local_actor="owner",
            allowed_actors=["owner"],
            graph_version="r1-quotes-v1",
        )
        old = Engine(value, ArtifactStore(value.artifact_root))
        receipt = old.ingest(
            TriggerInput(
                source="github",
                external_event_id="before34",
                project_id=value.project_id,
                repository=value.repository,
                issue_number=1,
                actor="owner",
                body="/mgar investigate retained old row",
            )
        )
        old.close()
        assert migrate(dsn) == [
            "0034_github_app.sql", "0035_feishu_source.sql", "0036_super_agent_budgets.sql",
            "0038_super_agent_fast_path.sql",
            "0039_super_agent_source_eval.sql",
            "0041_source_correlations.sql",
            "0042_source_subscriptions.sql",
            "0043_source_subscription_canonical_guard.sql",
            "0044_source_scheduler.sql",
            "0046_source_coding.sql",
            "0047_source_async.sql",
        ] and migrate(dsn) == []
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT count(*) FROM app.trigger_events WHERE event_id=%s", (receipt.event_id,)
            ).fetchone() == (1,)
            assert dict(
                conn.execute("SELECT name,sha256 FROM app.schema_migrations").fetchall()
            ) == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    finally:
        assert name.startswith("sa05_upgrade_") and len(name) == 45
        with psycopg.connect(database, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_explicit_health_probe_records_only_current_read_subset(env):
    assert env.runtime.status()["permissions"] == "unknown"
    report = env.runtime.probe_permissions()
    assert report["mode"] == "controlled" and report["permissions"] == "granted"
    assert report["permission_subset"] == {
        "metadata": "read",
        "issues": "read",
        "pull_requests": "read",
        "actions": "read",
        "checks": "read",
    }
    assert report["live_source_channel_gate"] == "NOT_RUN"
    app = create_app(
        env.engine.settings, env.engine, run_background=False, github_runtime=env.runtime
    )
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set(SESSION_COOKIE, env.cookie)
        count = len(env.calls)
        result = client.get("/api/v3/github-sources/" + str(env.source_id))
        assert result.json()["permissions"] == "granted" and len(env.calls) == count
        assert "ghs_" not in result.text
    env.sources.configure(
        env.cookie,
        env.source_id,
        GitHubConfigure(expected_revision=1, settings=env.settings, review_ref="changed"),
    )
    assert env.runtime.status()["permissions"] == "unknown"


def test_snapshot_revoked_during_token_mint_never_sends_data_get(env):
    identifier = grant(env)

    def response(request):
        if request.method == "POST":
            env.sources.revoke(
                env.cookie,
                env.source_id,
                identifier,
                GitHubRevoke(expected_revision=1, review_ref="revoke-during-mint"),
            )

    env.response = response
    with pytest.raises(DomainError):
        env.broker.snapshot(identifier, 1, ("src/a.py",))
    assert [r.method for r in env.calls] == ["POST"]


def test_history_revoked_during_mint_blocks_get_and_preserves_permission_gap(env):
    from mgar_v3.sources.gateway import SourceGateway

    gateway = SourceGateway(env.engine.source_ingress, env.source_id, 1)
    subject = env.adapter.verify_identity(env.instance, signed(payload()))
    request = HistoryRequest(source_thread=subject.source_thread, limit=1)

    def response(req):
        if req.method == "POST":
            env.identities.roles(
                env.cookie,
                env.binding["binding_id"],
                RoleChange(
                    expected_revision=1, roles=("requester",), review_ref="history-mint-revoke"
                ),
            )

    env.response = response
    with pytest.raises(DomainError):
        gateway.history(GitHubHistory(env.broker), request)
    assert [r.method for r in env.calls] == ["POST"]
    with env.engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.source_ingress_gaps WHERE source_instance_id=%s",
                (env.source_id,),
            ).fetchone()["n"]
            == 1
        )


def test_unreplayable_ci_topic_records_gap_without_network(env):
    from mgar_v3.sources.gateway import SourceGateway

    gateway = SourceGateway(env.engine.source_ingress, env.source_id, 1)
    subject = env.adapter.verify_identity(env.instance, signed(payload()))
    request = HistoryRequest(
        source_thread=subject.source_thread.model_copy(update={"native_thread_id": None}), limit=1
    )
    with pytest.raises(DomainError) as exc:
        gateway.history(GitHubHistory(env.broker), request)
    assert exc.value.code == "GITHUB_HISTORY_UNAVAILABLE" and env.calls == []
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT declaration FROM app.source_ingress_gaps").fetchone()[
                "declaration"
            ]["reason"]
            == "no_replay"
        )


def test_receive_denial_leaves_no_body_model_or_task(engine):
    env = setup(engine, receive={"enabled": False})
    result = env.runtime.receive(signed(payload()), 1)
    assert result.disposition == "rejected"
    with engine.transaction() as conn:
        for table in [
            "source_ingress_inputs",
            "source_ingress_bodies",
            "runs",
            "usage_calls",
            "outbox",
        ]:
            assert conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"] == 0


def test_unselected_signed_editor_invalidates_existing_body_without_new_evidence_or_content(env):
    original = payload()
    first = env.runtime.receive(signed(original), 1)
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    edit = json.loads(json.dumps(original))
    edit["action"] = "edited"
    edit["sender"] = {"id": 43, "type": "User", "login": "same-looking-name"}
    edit["comment"]["body"] = "UNSELECTED_EDITOR_BODY_NEVER_RETAIN"
    receipt = env.runtime.receive(signed(edit), 1)
    assert receipt.disposition == "rejected" and not receipt.input_ids
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_bodies").fetchone()["n"] == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.source_identities WHERE actor_ref->>'actor_id'='43'"
            ).fetchone()["n"]
            == 0
        )


def test_signed_delete_and_ack_callback_replay_cannot_resurrect_body(env):
    raw = payload()
    first = env.runtime.receive(signed(raw), 1)
    raw["action"] = "deleted"
    deletion = signed(raw, delivery="deleted-callback")
    a = env.runtime.receive(deletion, 1)
    b = env.runtime.receive(deletion, 1)
    assert a.committed and b.committed and a.input_ids == b.input_ids
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])


def test_unknown_config_actor_in_policy_cannot_expand_app_ceiling(env):
    from mgar_v3.sources.domain import ActorRef
    from mgar_v3.super_agent.policy_domain import PolicySave

    raw = env.policy.model_dump(mode="json")
    raw["selector"]["actors"].append(
        ActorRef(
            scope=env.instance.scope, id_type="github_id", actor_id="43", actor_kind="human"
        ).model_dump(mode="json")
    )
    env.engine.source_policies.save(
        env.cookie,
        env.policy_id,
        PolicySave(expected_revision=1, definition=raw, review_ref="wrong-ceiling"),
    )
    env.engine.source_ingress.configure(
        env.cookie,
        env.source_id,
        IngressConfigure(
            expected_generation=1,
            settings=env.control.model_copy(update={"policy_revision": 2}),
            review_ref="wrong-ceiling",
        ),
    )
    with pytest.raises(DomainError) as exc:
        env.runtime.receive(signed(payload()), 2)
    assert exc.value.code == "GITHUB_POLICY_SCOPE"


@pytest.mark.parametrize(
    "mode,kind", [("120000", "blob"), ("160000", "commit"), ("040000", "tree")]
)
def test_git_tree_rejects_symlink_submodule_and_nonfile_before_blob_get(env, mode, kind):
    identifier = grant(env)

    def response(request):
        if request.url.path.endswith("/git/trees/" + "e" * 40):
            return httpx.Response(
                200,
                json={
                    "sha": "e" * 40,
                    "truncated": False,
                    "tree": [{"path": "a.py", "mode": mode, "type": kind, "sha": "f" * 40}],
                },
            )

    env.response = response
    with pytest.raises(DomainError) as exc:
        env.broker.snapshot(identifier, 1, ("src/a.py",))
    assert exc.value.code == "GITHUB_SNAPSHOT_LINK"
    assert not any("/git/blobs/" in r.url.path for r in env.calls)


@pytest.mark.parametrize("corrupt", ["truncated", "sha", "duplicate", "commit"])
def test_incomplete_or_wrong_tree_proof_never_releases_snapshot(env, corrupt):
    identifier = grant(env)

    def response(request):
        if corrupt == "commit" and "/git/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": "c" * 40, "tree": {"sha": "d" * 40}})
        if "/git/trees/" in request.url.path:
            entry = {"path": "src", "mode": "040000", "type": "tree", "sha": "e" * 40}
            return httpx.Response(
                200,
                json={
                    "sha": "f" * 40 if corrupt == "sha" else "d" * 40,
                    "truncated": corrupt == "truncated",
                    "tree": [entry, entry] if corrupt == "duplicate" else [entry],
                },
            )

    env.response = response
    with pytest.raises(DomainError):
        env.broker.snapshot(identifier, 1, ("src/a.py",))
    assert not any("/git/blobs/" in r.url.path for r in env.calls)


def test_private_broker_read_requires_authority_and_cannot_mint_write_token(env):
    with pytest.raises(DomainError) as exc:
        env.broker._read(
            "snapshot", "/git/commits/" + HEAD, {"metadata": "read", "contents": "read"}
        )
    assert exc.value.code == "GITHUB_READ_AUTHORITY_REQUIRED"
    with pytest.raises(DomainError):
        env.broker._token("snapshot", {"metadata": "read", "contents": "write"})
    assert env.calls == []


def test_multiline_base64_blob_matches_independent_git_hash_object(env):
    import subprocess

    identifier = grant(env)
    content = b"synthetic source file\n"
    actual = (
        subprocess.check_output(["git", "hash-object", "--stdin"], input=content).decode().strip()
    )
    data = env.broker.snapshot(identifier, 1, ("src/a.py",))
    assert data == {"src/a.py": content}
    requests = [r for r in env.calls if "/git/blobs/" in r.url.path]
    assert len(requests) == 1 and requests[0].url.path.endswith(actual)
    assert any(r.url.path.endswith("/git/commits/" + HEAD) for r in env.calls)


def test_published_github_schemas_samples_and_manifest_are_frozen():
    from mgar_v3.settings import ROOT
    from mgar_v3.sources.github_domain import (
        GitHubConfigure,
        GitHubResource,
        GitHubRevoke,
        GitHubSettings,
        SnapshotGrant,
        SnapshotSave,
    )

    models = {
        "settings": GitHubSettings,
        "configure": GitHubConfigure,
        "resource": GitHubResource,
        "snapshot-grant": SnapshotGrant,
        "snapshot-save": SnapshotSave,
        "revoke": GitHubRevoke,
    }
    for name, model in models.items():
        assert (
            json.loads((ROOT / f"schemas/super-agent-v1/github-{name}-v1.schema.json").read_text())
            == model.model_json_schema()
        )
    manifest = json.loads((ROOT / "config/source-github-v1.json").read_text())
    assert manifest["default_runtime"] is None and manifest["live_source_channel_gate"] == "NOT_RUN"
    for artifact in manifest["artifacts"]:
        assert (
            hashlib.sha256((ROOT / artifact["path"]).read_bytes()).hexdigest() == artifact["sha256"]
        )
    samples = json.loads((ROOT / "fixtures/super-agent-v1/github-v1/recordings.json").read_text())
    assert samples["mode"] == "SYNTHETIC_CONTROLLED_NOT_PLATFORM_CAPTURE"
    assert len(samples["deliveries"]) == 8


def legacy_ci_policy(env):
    from mgar_v3.proactivity.domain import Activation, Policy

    policy = Policy(
        repository=env.engine.settings.repository,
        issue_number=7,
        kinds=["ci_failure"],
        allowed_actors=["MiXeD-Login"],
        source_ref=HEAD,
        question="Synthetic legacy CI investigation",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    env.engine.proactivity.create(policy, "owner")
    env.engine.proactivity.activate(
        Activation(policy_hash=policy.policy_hash, mode="auto"), "owner"
    )
    env.engine.source_ingress.configure(
        env.cookie,
        env.source_id,
        IngressConfigure(
            expected_generation=1,
            settings=env.control.model_copy(update={"owner": "legacy"}),
            review_ref="test-legacy-first",
        ),
    )


def legacy_ci_client(env):
    from pydantic import SecretStr

    settings = env.engine.settings.model_copy(
        update={"github_webhook_secret": SecretStr(KEY.decode())}
    )
    return TestClient(
        create_app(settings, env.engine, run_background=False, github_runtime=env.runtime),
        base_url=ORIGIN,
    )


def test_old_workflow_claim_cannot_become_a_new_gateway_business_input(env):
    legacy_ci_policy(env)
    candidate = signed(payload("workflow_run"), "workflow_run", delivery="old-workflow-claim")
    with legacy_ci_client(env) as client:
        old = client.post(
            "/api/v3/webhooks/github", content=candidate.payload, headers=candidate.headers
        )
        assert old.status_code == 202 and old.json()["observations"][0]["decision"] == "started"
    env.engine.source_ingress.configure(
        env.cookie,
        env.source_id,
        IngressConfigure(
            expected_generation=2, settings=env.control, review_ref="switch-after-old-claim"
        ),
    )
    with pytest.raises(DomainError) as exc:
        env.runtime.receive(candidate, 3)
    assert exc.value.code == "GITHUB_DELIVERY_ALREADY_LEGACY"
    with env.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_inputs").fetchone()["n"] == 0
        )


def test_workflow_owner_switch_cannot_interleave_old_dispatch_claim(env, monkeypatch):
    from threading import Event

    legacy_ci_policy(env)
    entered, release, switched = Event(), Event(), Event()
    observe = env.engine.proactivity.observe

    def paused(event, permit=None):
        entered.set()
        assert release.wait(5)
        return observe(event, permit)

    monkeypatch.setattr(env.engine.proactivity, "observe", paused)
    candidate = signed(payload("workflow_run"), "workflow_run", delivery="atomic-owner")

    def old():
        with legacy_ci_client(env) as client:
            return client.post(
                "/api/v3/webhooks/github", content=candidate.payload, headers=candidate.headers
            )

    def switch():
        env.engine.source_ingress.configure(
            env.cookie,
            env.source_id,
            IngressConfigure(
                expected_generation=2, settings=env.control, review_ref="concurrent-owner-switch"
            ),
        )
        switched.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(old)
        assert entered.wait(5)
        second = pool.submit(switch)
        try:
            assert not switched.wait(0.1), (
                "owner changed between legacy preflight and old workflow claim"
            )
        finally:
            release.set()
        assert first.result().status_code == 202
        second.result()
    with pytest.raises(DomainError) as exc:
        env.runtime.receive(candidate, 3)
    assert exc.value.code == "GITHUB_DELIVERY_ALREADY_LEGACY"
