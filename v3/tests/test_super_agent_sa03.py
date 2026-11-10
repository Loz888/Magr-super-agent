from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from shutil import copyfile
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from pydantic import SecretStr, ValidationError
from test_super_agent_sa02 import (
    ControlledPlatform,
    bind,
    proof,
    source,
)

from mgar_v3.api import create_app
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.graph.runner import Runner
from mgar_v3.settings import ROOT
from mgar_v3.sources.domain import ActorRef, SourceThreadRef
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.identity_domain import ControllerGrant, RoleChange
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.store.migrate import migrate
from mgar_v3.super_agent.policy_domain import (
    NotificationRequest,
    PolicyDefinition,
    PolicyFacts,
    PolicyPreview,
    PolicyRevoke,
    PolicySave,
    QuietHours,
)
from mgar_v3.web_access import SESSION_COOKIE, LoginInput, WebAccounts

BASE = "a" * 40
ORIGIN = "http://127.0.0.1:18080"
PREFIX = "/api/v3/source-policies"


def definition(installation, **changes):
    actor = ActorRef(
        scope=installation.scope, actor_id="Alice-AbC", actor_kind="human", id_type="open_id"
    )
    now = datetime.now(UTC)
    value = {
        "selector": {
            "scope": installation.scope,
            "source_revision": installation.revision,
            "conversations": ["Chat-A"],
            "actors": [actor],
            "event_kinds": ["message.created"],
        },
        "mode": "auto_investigate",
        "valid_from": now - timedelta(minutes=5),
        "valid_until": now + timedelta(hours=1),
        "receive": {"enabled": True},
        "admission": {"enabled": True, "request_kinds": ["explicit_request"]},
        "execution": {
            "enabled": True,
            "repositories": [
                {
                    "repository": "example/mgar-test",
                    "base_sha": BASE,
                    "paths": ["fixtures/orders", "src"],
                }
            ],
            "capabilities": ["read_repository"],
        },
        "notification": {},
        "budget": {},
        **changes,
    }
    return PolicyDefinition.model_validate(value)


def facts(**changes):
    return PolicyFacts(
        event_kind="message.created",
        repository="example/mgar-test",
        base_sha=BASE,
        path="fixtures/orders/service.py",
        capability="read_repository",
        **changes,
    )


def notify(installation, **changes):
    return {
        "enabled": True,
        "destinations": [
            {
                "thread": SourceThreadRef(scope=installation.scope, conversation_id="Chat-A"),
                "target": "github:example/mgar-test#issue:1",
                "visibility": "private",
            }
        ],
        "content_classes": ["verified_result", "necessary_question"],
        "data_classes": ["private_summary", "public_summary"],
        "max_bytes": 65536,
        "max_per_hour": 3,
        **changes,
    }


@pytest.fixture
def cookie(engine):
    accounts = WebAccounts(engine)
    owner = accounts.create_owner(
        LoginInput(username="test-owner", password=SecretStr("fixture-owner-password"))
    )
    return accounts.issue_session(owner)


@pytest.fixture
def env(engine, cookie):
    installation = source(engine, kind="github")
    identities = SourceIdentities(engine, {("github", "controlled-v1"): ControlledPlatform(engine)})
    binding, credential = bind(
        identities,
        cookie,
        installation,
        roles=("context_provider", "requester", "task_controller", "project_administrator"),
    )
    return SimpleNamespace(
        engine=engine,
        cookie=cookie,
        installation=installation,
        identities=identities,
        binding=binding,
        credential=credential,
        policies=engine.source_policies,
        policy_id=uuid4(),
        current=None,
    )


def sign(env, value=None, expected=0):
    row = env.policies.save(
        env.cookie,
        env.policy_id,
        PolicySave(
            expected_revision=expected,
            definition=value or definition(env.installation),
            review_ref="reviewed-source-policy",
        ),
    )
    env.current = row
    return row


def bound_run(env, trigger, value=None):
    if env.current is None:
        sign(env, value)
    receipt = env.engine.ingest(trigger)
    env.policies.bind_run(
        env.credential, env.policy_id, env.current["current_revision"], receipt.run_id, facts()
    )
    return receipt


def proposed(env, trigger, value=None):
    receipt = bound_run(env, trigger, value)
    action_id = Runner(env.engine).execute(receipt.run_id)
    assert action_id is not None
    return receipt, env.engine.actions(receipt.run_id)[0]


def request_for(action, installation, **changes):
    return NotificationRequest.model_validate(
        {
            "action_id": action["action_id"],
            "destination": notify(installation)["destinations"][0],
            "content_class": "verified_result",
            "data_class": "private_summary",
            "body_sha256": hashlib.sha256(action["payload"]["body"].encode()).hexdigest(),
            **changes,
        }
    )


def revoke(env):
    return env.policies.revoke(
        env.cookie,
        env.policy_id,
        PolicyRevoke(
            expected_revision=env.current["current_revision"], review_ref="stop-source-authority"
        ),
    )


@pytest.mark.parametrize(
    "mode,admitted,executed",
    [
        ("disabled", False, False),
        ("shadow", False, False),
        ("auto_investigate", True, True),
        ("auto_repair", True, True),
    ],
)
def test_four_modes_keep_receive_admission_execution_and_notification_independent(
    env, mode, admitted, executed
):
    sign(env, definition(env.installation, mode=mode))
    decisions = {
        layer: env.policies.evaluate(env.credential, env.policy_id, 1, layer, facts())
        for layer in ["receive", "admission", "execution", "notification"]
    }
    assert decisions["receive"].allowed
    assert decisions["admission"].allowed == admitted
    assert decisions["execution"].allowed == executed
    assert not decisions["notification"].allowed
    assert all(d.semantic_evaluation == "NOT_RUN" for d in decisions.values())
    if mode == "shadow":
        assert decisions["admission"].would_allow and decisions["execution"].would_allow
    with env.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.outbox").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0


@pytest.mark.parametrize("layer", ["admission", "execution", "notification"])
def test_receive_never_implies_work_reply_or_content_disclosure(env, layer):
    sign(env, definition(env.installation, admission={}, execution={}))
    receive = env.policies.evaluate(env.credential, env.policy_id, 1, "receive", facts())
    assert receive.allowed and not receive.retain_body and not receive.model_input
    assert not env.policies.evaluate(env.credential, env.policy_id, 1, layer, facts()).allowed


@pytest.mark.parametrize(
    "change",
    [
        {"actor_id": "alice-abc"},
        {"actor_id": " Alice-AbC"},
        {"id_type": "user_id"},
        {"tenant_id": "Tenant-B"},
        {"installation_id": "Install-B"},
        {"conversation_id": "Chat-B"},
        {"event_kind": "private.message"},
        {"native_thread_id": "Other"},
    ],
)
def test_same_text_different_verified_recipient_or_installation_is_not_authority(env, change):
    value = definition(env.installation)
    raw = value.model_dump(mode="json")
    raw["selector"]["native_threads"] = ["Topic-A"]
    value = PolicyDefinition.model_validate(raw)
    actor = value.selector.actors[0].model_dump(mode="json")
    thread = SourceThreadRef(
        scope=env.installation.scope, conversation_id="Chat-A", native_thread_id="Topic-A"
    ).model_dump(mode="json")
    metadata = facts().model_dump(mode="json")
    for key, val in change.items():
        if key in {"actor_id", "id_type"}:
            actor[key] = val
        elif key in {"tenant_id", "installation_id"}:
            actor["scope"][key] = val
            thread["scope"][key] = val
        elif key in {"conversation_id", "native_thread_id"}:
            thread[key] = val
        else:
            metadata[key] = val
    preview = env.policies.preview(
        env.cookie,
        env.policy_id,
        PolicyPreview(
            expected_revision=0, definition=value, actor=actor, source_thread=thread, facts=metadata
        ),
    )
    assert all(not d["allowed"] for d in preview["decisions"].values())
    assert not preview["grants_authority"]


def test_context_provider_cannot_turn_a_quote_into_an_explicit_request(env):
    only, credential = bind(env.identities, env.cookie, env.installation, actor="Contributor")
    raw = definition(env.installation).model_dump(mode="json")
    raw["selector"]["actors"] = [only["actor_ref"]]
    sign(env, PolicyDefinition.model_validate(raw))
    assert env.policies.evaluate(credential, env.policy_id, 1, "receive", facts()).allowed
    assert not env.policies.evaluate(credential, env.policy_id, 1, "admission", facts()).allowed
    assert not env.policies.evaluate(credential, env.policy_id, 1, "execution", facts()).allowed


def test_team_rule_is_owner_signed_and_bounded_not_inferred_from_message(env):
    binding, credential = bind(
        env.identities, env.cookie, env.installation, actor="CI-bot", kind="bot"
    )
    raw = definition(env.installation).model_dump(mode="json")
    raw["selector"]["actors"] = [binding["actor_ref"]]
    raw["admission"] = {
        "enabled": True,
        "request_kinds": ["team_rule"],
        "rule_ref": "ci-failure-v1",
        "rule_sha256": "b" * 64,
    }
    sign(env, PolicyDefinition.model_validate(raw))
    candidate = facts(request_kind="team_rule")
    assert env.policies.evaluate(credential, env.policy_id, 1, "admission", candidate).allowed
    assert not env.policies.evaluate(credential, env.policy_id, 1, "admission", facts()).allowed
    assert not env.policies.evaluate(
        credential,
        env.policy_id,
        1,
        "execution",
        candidate.model_copy(update={"capability": "edit_files"}),
    ).allowed


def test_followup_requires_exact_current_ticket_controller_not_a_quoted_id(env, trigger):
    receipt = env.engine.ingest(trigger)
    value = definition(
        env.installation, admission={"enabled": True, "request_kinds": ["follow_up"]}
    )
    sign(env, value)
    candidate = facts(request_kind="follow_up", ticket_id=receipt.ticket_id)
    assert not env.policies.evaluate(
        env.credential, env.policy_id, 1, "admission", candidate
    ).allowed
    env.identities.grant_controller(
        env.cookie,
        env.binding["binding_id"],
        ControllerGrant(
            expected_revision=env.binding["revision"],
            ticket_id=receipt.ticket_id,
            review_ref="exact-task-control",
        ),
    )
    current = proof(env.identities, env.installation)
    assert env.policies.evaluate(current, env.policy_id, 1, "admission", candidate).allowed
    assert not env.policies.evaluate(
        current, env.policy_id, 1, "admission", candidate.model_copy(update={"ticket_id": uuid4()})
    ).allowed


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"repository": "other/repository"}, "EXECUTION_SCOPE_DENIED"),
        ({"base_sha": "b" * 40}, "EXECUTION_SCOPE_DENIED"),
        ({"path": "fixtures/orders-other/file.py"}, "EXECUTION_SCOPE_DENIED"),
        ({"path": "secrets/file"}, "EXECUTION_SCOPE_DENIED"),
        ({"capability": "edit_files"}, "EXECUTION_CAPABILITY_DENIED"),
        (
            {"capability": "network_access", "network_host": "attacker.example"},
            "EXECUTION_CAPABILITY_DENIED",
        ),
    ],
)
def test_execution_boundary_rechecks_repository_base_path_and_capability(env, change, reason):
    sign(env)
    result = env.policies.evaluate(
        env.credential, env.policy_id, 1, "execution", facts().model_copy(update=change)
    )
    assert not result.allowed and result.reason_code == reason


def test_repair_mode_is_explicit_and_model_budget_is_not_created_by_policy(env):
    raw = definition(env.installation, mode="auto_repair").model_dump(mode="json")
    raw["execution"]["capabilities"] = ["edit_files", "run_tests", "network_access", "model_call"]
    raw["execution"]["network_hosts"] = ["allowed.example"]
    raw["budget"] = {
        "total_limit_micros": 100,
        "router_limit_micros": 50,
        "accounting_ref": "planned-shared-budget",
    }
    sign(env, PolicyDefinition.model_validate(raw))
    for capability in ["edit_files", "run_tests"]:
        assert env.policies.evaluate(
            env.credential,
            env.policy_id,
            1,
            "execution",
            facts().model_copy(update={"capability": capability}),
        ).allowed
    network = facts().model_copy(
        update={"capability": "network_access", "network_host": "attacker.example"}
    )
    assert not env.policies.evaluate(env.credential, env.policy_id, 1, "execution", network).allowed
    model = env.policies.evaluate(
        env.credential,
        env.policy_id,
        1,
        "execution",
        facts().model_copy(update={"capability": "model_call"}),
    )
    assert not model.allowed and model.reason_code == "SOURCE_SHARED_BUDGET_NOT_AVAILABLE"


@pytest.mark.parametrize(
    "change",
    [
        {"historical": True},
        {"event_kind": "unapproved.event"},
    ],
)
def test_historical_or_unapproved_event_cannot_start_new_work(env, change):
    sign(
        env, definition(env.installation, receive={"enabled": True, "allow_history_context": True})
    )
    candidate = facts().model_copy(update=change)
    assert not env.policies.evaluate(
        env.credential, env.policy_id, 1, "admission", candidate
    ).allowed


def test_signed_version_and_owner_are_immutable_and_preview_has_no_side_effects(env):
    request = PolicyPreview(
        expected_revision=0,
        definition=definition(env.installation, mode="shadow"),
        actor=definition(env.installation).selector.actors[0],
        source_thread=SourceThreadRef(scope=env.installation.scope, conversation_id="Chat-A"),
        facts=facts(),
    )
    with env.engine.transaction() as conn:
        before = {
            table: conn.execute(f"SELECT count(*) AS n FROM app.{table}").fetchone()["n"]
            for table in ["source_policy_audit", "source_policies", "runs", "outbox", "usage_calls"]
        }
    preview = env.policies.preview(env.cookie, env.policy_id, request)
    assert preview["preview_only"] and not preview["grants_authority"]
    assert (
        not preview["decisions"]["execution"]["allowed"]
        and preview["decisions"]["execution"]["would_allow"]
    )
    with env.engine.transaction() as conn:
        assert before == {
            table: conn.execute(f"SELECT count(*) AS n FROM app.{table}").fetchone()["n"]
            for table in before
        }
    saved = sign(env)
    with env.engine.transaction() as conn:
        owner = env.identities._console(conn, env.cookie)
        assert saved["signed_by"] == owner and saved["signed_at"].tzinfo is not None
    assert saved["definition_sha256"] == digest(saved["definition"])
    with pytest.raises(psycopg.errors.RaiseException), env.engine.transaction() as conn:
        conn.execute("UPDATE app.source_policy_versions SET definition='{}'")
    with pytest.raises(psycopg.errors.RaiseException), env.engine.transaction() as conn:
        conn.execute("DELETE FROM app.source_policy_audit")


def test_policy_save_cas_has_one_winner_and_revocation_is_sticky(env):
    sign(env)

    def save_one(mode):
        try:
            env.policies.save(
                env.cookie,
                env.policy_id,
                PolicySave(
                    expected_revision=1,
                    definition=definition(env.installation, mode=mode),
                    review_ref="concurrent-save",
                ),
            )
            return "ok"
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save_one, ["shadow", "disabled"]))
    assert sorted(results) == ["SOURCE_POLICY_REVISION_CONFLICT", "ok"]
    env.current = env.policies.read(env.cookie, env.policy_id)
    revoked = revoke(env)
    assert revoked["current_revision"] == 3 and revoked["revoked"]
    assert [row["revision"] for row in env.policies.history(env.cookie, env.policy_id)] == [1, 2, 3]
    with pytest.raises(DomainError) as error:
        env.policies.save(
            env.cookie,
            env.policy_id,
            PolicySave(
                expected_revision=3, definition=definition(env.installation), review_ref="resurrect"
            ),
        )
    assert error.value.code == "SOURCE_POLICY_REVOKED"


def test_save_and_revoke_rollback_with_same_engine_transaction(env, trigger):
    receipt = bound_run(env, trigger)
    running = env.engine.begin_run(receipt.run_id)
    with pytest.raises(RuntimeError), env.engine.transaction():
        revoke(env)
        assert env.engine.get_run(receipt.run_id)["status"] == "paused"
        raise RuntimeError("rollback signed revision and fences")
    assert env.policies.read(env.cookie, env.policy_id)["current_revision"] == 1
    assert env.engine.get_run(receipt.run_id)["execution_epoch"] == running["execution_epoch"]
    env.engine.recovery.heartbeat(receipt.run_id, running["execution_epoch"])
    assert len(env.policies.history(env.cookie, env.policy_id)) == 1


def test_policy_update_preview_counts_pending_and_invalidates_old_tools_and_worker_lease(
    env, trigger
):
    receipt = bound_run(env, trigger)
    running = env.engine.begin_run(receipt.run_id)
    job = env.engine.submit_read_job(receipt.run_id, running["execution_epoch"], "pending")
    old = env.current["definition"]
    new = PolicyDefinition.model_validate({**old, "mode": "disabled"})
    request = PolicyPreview(
        expected_revision=1,
        definition=new,
        actor=new.selector.actors[0],
        source_thread=SourceThreadRef(scope=env.installation.scope, conversation_id="Chat-A"),
        facts=facts(),
    )
    preview = env.policies.preview(env.cookie, env.policy_id, request)
    assert preview["impact"]["would_fence"] == 1
    assert env.engine.get_run(receipt.run_id)["status"] == "running"
    sign(env, new, expected=1)
    current = env.engine.get_run(receipt.run_id)
    assert current["status"] == "paused" and current["execution_epoch"] > running["execution_epoch"]
    with env.engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT status FROM app.read_jobs WHERE job_id=%s", (job["job_id"],)
            ).fetchone()["status"]
            == "cancelled"
        )
        assert conn.execute(
            "SELECT expires_at<=clock_timestamp() AS expired FROM app.worker_heartbeats "
            "WHERE run_id=%s",
            (receipt.run_id,),
        ).fetchone()["expired"]
    for operation in [
        lambda: env.engine.claim_read_job(job["job_id"], running["execution_epoch"]),
        lambda: env.engine.recovery.heartbeat(receipt.run_id, running["execution_epoch"]),
        lambda: env.engine.begin_run(receipt.run_id),
        lambda: env.engine.resume(receipt.run_id),
    ]:
        with pytest.raises(DomainError):
            operation()


def test_identity_role_revocation_also_fences_associated_policy_run_immediately(env, trigger):
    receipt = bound_run(env, trigger)
    running = env.engine.begin_run(receipt.run_id)
    env.identities.roles(
        env.cookie,
        env.binding["binding_id"],
        RoleChange(
            expected_revision=env.binding["revision"],
            roles=("context_provider",),
            review_ref="remove-control-and-request",
        ),
    )
    assert env.engine.get_run(receipt.run_id)["status"] == "paused"
    with pytest.raises(DomainError):
        env.engine.submit_read_job(receipt.run_id, running["execution_epoch"], "late")


def test_source_configuration_revision_fences_work_without_changing_identity_case(env, trigger):
    receipt = bound_run(env, trigger)
    env.engine.begin_run(receipt.run_id)
    SourceRegistry(env.engine).register_instance(
        env.installation.model_copy(update={"revision": 2, "display_name": "new name"})
    )
    assert env.engine.get_run(receipt.run_id)["status"] == "paused"
    with pytest.raises(DomainError):
        env.engine.begin_run(receipt.run_id)


def test_inflight_tool_result_is_retained_after_revocation_without_mailbox_or_revival(env, trigger):
    receipt = bound_run(env, trigger)
    running = env.engine.begin_run(receipt.run_id)
    job = env.engine.submit_read_job(receipt.run_id, running["execution_epoch"], "inflight")
    claimed = env.engine.claim_read_job(job["job_id"], running["execution_epoch"])
    with env.engine.transaction() as conn:
        before = conn.execute(
            "SELECT count(*) AS n FROM app.mailbox WHERE run_id=%s", (receipt.run_id,)
        ).fetchone()["n"]
    revoke(env)
    result = env.engine.finish_read_job(
        job["job_id"], claimed["lease_token"], b"true late tool result", "fixture://late", "v1"
    )
    assert result["status"] == "succeeded" and result["lease_token"] is None
    assert env.engine.artifacts.read(result["content_hash"]) == b"true late tool result"
    assert env.engine.get_run(receipt.run_id)["status"] == "paused"
    with env.engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.mailbox WHERE run_id=%s", (receipt.run_id,)
            ).fetchone()["n"]
            == before
        )
    with pytest.raises(DomainError):
        env.engine.finish_read_job(
            job["job_id"], claimed["lease_token"], b"duplicate", "fixture://late", "v1"
        )


def test_tool_capability_policy_denial_happens_before_existing_tool_budget_charge(env, trigger):
    receipt = bound_run(env, trigger)
    running = env.engine.begin_run(receipt.run_id)
    token = "s" * 40
    with env.engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.tool_grants(token_hash,run_id,task_id,role,execution_epoch,"
            "actor,scope,remaining_calls,expires_at) VALUES (%s,%s,'task','knowledge',%s,"
            "'owner',%s,5,now()+interval '1 hour')",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                receipt.run_id,
                running["execution_epoch"],
                Jsonb({"input_revision": 1}),
            ),
        )
    with pytest.raises(DomainError) as error:
        env.engine.tools._grant(token, "search_knowledge")
    assert error.value.code == "SOURCE_POLICY_TOOL_DENIED"
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT remaining_calls FROM app.tool_grants").fetchone()[
                "remaining_calls"
            ]
            == 5
        )


def test_live_bridge_and_old_readonly_mandate_cannot_be_upgraded(env, trigger):
    sign(env, definition(env.installation, mode="auto_repair"))
    receipt = env.engine.ingest(trigger)
    with env.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.mandates SET scope=scope||'{\"read_only\":true}'::jsonb WHERE "
            "mandate_id=(SELECT mandate_id FROM app.tickets WHERE ticket_id=%s)",
            (receipt.ticket_id,),
        )
    with pytest.raises(DomainError) as error:
        env.policies.bind_run(env.credential, env.policy_id, 1, receipt.run_id, facts())
    assert error.value.code == "SOURCE_POLICY_LEGACY_READ_ONLY"
    with env.engine.transaction() as conn:
        conn.execute("UPDATE app.runs SET execution_mode='live' WHERE run_id=%s", (receipt.run_id,))
    with pytest.raises(DomainError) as error:
        env.policies.bind_run(env.credential, env.policy_id, 1, receipt.run_id, facts())
    assert error.value.code == "SOURCE_POLICY_BRIDGE_UNAVAILABLE"


def test_expiry_blocks_new_operation_and_sweep_expires_persisted_lease(env, trigger):
    value = definition(env.installation, valid_until=datetime.now(UTC) + timedelta(seconds=2))
    receipt = bound_run(env, trigger, value)
    running = env.engine.begin_run(receipt.run_id)
    time.sleep(2.05)
    with pytest.raises(DomainError) as error:
        env.engine.submit_read_job(receipt.run_id, running["execution_epoch"], "expired")
    assert error.value.code == "SOURCE_POLICY_TIME"
    assert env.policies.expire_authorities() == 1
    assert env.policies.expire_authorities() == 0
    assert env.engine.get_run(receipt.run_id)["status"] == "paused"


def test_pending_notification_needs_independent_exact_grant_and_old_action_approval(env, trigger):
    receipt, action = proposed(
        env, trigger, definition(env.installation, notification=notify(env.installation))
    )
    with pytest.raises(DomainError) as error:
        env.engine.approve(action["action_id"], action["payload_hash"], "owner")
    assert error.value.code == "SOURCE_POLICY_NOTIFICATION_REQUIRED"
    request = request_for(action, env.installation)
    env.policies.authorize_notification(receipt.run_id, request)
    assert (
        env.engine.claim_effect("fixture") is None
    )  # Policy fact alone never creates an approved Outbox.
    env.engine.approve(action["action_id"], action["payload_hash"], "owner")
    revoke(env)
    assert env.engine.actions(receipt.run_id)[0]["status"] == "cancelled"
    assert env.engine.claim_effect("fixture") is None
    assert env.engine.get_run(receipt.run_id)["status"] == "paused"


@pytest.mark.parametrize("mode", ["disabled", "shadow"])
def test_disabled_and_shadow_cannot_bind_or_queue_even_explicit_request(env, trigger, mode):
    sign(env, definition(env.installation, mode=mode, notification=notify(env.installation)))
    receipt = env.engine.ingest(trigger)
    with pytest.raises(DomainError):
        env.policies.bind_run(env.credential, env.policy_id, 1, receipt.run_id, facts())
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_run_policies").fetchone()["n"] == 0
        )
        assert conn.execute("SELECT count(*) AS n FROM app.outbox").fetchone()["n"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"body_sha256": "0" * 64},
        {"content_class": "critical_fault"},
        {"data_class": "private_log"},
        {"destination_target": "github:other/repo#issue:1"},
        {"public": True},
    ],
)
def test_notification_rejects_changed_body_destination_class_and_private_to_public(
    env, trigger, change
):
    receipt, action = proposed(
        env, trigger, definition(env.installation, notification=notify(env.installation))
    )
    raw = request_for(action, env.installation).model_dump(mode="json")
    if "destination_target" in change:
        raw["destination"]["target"] = change["destination_target"]
    elif "public" in change:
        raw["destination"]["visibility"] = "public"
    else:
        raw.update(change)
    with pytest.raises(DomainError):
        env.policies.authorize_notification(receipt.run_id, NotificationRequest.model_validate(raw))
    with env.engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.source_notification_authorizations"
            ).fetchone()["n"]
            == 0
        )


def test_quiet_hours_and_global_policy_rate_limit_are_checked_again_at_dispatch(env, trigger):
    now = datetime.now(UTC)
    quiet = {
        "timezone": "UTC",
        "start": (now - timedelta(minutes=1)).strftime("%H:%M"),
        "end": (now + timedelta(minutes=2)).strftime("%H:%M"),
    }
    value = definition(env.installation, notification=notify(env.installation, quiet_hours=[quiet]))
    receipt, action = proposed(env, trigger, value)
    with pytest.raises(DomainError) as error:
        env.policies.authorize_notification(receipt.run_id, request_for(action, env.installation))
    assert error.value.code == "SOURCE_POLICY_QUIET_HOURS"


def test_notification_quota_does_not_reset_with_new_run_or_duplicate_authorization(env, trigger):
    value = definition(env.installation, notification=notify(env.installation, max_per_hour=1))
    receipt, action = proposed(env, trigger, value)
    request = request_for(action, env.installation)
    env.policies.authorize_notification(receipt.run_id, request)
    env.policies.authorize_notification(receipt.run_id, request)
    # A second immutable proposal still shares the same policy allowance.
    duplicate = uuid4()
    with env.engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.actions(action_id,run_id,action_type,target,payload,"
            "payload_hash,base_revision,execution_epoch,fencing_token,dedupe_key,status,"
            "expires_at) SELECT %s,run_id,action_type,target,payload,payload_hash,"
            "base_revision,execution_epoch,fencing_token,%s,'proposed',expires_at FROM "
            "app.actions WHERE action_id=%s",
            (duplicate, str(duplicate), action["action_id"]),
        )
    with pytest.raises(DomainError) as error:
        env.policies.authorize_notification(
            receipt.run_id, request.model_copy(update={"action_id": duplicate})
        )
    assert error.value.code == "SOURCE_POLICY_NOTIFICATION_QUOTA"


def test_public_destination_cannot_receive_private_content_even_if_owner_listed_that_class(
    env, trigger
):
    grant = notify(env.installation)
    grant["destinations"][0]["visibility"] = "public"
    receipt, action = proposed(env, trigger, definition(env.installation, notification=grant))
    private = request_for(action, env.installation).model_dump(mode="json")
    private["destination"]["visibility"] = "public"
    with pytest.raises(DomainError) as error:
        env.policies.authorize_notification(
            receipt.run_id, NotificationRequest.model_validate(private)
        )
    assert error.value.code == "SOURCE_POLICY_DISCLOSURE_DENIED"
    private["data_class"] = "public_summary"
    env.policies.authorize_notification(receipt.run_id, NotificationRequest.model_validate(private))


def test_notification_content_size_is_checked_on_the_actual_action_bytes(env, trigger):
    receipt, action = proposed(
        env,
        trigger,
        definition(env.installation, notification=notify(env.installation, max_bytes=3)),
    )
    with pytest.raises(DomainError) as error:
        env.policies.authorize_notification(receipt.run_id, request_for(action, env.installation))
    assert error.value.code == "SOURCE_POLICY_DISCLOSURE_DENIED"


def test_quiet_hours_are_rechecked_after_authorization_before_dispatch(env, trigger, monkeypatch):
    at = datetime.now(UTC)
    later = at + timedelta(minutes=15)
    quiet = {
        "timezone": "UTC",
        "start": (at + timedelta(minutes=10)).strftime("%H:%M"),
        "end": (at + timedelta(minutes=20)).strftime("%H:%M"),
    }
    receipt, action = proposed(
        env,
        trigger,
        definition(env.installation, notification=notify(env.installation, quiet_hours=[quiet])),
    )
    env.policies.authorize_notification(receipt.run_id, request_for(action, env.installation))
    env.engine.approve(action["action_id"], action["payload_hash"], "owner")
    monkeypatch.setattr(env.policies, "_now", lambda conn: later)
    assert env.engine.claim_effect("fixture") is None
    blocked = env.engine.actions(receipt.run_id)[0]
    assert blocked["status"] == "expired" and blocked["reason_code"] == "SOURCE_POLICY_QUIET_HOURS"


def test_delayed_ready_notifications_still_share_the_current_dispatch_window(env, trigger):
    receipt, action = proposed(
        env,
        trigger,
        definition(env.installation, notification=notify(env.installation, max_per_hour=1)),
    )
    request = request_for(action, env.installation)
    # A valid old authorization may wait across windows; its dispatch must count today.
    with env.engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.source_notification_authorizations(action_id,run_id,"
            "payload_hash,body_sha256,destination,content_class,data_class,authorized_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,now()-interval '2 hours')",
            (
                action["action_id"],
                receipt.run_id,
                action["payload_hash"],
                request.body_sha256,
                Jsonb(request.destination.model_dump(mode="json")),
                request.content_class,
                request.data_class,
            ),
        )
        second = uuid4()
        conn.execute(
            "INSERT INTO app.actions(action_id,run_id,action_type,target,payload,"
            "payload_hash,base_revision,execution_epoch,fencing_token,dedupe_key,status,"
            "expires_at) "
            "SELECT %s,run_id,action_type,target,payload,payload_hash,base_revision,"
            "execution_epoch,fencing_token,%s,'proposed',expires_at "
            "FROM app.actions WHERE action_id=%s",
            (second, str(second), action["action_id"]),
        )
    env.policies.authorize_notification(
        receipt.run_id, request.model_copy(update={"action_id": second})
    )
    env.engine.approve(action["action_id"], action["payload_hash"], "owner")
    env.engine.approve(second, action["payload_hash"], "owner")
    first = env.engine.claim_effect("fixture")
    assert first is not None
    assert env.engine.claim_effect("fixture") is None
    remaining = [
        row for row in env.engine.actions(receipt.run_id) if row["action_id"] != first["action_id"]
    ][0]
    assert (
        remaining["status"] == "expired"
        and remaining["reason_code"] == "SOURCE_POLICY_NOTIFICATION_QUOTA"
    )


def test_notification_destination_cannot_disguise_another_platform(env):
    raw = definition(env.installation, notification=notify(env.installation)).model_dump(
        mode="json"
    )
    raw["notification"]["destinations"][0]["target"] = "feishu:private-chat"
    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate(raw)


def test_inflight_effect_unknown_and_reconciliation_preserve_truth_without_resuming_revoked_run(
    env, trigger
):
    receipt, action = proposed(
        env, trigger, definition(env.installation, notification=notify(env.installation))
    )
    env.policies.authorize_notification(receipt.run_id, request_for(action, env.installation))
    env.engine.approve(action["action_id"], action["payload_hash"], "owner")
    claimed = env.engine.claim_effect("fixture")
    assert claimed and claimed["action_id"] == action["action_id"]
    revoke(env)
    assert env.engine.actions(receipt.run_id)[0]["status"] == "dispatching"
    env.engine.effect_result(action["action_id"], claimed["lease_token"], unknown=True)
    assert env.engine.claim_effect("fixture") is None
    assert not env.engine.reconcile_effect(action["action_id"], None)
    assert env.engine.reconcile_effect(action["action_id"], "controlled-real-receipt")
    assert env.engine.actions(receipt.run_id)[0]["status"] == "succeeded"
    assert env.engine.get_run(receipt.run_id)["status"] == "paused"
    assert env.engine.claim_effect("fixture") is None


def test_revocation_never_refunds_unknown_usage_or_blocks_real_late_settlement(env, trigger):
    receipt = bound_run(env, trigger)
    env.engine.begin_run(receipt.run_id)
    call_id = uuid4()
    with env.engine.transaction() as conn:
        run = env.engine._run(conn, receipt.run_id)
        conn.execute(
            "UPDATE app.budgets SET limit_micros=100,reserved_micros=80 WHERE mandate_id=%s",
            (run["mandate_id"],),
        )
        conn.execute(
            "INSERT INTO app.authorization_budgets(profile_hash,limit_micros,"
            "reserved_micros,used_calls,call_limit) VALUES (%s,100,80,1,1)",
            ("f" * 64,),
        )
        conn.execute(
            "INSERT INTO app.usage_calls(call_id,run_id,step_key,status,reserved_micros,"
            "profile_hash) VALUES (%s,%s,'controlled-inflight','reserved',80,%s)",
            (call_id, receipt.run_id, "f" * 64),
        )
    revoke(env)
    env.engine.settle_call(call_id, None)
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT reserved_micros FROM app.budgets").fetchone()["reserved_micros"]
            == 80
        )
        assert (
            conn.execute(
                "SELECT status FROM app.usage_calls WHERE call_id=%s", (call_id,)
            ).fetchone()["status"]
            == "unknown"
        )
    env.engine.settle_call(call_id, 30, usage={"controlled_receipt": True})
    with env.engine.transaction() as conn:
        ledger = conn.execute("SELECT reserved_micros,settled_micros FROM app.budgets").fetchone()
        assert ledger == {"reserved_micros": 0, "settled_micros": 30}
        assert (
            conn.execute("SELECT used_calls FROM app.authorization_budgets").fetchone()[
                "used_calls"
            ]
            == 1
        )
    assert env.engine.get_run(receipt.run_id)["status"] == "paused"


def test_policy_revocation_linearizes_before_next_tool_claim(env, trigger):
    receipt = bound_run(env, trigger)
    running = env.engine.begin_run(receipt.run_id)
    entered, release, done = Event(), Event(), Event()
    errors = []

    def protected():
        try:
            with env.engine.transaction() as conn:
                env.engine._run(conn, receipt.run_id, running["execution_epoch"])
                entered.set()
                assert release.wait(5)
        except BaseException as exc:
            errors.append(exc)

    def revoker():
        try:
            revoke(env)
            done.set()
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=protected)
    worker.start()
    assert entered.wait(5)
    other = Thread(target=revoker)
    other.start()
    try:
        assert not done.wait(0.1)
    finally:
        release.set()
        worker.join(5)
        other.join(5)
    assert not errors and done.is_set()
    with pytest.raises(DomainError):
        env.engine.submit_read_job(receipt.run_id, running["execution_epoch"], "after-revoke")


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/etc/passwd",
        "src/../secret",
        "src//secret",
        "src\\file",
        "src/%2e%2e/file",
        "src/*",
    ],
)
def test_unsafe_path_boundaries_are_rejected_before_signing(env, path):
    raw = definition(env.installation).model_dump(mode="json")
    raw["execution"]["repositories"][0]["paths"] = [path]
    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate(raw)


@pytest.mark.parametrize(
    "change",
    [
        {"admission": {"enabled": True, "request_kinds": ["team_rule"]}},
        {"receive": {"enabled": False, "model_input": True}},
        {"receive": {"enabled": True, "retain_body": True}},
        {"budget": {"total_limit_micros": 5}},
        {"budget": {"total_limit_micros": 0, "router_limit_micros": 5}},
        {"budget": {"timezone": "bad/zone"}},
        {"notification": {"enabled": True}},
        {"mode": "auto"},
        {"signed_by": "message author"},
    ],
)
def test_incomplete_or_text_supplied_authority_is_not_a_policy(env, change):
    raw = definition(env.installation).model_dump(mode="json")
    raw.update(change)
    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate(raw)


def test_investigation_cannot_grant_editing_and_notification_scope_cannot_cross_tenant(env):
    raw = definition(env.installation).model_dump(mode="json")
    raw["execution"]["capabilities"] = ["edit_files"]
    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate(raw)
    raw = definition(env.installation, notification=notify(env.installation)).model_dump(
        mode="json"
    )
    raw["notification"]["destinations"][0]["thread"]["scope"]["tenant_id"] = "Other"
    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate(raw)


@pytest.mark.parametrize(
    "at,expected",
    [
        ("2026-11-09T21:59:00+00:00", False),
        ("2026-11-09T22:00:00+00:00", True),
        ("2026-11-10T01:59:00+00:00", True),
        ("2026-11-10T02:00:00+00:00", False),
        ("2026-11-10T23:00:00+00:00", False),
    ],
)
def test_quiet_hours_midnight_uses_start_days_and_half_open_end(at, expected):
    quiet = QuietHours(timezone="UTC", start="22:00", end="02:00", weekdays=(0,))
    assert quiet.contains(datetime.fromisoformat(at)) == expected


@pytest.mark.parametrize("at", ["2026-11-01T05:30:00+00:00", "2026-11-01T06:30:00+00:00"])
def test_quiet_hours_cover_both_dst_fold_instants(at):
    quiet = QuietHours(timezone="America/New_York", start="01:00", end="02:00", weekdays=(6,))
    assert quiet.contains(datetime.fromisoformat(at))


@pytest.mark.parametrize(
    "name,model",
    [
        ("source-policy-definition-v1", PolicyDefinition),
        ("source-policy-save-v1", PolicySave),
        ("source-policy-preview-v1", PolicyPreview),
        ("source-policy-revoke-v1", PolicyRevoke),
    ],
)
def test_published_policy_schema_matches_runtime(name, model):
    actual = json.loads((ROOT / "schemas/super-agent-v1" / (name + ".schema.json")).read_text())
    expected = model.model_json_schema()
    expected["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    assert actual == expected


def test_policy_api_requires_current_cookie_origin_csrf_and_never_accepts_bearer_bindings(
    env, settings
):
    with TestClient(
        create_app(settings, env.engine, run_background=False),
        base_url=ORIGIN,
        client=("127.0.0.1", 12345),
    ) as client:
        route = PREFIX + "/" + str(env.policy_id)
        payload = PolicySave(
            expected_revision=0, definition=definition(env.installation), review_ref="http-reviewed"
        ).model_dump(mode="json")
        client.headers["X-MGAR-Token"] = settings.local_token.get_secret_value()
        assert client.put(route, json=payload).status_code == 401
        client.cookies.set(SESSION_COOKIE, env.cookie)
        assert client.put(route, json=payload).status_code == 403
        client.headers["Origin"] = ORIGIN
        assert client.put(route, json=payload).status_code == 403
        client.headers["X-CSRF-Token"] = WebAccounts(env.engine).session(env.cookie).csrf_token
        assert (
            client.put(route, json=payload, headers={"Origin": "https://evil.example"}).status_code
            == 403
        )
        response = client.put(route, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["signed_by"] and response.headers["cache-control"] == "no-store"
        assert client.put(route, json=payload).status_code == 409
        assert client.get(route + "/history").status_code == 200
        assert (
            client.post(
                route + "/bindings",
                json={"run_id": str(uuid4()), "token": env.credential.token.get_secret_value()},
            ).status_code
            == 404
        )
        assert (
            client.post(
                route + "/revoke", json={"expected_revision": 1, "review_ref": "http-stop"}
            ).status_code
            == 200
        )
        WebAccounts(env.engine).revoke(env.cookie)
        assert client.get(route).status_code == 401


def test_revoked_console_session_between_dependency_and_save_is_rechecked(
    env, settings, monkeypatch
):
    original = WebAccounts.session
    csrf = original(WebAccounts(env.engine), env.cookie).csrf_token

    def revoked(accounts, cookie):
        session = original(accounts, cookie)
        if session is not None:
            accounts.revoke(cookie)
        return session

    with TestClient(
        create_app(settings, env.engine, run_background=False),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        client=("127.0.0.1", 12345),
    ) as client:
        client.cookies.set(SESSION_COOKIE, env.cookie)
        monkeypatch.setattr(WebAccounts, "session", revoked)
        response = client.put(
            PREFIX + "/" + str(env.policy_id),
            json=PolicySave(
                expected_revision=0,
                definition=definition(env.installation),
                review_ref="stale-session",
            ).model_dump(mode="json"),
        )
        assert response.status_code == 401
    with env.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.source_policies").fetchone()["n"] == 0


def test_current_policy_and_signed_history_are_project_scoped(env):
    sign(env)
    previous = env.engine.settings.project_id
    env.engine.settings.project_id = "other-project"
    try:
        with pytest.raises(DomainError) as error:
            env.policies.read(env.cookie, env.policy_id)
        assert error.value.code == "SOURCE_POLICY_NOT_FOUND"
    finally:
        env.engine.settings.project_id = previous


def test_database_binding_chain_cannot_cross_project_or_rebind_existing_run(env, trigger):
    receipt = bound_run(env, trigger)
    with pytest.raises(psycopg.errors.RaiseException), env.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.source_run_policies SET policy_revision=2 WHERE run_id=%s",
            (receipt.run_id,),
        )
    with pytest.raises(psycopg.errors.RaiseException), env.engine.transaction() as conn:
        conn.execute("DELETE FROM app.source_run_policies WHERE run_id=%s", (receipt.run_id,))
    original = env.engine.settings.project_id
    env.engine.settings.project_id = "other-project"
    try:
        foreign = env.engine.ingest(
            trigger.model_copy(
                update={"project_id": "other-project", "external_event_id": "other-project-event"}
            )
        )
        other = env.engine.get_run(foreign.run_id)
    finally:
        env.engine.settings.project_id = original
    with pytest.raises(psycopg.errors.ForeignKeyViolation), env.engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.source_run_policies(run_id,ticket_id,thread_id,policy_id,"
            "policy_revision,project_id,identity_id,identity_revision,binding_id,"
            "evidence_id,facts) "
            "SELECT %s,%s,%s,policy_id,policy_revision,project_id,identity_id,"
            "identity_revision,binding_id,evidence_id,facts "
            "FROM app.source_run_policies WHERE run_id=%s",
            (foreign.run_id, foreign.ticket_id, other["thread_id"], receipt.run_id),
        )


def test_forward_upgrade_31_to_32_preserves_old_source_business_and_financial_rows(
    settings, trigger, tmp_path
):
    base = settings.database_url.get_secret_value()
    name = "mgar_sa03_upgrade_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        dsn = make_conninfo(base, dbname=name)
        old, new = tmp_path / "old", tmp_path / "new"
        old.mkdir()
        new.mkdir()
        paths = [
            p
            for p in sorted((ROOT / "migrations").glob("*.sql"))
            if p.name <= "0032_source_policies.sql"
        ]
        for path in paths:
            copyfile(path, new / path.name)
            if path.name < "0032_source_policies.sql":
                copyfile(path, old / path.name)
        assert len(migrate(dsn, old)) == 31
        config = settings.model_copy(update={"database_url": SecretStr(dsn)})
        prior = Engine(config, ArtifactStore(tmp_path / "artifacts"))
        try:
            receipt = prior.ingest(trigger)
            installation = source(prior)
            with prior.transaction() as conn:
                old_run = conn.execute(
                    "SELECT * FROM app.runs WHERE run_id=%s", (receipt.run_id,)
                ).fetchone()
                old_budget = conn.execute("SELECT * FROM app.budgets").fetchone()
        finally:
            prior.close()
        assert migrate(dsn, new) == ["0032_source_policies.sql"] and migrate(dsn, new) == []
        current = Engine(config, ArtifactStore(tmp_path / "artifacts"))
        try:
            assert (
                SourceRegistry(current).get_instance(installation.scope.source_instance_id)
                == installation
            )
            with current.transaction() as conn:
                assert (
                    conn.execute(
                        "SELECT * FROM app.runs WHERE run_id=%s", (receipt.run_id,)
                    ).fetchone()
                    == old_run
                )
                assert conn.execute("SELECT * FROM app.budgets").fetchone() == old_budget
                actual = conn.execute(
                    "SELECT name,sha256 FROM app.schema_migrations ORDER BY name"
                ).fetchall()
                assert [(r["name"], r["sha256"]) for r in actual] == [
                    (p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths
                ]
            assert (
                current.begin_run(receipt.run_id)["status"] == "running"
            )  # No automatic migration into source grants.
        finally:
            current.close()
    finally:
        assert name.startswith("mgar_sa03_upgrade_") and len(name) == 50
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
