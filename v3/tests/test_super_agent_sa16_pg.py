"""Initial formal SA16 checks on real PG and the actual original composition."""

import hashlib
import json
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from sa08_support import original_state
from sa16_support import admitted, cycle, decision, rows, setup

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_autonomy import SourceAutonomy
from mgar_v3.super_agent.source_autonomy_domain import (
    AutonomyCycleRequest,
    AutonomyRevisionRequest,
    AutonomyRule,
    AutonomyRuleSave,
)


def test_formal0048_original_engine_exact_shared_patch_and45_checksums(engine):
    a = Path(__file__).resolve().parents[1]
    d = a / "docs/super-agent-v1/sa16-proposal"
    metadata = json.loads((d / "patch-metadata.json").read_text())
    expected = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (a / "migrations").glob("*.sql")
    }
    assert len(expected) == 45
    assert expected["0048_source_autonomy.sql"] == metadata["sql_sha256"]
    assert (a / "migrations/0048_source_autonomy.sql").read_bytes() == (
        d / "0048_source_autonomy.sql"
    ).read_bytes()
    for name, hashes in metadata["files"].items():
        assert hashlib.sha256((a / name).read_bytes()).hexdigest() == hashes["after_sha256"]
    assert isinstance(engine.source_autonomy, SourceAutonomy)
    assert engine.source_autonomy.engine is engine
    assert engine.source_autonomy._profiles == {}
    assert engine.source_coding.runtime is None and engine.source_async.runtime is None
    with engine.transaction() as conn:
        actual = {
            r["name"]: r["sha256"]
            for r in conn.execute("SELECT name,sha256 FROM app.schema_migrations")
        }
    assert actual == expected


@pytest.mark.parametrize("action", ["ignore", "observe", "ask"])
def test_non_create_suggestions_never_create_task_worker_reply_or_money(engine, tmp_path, action):
    env = setup(engine, tmp_path)
    decision(env, action=action)
    before = original_state(engine)
    result = cycle(env)
    assert result["outcomes"][0]["reason_code"] == "AUTONOMY_NO_NEW_TASK"
    assert original_state(engine) == before
    assert result["model_calls"] == 0 and not result["notification_sent"]


def test_shadow_would_create_keeps_all_original_business_records_unchanged(engine, tmp_path):
    env = setup(engine, tmp_path, mode="shadow")
    decision(env)
    before = original_state(engine)
    result = cycle(env)
    assert result["outcomes"][0]["disposition"] == "shadow", result
    assert original_state(engine) == before
    assert rows(env, "sa_autonomy_attempts") == []


def test_auto_investigate_builds_real_readonly_original_mandate_and_queue(engine, tmp_path):
    env = setup(engine, tmp_path)
    work = admitted(env)
    task = env.autonomy.get_attempt(env.cookie, work.attempt_id)
    assert task["original_run_state"] == "queued" and not task["terminal_success"]
    assert len(rows(env, "threads")) == len(rows(env, "tickets")) == len(rows(env, "runs")) == 1
    scope = rows(env, "mandates")[0]["scope"]
    assert scope["read_only"] and scope["actions"] == [] and "issue_number" not in scope
    assert rows(env, "source_run_policies")[0]["facts"]["request_kind"] == "team_rule"
    assert rows(env, "sa_scheduler_queue")[0]["run_id"] == work.run_id
    assert rows(env, "actions") == rows(env, "outbox") == rows(env, "usage_calls") == []
    with engine.transaction() as conn:
        run = engine._run(conn, work.run_id)
        engine.source_policies.check_run(conn, run, "read_repository")
        assert conn.execute(
            "SELECT app.sa_autonomy_run_current(%s) AS ok", (work.run_id,)
        ).fetchone()["ok"]
        for cap in ("edit_files", "run_tests", "model_call", "network_access"):
            with pytest.raises(DomainError):
                engine.source_policies.check_run(conn, run, cap)


def test_parallel_cycles_claim_a_signal_once_and_second_pass_is_idle(engine, tmp_path):
    env = setup(engine, tmp_path)
    decision(env)
    barrier = threading.Barrier(2)
    results, failures = [], []

    def run():
        try:
            barrier.wait(5)
            results.append(cycle(env))
        except BaseException as e:
            failures.append(e)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
        assert not t.is_alive()
    assert not failures, failures
    assert sum(r["created"] for x in results for r in x["outcomes"]) == 1
    assert len(rows(env, "runs")) == len(rows(env, "sa_autonomy_claims")) == 1
    assert cycle(env)["outcomes"] == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("policy_sha256", "0" * 64),
        ("rule_sha256", "1" * 64),
        ("rule_ref", "forged-rule"),
        ("objective_sha256", "2" * 64),
        ("profile_sha256", "3" * 64),
    ],
)
def test_owner_rule_cannot_replace_frozen_authority_or_profile(engine, tmp_path, field, value):
    env = setup(engine, tmp_path)
    bad = AutonomyRule.model_validate_json(
        json.dumps({**env.rule.model_dump(mode="json"), field: value})
    )
    before = rows(env, "sa_autonomy_rule_versions")
    with pytest.raises(DomainError):
        env.autonomy.save_rule(
            env.cookie,
            env.rule_id,
            AutonomyRuleSave(
                expected_revision=1, definition=bad, review_ref="malformed-owner-edit"
            ),
        )
    assert rows(env, "sa_autonomy_rule_versions") == before


def test_rule_cas_and_owner_cookie_cannot_be_substituted_by_local_token(engine, tmp_path):
    env = setup(engine, tmp_path)
    request = AutonomyRuleSave(expected_revision=0, definition=env.rule, review_ref="stale")
    for cookie in ("", engine.settings.local_token.get_secret_value()):
        with pytest.raises(DomainError, match="AUTH_REQUIRED"):
            env.autonomy.save_rule(cookie, env.rule_id, request)
    with pytest.raises(DomainError, match="AUTONOMY_REVISION_CONFLICT"):
        env.autonomy.save_rule(env.cookie, env.rule_id, request)


@pytest.mark.parametrize("table", ["rule_versions", "claims", "events"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_signed_history_and_claims_cannot_be_rewritten(engine, tmp_path, table, operation):
    env = setup(engine, tmp_path)
    admitted(env)
    query = (
        ("DELETE FROM app.sa_autonomy_" + table)
        if operation == "DELETE"
        else ("UPDATE app.sa_autonomy_" + table + " SET project_id=project_id")
    )
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute(query)


@pytest.mark.parametrize("kind", ["disable", "revision"])
def test_committed_rule_change_cancels_original_task_and_closes_queue_same_transaction(
    engine, tmp_path, kind
):
    env = setup(engine, tmp_path)
    work = admitted(env)
    if kind == "disable":
        result = env.autonomy.disable_rule(
            env.cookie,
            env.rule_id,
            AutonomyRevisionRequest(expected_revision=1, review_ref="owner-disable"),
        )
    else:
        result = env.autonomy.save_rule(
            env.cookie,
            env.rule_id,
            AutonomyRuleSave(
                expected_revision=1,
                definition=env.rule.model_copy(update={"cooldown_seconds": 2}),
                review_ref="new-frozen-settings",
            ),
        )
    assert result["revision"] == 2
    current = env.autonomy.get_attempt(env.cookie, work.attempt_id)
    assert current["original_run_state"] == "cancelled" and not current["current_authority"]
    assert rows(env, "sa_scheduler_queue")[0]["state"] == "closed"
    with engine.transaction() as conn:
        assert not conn.execute(
            "SELECT app.sa_autonomy_run_current(%s) AS ok", (work.run_id,)
        ).fetchone()["ok"]
    assert cycle(env)["outcomes"] == []


def test_original_stop_and_new_signal_do_not_automatically_reopen_a_ticket(engine, tmp_path):
    env = setup(engine, tmp_path)
    work = admitted(env)
    env.autonomy.stop(
        env.cookie, work.attempt_id, AutonomyRevisionRequest(expected_revision=1, review_ref="stop")
    )
    decision(env)
    result = cycle(env)
    assert not any(x["created"] for x in result["outcomes"])
    assert any(
        x["reason_code"] == "AUTONOMY_EXPLICIT_REOPEN_REQUIRED" for x in result["outcomes"]
    ), result
    assert len(rows(env, "runs")) == 1


@pytest.mark.parametrize("reason", ["idle", "ttl"])
def test_heartbeat_does_not_renew_work_and_deadlines_stop_original_run(
    engine, tmp_path, monkeypatch, reason
):
    env = setup(engine, tmp_path)
    work = admitted(env)
    with engine.transaction() as conn:
        at = env.autonomy.now(conn)
        for _ in range(3):
            engine._event(conn, work.run_id, "worker_heartbeat", says="still-working")
    future = at + timedelta(seconds=31 if reason == "idle" else 121)
    monkeypatch.setattr(engine.source_ingress, "now", lambda conn: future)
    result = env.autonomy.maintain(env.cookie, AutonomyCycleRequest())
    expected = "AUTONOMY_NO_SEMANTIC_PROGRESS" if reason == "idle" else "AUTONOMY_TASK_EXPIRED"
    assert result["outcomes"][0]["reason_code"] == expected, result
    assert rows(env, "runs")[0]["status"] == "cancelled"
    assert rows(env, "usage_calls") == []


def test_unknown_usage_survives_stop_and_blocks_new_run_without_refund(engine, tmp_path):
    env = setup(engine, tmp_path)
    work = admitted(env)
    with engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.usage_calls(call_id,run_id,step_key,status,reserved_micros) "
            "VALUES(%s,%s,'sa16-synthetic-unknown','unknown',123)",
            (uuid4(), work.run_id),
        )
    before = rows(env, "usage_calls")
    stopped = env.autonomy.stop(
        env.cookie,
        work.attempt_id,
        AutonomyRevisionRequest(expected_revision=1, review_ref="stop-with-unknown"),
    )
    assert stopped["unknown_usage"] and not stopped["terminal_success"]
    assert rows(env, "usage_calls") == before
    assert not stopped["maintenance"]["may_release_resource"]


def test_trusted_profile_immutable_and_original_connection_enforced(engine, tmp_path):
    env = setup(engine, tmp_path)
    with pytest.raises(DomainError):
        env.autonomy.register_profile({"profile_sha256": "0" * 64})
    with pytest.raises(DomainError, match="PROFILE_IMMUTABLE"):
        env.autonomy.register_profile(replace(env.profile, objective="different objective"))
    with psycopg.connect(
        engine.settings.database_url.get_secret_value(), autocommit=True
    ) as foreign:
        with (
            foreign.transaction(),
            pytest.raises(DomainError, match="ORIGINAL_CONNECTION_REQUIRED"),
        ):
            env.autonomy._conn(foreign)
    with pytest.raises(DomainError, match="ORIGINAL_TRANSACTION_REQUIRED"):
        env.autonomy._conn(engine.connection)


def test_no_rule_or_schedule_has_no_original_business_or_model_side_effect(engine):
    from pydantic import SecretStr

    from mgar_v3.web_access import LoginInput, WebAccounts

    account = WebAccounts(engine)
    user = account.create_owner(
        LoginInput(username="sa16-idle-owner", password=SecretStr("controlled-fixture-password"))
    )
    cookie = account.issue_session(user)
    before = original_state(engine)
    assert engine.source_autonomy.cycle(cookie, AutonomyCycleRequest())["outcomes"] == []
    assert engine.source_autonomy.tick(cookie, AutonomyCycleRequest())["outcomes"] == []
    assert original_state(engine) == before
