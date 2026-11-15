"""First formal SA19 installation, custody, atomic revocation and control boundaries."""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sa19_support import bind_ingress, create_source, owner, policy, save_policy

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError, TriggerInput
from mgar_v3.sources.domain import SourceScope
from mgar_v3.super_agent.policy_domain import PolicyPreview, PolicySave
from mgar_v3.super_agent.source_console import SourceConsole
from mgar_v3.super_agent.source_console_domain import (
    ConsoleCredentialSave,
    ConsoleDisableSources,
    ConsoleSourceRevoke,
    ConsoleSourceSave,
    ConsoleTaskStop,
    credential_binding,
    credential_values,
)

A = Path(__file__).resolve().parents[1]


def test_confirmed0052_exact_shared_files_49_migrations_and_actual_engine_routes(engine):
    assert type(engine.source_console) is SourceConsole and engine.source_console.engine is engine
    ack = json.loads((A / "docs/super-agent-v1/sa19-proposal/ownership-confirmed.json").read_text())
    assert ack["commit"] == "8d19308a585e80885fc4f0bfeec4853395236ab8"
    for item in ack["files"]:
        assert (
            hashlib.sha256((A / item["path"].removeprefix("v3/")).read_bytes()).hexdigest()
            == item["after_sha256"]
        )
    with engine.transaction() as conn:
        rows = conn.execute("SELECT name,sha256 FROM app.schema_migrations").fetchall()
        assert len(rows) == 49 and all(
            hashlib.sha256((A / "migrations" / r["name"]).read_bytes()).hexdigest() == r["sha256"]
            for r in rows
        )
    app = create_app(engine.settings, engine, run_background=False)
    routes = {
        method + " " + r.path for r in app.routes if hasattr(r, "methods") for method in r.methods
    }
    assert set(ack["interfaces"]) <= routes
    assert "open-source-console" in (A / "web/agent.html").read_text()
    assert not engine.source_console.vault.key_path.exists()


@pytest.mark.parametrize("kind", ["github", "feishu"])
def test_actual_platform_scope_and_encrypted_credential_save_are_redacted_and_never_activate(
    engine, kind
):
    env = owner(engine)
    source = create_source(env, kind)
    value = env.service.source(env.cookie, source.source_id)
    assert value["source_revision"] == 2 and value["settings_revision"] == 1
    assert value["credential"]["configured"] and value["credential"]["write_only"]
    assert (
        value["health"]["connection"] == "unknown" and value["health"]["permissions"] == "unknown"
    )
    assert not env.service.capabilities()["automatic_repair"]
    with engine.transaction() as conn:
        row = env.service._credential(conn, source.source_id)
        assert row and row["revision"] == 1
        assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.router_calls").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 0
    plain = credential_values(source.request.credentials)
    for secret in plain.values():
        assert secret not in json.dumps(value, default=str) and secret.encode() not in bytes(
            row["ciphertext"]
        )
    binding = credential_binding(
        engine.settings.project_id,
        str(source.source_id),
        SourceScope.model_validate(source.value["scope"]).key,
        kind,
        1,
    )
    assert env.service.vault.decrypt(bytes(row["ciphertext"]), binding) == plain
    assert os.stat(env.service.vault.key_path).st_mode & 0o777 == 0o600
    assert os.stat(env.service.vault.key_path.parent).st_mode & 0o777 == 0o700


def test_scope_only_setup_never_creates_key_and_keeps_credentials_unconfigured(engine):
    env = owner(engine)
    source = create_source(env, credentials=False, enabled=False)
    assert not env.service.vault.key_path.exists()
    assert not source.value["credential"]["configured"]
    assert not source.value["settings"]["enabled"]


def test_ciphertext_is_bound_to_exact_installation_and_revision(engine):
    env = owner(engine)
    source = create_source(env)
    with engine.transaction() as conn:
        row = env.service._credential(conn, source.source_id)
    with pytest.raises(DomainError):
        env.service.vault.decrypt(
            bytes(row["ciphertext"]),
            credential_binding(
                engine.settings.project_id,
                str(uuid4()),
                env.service._scope(source.source_id, source.request).key,
                "github",
                1,
            ),
        )
    with pytest.raises(DomainError):
        env.service.vault.decrypt(
            bytes(row["ciphertext"]),
            credential_binding(
                engine.settings.project_id,
                str(source.source_id),
                env.service._scope(source.source_id, source.request).key,
                "github",
                2,
            ),
        )


def test_credentials_rotate_with_original_source_revision_and_stale_cas_cannot_overwrite(engine):
    env = owner(engine)
    source = create_source(env)
    request = ConsoleCredentialSave(
        expected_source_revision=2,
        expected_credential_revision=1,
        credentials=source.request.credentials,
        valid_until=datetime.now(UTC) + timedelta(hours=1),
        review_ref="fixture-rotate",
    )
    value = env.service.save_credentials(env.cookie, source.source_id, request)
    assert (
        value["source_revision"] == 3
        and value["settings_revision"] == 2
        and value["credential"]["revision"] == 2
    )
    with pytest.raises(DomainError, match="设置已变化"):
        env.service.save_credentials(env.cookie, source.source_id, request)
    assert env.service.source(env.cookie, source.source_id)["credential"]["revision"] == 2


def test_owner_revoked_during_key_io_prevents_any_source_or_secret_commit(engine, monkeypatch):
    env = owner(engine)
    identifier = uuid4()
    source_request = ConsoleSourceSave.model_validate(
        {
            "source_kind": "github",
            "expected_source_revision": 0,
            "configuration": {
                "expected_revision": 0,
                "settings": {
                    "app_id": 1,
                    "account_id": 2,
                    "installation_id": 3,
                    "repository": {
                        "repository_id": 4,
                        "full_name": engine.settings.repository,
                        "private": False,
                    },
                    "events": ["issue_comment"],
                    "sender_ids": [10],
                    "own_bot_ids": [20],
                },
                "review_ref": "fixture",
            },
            "credentials": {
                "app_private_key": "fixture-private",
                "webhook_secret": "fixture-webhook",
            },
            "credential_valid_until": datetime.now(UTC) + timedelta(hours=1),
        }
    )
    original = env.service.vault.encrypt

    def revoke(*args):
        result = original(*args)
        env.accounts.revoke(env.cookie)
        return result

    monkeypatch.setattr(env.service.vault, "encrypt", revoke)
    with pytest.raises(DomainError) as failure:
        env.service.save_source(env.cookie, identifier, source_request)
    assert failure.value.code == "AUTH_REQUIRED"
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.source_instances").fetchone()["n"] == 0
        assert (
            conn.execute("SELECT count(*) AS n FROM app.sa_console_credentials").fetchone()["n"]
            == 0
        )


def test_source_revision_changed_during_key_io_is_rechecked_before_mutation(engine, monkeypatch):
    env = owner(engine)
    source = create_source(env, credentials=False)
    request = ConsoleSourceSave.model_validate(
        {
            **source.request.model_dump(mode="json"),
            "expected_source_revision": 2,
            "configuration": {
                **source.request.configuration.model_dump(mode="json"),
                "expected_revision": 1,
            },
            "credentials": {
                "app_private_key": "fixture-private",
                "webhook_secret": "fixture-webhook",
            },
            "credential_valid_until": datetime.now(UTC) + timedelta(hours=1),
        }
    )
    original = env.service.vault.encrypt

    def change(*args):
        result = original(*args)
        registry = env.service.identities.registry
        instance = registry.get_instance(source.source_id)
        registry.register_instance(
            instance.model_copy(
                update={"revision": instance.revision + 1, "display_name": "concurrent"}
            )
        )
        return result

    monkeypatch.setattr(env.service.vault, "encrypt", change)
    with pytest.raises(DomainError) as failure:
        env.service.save_source(env.cookie, source.source_id, request)
    assert failure.value.code == "CONSOLE_REVISION_CONFLICT"
    with engine.transaction() as conn:
        assert env.service._credential(conn, source.source_id) is None
        assert env.service.identities.registry._instance(conn, source.source_id).revision == 3


@pytest.mark.parametrize("kind", ["github", "feishu"])
def test_source_revoke_composes_actual_policy_settings_ingress_and_cipher_erasure(engine, kind):
    env = owner(engine)
    source = create_source(env, kind)
    p = save_policy(source)
    bind_ingress(p)
    result = env.service.revoke_source(
        env.cookie,
        source.source_id,
        ConsoleSourceRevoke(expected_source_revision=2, review_ref="fixture-revoke"),
    )
    assert result["source_revision"] == 3 and not result["settings"]["enabled"]
    assert result["ingress"]["settings"]["owner"] == "disabled"
    assert result["credential"]["revoked"] and not result["credential"]["configured"]
    assert engine.source_policies.read(env.cookie, p.policy_id)["revoked"]
    with engine.transaction() as conn:
        assert env.service._credential(conn, source.source_id)["ciphertext"] is None


def test_global_disable_is_atomic_over_both_original_sources_and_has_no_parallel_flag(engine):
    env = owner(engine)
    first = create_source(env)
    second = create_source(env, "feishu")
    for source in [first, second]:
        bind_ingress(save_policy(source))
    before = env.service.snapshot(env.cookie)
    result = env.service.disable_all(
        env.cookie,
        ConsoleDisableSources(
            expected_snapshot_sha256=before["snapshot_sha256"], review_ref="fixture-global"
        ),
    )
    assert result["disabled_source_count"] == 2 and result["new_work_blocked"]
    view = env.service.snapshot(env.cookie)
    assert all(
        not s["settings"]["enabled"]
        and not s["credential"]["configured"]
        and s["ingress"]["settings"]["owner"] == "disabled"
        for s in view["sources"]
    )
    assert all(p["revoked"] for p in view["policies"])
    with engine.transaction() as conn:
        tables = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='app' AND tablename LIKE 'sa_console_%'"
        ).fetchall()
        assert {r["tablename"] for r in tables} == {"sa_console_credentials", "sa_console_audit"}


def test_stale_global_snapshot_is_rejected_without_disabling_new_source(engine):
    env = owner(engine)
    view = env.service.snapshot(env.cookie)
    source = create_source(env)
    with pytest.raises(DomainError) as failure:
        env.service.disable_all(
            env.cookie,
            ConsoleDisableSources(
                expected_snapshot_sha256=view["snapshot_sha256"], review_ref="fixture-stale"
            ),
        )
    assert failure.value.code == "CONSOLE_SNAPSHOT_STALE"
    assert env.service.source(env.cookie, source.source_id)["settings"]["enabled"]


def test_global_failure_rolls_back_first_source_and_credential_erasure(engine, monkeypatch):
    env = owner(engine)
    create_source(env)
    create_source(env, "feishu")
    before = env.service.snapshot(env.cookie)
    original = env.service._revoke_source
    calls = []

    def fail(*args):
        result = original(*args)
        calls.append(True)
        if len(calls) == 2:
            raise DomainError("FIXTURE_ROLLBACK", "controlled abort")
        return result

    monkeypatch.setattr(env.service, "_revoke_source", fail)
    with pytest.raises(DomainError):
        env.service.disable_all(
            env.cookie,
            ConsoleDisableSources(
                expected_snapshot_sha256=before["snapshot_sha256"], review_ref="fixture-rollback"
            ),
        )
    after = env.service.snapshot(env.cookie)
    assert before["snapshot_sha256"] == after["snapshot_sha256"]
    assert all(s["credential"]["configured"] and s["settings"]["enabled"] for s in after["sources"])


@pytest.mark.parametrize("mode", ["auto_repair", "shadow_with_write_grant"])
def test_current_worker_gate_blocks_backend_repair_authority_not_only_ui(engine, mode):
    env = owner(engine)
    source = create_source(env)
    changes = (
        {}
        if mode == "auto_repair"
        else {
            "execution": {
                "enabled": True,
                "repositories": [
                    {
                        "repository": engine.settings.repository,
                        "base_sha": "a" * 40,
                        "paths": ["src"],
                    }
                ],
                "capabilities": ["edit_files"],
            }
        }
    )
    definition, _, _ = policy(
        source, mode="auto_repair" if mode == "auto_repair" else "shadow", **changes
    )
    with pytest.raises(DomainError) as failure:
        env.service.save_policy(
            env.cookie,
            uuid4(),
            PolicySave(expected_revision=0, definition=definition, review_ref="fixture-denied"),
        )
    assert failure.value.code == "CONSOLE_WORKER_GATE"
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.source_policies").fetchone()["n"] == 0


@pytest.mark.parametrize("change", ["conversation", "actor", "event"])
def test_rule_scope_cannot_exceed_selected_app_group_user_or_events(engine, change):
    env = owner(engine)
    source = create_source(env, "feishu")
    definition, _, _ = policy(source)
    raw = definition.model_dump(mode="json")
    if change == "conversation":
        raw["selector"]["conversations"] = ["another-private-group"]
    elif change == "actor":
        raw["selector"]["actors"][0]["actor_id"] = "another-human"
    else:
        raw["selector"]["event_kinds"] = ["unselected.event"]
    with pytest.raises(DomainError) as failure:
        env.service.save_policy(
            env.cookie,
            uuid4(),
            PolicySave(
                expected_revision=0,
                definition=type(definition).model_validate(raw),
                review_ref="fixture-scope",
            ),
        )
    assert failure.value.code == "CONSOLE_POLICY_SCOPE"


def test_preview_and_observation_are_read_only_and_semantics_explicitly_not_run(engine):
    env = owner(engine)
    source = create_source(env)
    definition, actor, thread = policy(source)
    request = PolicyPreview(
        expected_revision=0,
        definition=definition,
        actor=actor,
        source_thread=thread,
        facts={"event_kind": "issue_comment.created"},
    )
    tables = [
        "source_policies",
        "runs",
        "actions",
        "router_calls",
        "usage_calls",
        "sa_console_audit",
    ]

    def counts():
        with engine.transaction() as conn:
            return {
                t: conn.execute("SELECT count(*) AS n FROM app." + t).fetchone()["n"]
                for t in tables
            }

    before = counts()
    value = env.service.preview_policy(env.cookie, uuid4(), request)
    assert not value["creates_task"] and value["semantic_evaluation"] == "NOT_RUN"
    for _ in range(2):
        env.service.snapshot(env.cookie)
        env.service.source(env.cookie, source.source_id)
    assert counts() == before


def test_owner_can_stop_original_run_and_epoch_cas_rejects_stale_command(engine, trigger):
    env = owner(engine)
    receipt = engine.ingest(trigger)
    identifier = receipt.run_id
    run = engine.get_run(identifier)
    result = env.service.stop_task(
        env.cookie,
        identifier,
        ConsoleTaskStop(expected_epoch=run["execution_epoch"], review_ref="fixture-stop"),
    )
    assert (
        result["run"]["status"] == "cancelled"
        and result["run"]["execution_epoch"] > run["execution_epoch"]
    )
    assert result["stop_committed"] and result["quiescence_confirmed"]
    with pytest.raises(DomainError):
        env.service.stop_task(
            env.cookie,
            identifier,
            ConsoleTaskStop(expected_epoch=run["execution_epoch"], review_ref="fixture-stale-stop"),
        )
    assert env.service.task(env.cookie, identifier)["run"]["status"] == "cancelled"


def test_other_project_run_is_not_readable_or_stoppable_from_console(engine, trigger):
    env = owner(engine)
    receipt = engine.ingest(
        TriggerInput.model_validate(
            {
                **trigger.model_dump(mode="json"),
                "project_id": "another-project",
                "external_event_id": "other-project",
            }
        )
    )
    for action in [
        lambda: env.service.task(env.cookie, receipt.run_id),
        lambda: env.service.stop_task(
            env.cookie,
            receipt.run_id,
            ConsoleTaskStop(expected_epoch=0, review_ref="fixture-other"),
        ),
    ]:
        with pytest.raises(DomainError) as failure:
            action()
        assert failure.value.code == "CONSOLE_TASK_NOT_FOUND"
    assert not env.service.snapshot(env.cookie)["tasks"]["items"]


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "hardlink"])
def test_unsafe_key_files_fail_closed_without_replacing_credentials(engine, unsafe):
    env = owner(engine)
    source = create_source(env)
    key = env.service.vault.key_path
    extra = key.with_name("fixture-original-key")
    if unsafe == "mode":
        key.chmod(0o644)
    else:
        key.rename(extra)
        if unsafe == "symlink":
            key.symlink_to(extra)
        else:
            os.link(extra, key)
    try:
        with pytest.raises(DomainError):
            env.service.save_credentials(
                env.cookie,
                source.source_id,
                ConsoleCredentialSave(
                    expected_source_revision=2,
                    expected_credential_revision=1,
                    credentials=source.request.credentials,
                    valid_until=datetime.now(UTC) + timedelta(hours=1),
                    review_ref="fixture-unsafe",
                ),
            )
        with engine.transaction() as conn:
            assert env.service._credential(conn, source.source_id)["revision"] == 1
    finally:
        if unsafe == "mode":
            key.chmod(0o600)
        else:
            key.unlink()
            extra.rename(key)


def test_key_io_refuses_active_business_transaction(engine):
    env = owner(engine)
    with engine.transaction():
        with pytest.raises(DomainError) as failure:
            env.service.vault.encrypt({"app_secret": "fixture"}, "fixture")
        assert failure.value.code == "CONSOLE_IO_IN_TRANSACTION"
    assert not env.service.vault.key_path.exists()
