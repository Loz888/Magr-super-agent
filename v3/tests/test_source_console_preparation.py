"""First typed console/credential scope and exact proposed SQL checks, no runtime."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from mgar_v3.sources.domain import ChannelCapabilities, SourceInstance, SourceScope
from mgar_v3.sources.github import CAPABILITIES
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.super_agent.source_console_domain import (
    ConsoleCredentialSave,
    ConsoleDisableSources,
    ConsoleSourceRevoke,
    ConsoleSourceSave,
    ConsoleTaskStop,
    FeishuConsoleCredentials,
    GitHubConsoleCredentials,
    credential_binding,
    credential_values,
    expiry_valid,
)
from mgar_v3.web_access import LoginInput, WebAccounts

A = Path(__file__).resolve().parents[1]


def source_payload(kind="github"):
    settings = (
        {
            "app_id": 1,
            "account_id": 2,
            "installation_id": 3,
            "repository": {"repository_id": 4, "full_name": "example/mgar-test", "private": False},
            "events": ["issue_comment"],
            "sender_ids": [10],
            "own_bot_ids": [20],
        }
        if kind == "github"
        else {
            "app_id": "cli_SA19",
            "tenant_key": "Tenant_SA19",
            "bot_open_id": "Bot_SA19",
            "chat_ids": ["Chat_SA19"],
            "sender_open_ids": ["Human_SA19"],
            "scopes_review_ref": "fixture-scopes",
        }
    )
    return {
        "source_kind": kind,
        "expected_source_revision": 0,
        "configuration": {"expected_revision": 0, "settings": settings, "review_ref": "fixture"},
    }


def secret_payload(kind="github"):
    return (
        {"app_private_key": "SA19_fixture_PRIVATE", "webhook_secret": "SA19_fixture_WEBHOOK"}
        if kind == "github"
        else {
            "app_secret": "SA19_fixture_APP",
            "encrypt_key": "SA19_fixture_ENCRYPT",
            "verification_token": "SA19_fixture_TOKEN",
        }
    )


@pytest.mark.parametrize("kind", ["github", "feishu"])
def test_console_source_defaults_disabled_and_preserves_platform_identifiers(kind):
    value = ConsoleSourceSave.model_validate(source_payload(kind))
    assert not value.configuration.settings.enabled
    assert value.expected_credential_revision == 0 and value.credentials is None
    if kind == "feishu":
        assert value.configuration.settings.sender_open_ids == ("Human_SA19",)
        assert value.configuration.settings.receive_mode == "mentions_or_topics"


@pytest.mark.parametrize("kind", ["github", "feishu"])
def test_credential_models_redact_repr_and_serialized_public_models(kind):
    payload = source_payload(kind)
    payload.update(
        credentials=secret_payload(kind),
        credential_valid_until=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    value = ConsoleSourceSave.model_validate(payload)
    assert value.credentials is not None
    plain = credential_values(value.credentials)
    for secret in plain.values():
        assert secret not in repr(value) and secret not in value.model_dump_json()


@pytest.mark.parametrize("field", ["expected_source_revision", "expected_credential_revision"])
@pytest.mark.parametrize("bad", [-1, True, "1", None])
def test_source_cas_is_a_strict_nonnegative_counter(field, bad):
    payload = source_payload()
    payload[field] = bad
    with pytest.raises(ValidationError):
        ConsoleSourceSave.model_validate(payload)


@pytest.mark.parametrize("kind", ["github", "feishu"])
def test_platform_configuration_and_secrets_cannot_be_crossed(kind):
    payload = source_payload(kind)
    other = "feishu" if kind == "github" else "github"
    payload["configuration"] = source_payload(other)["configuration"]
    with pytest.raises(ValidationError):
        ConsoleSourceSave.model_validate(payload)


@pytest.mark.parametrize("kind", ["github", "feishu"])
def test_platform_credentials_cannot_be_crossed(kind):
    payload = source_payload(kind)
    payload.update(
        credentials=secret_payload("feishu" if kind == "github" else "github"),
        credential_valid_until=datetime.now(UTC) + timedelta(hours=1),
    )
    with pytest.raises(ValidationError):
        ConsoleSourceSave.model_validate(payload)


@pytest.mark.parametrize("field", ["actor", "provider", "key_path", "activation", "gates"])
def test_console_caller_cannot_inject_identity_providers_or_activation(field):
    payload = source_payload()
    payload[field] = "not-authority"
    with pytest.raises(ValidationError):
        ConsoleSourceSave.model_validate(payload)


@pytest.mark.parametrize("kind", ["github", "feishu"])
@pytest.mark.parametrize("only", ["credentials", "credential_valid_until"])
def test_credential_replacement_requires_both_values_and_explicit_expiry(kind, only):
    payload = source_payload(kind)
    payload[only] = secret_payload(kind) if only == "credentials" else datetime.now(UTC)
    with pytest.raises(ValidationError):
        ConsoleSourceSave.model_validate(payload)


@pytest.mark.parametrize("field", ["app_secret", "verification_token", "encrypt_key"])
@pytest.mark.parametrize("bad", ["", "  ", "bad\nsecret", "x" * 4097])
def test_feishu_secret_input_rejects_empty_control_and_oversize(field, bad):
    payload = secret_payload("feishu")
    payload[field] = bad
    with pytest.raises(ValidationError):
        FeishuConsoleCredentials.model_validate(payload)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("app_private_key", ""),
        ("app_private_key", "x" * 16385),
        ("app_private_key", "bad\0key"),
        ("webhook_secret", "bad\rsecret"),
        ("webhook_secret", "x" * 4097),
    ],
)
def test_github_secret_bounds_and_multiline_key(field, bad):
    payload = secret_payload()
    payload[field] = bad
    with pytest.raises(ValidationError):
        GitHubConsoleCredentials.model_validate(payload)


def test_multiline_private_key_remains_exact_and_binding_is_installation_specific():
    raw = "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n"
    secret = GitHubConsoleCredentials(app_private_key=raw, webhook_secret="fixture")
    assert credential_values(secret)["app_private_key"] == raw
    assert credential_binding("p", "a", "scope", "github", 1) != credential_binding(
        "p", "b", "scope", "github", 1
    )
    assert credential_binding("p", "a", "scope", "github", 1) != credential_binding(
        "p", "a", "scope", "github", 2
    )


@pytest.mark.parametrize(
    "model,body",
    [
        (
            ConsoleDisableSources,
            {"expected_snapshot_sha256": "not-a-hash", "review_ref": "fixture"},
        ),
        (ConsoleTaskStop, {"expected_epoch": True, "review_ref": "fixture"}),
        (ConsoleSourceRevoke, {"expected_source_revision": 0, "review_ref": "fixture"}),
    ],
)
def test_destructive_commands_are_bounded_exact_versions(model, body):
    with pytest.raises(ValidationError):
        model.model_validate(body)


def test_expiry_is_future_bounded_and_credential_save_has_no_read_or_activation_field():
    now = datetime.now(UTC)
    assert expiry_valid(now + timedelta(days=365), now)
    assert not expiry_valid(now, now) and not expiry_valid(now + timedelta(days=366), now)
    assert set(ConsoleCredentialSave.model_fields) == {
        "expected_source_revision",
        "expected_credential_revision",
        "credentials",
        "valid_until",
        "review_ref",
    }


@pytest.fixture
def draft(engine):
    with engine.transaction() as conn:
        if (
            conn.execute("SELECT to_regclass('app.sa_console_credentials') AS t").fetchone()["t"]
            is None
        ):
            conn.execute(
                (A / "docs/super-agent-v1/sa19-proposal/0052_source_console.sql").read_text()
            )
    owner = WebAccounts(engine).create_owner(
        LoginInput(username="sa19-owner", password="fixture-password-123")
    )
    identifier = uuid4()
    scope = SourceScope(
        source_kind="github", source_instance_id=identifier, tenant_id="2", installation_id="3"
    )
    engine.source_ingress.identities.registry.register_instance(
        SourceInstance(
            scope=scope,
            project_id=engine.settings.project_id,
            adapter_version="github-app-v1",
            capabilities=CAPABILITIES,
            channel_capabilities=ChannelCapabilities(
                send="unsupported",
                idempotent_send="unsupported",
                query_delivery="unsupported",
                delete_message="unsupported",
            ),
            credential_ref="vault://sa19/fixture",
        )
    )
    encrypted = BodyCipher(Fernet.generate_key()).encrypt(b"fixture-only-credentials", "fixture")
    with engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.sa_console_credentials(source_instance_id,project_id,"
            "source_kind,scope_sha256,revision,ciphertext,ciphertext_sha256,field_names,"
            "valid_until,updated_by,review_ref) VALUES(%s,%s,'github',%s,1,%s,%s,%s,%s,%s,'fixture')",
            (
                identifier,
                engine.settings.project_id,
                scope.key,
                encrypted,
                hashlib.sha256(encrypted).hexdigest(),
                ["app_private_key", "webhook_secret"],
                datetime.now(UTC) + timedelta(hours=1),
                owner,
            ),
        )
    return engine, identifier, owner


def test_proposed_sql_is_overlay_only_two_tables_without_migration_or_runtime(draft):
    engine, _, _ = draft
    assert not (A / "migrations/0052_source_console.sql").exists()
    assert not hasattr(engine, "source_console")
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.schema_migrations").fetchone()["n"] == 48
        tables = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='app' "
            "AND tablename LIKE 'sa_console_%' ORDER BY tablename"
        ).fetchall()
        assert [r["tablename"] for r in tables] == ["sa_console_audit", "sa_console_credentials"]
        assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 0


def test_proposed_credentials_do_not_store_plaintext_and_erase_current_cipher_on_revoke(draft):
    engine, identifier, _ = draft
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM app.sa_console_credentials WHERE source_instance_id=%s", (identifier,)
        ).fetchone()
        assert b"fixture-only-credentials" not in bytes(row["ciphertext"])
        conn.execute(
            "UPDATE app.sa_console_credentials SET revision=2,ciphertext=NULL,"
            "ciphertext_sha256=NULL,revoked_at=clock_timestamp(),updated_at=clock_timestamp() "
            "WHERE source_instance_id=%s",
            (identifier,),
        )
        row = conn.execute(
            "SELECT * FROM app.sa_console_credentials WHERE source_instance_id=%s", (identifier,)
        ).fetchone()
        assert row["revision"] == 2 and row["ciphertext"] is None and row["revoked_at"]


@pytest.mark.parametrize(
    "assignment",
    [
        "revision=revision",
        "project_id='another-project'",
        "scope_sha256=repeat('f',64)",
        "revision=revision+1,ciphertext='plain'::bytea",
    ],
)
def test_proposed_credential_guard_rejects_stale_scope_and_corrupt_cipher(draft, assignment):
    engine, identifier, _ = draft
    with engine.transaction() as conn:
        with pytest.raises(psycopg.Error), conn.transaction():
            # Assignments are fixed test literals, never request data.
            conn.execute(
                "UPDATE app.sa_console_credentials SET "
                + assignment
                + " WHERE source_instance_id=%s",
                (identifier,),
            )


def test_proposed_metadata_audit_is_append_only_and_rejects_content_fields(draft):
    engine, identifier, owner = draft
    with engine.transaction() as conn:
        audit_id = uuid4()
        conn.execute(
            "INSERT INTO app.sa_console_audit(event_id,project_id,actor_user_id,event,"
            "target_id,review_ref,details) VALUES(%s,%s,%s,'source_saved',%s,'fixture','{}')",
            (audit_id, engine.settings.project_id, owner, identifier),
        )
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(
                "UPDATE app.sa_console_audit SET review_ref='changed' WHERE event_id=%s",
                (audit_id,),
            )
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute("DELETE FROM app.sa_console_audit WHERE event_id=%s", (audit_id,))
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(
                "INSERT INTO app.sa_console_audit(event_id,project_id,actor_user_id,event,"
                "review_ref,details) VALUES(%s,%s,%s,'source_saved','fixture',%s)",
                (uuid4(), engine.settings.project_id, owner, json.dumps({"secret": "not-allowed"})),
            )
