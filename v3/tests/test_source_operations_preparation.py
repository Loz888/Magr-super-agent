"""First SA20 inert contracts, pure observation and real PG proposal overlay."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import ValidationError
from sa19_support import owner

from mgar_v3.domain import digest
from mgar_v3.super_agent.source_operations_domain import (
    BackupManifest,
    BackupPart,
    ComponentCredential,
    ComponentGrant,
    ComponentHeartbeat,
    ComponentObservation,
    ComponentOpen,
    ComponentTick,
    IncidentDecision,
    OperationsDefinition,
    OperationsSave,
    OperationsSnapshot,
    RunObservation,
    ServiceClient,
    ServiceSecrets,
    SignalSubmission,
    https_origin,
    local_api,
    signals,
    valid_definition,
)

A = Path(__file__).resolve().parents[1]
P = A / "docs/super-agent-v1/sa20-proposal"


def definition(**changes):
    return OperationsDefinition(valid_until=datetime.now(UTC) + timedelta(hours=1), **changes)


@pytest.mark.parametrize(
    "field",
    [
        "allow_paid",
        "auto_repair",
        "execute",
        "run_id",
        "reset_budget",
        "retry_unknown",
        "model",
        "shell",
    ],
)
def test_operational_control_cannot_become_business_or_spending_authority(field):
    raw = definition().model_dump(mode="json")
    raw[field] = True
    with pytest.raises(ValidationError):
        OperationsDefinition.model_validate(raw)


def test_default_operations_and_restore_are_inert_and_expiry_is_explicit():
    d = definition()
    assert not d.enabled and not d.components
    assert valid_definition(d, datetime.now(UTC))
    assert not valid_definition(d, d.valid_until)
    assert not valid_definition(
        d.model_copy(update={"valid_until": datetime.now(UTC) + timedelta(days=31)}),
        datetime.now(UTC),
    )
    assert not signals(
        OperationsSnapshot(project_id="fixture", observed_at=datetime.now(UTC), config_revision=0),
        d,
    )


@pytest.mark.parametrize(
    "bad",
    [
        "http://example.test",
        "https://*.example.test",
        "https://example.test/",
        "https://example.test/path",
        "https://u:p@example.test",
        "https://example.test?q=x",
        "https://example.test#x",
        "https://EXAMPLE.test",
        "https://example.test.",
        "https://example.test:70000",
        "https://bad_host.test",
        "https://good.test\\@evil.test",
    ],
)
def test_public_origin_requires_exact_canonical_https_without_broadening(bad):
    with pytest.raises(ValueError):
        https_origin(bad)


@pytest.mark.parametrize(
    "good", ["https://magr.example.test", "https://127.0.0.1:8443", "https://[::1]:8443"]
)
def test_exact_https_origin_preserves_the_explicit_authority(good):
    assert https_origin(good) == good


@pytest.mark.parametrize(
    "bad",
    [
        "https://127.0.0.1",
        "http://evil.test",
        "http://127.0.0.1.evil.test",
        "http://user@127.0.0.1",
        "http://127.0.0.1/path",
        "http://127.0.0.1?token=bad",
        "http://127.0.0.1:70000",
        "http://127.0.0.1#x",
    ],
)
def test_service_credentials_only_connect_to_fixed_local_engine(bad):
    with pytest.raises(ValueError):
        local_api(bad)


@pytest.mark.parametrize(
    "field", ["database_url", "local_token", "allow_paid", "commands", "platform_secret"]
)
def test_role_client_does_not_accept_engine_or_platform_configuration(field):
    raw = {
        "api_url": "http://127.0.0.1:18081",
        "component_id": str(uuid4()),
        "role": "watchdog",
        "token": "x" * 32,
        field: "forbidden",
    }
    with pytest.raises(ValidationError):
        ServiceClient.model_validate(raw)


@pytest.mark.parametrize("token", ["short", "x" * 300, "x" * 31 + "\n", "x" * 31 + " "])
def test_service_tokens_are_bounded_and_not_header_injection(token):
    with pytest.raises(ValidationError):
        ComponentCredential(component_id=uuid4(), token=token)


def test_component_credentials_are_separate_and_redacted_in_model_serialization():
    first = ComponentCredential(component_id=uuid4(), token="private_a_" + "a" * 32)
    raw = ServiceSecrets(components=(first,))
    assert first.token.get_secret_value() not in raw.model_dump_json()
    assert first.token.get_secret_value() not in repr(raw)
    with pytest.raises(ValidationError):
        ServiceSecrets(components=(first, first.model_copy(update={"component_id": uuid4()})))


@pytest.mark.parametrize("role", ["scheduler", "worker", "dispatcher", "watchdog"])
def test_only_source_role_can_be_bound_to_selected_sources(role):
    with pytest.raises(ValidationError):
        ComponentGrant(component_id=uuid4(), role=role, source_ids=(uuid4(),))


def test_enabled_source_scope_is_exact_nonempty_and_distinct():
    with pytest.raises(ValidationError):
        ComponentGrant(component_id=uuid4(), role="source", enabled=True)
    source = uuid4()
    with pytest.raises(ValidationError):
        ComponentGrant(component_id=uuid4(), role="source", source_ids=(source, source))
    grant = ComponentGrant(component_id=uuid4(), role="source", source_ids=(source,), enabled=True)
    assert grant.source_ids == (source,)
    with pytest.raises(ValidationError):
        definition(components=(grant, grant))


@pytest.mark.parametrize(
    "model,raw",
    [
        (
            ComponentOpen,
            {
                "config_revision": 1,
                "expected_generation": -1,
                "boot_id": str(uuid4()),
                "request_id": str(uuid4()),
            },
        ),
        (
            ComponentHeartbeat,
            {"generation": 0, "boot_id": str(uuid4()), "sequence": 1, "state": "ready"},
        ),
        (
            ComponentHeartbeat,
            {"generation": 1, "boot_id": str(uuid4()), "sequence": True, "state": "ready"},
        ),
        (
            ComponentTick,
            {"generation": 1, "boot_id": str(uuid4()), "sequence": 0, "request_id": str(uuid4())},
        ),
        (IncidentDecision, {"expected_revision": 1, "decision": "retry", "review_ref": "fixture"}),
        (IncidentDecision, {"expected_revision": 1, "decision": "cancel", "review_ref": "fixture"}),
    ],
)
def test_lease_and_incident_contracts_do_not_allow_zero_fences_or_auto_control(model, raw):
    with pytest.raises(ValidationError):
        model.model_validate(raw)


def test_heartbeat_is_not_semantic_progress_and_repeated_poll_is_deduplicated():
    at = datetime.now(UTC)
    grant = ComponentGrant(component_id=uuid4(), role="worker", enabled=True)
    d = definition(enabled=True, components=(grant,))
    component = ComponentObservation(
        component_id=grant.component_id,
        role="worker",
        generation=1,
        boot_id=uuid4(),
        state="ready",
        expires_at=at + timedelta(seconds=30),
        sequence=1,
    )
    run = RunObservation(
        run_id=uuid4(),
        epoch=2,
        state="running",
        progress_cursor=7,
        progress_at=at - timedelta(seconds=180),
    )
    snap = OperationsSnapshot(
        project_id="fixture",
        observed_at=at,
        config_revision=1,
        components=(component,),
        runs=(run,),
        runner_available=True,
    )
    before = signals(snap, d)
    after = signals(
        snap.model_copy(
            update={
                "observed_at": at + timedelta(seconds=5),
                "components": (
                    component.model_copy(
                        update={"sequence": 99, "expires_at": at + timedelta(seconds=40)}
                    ),
                ),
            }
        ),
        d,
    )
    assert [x.kind for x in before] == ["semantic_stalled"]
    assert [x.key("fixture") for x in before] == [x.key("fixture") for x in after]
    moved = signals(
        snap.model_copy(update={"runs": (run.model_copy(update={"progress_cursor": 8}),)}), d
    )
    assert before[0].key("fixture") != moved[0].key("fixture")


def test_waiting_runner_and_legitimate_background_job_are_not_stalled_model_loops():
    at = datetime.now(UTC)
    run = RunObservation(
        run_id=uuid4(),
        epoch=1,
        state="queued",
        progress_cursor=1,
        progress_at=at - timedelta(hours=1),
    )
    snap = OperationsSnapshot(project_id="fixture", observed_at=at, config_revision=1, runs=(run,))
    d = definition(enabled=True)
    assert [s.kind for s in signals(snap, d)] == ["waiting_runner"]
    working = snap.model_copy(
        update={
            "runner_available": True,
            "runs": (run.model_copy(update={"state": "running", "pending_jobs": 1}),),
        }
    )
    assert not signals(working, d)
    overdue = working.model_copy(
        update={"runs": (working.runs[0].model_copy(update={"overdue_jobs": 1}),)}
    )
    assert [s.kind for s in signals(overdue, d)] == ["job_overdue"]


def test_unknown_results_remain_observable_on_cancelled_run_and_new_cause_has_new_key():
    at = datetime.now(UTC)
    run = RunObservation(
        run_id=uuid4(),
        epoch=1,
        state="cancelled",
        progress_cursor=1,
        progress_at=at,
        unknown_calls=1,
        unknown_calls_cursor="a" * 64,
    )
    snap = OperationsSnapshot(project_id="fixture", observed_at=at, config_revision=1, runs=(run,))
    d = definition(enabled=True)
    first = signals(snap, d)
    assert [s.kind for s in first] == ["usage_unknown"] and first[0].severity == "critical"
    changed = snap.model_copy(
        update={"runs": (run.model_copy(update={"unknown_calls_cursor": "b" * 64}),)}
    )
    assert first[0].key("fixture") != signals(changed, d)[0].key("fixture")


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../key",
        "cas/../key",
        "cas//key",
        "./key",
        "C:\\secret",
        "cas/key\x00",
        "cas/.hidden",
    ],
)
def test_backup_inventory_rejects_ambiguous_or_traversing_paths(path):
    with pytest.raises(ValidationError):
        BackupPart(path=path, kind="cas", sha256="a" * 64, size=0)


def test_backup_binds_database_cas_and_required_keys_without_reactivation():
    parts = tuple(
        BackupPart(path=name, kind=kind, sha256="a" * 64, size=1)
        for name, kind in [
            ("database.dump", "database"),
            ("cas/file", "cas"),
            ("keys/source.key", "source_key"),
            ("keys/console.key", "console_key"),
        ]
    )
    kwargs = {
        "backup_id": uuid4(),
        "project_id": "fixture",
        "created_at": datetime.now(UTC),
        "migration_sha256": "b" * 64,
    }
    value = BackupManifest(parts=parts, **kwargs)
    assert value.unknown_writes_preserved and not value.activation_on_restore
    with pytest.raises(ValidationError):
        BackupManifest(parts=parts[:-1], **kwargs)
    with pytest.raises(ValidationError):
        BackupManifest(parts=parts, **kwargs, activation_on_restore=True)
    with pytest.raises(ValidationError):
        BackupManifest(parts=parts + (parts[1],), **kwargs)


@pytest.fixture
def draft(engine):
    sql = (P / "0053_source_operations.sql").read_text()
    env = owner(engine)
    with engine.transaction() as conn:
        conn.execute(sql)
    return env


def install_control(e, revision=1, enabled=True):
    cid = getattr(e, "component_id", uuid4())
    e.component_id = cid
    d = definition(
        enabled=enabled, components=(ComponentGrant(component_id=cid, role="worker", enabled=True),)
    )
    with e.engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.sa_operations_versions(project_id,revision,definition,definition_sha256,signed_by,review_ref,valid_until) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                e.engine.settings.project_id,
                revision,
                Jsonb(d.model_dump(mode="json")),
                digest(d.model_dump(mode="json")),
                e.user_id,
                "fixture-signed-ops",
                d.valid_until,
            ),
        )
        conn.execute(
            "INSERT INTO app.sa_operations_controls(project_id,revision) VALUES(%s,%s) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision",
            (e.engine.settings.project_id, revision),
        )
    return d


def open_component(e):
    with e.engine.transaction() as conn:
        return conn.execute(
            "INSERT INTO app.sa_operations_components(project_id,component_id,role,config_revision,generation,boot_id,open_request_id,state,expires_at) VALUES(%s,%s,'worker',1,1,%s,%s,'starting',clock_timestamp()+interval '20 seconds') RETURNING *",
            (e.engine.settings.project_id, e.component_id, uuid4(), uuid4()),
        ).fetchone()


def test_exact0053_is_overlay_only_four_tables_and_original49_migration_ledger(draft):
    e = draft
    with e.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.schema_migrations").fetchone()["n"] == 49
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM pg_tables WHERE schemaname='app' AND tablename LIKE 'sa_operations_%'"
            ).fetchone()["n"]
            == 4
        )
        for table in ["runs", "actions", "usage_calls", "router_calls"]:
            assert conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"] == 0
    assert not hasattr(e.engine, "source_operations")
    assert not (A / "migrations/0053_source_operations.sql").exists()
    manifest = json.loads((P / "proposal-manifest.json").read_text())
    assert (
        manifest["SQL_sha256"]
        == hashlib.sha256((P / "0053_source_operations.sql").read_bytes()).hexdigest()
    )
    for item in manifest["files"]:
        assert (
            hashlib.sha256((A / item["path"].removeprefix("v3/")).read_bytes()).hexdigest()
            == item["before_sha256"]
        )


def test_ops_versions_are_immutable_and_no_body_or_financial_permissions_fit(draft):
    install_control(draft)
    with pytest.raises(psycopg.Error), draft.engine.transaction() as conn:
        conn.execute("UPDATE app.sa_operations_versions SET review_ref='changed'")
    with pytest.raises(psycopg.Error), draft.engine.transaction() as conn:
        conn.execute("DELETE FROM app.sa_operations_versions")
    with pytest.raises(psycopg.Error), draft.engine.transaction() as conn:
        conn.execute(
            'UPDATE app.sa_operations_versions SET definition=definition||\'{"body":"private"}\'::jsonb'
        )


def test_component_generation_and_pending_unknown_survive_reopen_attempt(draft):
    e = draft
    install_control(e)
    row = open_component(e)
    with pytest.raises(psycopg.Error), e.engine.transaction() as conn:
        conn.execute("UPDATE app.sa_operations_components SET generation=0")
    request = uuid4()
    with e.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.sa_operations_components SET tick_request_id=%s,tick_sequence=1,tick_state='reserved'",
            (request,),
        )
        conn.execute("UPDATE app.sa_operations_components SET tick_state='unknown'")
    with pytest.raises(psycopg.Error), e.engine.transaction() as conn:
        conn.execute("UPDATE app.sa_operations_components SET generation=2,boot_id=%s", (uuid4(),))
    with e.engine.transaction() as conn:
        current = conn.execute("SELECT * FROM app.sa_operations_components").fetchone()
        assert current["generation"] == row["generation"] and current["tick_state"] == "unknown"
        assert current["tick_request_id"] == request


def test_committed_ops_revoke_blocks_new_heartbeat_but_keeps_old_result_receipt(draft):
    e = draft
    install_control(e)
    open_component(e)
    with e.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.sa_operations_components SET tick_request_id=%s,tick_sequence=1,tick_state='reserved'",
            (uuid4(),),
        )
    install_control(e, revision=2, enabled=False)
    with pytest.raises(psycopg.Error), e.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.sa_operations_components SET sequence=sequence+1,heartbeat_at=clock_timestamp(),expires_at=clock_timestamp()+interval '20 seconds'"
        )
    with e.engine.transaction() as conn:
        conn.execute("UPDATE app.sa_operations_components SET tick_state='unknown'")
        assert (
            conn.execute("SELECT tick_state FROM app.sa_operations_components").fetchone()[
                "tick_state"
            ]
            == "unknown"
        )


def test_operational_save_and_signal_submission_do_not_accept_untrusted_execution_fields():
    with pytest.raises(ValidationError):
        OperationsSave(expected_revision=True, definition=definition(), review_ref="fixture")
    with pytest.raises(ValidationError):
        SignalSubmission(
            kind="usage_unknown",
            observation_key="a" * 64,
            snapshot_sha256="b" * 64,
            reset_budget=True,
        )
