from __future__ import annotations

import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from shutil import copyfile
from threading import Event, Thread
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr, ValidationError

from mgar_v3.api import create_app
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import ROOT
from mgar_v3.sources.domain import (
    ActorRef,
    ChannelCapabilities,
    SourceCapabilities,
    SourceInstance,
    SourceScope,
    SourceThreadRef,
)
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.identity_domain import (
    BindIdentity,
    ControllerGrant,
    IdentityCredential,
    RestoreBinding,
    RestoreIdentity,
    RevisionCommand,
    RoleChange,
    VerifiedSubject,
)
from mgar_v3.sources.legacy import legacy_scope
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.store.migrate import migrate
from mgar_v3.web_access import SESSION_COOKIE, LoginInput, WebAccounts

ORIGIN = "http://127.0.0.1:18080"
KEY = b"synthetic-platform-signing-key-not-a-live-credential"
PREFIX = "/api/v3/source-identities"


class ControlledPlatform:
    """Real HMAC over a synthetic platform envelope; not a GitHub/Feishu SDK claim."""

    verifier_id = "controlled-hmac-v1"

    def __init__(self, engine):
        self.engine = engine
        self.calls = 0

    def verify_identity(self, instance, envelope):
        assert self.engine.connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        self.calls += 1
        expected = hmac.new(KEY, envelope.payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, envelope.headers.get("X-Controlled-Signature", "")):
            raise DomainError("IDENTITY_SIGNATURE_INVALID", "Controlled signature rejected", 401)
        raw = json.loads(envelope.payload)
        identity = SourceScope(
            source_kind=instance.scope.source_kind,
            source_instance_id=instance.scope.source_instance_id,
            tenant_id=raw["tenant_id"],
            installation_id=raw["installation_id"],
        )
        # Authorship comes only from the signed platform sender, never body/quotes/names.
        actor = ActorRef(
            scope=identity,
            id_type=raw["sender"]["id_type"],
            actor_id=raw["sender"]["id"],
            actor_kind=raw["sender"]["kind"],
        )
        return VerifiedSubject(
            actor=actor,
            source_thread=SourceThreadRef(scope=identity, conversation_id=raw["conversation_id"]),
            remote_event_id=raw["event_id"],
            payload_sha256=hashlib.sha256(envelope.payload).hexdigest(),
            occurred_at=raw["occurred_at"],
            verification_method=raw.get("method", "platform_signature"),
            historical_backfill=raw.get("historical", False),
        )


def source(engine, kind="feishu", **changes):
    identity = SourceScope(
        source_kind=kind,
        source_instance_id=uuid4(),
        tenant_id=changes.get("tenant_id", "Tenant-A"),
        installation_id=changes.get("installation_id", "Install-A"),
    )
    definition = SourceInstance(
        scope=identity,
        project_id=engine.settings.project_id,
        adapter_version="controlled-v1",
        capabilities=SourceCapabilities.model_validate(
            {k: "unknown" for k in SourceCapabilities.model_fields}
        ),
        channel_capabilities=ChannelCapabilities.model_validate(
            {k: "unknown" for k in ChannelCapabilities.model_fields}
        ),
    )
    SourceRegistry(engine).register_instance(definition)
    return definition


def envelope(instance, actor="Alice-AbC", kind="human", conversation="Chat-A", **changes):
    data = {
        "tenant_id": instance.scope.tenant_id,
        "installation_id": instance.scope.installation_id,
        "event_id": str(uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "conversation_id": conversation,
        "sender": {"id": actor, "kind": kind, "id_type": "open_id"},
        "display_name": "Same display name",
        "body": "synthetic private body must not be retained",
        **changes,
    }
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
    return TransportEnvelope(
        headers={"X-Controlled-Signature": hmac.new(KEY, raw, hashlib.sha256).hexdigest()},
        payload=raw,
    )


@pytest.fixture
def platform(engine):
    return ControlledPlatform(engine)


@pytest.fixture
def service(engine, platform):
    return SourceIdentities(
        engine, {("feishu", "controlled-v1"): platform, ("github", "controlled-v1"): platform}
    )


@pytest.fixture
def installation(engine):
    return source(engine)


@pytest.fixture
def cookie(engine):
    accounts = WebAccounts(engine)
    owner = accounts.create_owner(
        LoginInput(username="test-owner", password=SecretStr("fixture-owner-password"))
    )
    return accounts.issue_session(owner)


def proof(service, installation, **changes):
    return service.authenticate(
        installation.scope.source_instance_id, envelope(installation, **changes)
    )


def bind(
    service,
    cookie,
    installation,
    roles=("context_provider",),
    actor="Alice-AbC",
    kind="human",
    conversation="Chat-A",
    **target,
):
    receipt = proof(service, installation, actor=actor, kind=kind, conversation=conversation)
    record = next(
        row for row in service.evidence(cookie) if row["evidence_id"] == receipt.evidence_id
    )
    binding = service.bind(
        cookie,
        BindIdentity(
            evidence_id=receipt.evidence_id,
            expected_identity_revision=record["identity_revision"],
            roles=roles,
            review_ref="operator-reviewed",
            **target,
        ),
    )
    return binding, proof(service, installation, actor=actor, kind=kind, conversation=conversation)


def test_verified_proofs_are_not_public_json_assertions_and_no_verifier_fails_closed(
    engine, installation
):
    with pytest.raises(DomainError) as error:
        SourceIdentities(engine).authenticate(
            installation.scope.source_instance_id, envelope(installation)
        )
    assert error.value.code == "IDENTITY_VERIFIER_MISSING"


@pytest.mark.parametrize("tamper", ["signature", "sender", "tenant", "installation"])
def test_forged_sender_and_cross_scope_signatures_cannot_establish_identity(
    service, installation, tamper
):
    signed = envelope(installation)
    if tamper == "signature":
        candidate = TransportEnvelope(headers={}, payload=signed.payload)
    elif tamper == "sender":
        data = json.loads(signed.payload)
        data["sender"]["id"] = "Administrator"
        candidate = TransportEnvelope(headers=signed.headers, payload=json.dumps(data).encode())
    else:
        candidate = envelope(installation, **{tamper + "_id": "Other"})
    with pytest.raises(DomainError):
        service.authenticate(installation.scope.source_instance_id, candidate)
    with service.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.source_identities").fetchone()["n"] == 0
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_role_bindings").fetchone()["n"] == 0
        )


def test_case_whitespace_unicode_and_same_names_never_auto_bind(service, installation):
    values = ["AbC", "abc", " AbC", "AbC ", "é", "e\u0301", "用户"]
    receipts = [proof(service, installation, actor=value) for value in values]
    assert len({receipt.identity_id for receipt in receipts}) == len(values)
    with service.engine.transaction() as conn:
        rows = conn.execute("SELECT actor_id,principal_id FROM app.source_identities").fetchall()
        assert {row["actor_id"] for row in rows} == set(values)
        assert all(row["principal_id"] is None for row in rows)


def test_same_actor_in_two_tenants_or_installs_has_independent_identity(
    service, engine, installation
):
    other = source(engine, tenant_id="Tenant-B")
    again = source(engine, installation_id="Install-B")
    receipts = [
        proof(service, item, actor="Same-ID", event_id="same-event-id")
        for item in [installation, other, again]
    ]
    assert len({r.identity_id for r in receipts}) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"historical": True},
        {"occurred_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
        {"occurred_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    ],
)
def test_backfill_and_stale_callbacks_do_not_create_control_credentials(
    service, installation, changes
):
    with pytest.raises(DomainError):
        proof(service, installation, **changes)


def test_replayed_proof_does_not_issue_another_bearer(service, installation):
    signed = envelope(installation)
    first = service.authenticate(installation.scope.source_instance_id, signed)
    with pytest.raises(DomainError) as error:
        service.authenticate(installation.scope.source_instance_id, signed)
    assert error.value.code == "IDENTITY_PROOF_REPLAY"
    assert first.token.get_secret_value() not in repr(first)


def test_malformed_verified_sender_fails_closed_and_records_minimal_denial(
    service, installation, cookie
):
    with pytest.raises(DomainError) as error:
        proof(service, installation, sender={"id": 7, "kind": "human", "id_type": "open_id"})
    assert error.value.code == "IDENTITY_ASSERTION_INVALID"
    with service.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.source_identities").fetchone()["n"] == 0
    assert any(row["reason_code"] == "IDENTITY_ASSERTION_INVALID" for row in service.audit(cookie))


def test_verified_assertion_is_bound_to_the_actual_signed_bytes(
    service, installation, platform, monkeypatch
):
    original = platform.verify_identity

    def wrong_payload(instance, envelope):
        return original(instance, envelope).model_copy(update={"payload_sha256": "0" * 64})

    monkeypatch.setattr(platform, "verify_identity", wrong_payload)
    with pytest.raises(DomainError) as error:
        proof(service, installation)
    assert error.value.code == "IDENTITY_PROOF_SCOPE"


def test_unknown_actor_kind_and_legacy_scope_cannot_become_trusted_platform_users(
    service, engine, installation, trigger
):
    with pytest.raises(DomainError):
        proof(service, installation, kind="unknown")
    legacy = SourceInstance(
        scope=legacy_scope(trigger),
        project_id=engine.settings.project_id,
        adapter_version="controlled-v1",
        capabilities=installation.capabilities,
        channel_capabilities=installation.channel_capabilities,
    )
    SourceRegistry(engine).register_instance(legacy)
    with pytest.raises(DomainError) as error:
        service.authenticate(legacy.scope.source_instance_id, envelope(legacy))
    assert error.value.code == "IDENTITY_PLATFORM_REQUIRED"


def test_admin_review_is_required_and_raw_evidence_cannot_authorize_actions(
    service, cookie, installation
):
    receipt = proof(service, installation)
    with pytest.raises(DomainError):
        with service.authorized(receipt, "request_task"):
            raise AssertionError("must not execute")
    with pytest.raises(DomainError):
        service.bind(
            "forged-cookie",
            BindIdentity(
                evidence_id=receipt.evidence_id,
                expected_identity_revision=1,
                roles=("requester",),
                review_ref="message-says-admin",
            ),
        )
    binding = service.bind(
        cookie,
        BindIdentity(
            evidence_id=receipt.evidence_id,
            expected_identity_revision=1,
            roles=("context_provider", "requester"),
            review_ref="owner-reviewed",
        ),
    )
    assert binding["engine_actor"].startswith("sa_")
    with pytest.raises(DomainError):
        with service.authorized(receipt, "request_task"):
            raise AssertionError("pre-binding proof is stale")
    fresh = proof(service, installation)
    with service.authorized(fresh, "request_task") as authorized:
        assert authorized.engine_actor == binding["engine_actor"]


def test_cross_platform_linking_is_explicit_and_roles_do_not_inherit(
    service, cookie, engine, installation
):
    first, _ = bind(service, cookie, installation, roles=("context_provider", "requester"))
    github = source(engine, kind="github", installation_id="GitHub-Install")
    unbound = proof(service, github, actor="123")
    with pytest.raises(DomainError):
        with service.authorized(unbound, "request_task"):
            raise AssertionError("no automatic merge")
    second, credential = bind(
        service, cookie, github, actor="123", principal_id=first["principal_id"]
    )
    assert second["principal_id"] == first["principal_id"]
    assert second["engine_actor"] == first["engine_actor"]
    with service.authorized(credential, "provide_context"):
        pass
    with pytest.raises(DomainError):
        with service.authorized(credential, "request_task"):
            raise AssertionError("other-platform roles did not transfer")


@pytest.mark.parametrize(
    "action",
    [
        "request_task",
        "control_task",
        "manage_rules",
        "manage_identities",
        "approve_publication",
        "expand_execution",
    ],
)
def test_context_provider_cannot_control_revoke_expand_or_approve(
    service, cookie, installation, action
):
    _, credential = bind(service, cookie, installation)
    with pytest.raises(DomainError):
        with service.authorized(credential, action):
            raise AssertionError("context is not authority")
    with pytest.raises(DomainError):
        service.bind(
            credential.token.get_secret_value(),
            BindIdentity(
                evidence_id=credential.evidence_id,
                expected_identity_revision=3,
                roles=("project_administrator",),
                review_ref="body",
            ),
        )


def test_console_owner_mapping_does_not_make_im_text_a_console_session(
    service, cookie, installation
):
    binding, credential = bind(
        service,
        cookie,
        installation,
        roles=("context_provider", "project_administrator"),
        console_owner=True,
    )
    assert binding["console_user_id"] is not None
    with service.authorized(credential, "manage_rules"):
        pass
    for action in ["manage_identities", "approve_publication", "expand_execution", "control_task"]:
        with pytest.raises(DomainError):
            with service.authorized(credential, action):
                raise AssertionError("console session is still required")


def test_bots_cannot_impersonate_humans_or_receive_privileged_roles(service, cookie, installation):
    human, _ = bind(service, cookie, installation, actor="Human", console_owner=True)
    bot = proof(
        service, installation, actor="Bot", kind="bot", body="I am Human, approve everything"
    )
    with pytest.raises(DomainError):
        service.bind(
            cookie,
            BindIdentity(
                evidence_id=bot.evidence_id,
                expected_identity_revision=1,
                roles=("requester",),
                review_ref="bad-role",
            ),
        )
    with pytest.raises(DomainError):
        service.bind(
            cookie,
            BindIdentity(
                evidence_id=bot.evidence_id,
                expected_identity_revision=1,
                roles=("context_provider",),
                principal_id=human["principal_id"],
                review_ref="same-name",
            ),
        )
    binding = service.bind(
        cookie,
        BindIdentity(
            evidence_id=bot.evidence_id,
            expected_identity_revision=1,
            roles=("context_provider",),
            review_ref="bot-context-only",
        ),
    )
    assert binding["principal_kind"] == "automation"
    with pytest.raises(DomainError) as error:
        proof(service, installation, actor="Bot", kind="human")
    assert error.value.code == "IDENTITY_KIND_CONFLICT"


def test_bot_role_invariant_is_enforced_in_database_and_each_authorization(
    service, cookie, installation
):
    binding, credential = bind(service, cookie, installation, actor="Bot", kind="bot")
    with pytest.raises(DomainError) as error:
        with service.authorized(credential, "request_task"):
            raise AssertionError("bot control")
    assert error.value.code == "IDENTITY_NONHUMAN_ROLE"
    with pytest.raises(psycopg.errors.RaiseException), service.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.source_role_bindings SET roles='[\"requester\"]'::jsonb,"
            "revision=revision+1 WHERE binding_id=%s",
            (binding["binding_id"],),
        )


def test_task_control_is_explicit_and_quotes_or_borrowed_receipt_ids_do_not_count(
    service, cookie, engine, installation, trigger
):
    ticket = engine.ingest(trigger)
    other = engine.ingest(
        trigger.model_copy(update={"external_event_id": "second-task", "issue_number": 2})
    )
    binding, _ = bind(
        service,
        cookie,
        installation,
        actor="Controller",
        roles=("context_provider", "task_controller"),
    )
    granted = service.grant_controller(
        cookie,
        binding["binding_id"],
        ControllerGrant(
            ticket_id=ticket.ticket_id,
            expected_revision=binding["revision"],
            review_ref="explicit-ticket-owner",
        ),
    )
    controller = proof(service, installation, actor="Controller")
    attacker_binding, _ = bind(service, cookie, installation, actor="Attacker")
    attacker = proof(
        service,
        installation,
        actor="Attacker",
        body={
            "quoted_sender": "Controller",
            "requester": "Controller",
            "evidence_id": str(controller.evidence_id),
            "roles": ["project_administrator"],
        },
    )
    with pytest.raises(DomainError):
        with service.authorized(attacker, "control_task", ticket.ticket_id):
            engine.cancel(ticket.run_id)
    borrowed = IdentityCredential(
        evidence_id=controller.evidence_id, identity_id=controller.identity_id, token=attacker.token
    )
    with pytest.raises(DomainError):
        with service.authorized(borrowed, "control_task", ticket.ticket_id):
            engine.cancel(ticket.run_id)
    assert attacker_binding["principal_id"] != granted["principal_id"]
    with pytest.raises(DomainError):
        with service.authorized(controller, "control_task", other.ticket_id):
            engine.cancel(other.run_id)
    with service.authorized(controller, "control_task", ticket.ticket_id) as authorized:
        assert authorized.ticket_id == ticket.ticket_id
        engine.cancel(ticket.run_id)
    assert engine.get_run(ticket.run_id)["status"] == "cancelled"
    assert engine.get_run(other.run_id)["status"] == "queued"


def test_same_platform_identity_in_another_chat_has_no_inherited_role(
    service, cookie, installation
):
    binding, _ = bind(service, cookie, installation, roles=("requester",))
    elsewhere = proof(service, installation, conversation="Other-Chat")
    assert elsewhere.identity_id == binding["identity_id"]
    with pytest.raises(DomainError):
        with service.authorized(elsewhere, "request_task"):
            raise AssertionError("chat scope did not transfer")


def test_revocation_and_role_changes_fence_old_credentials_immediately(
    service, cookie, installation
):
    binding, credential = bind(
        service, cookie, installation, roles=("context_provider", "requester")
    )
    with service.authorized(credential, "request_task"):
        pass
    changed = service.roles(
        cookie,
        binding["binding_id"],
        RoleChange(
            expected_revision=binding["revision"],
            roles=("context_provider",),
            review_ref="narrow-role",
        ),
    )
    with pytest.raises(DomainError):
        with service.authorized(credential, "request_task"):
            raise AssertionError("cached role")
    with pytest.raises(DomainError):
        with service.authorized(proof(service, installation), "request_task"):
            raise AssertionError("removed role")
    restored = service.roles(
        cookie,
        binding["binding_id"],
        RoleChange(
            expected_revision=changed["revision"],
            roles=("requester",),
            review_ref="explicit-regrant",
        ),
    )
    with pytest.raises(DomainError):
        with service.authorized(credential, "request_task"):
            raise AssertionError("old credential resurrected")
    fresh = proof(service, installation)
    with service.authorized(fresh, "request_task"):
        pass
    revoked = service.revoke_binding(
        cookie,
        binding["binding_id"],
        RevisionCommand(expected_revision=restored["revision"], review_ref="stop"),
    )
    assert not revoked["active"]
    with pytest.raises(DomainError):
        with service.authorized(fresh, "request_task"):
            raise AssertionError("revoked binding")
    with pytest.raises(DomainError):
        with service.authorized(proof(service, installation), "request_task"):
            raise AssertionError("verification cannot regrant")


def test_explicit_restore_needs_post_revocation_proof_and_does_not_restore_ticket_grants(
    service, cookie, engine, installation, trigger
):
    ticket = engine.ingest(trigger)
    binding, _ = bind(service, cookie, installation, roles=("task_controller",))
    binding = service.grant_controller(
        cookie,
        binding["binding_id"],
        ControllerGrant(
            ticket_id=ticket.ticket_id,
            expected_revision=binding["revision"],
            review_ref="controller",
        ),
    )
    before = proof(service, installation)
    revoked = service.revoke_binding(
        cookie,
        binding["binding_id"],
        RevisionCommand(expected_revision=binding["revision"], review_ref="revoke"),
    )
    with pytest.raises(DomainError):
        service.restore_binding(
            cookie,
            binding["binding_id"],
            RestoreBinding(
                evidence_id=before.evidence_id,
                expected_revision=revoked["revision"],
                roles=("task_controller",),
                review_ref="old-proof",
            ),
        )
    after = proof(service, installation)
    restored = service.restore_binding(
        cookie,
        binding["binding_id"],
        RestoreBinding(
            evidence_id=after.evidence_id,
            expected_revision=revoked["revision"],
            roles=("task_controller",),
            review_ref="fresh-owner-review",
        ),
    )
    assert restored["controlled_tickets"] == []
    fresh = proof(service, installation)
    with pytest.raises(DomainError):
        with service.authorized(fresh, "control_task", ticket.ticket_id):
            engine.cancel(ticket.run_id)


def test_identity_revocation_is_sticky_until_explicit_owner_restore(service, cookie, installation):
    binding, credential = bind(service, cookie, installation, roles=("requester",))
    result = service.revoke_identity(
        cookie,
        binding["identity_id"],
        RevisionCommand(
            expected_revision=binding["identity_revision"], review_ref="source-user-revoked"
        ),
    )
    with pytest.raises(DomainError):
        with service.authorized(credential, "request_task"):
            raise AssertionError("old proof")
    current = proof(service, installation)
    with pytest.raises(DomainError):
        with service.authorized(current, "request_task"):
            raise AssertionError("fresh message cannot undo revoke")
    with pytest.raises(DomainError):
        service.restore_identity(
            cookie,
            binding["identity_id"],
            RestoreIdentity(
                evidence_id=credential.evidence_id,
                expected_revision=result["revision"],
                review_ref="stale",
            ),
        )
    restored = service.restore_identity(
        cookie,
        binding["identity_id"],
        RestoreIdentity(
            evidence_id=current.evidence_id,
            expected_revision=result["revision"],
            review_ref="new-proof-owner-review",
        ),
    )
    assert not restored["revoked"]
    with pytest.raises(DomainError):
        with service.authorized(current, "request_task"):
            raise AssertionError("restore fences its old proof too")
    with service.authorized(proof(service, installation), "request_task"):
        pass


def test_controller_revoke_invalidates_old_proof_and_exact_task_membership(
    service, cookie, engine, installation, trigger
):
    ticket = engine.ingest(trigger)
    binding, _ = bind(service, cookie, installation, roles=("task_controller",))
    granted = service.grant_controller(
        cookie,
        binding["binding_id"],
        ControllerGrant(
            ticket_id=ticket.ticket_id, expected_revision=binding["revision"], review_ref="grant"
        ),
    )
    current = proof(service, installation)
    service.revoke_controller(
        cookie,
        binding["binding_id"],
        ticket.ticket_id,
        RevisionCommand(expected_revision=granted["revision"], review_ref="stop-controller"),
    )
    for credential in [current, proof(service, installation)]:
        with pytest.raises(DomainError):
            with service.authorized(credential, "control_task", ticket.ticket_id):
                engine.cancel(ticket.run_id)
    assert engine.get_run(ticket.run_id)["status"] == "queued"


def test_changed_source_revision_and_expired_credentials_are_not_authority(
    service, cookie, installation, monkeypatch
):
    _, credential = bind(service, cookie, installation, roles=("requester",))
    SourceRegistry(service.engine).register_instance(
        installation.model_copy(update={"revision": 2, "display_name": "new scope revision"})
    )
    with pytest.raises(DomainError):
        with service.authorized(credential, "request_task"):
            raise AssertionError("source changed")
    monkeypatch.setattr("mgar_v3.sources.identity.PROOF_SECONDS", 1)
    fresh = proof(service, installation)
    time.sleep(1.15)
    with pytest.raises(DomainError):
        with service.authorized(fresh, "request_task"):
            raise AssertionError("expired")


def test_replaced_console_session_cannot_manage_with_a_cached_browser_identity(
    service, cookie, installation
):
    binding, _ = bind(service, cookie, installation)
    accounts = WebAccounts(service.engine)
    cached = accounts.session(cookie)
    assert cached is not None
    accounts.revoke(cookie)
    with pytest.raises(DomainError) as error:
        service.roles(
            cookie,
            binding["binding_id"],
            RoleChange(
                expected_revision=binding["revision"],
                roles=("project_administrator",),
                review_ref="cached-session",
            ),
        )
    assert error.value.code == "AUTH_REQUIRED"


def test_admin_mutation_rolls_back_entire_binding_and_audits_denial(
    service, cookie, installation, monkeypatch
):
    receipt = proof(service, installation)
    original = service._view

    def fail_after_writes(*args):
        raise DomainError("INJECTED_IDENTITY_FAILURE", "controlled rollback")

    monkeypatch.setattr(service, "_view", fail_after_writes)
    request = BindIdentity(
        evidence_id=receipt.evidence_id,
        expected_identity_revision=1,
        roles=("requester",),
        review_ref="rollback",
    )
    with pytest.raises(DomainError):
        service.bind(cookie, request)
    with service.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_role_bindings").fetchone()["n"] == 0
        )
        assert conn.execute("SELECT count(*) AS n FROM app.source_principals").fetchone()["n"] == 0
        identity = conn.execute(
            "SELECT principal_id,revision FROM app.source_identities"
        ).fetchone()
        assert identity["principal_id"] is None and identity["revision"] == 1
    monkeypatch.setattr(service, "_view", original)
    assert service.bind(cookie, request)["engine_actor"].startswith("sa_")
    assert any(
        row["decision"] == "deny" and row["reason_code"] == "INJECTED_IDENTITY_FAILURE"
        for row in service.audit(cookie)
    )


def test_authorization_and_mutation_rollback_together(
    service, cookie, engine, installation, trigger
):
    ticket = engine.ingest(trigger)
    binding, _ = bind(service, cookie, installation, roles=("task_controller",))
    service.grant_controller(
        cookie,
        binding["binding_id"],
        ControllerGrant(
            ticket_id=ticket.ticket_id,
            expected_revision=binding["revision"],
            review_ref="controlled",
        ),
    )
    current = proof(service, installation)
    with pytest.raises(RuntimeError):
        with service.authorized(current, "control_task", ticket.ticket_id):
            engine.cancel(ticket.run_id)
            raise RuntimeError("rollback protected operation")
    assert engine.get_run(ticket.run_id)["status"] == "queued"


def test_two_role_updates_have_one_revision_winner(service, cookie, installation):
    binding, _ = bind(service, cookie, installation)

    def update(role):
        try:
            service.roles(
                cookie,
                binding["binding_id"],
                RoleChange(expected_revision=binding["revision"], roles=(role,), review_ref="race"),
            )
            return "ok"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["requester", "project_administrator"]))
    assert sorted(results) == ["IDENTITY_REVISION_CONFLICT", "ok"]


def test_revocation_linearizes_with_protected_engine_transaction(
    service, cookie, engine, installation, trigger
):
    ticket = engine.ingest(trigger)
    binding, _ = bind(service, cookie, installation, roles=("task_controller",))
    binding = service.grant_controller(
        cookie,
        binding["binding_id"],
        ControllerGrant(
            ticket_id=ticket.ticket_id, expected_revision=binding["revision"], review_ref="grant"
        ),
    )
    current = proof(service, installation)
    entered, release, revoked = Event(), Event(), Event()
    errors = []

    def work():
        try:
            with service.authorized(current, "control_task", ticket.ticket_id):
                entered.set()
                assert release.wait(5)
                engine.cancel(ticket.run_id)
        except BaseException as exc:
            errors.append(exc)

    def revoke():
        try:
            service.revoke_binding(
                cookie,
                binding["binding_id"],
                RevisionCommand(
                    expected_revision=binding["revision"], review_ref="concurrent-revoke"
                ),
            )
            revoked.set()
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=work)
    worker.start()
    assert entered.wait(5)
    revoker = Thread(target=revoke)
    revoker.start()
    try:
        assert not revoked.wait(0.1)
    finally:
        release.set()
        worker.join(5)
        revoker.join(5)
    assert not errors and revoked.is_set() and not worker.is_alive() and not revoker.is_alive()
    with pytest.raises(DomainError):
        with service.authorized(current, "control_task", ticket.ticket_id):
            raise AssertionError("new operation after revoke")


def test_identity_tables_and_audit_never_retain_message_body_or_bearer(
    service, cookie, installation
):
    _, credential = bind(service, cookie, installation)
    exported = json.dumps(
        [service.evidence(cookie), service.bindings(cookie), service.audit(cookie)], default=str
    )
    assert credential.token.get_secret_value() not in exported
    assert "credential_hash" not in exported
    assert "synthetic private body must not be retained" not in exported
    with service.engine.transaction() as conn:
        stored = conn.execute(
            "SELECT credential_hash FROM app.source_identity_evidence WHERE evidence_id=%s",
            (credential.evidence_id,),
        ).fetchone()
        assert (
            stored["credential_hash"]
            == hashlib.sha256(credential.token.get_secret_value().encode()).hexdigest()
        )
    with pytest.raises(psycopg.errors.RaiseException), service.engine.transaction() as conn:
        conn.execute("UPDATE app.source_identity_evidence SET auth_revision=999")
    with pytest.raises(psycopg.errors.RaiseException), service.engine.transaction() as conn:
        conn.execute("DELETE FROM app.source_identity_audit")


def test_cross_project_principal_and_controller_targets_are_rejected(
    service, cookie, engine, installation
):
    _, _ = bind(service, cookie, installation, actor="First")
    other_id = uuid4()
    with engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.source_principals(principal_id,project_id,principal_kind,"
            "engine_actor) "
            "VALUES (%s,'other-project','human',%s)",
            (other_id, "sa_" + other_id.hex),
        )
    incoming = proof(service, installation, actor="Second")
    with pytest.raises(DomainError) as error:
        service.bind(
            cookie,
            BindIdentity(
                evidence_id=incoming.evidence_id,
                expected_identity_revision=1,
                roles=("requester",),
                principal_id=other_id,
                review_ref="cross-project",
            ),
        )
    assert error.value.code == "IDENTITY_PRINCIPAL_NOT_FOUND"
    binding, _ = bind(service, cookie, installation, actor="Controller", roles=("task_controller",))
    with pytest.raises(DomainError):
        service.grant_controller(
            cookie,
            binding["binding_id"],
            ControllerGrant(
                ticket_id=uuid4(),
                expected_revision=binding["revision"],
                review_ref="foreign-or-unknown-task",
            ),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_identity_revision": True},
        {"roles": ["admin"]},
        {"roles": ["requester", "requester"]},
        {"roles": []},
        {"sender": {"id": "owner"}},
        {"console_owner": True, "principal_id": str(uuid4())},
    ],
)
def test_management_schema_does_not_accept_role_aliases_or_sender_authority(changes):
    with pytest.raises(ValidationError):
        BindIdentity.model_validate(
            {
                "evidence_id": str(uuid4()),
                "expected_identity_revision": 1,
                "roles": ["context_provider"],
                "review_ref": "review",
                **changes,
            }
        )


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("source-identity-bind-v1", BindIdentity),
        ("source-identity-role-change-v1", RoleChange),
        ("source-identity-subject-v1", VerifiedSubject),
    ],
)
def test_published_identity_schema_matches_the_runtime_contract(name, model):
    published = json.loads((ROOT / "schemas/super-agent-v1" / (name + ".schema.json")).read_text())
    expected = model.model_json_schema()
    expected["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    assert published == expected


def test_management_api_reuses_cookie_origin_csrf_and_hides_gateway_credentials(
    service, settings, engine, installation
):
    with TestClient(
        create_app(settings, engine, run_background=False),
        base_url=ORIGIN,
        client=("127.0.0.1", 12345),
    ) as client:
        assert client.get(PREFIX + "/bindings").status_code == 401
        client.headers["X-MGAR-Token"] = settings.local_token.get_secret_value()
        assert client.get(PREFIX + "/bindings").status_code == 401
        client.headers.pop("X-MGAR-Token")
        created = client.post(
            "/api/v3/auth/setup",
            headers={"Origin": ORIGIN},
            json={"username": "test-owner", "password": "fixture-owner-password"},
        )
        assert created.status_code == 201
        cookie = client.cookies.get(SESSION_COOKIE)
        assert cookie
        receipt = proof(service, installation)
        request = {
            "evidence_id": str(receipt.evidence_id),
            "expected_identity_revision": 1,
            "roles": ["context_provider", "requester"],
            "review_ref": "authenticated-owner-review",
        }
        assert client.post(PREFIX + "/bindings", json=request).status_code == 403
        client.headers["Origin"] = ORIGIN
        assert client.post(PREFIX + "/bindings", json=request).status_code == 403
        client.headers["X-CSRF-Token"] = created.json()["csrf_token"]
        assert (
            client.post(
                PREFIX + "/bindings", json=request, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        response = client.post(PREFIX + "/bindings", json=request)
        assert response.status_code == 200, response.text
        binding = response.json()
        fresh = proof(service, installation)
        for suffix in ["/bindings", "/principals", "/evidence", "/audit"]:
            view = client.get(PREFIX + suffix)
            assert view.status_code == 200 and view.headers["cache-control"] == "no-store"
            assert fresh.token.get_secret_value() not in view.text
            assert "credential_hash" not in view.text
        assert (
            client.post(
                PREFIX + "/evidence", json={"verified": True, "sender": "owner"}
            ).status_code
            == 405
        )
        assert client.post(PREFIX + "/authenticate", json={"sender": "owner"}).status_code == 404
        assert (
            client.put(
                PREFIX + "/bindings/" + binding["binding_id"] + "/roles",
                json={
                    "expected_revision": binding["revision"],
                    "roles": ["context_provider"],
                    "review_ref": "reduce",
                },
            ).status_code
            == 200
        )
        with pytest.raises(DomainError):
            with service.authorized(fresh, "request_task"):
                raise AssertionError("API role change fenced old credential")
        assert client.post("/api/v3/auth/logout").status_code == 204
        assert client.get(PREFIX + "/bindings").status_code == 401


def test_session_revoked_between_http_dependency_and_mutation_cannot_change_roles(
    service, cookie, installation, settings, engine, monkeypatch
):
    binding, _ = bind(service, cookie, installation)
    csrf = WebAccounts(engine).session(cookie).csrf_token
    original = WebAccounts.session

    def revoke_after_valid_lookup(accounts, token):
        result = original(accounts, token)
        if result is not None:
            accounts.revoke(token)
        return result

    with TestClient(
        create_app(settings, engine, run_background=False),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        client=("127.0.0.1", 12345),
    ) as client:
        client.cookies.set(SESSION_COOKIE, cookie)
        monkeypatch.setattr(WebAccounts, "session", revoke_after_valid_lookup)
        response = client.put(
            PREFIX + "/bindings/" + str(binding["binding_id"]) + "/roles",
            json={
                "expected_revision": binding["revision"],
                "roles": ["project_administrator"],
                "review_ref": "session-race",
            },
        )
        assert response.status_code == 401 and response.json()["code"] == "AUTH_REQUIRED"
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT roles,revision FROM app.source_role_bindings WHERE binding_id=%s",
            (binding["binding_id"],),
        ).fetchone()
        assert row["roles"] == ["context_provider"] and row["revision"] == binding["revision"]


def test_api_receipt_uuid_is_not_a_platform_identity_or_admin_authorization(settings, engine):
    with TestClient(
        create_app(settings, engine, run_background=False),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
        client=("127.0.0.1", 12345),
    ) as client:
        created = client.post(
            "/api/v3/auth/setup",
            json={"username": "test-owner", "password": "fixture-owner-password"},
        )
        client.headers["X-CSRF-Token"] = created.json()["csrf_token"]
        response = client.post(
            PREFIX + "/bindings",
            json={
                "evidence_id": str(uuid4()),
                "expected_identity_revision": 1,
                "roles": ["project_administrator"],
                "review_ref": "quoted-receipt",
            },
        )
        assert (
            response.status_code == 403 and response.json()["code"] == "IDENTITY_EVIDENCE_INVALID"
        )
        assert (
            client.post(
                PREFIX + "/bindings",
                json={"actor_id": "owner", "sender": "owner", "roles": ["project_administrator"]},
            ).status_code
            == 422
        )


def test_30_to_31_upgrade_preserves_source_contract_and_old_business_rows(
    settings, trigger, tmp_path
):
    base = settings.database_url.get_secret_value()
    name = "mgar_sa02_upgrade_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        dsn = make_conninfo(base, dbname=name)
        older, upgrade = tmp_path / "old-30", tmp_path / "up-to-0031"
        older.mkdir()
        upgrade.mkdir()
        paths = [
            p
            for p in sorted((ROOT / "migrations").glob("*.sql"))
            if p.name <= "0031_source_identities.sql"
        ]
        for path in paths:
            copyfile(path, upgrade / path.name)
            if path.name < "0031_source_identities.sql":
                copyfile(path, older / path.name)
        assert len(migrate(dsn, older)) == 30
        config = settings.model_copy(update={"database_url": SecretStr(dsn)})
        previous = Engine(config, ArtifactStore(tmp_path / "artifacts"))
        try:
            original = previous.ingest(trigger)
            registered = source(previous)
            with previous.transaction() as conn:
                trigger_row = conn.execute(
                    "SELECT * FROM app.trigger_events WHERE event_id=%s", (original.event_id,)
                ).fetchone()
        finally:
            previous.close()
        assert (
            migrate(dsn, upgrade) == ["0031_source_identities.sql"] and migrate(dsn, upgrade) == []
        )
        current = Engine(config, ArtifactStore(tmp_path / "artifacts"))
        try:
            assert (
                SourceRegistry(current).get_instance(registered.scope.source_instance_id)
                == registered
            )
            with current.transaction() as conn:
                assert (
                    conn.execute(
                        "SELECT * FROM app.trigger_events WHERE event_id=%s", (original.event_id,)
                    ).fetchone()
                    == trigger_row
                )
                actual = conn.execute(
                    "SELECT name,sha256 FROM app.schema_migrations ORDER BY name"
                ).fetchall()
                assert [(r["name"], r["sha256"]) for r in actual] == [
                    (p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths
                ]
        finally:
            current.close()
    finally:
        assert name.startswith("mgar_sa02_upgrade_") and len(name) == 50
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
