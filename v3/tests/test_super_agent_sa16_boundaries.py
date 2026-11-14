"""First new lifecycle/schedule boundaries; previous formal64/private87 excluded."""

import ast
import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sa12_support import claim, ready, session
from sa16_support import admitted, cycle, decision, rows, setup

from mgar_v3.domain import Claim, DomainError, Investigation
from mgar_v3.graph.runner import FixtureInvestigator, Runner
from mgar_v3.settings import ROOT
from mgar_v3.super_agent.source_autonomy_domain import (
    AutonomyCycleRequest,
    AutonomyReopenRequest,
    AutonomyRevisionRequest,
    AutonomyRuleSave,
    DailySignal,
)
from mgar_v3.super_agent.source_scheduler_domain import SchedulerAssignmentRequest


class ExactFixtureReader(FixtureInvestigator):
    """Trusted zero-model reader of the exact approved fixture source path."""

    def investigate(self, question, content, evidence_id):
        quote = next(line for line in content.splitlines() if line.startswith("#"))
        return Investigation(
            claims=[Claim(statement=quote, quote=quote, evidence_id=evidence_id)],
            open_questions=["Only an exact mechanical fixture quote was verified."],
        )


def test_original_readonly_graph_verifier_completes_silently_from_team_rule(engine, tmp_path):
    env = setup(engine, tmp_path)
    work = admitted(env)
    worker = session(env.scheduler)
    active = ready(env.scheduler, worker, claim(env.scheduler, worker))
    request = SchedulerAssignmentRequest(
        assignment_id=active["assignment_id"],
        session_id=worker["session_id"],
        epoch=active["execution_epoch"],
        expected_revision=active["revision"],
    )
    with engine.source_scheduler.worker_context(worker["bearer"], request):
        result = Runner(engine, ExactFixtureReader(ROOT / env.profile.read_path)).execute(
            work.run_id
        )
    assert result is None
    state = env.autonomy.get_attempt(env.cookie, work.attempt_id)
    assert state["original_run_state"] == "succeeded" and state["terminal_success"]
    assert rows(env, "verdicts")[0]["passed"]
    assert rows(env, "actions") == rows(env, "outbox") == rows(env, "usage_calls") == []
    assert len(rows(env, "runs")) == 1


@pytest.mark.parametrize("limit", ["cooldown", "active"])
def test_new_candidates_obey_durable_cooldown_and_active_caps(engine, tmp_path, limit):
    env = setup(engine, tmp_path, rule_changes={"cooldown_seconds": 300, "max_active": 1})
    admitted(env)
    decision(env, topic="same-topic" if limit == "cooldown" else "other-topic")
    result = cycle(env)
    expected = "AUTONOMY_COOLDOWN" if limit == "cooldown" else "AUTONOMY_ACTIVE_LIMIT"
    assert any(o["reason_code"] == expected for o in result["outcomes"]), result
    assert len(rows(env, "runs")) == 1


def test_explicit_reopen_creates_new_run_under_same_ticket_without_resetting_old_budgets(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    work = admitted(env)
    env.autonomy.stop(
        env.cookie, work.attempt_id, AutonomyRevisionRequest(expected_revision=1, review_ref="stop")
    )
    old_budgets = rows(env, "budgets")
    receipt, suggestion = decision(env)
    with engine.transaction() as conn:
        new_ref = engine.source_correlations._input(conn, receipt.input_ids[0]).ref
        old = engine._run(conn, work.run_id)
    result = env.autonomy.reopen(
        env.cookie,
        work.attempt_id,
        AutonomyReopenRequest(
            expected_revision=2,
            expected_epoch=old["execution_epoch"],
            decision_key=suggestion.decision_key,
            source_input=new_ref,
            review_ref="new-exact-owner-review-for-this-source",
        ),
    )
    assert result["created"], result
    assert len(rows(env, "threads")) == len(rows(env, "tickets")) == 1
    assert len(rows(env, "runs")) == 2
    attempts = rows(env, "sa_autonomy_attempts")
    new = next(a for a in attempts if a["run_id"] == result["run_id"])
    assert new["reopened_from"] == work.attempt_id
    assert new["budget_profile_sha256"] == attempts[0]["budget_profile_sha256"]
    assert all(b in rows(env, "budgets") for b in old_budgets)
    assert next(r for r in rows(env, "runs") if r["run_id"] == work.run_id)["status"] == "cancelled"
    assert any(e["kind"] == "source_autonomy_reopen_reviewed" for e in rows(env, "run_events"))


@pytest.mark.parametrize("fault", ["old_epoch", "same_source", "attempt_limit", "unknown"])
def test_reopen_rejects_stale_authority_without_recreating_task_or_refunding(
    engine, tmp_path, fault
):
    env = setup(
        engine, tmp_path, rule_changes={"max_attempts": 1 if fault == "attempt_limit" else 3}
    )
    work = admitted(env)
    env.autonomy.stop(
        env.cookie, work.attempt_id, AutonomyRevisionRequest(expected_revision=1, review_ref="stop")
    )
    receipt, suggestion = decision(env)
    if fault == "unknown":
        from uuid import uuid4

        with engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.usage_calls(call_id,run_id,step_key,status,reserved_micros) "
                "VALUES(%s,%s,'pending-original','unknown',17)",
                (uuid4(), work.run_id),
            )
    with engine.transaction() as conn:
        ref = engine.source_correlations._input(
            conn, (work.receipt if fault == "same_source" else receipt).input_ids[0]
        ).ref
        old = engine._run(conn, work.run_id)
    before = rows(env, "usage_calls")
    request = AutonomyReopenRequest(
        expected_revision=2,
        expected_epoch=old["execution_epoch"] + (fault == "old_epoch"),
        decision_key=suggestion.decision_key,
        source_input=ref,
        review_ref="new-Owner-review",
    )
    if fault == "attempt_limit":
        result = env.autonomy.reopen(env.cookie, work.attempt_id, request)
        assert not result["created"] and result["reason_code"] == "AUTONOMY_ATTEMPT_LIMIT", result
    else:
        with pytest.raises(DomainError):
            env.autonomy.reopen(env.cookie, work.attempt_id, request)
    assert len(rows(env, "runs")) == 1 and rows(env, "usage_calls") == before


def test_waiting_drains_then_releases_only_after_original_resource_absence_receipt(
    engine, tmp_path
):
    env = setup(engine, tmp_path)
    work = admitted(env)
    worker = session(env.scheduler)
    active = ready(env.scheduler, worker, claim(env.scheduler, worker))
    with engine.transaction() as conn:
        conn.execute("UPDATE app.runs SET status='waiting_input' WHERE run_id=%s", (work.run_id,))
    result = env.autonomy.maintain(env.cookie, AutonomyCycleRequest())
    assert result["outcomes"][0]["reason_code"] == "AUTONOMY_WAITING"
    assignment = engine.source_scheduler.get_assignment(env.cookie, active["assignment_id"])
    assert assignment["state"] == "draining"
    engine.source_scheduler.maintain()
    assignment = engine.source_scheduler.get_assignment(env.cookie, active["assignment_id"])
    assert assignment["state"] == "released"
    assert rows(env, "usage_calls") == rows(env, "actions") == []


def timed(env, monkeypatch, *, work=False):
    # Advance the trusted test clock to the next whole-minute signal. Real
    # schedule/DST arithmetic and the PG cursor are used; no wall-clock claim.
    now = datetime.now(UTC)
    due = now.replace(second=0, microsecond=0) + timedelta(minutes=2)
    ref = None
    if work:
        receipt, _ = decision(env, action="observe")
        with env.engine.transaction() as conn:
            ref = env.engine.source_correlations._input(conn, receipt.input_ids[0]).ref
    schedule = DailySignal(
        timezone="UTC", at=due.strftime("%H:%M"), allow_scheduled_work=work, seed_input=ref
    )
    rule = env.rule.model_copy(update={"schedule": schedule})
    env.autonomy.save_rule(
        env.cookie,
        env.rule_id,
        AutonomyRuleSave(
            expected_revision=1, definition=rule, review_ref="exact-timer-and-seed-Owner-review"
        ),
    )
    monkeypatch.setattr(env.engine.source_ingress, "now", lambda conn: due + timedelta(seconds=1))
    return rule


@pytest.mark.parametrize("work", [False, True])
def test_persistent_tick_unique_across_repeated_cycles_and_rule_revision(
    engine, tmp_path, monkeypatch, work
):
    env = setup(engine, tmp_path)
    rule = timed(env, monkeypatch, work=work)
    before = len(rows(env, "runs"))
    one = env.autonomy.tick(env.cookie, AutonomyCycleRequest())
    assert len(one["outcomes"]) == 1
    assert one["outcomes"][0]["created"] is work, one
    assert len(rows(env, "runs")) == before + int(work)
    assert env.autonomy.tick(env.cookie, AutonomyCycleRequest())["outcomes"] == []
    cursor = env.autonomy.get_rule(env.cookie, env.rule_id)["last_local_date"]
    env.autonomy.save_rule(
        env.cookie,
        env.rule_id,
        AutonomyRuleSave(
            expected_revision=2, definition=rule, review_ref="same-schedule-new-Owner-review"
        ),
    )
    assert env.autonomy.get_rule(env.cookie, env.rule_id)["last_local_date"] == cursor
    assert env.autonomy.tick(env.cookie, AutonomyCycleRequest())["outcomes"] == []
    assert len(rows(env, "sa_autonomy_ticks")) == 1
    assert rows(env, "usage_calls") == rows(env, "actions") == rows(env, "outbox") == []


def test_expired_or_changed_timer_seed_is_audited_without_original_task(
    engine, tmp_path, monkeypatch
):
    env = setup(engine, tmp_path)
    timed(env, monkeypatch, work=True)
    original = rows(env, "source_ingress_inputs")[0]
    # Genuine committed removal of the retained body invalidates the reviewed ref.
    with engine.transaction() as conn:
        conn.execute(
            "DELETE FROM app.source_ingress_bodies WHERE input_id=%s", (original["input_id"],)
        )
    result = env.autonomy.tick(env.cookie, AutonomyCycleRequest())
    assert len(result["outcomes"]) == 1 and not result["outcomes"][0]["created"], result
    assert rows(env, "runs") == [] and len(rows(env, "sa_autonomy_ticks")) == 1


def test_format_repair_preserves_agreed_scheduler_ast_and_exact_hash_chain():
    a = Path(__file__).resolve().parents[1]
    d = a / "docs/super-agent-v1/sa16-proposal"
    m = json.loads((d / "post-integration-format.json").read_text())
    before = subprocess.check_output(["git", "show", m["base_commit"] + ":v3/" + m["file"]], cwd=a)
    after = (a / m["file"]).read_bytes()
    assert hashlib.sha256(before).hexdigest() == m["before_sha256"]
    assert hashlib.sha256(after).hexdigest() == m["after_sha256"]
    assert ast.dump(ast.parse(before)) == ast.dump(ast.parse(after))
    assert (
        hashlib.sha256((d / "post-integration-format.patch").read_bytes()).hexdigest()
        == m["patch_sha256"]
    )
