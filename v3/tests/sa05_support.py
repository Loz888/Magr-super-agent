"""Synthetic GitHub App deliveries + controlled HTTP; never live GitHub or paid models."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from test_super_agent_sa03 import definition

from mgar_v3.sources.domain import ChannelCapabilities, SourceInstance, SourceScope
from mgar_v3.sources.github import ACTIONS, CAPABILITIES, GitHubAdapter
from mgar_v3.sources.github_auth import GitHubActivation, GitHubSecrets
from mgar_v3.sources.github_broker import GitHubReadBroker
from mgar_v3.sources.github_domain import (
    VERSION,
    GitHubConfigure,
    GitHubSettings,
    SnapshotGrant,
    SnapshotSave,
)
from mgar_v3.sources.github_runtime import GitHubRuntime
from mgar_v3.sources.github_store import GitHubSources
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.identity_domain import BindIdentity
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.sources.ingress_domain import IngressConfigure, IngressSettings
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.web_access import LoginInput, WebAccounts

KEY = b"SAV1-05-synthetic-github-webhook-key"
HEAD = "a" * 40
BASE = "b" * 40


def payload(family="issue_comment", action=None, **changes):
    now = datetime.now(UTC).isoformat()
    user = {"id": 42, "login": "MiXeD-Login", "type": "User"}
    repo = {"id": 900, "full_name": "example/mgar-test", "private": True, "owner": {"id": 100}}
    pull = {
        "id": 300,
        "number": 7,
        "title": "synthetic PR",
        "body": "synthetic PR body",
        "updated_at": now,
        "user": user,
        "head": {"sha": HEAD, "repo": repo},
        "base": {"sha": BASE, "repo": repo},
    }
    comment = {
        "id": 800,
        "body": "synthetic content /mgar investigate",
        "user": user,
        "created_at": now,
        "updated_at": now,
    }
    data = {
        "installation": {"id": 200},
        "repository": repo,
        "sender": user,
        "action": action
        or (
            "opened"
            if family in {"issues", "pull_request"}
            else "submitted"
            if family == "pull_request_review"
            else "completed"
            if family in {"workflow_run", "check_run", "check_suite"}
            else "created"
        ),
    }
    if family == "issues":
        data["issue"] = {
            "id": 700,
            "number": 7,
            "title": "synthetic issue",
            "body": "synthetic body",
            "updated_at": now,
            "user": user,
        }
    elif family == "issue_comment":
        data.update(issue={"id": 700, "number": 7}, comment=comment)
    elif family == "pull_request":
        data["pull_request"] = pull
    elif family == "pull_request_review":
        data.update(pull_request=pull, review={**comment, "id": 600, "submitted_at": now})
    elif family == "pull_request_review_comment":
        data.update(pull_request=pull, comment=comment)
    else:
        obj = {
            "id": 500,
            "head_sha": HEAD,
            "status": "completed",
            "conclusion": "failure",
            "name": "synthetic CI",
            "created_at": now,
            "updated_at": now,
            "pull_requests": [{"number": 7}],
            "app": {"id": 333},
        }
        if family == "workflow_run":
            obj.update(run_attempt=2, head_repository=repo)
        elif family == "check_run":
            obj["check_suite"] = {"id": 501}
        data[family] = obj
    data.update(changes)
    return data


def signed(raw, family="issue_comment", delivery=None, headers=None):
    data = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    values = {
        "X-Hub-Signature-256": "sha256=" + hmac.new(KEY, data, hashlib.sha256).hexdigest(),
        "X-GitHub-Delivery": delivery or str(uuid4()),
        "X-GitHub-Event": family,
    }
    values.update(headers or {})
    return TransportEnvelope(values, data)


def setup(engine, *, mode="auto_investigate", receive=None):
    accounts = WebAccounts(engine)
    owner = accounts.create_owner(
        LoginInput(username="test-owner", password=SecretStr("fixture-owner-password"))
    )
    cookie = accounts.issue_session(owner)
    source_id = uuid4()
    registry = SourceRegistry(engine)
    instance = SourceInstance(
        scope=SourceScope(
            source_kind="github",
            source_instance_id=source_id,
            tenant_id="100",
            installation_id="200",
        ),
        project_id=engine.settings.project_id,
        adapter_version=VERSION,
        capabilities=CAPABILITIES,
        channel_capabilities=ChannelCapabilities(
            send="unsupported",
            idempotent_send="unsupported",
            query_delivery="unsupported",
            delete_message="unsupported",
        ),
        credential_ref="vault://synthetic/github-app",
    )
    registry.register_instance(instance)
    settings = GitHubSettings(
        app_id=10,
        account_id=100,
        installation_id=200,
        repository={"repository_id": 900, "full_name": engine.settings.repository, "private": True},
        events=tuple(ACTIONS),
        sender_ids=(42,),
        context_bot_ids=(77,),
        own_bot_ids=(99,),
        enabled=True,
    )
    sources = GitHubSources(engine)
    sources.configure(
        cookie,
        source_id,
        GitHubConfigure(expected_revision=0, settings=settings, review_ref="SA05-synthetic-owner"),
    )
    instance = registry.get_instance(source_id)
    adapter = GitHubAdapter(settings, KEY)
    identities = SourceIdentities(engine, {("github", VERSION): adapter})
    credential = identities.authenticate(
        source_id, signed(payload(), delivery="identity-bootstrap")
    )
    record = next(
        r for r in identities.evidence(cookie) if r["evidence_id"] == credential.evidence_id
    )
    binding = identities.bind(
        cookie,
        BindIdentity(
            evidence_id=credential.evidence_id,
            expected_identity_revision=record["identity_revision"],
            roles=("context_provider", "requester"),
            review_ref="SA05-identity-reviewed",
        ),
    )
    actor = adapter.verify_identity(instance, signed(payload())).actor
    raw = definition(instance, mode=mode).model_dump(mode="json")
    raw["selector"].update(
        actors=[actor.model_dump(mode="json")],
        conversations=["repository:900"],
        event_kinds=[
            family + "." + action for family, actions in ACTIONS.items() for action in actions
        ],
    )
    raw["execution"]["repositories"][0].update(base_sha=HEAD, paths=["src"])
    raw["receive"] = receive or {
        "enabled": True,
        "retain_body": True,
        "body_ttl_seconds": 300,
        "allow_history_context": True,
    }
    policy = PolicyDefinition.model_validate(raw)
    policy_id = uuid4()
    engine.source_policies.save(
        cookie,
        policy_id,
        PolicySave(expected_revision=0, definition=policy, review_ref="SA05-signed-policy"),
    )
    control = IngressSettings(
        owner="gateway",
        policy_id=policy_id,
        policy_revision=1,
        legacy_repository=engine.settings.repository,
    )
    engine.source_ingress.configure(
        cookie,
        source_id,
        IngressConfigure(expected_generation=0, settings=control, review_ref="SA05-single-owner"),
    )
    engine.source_ingress.cipher = BodyCipher(Fernet.generate_key())
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    secrets = GitHubSecrets(KEY, pem)
    activation = GitHubActivation(
        source_id,
        1,
        900,
        datetime.now(UTC) + timedelta(minutes=30),
        frozenset({"webhook", "history", "snapshot", "health"}),
        "SA05-controlled-no-network",
    )
    env = SimpleNamespace(
        engine=engine,
        cookie=cookie,
        sources=sources,
        settings=settings,
        instance=instance,
        source_id=source_id,
        adapter=adapter,
        identities=identities,
        credential=credential,
        binding=binding,
        policy_id=policy_id,
        policy=policy,
        control=control,
        secrets=secrets,
        activation=activation,
        calls=[],
        response=None,
    )

    def handler(request):
        env.calls.append(request)
        if env.response:
            changed = env.response(request)
            if changed is not None:
                return changed
        if request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "token": "ghs_SYNTHETIC_SHORT_LIVED_ONLY",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=59)).isoformat(),
                    "repositories": [{"id": 900, "full_name": engine.settings.repository}],
                    "permissions": body["permissions"],
                },
            )
        content = b"synthetic source file\n"
        blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if "/git/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": HEAD, "tree": {"sha": "d" * 40}})
        if "/git/trees/" in request.url.path:
            root = request.url.path.endswith("d" * 40)
            return httpx.Response(
                200,
                json={
                    "sha": "d" * 40 if root else "e" * 40,
                    "truncated": False,
                    "tree": [{"path": "src", "mode": "040000", "type": "tree", "sha": "e" * 40}]
                    if root
                    else [{"path": "a.py", "mode": "100644", "type": "blob", "sha": blob}],
                },
            )
        if "/git/blobs/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "sha": blob,
                    "encoding": "base64",
                    "size": len(content),
                    "content": base64.encodebytes(content).decode(),
                    "url": "https://evil.invalid/ignored",
                },
            )
        if "/pulls/" in request.url.path:
            return httpx.Response(200, json=payload("pull_request")["pull_request"])
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if request.url.path == "/installation/repositories":
            return httpx.Response(200, json={"repositories": [payload()["repository"]]})
        return httpx.Response(404)

    env.broker = GitHubReadBroker(
        sources, source_id, 1, secrets, activation, transport=httpx.MockTransport(handler)
    )
    env.runtime = GitHubRuntime(env.broker)
    return env


def grant(env, **changes):
    with env.engine.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM app.source_identities WHERE identity_id=%s",
            (env.credential.identity_id,),
        ).fetchone()
    value = SnapshotGrant(
        repository_id=900,
        head_sha=HEAD,
        paths=("src",),
        max_bytes=4096,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        policy_id=env.policy_id,
        policy_revision=1,
        identity_id=env.credential.identity_id,
        identity_revision=row["revision"],
        **changes,
    )
    identifier = uuid4()
    env.sources.save_grant(
        env.cookie,
        env.source_id,
        identifier,
        SnapshotSave(expected_revision=0, definition=value, review_ref="SA05-specific-repo-read"),
    )
    return identifier
