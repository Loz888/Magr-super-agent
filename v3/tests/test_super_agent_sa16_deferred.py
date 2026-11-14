"""First checks for deferred admission visibility and rule-revision immutability."""

from sa16_support import configure_scheduler, cycle, decision, rows, setup

from mgar_v3.super_agent.source_autonomy_domain import AutonomyCycleRequest, AutonomyRuleSave


def deferred(engine, tmp_path):
    env = setup(engine, tmp_path, scheduler=False)
    decision(env)
    result = cycle(env)
    assert result["outcomes"][0]["reason_code"] == "AUTONOMY_ORIGINAL_ADMISSION_WAIT", result
    assert rows(env, "runs") == rows(env, "sa_autonomy_attempts") == []
    return env


def test_original_admission_wait_is_visible_and_retries_same_claim_without_second_task(
    engine, tmp_path
):
    env = deferred(engine, tmp_path)
    initial = env.autonomy.claims(env.cookie)["claims"][0]
    assert initial["disposition"] == "held" and initial["attempt_id"] is None
    assert initial["reason_code"] == "AUTONOMY_ORIGINAL_ADMISSION_WAIT"
    configure_scheduler(engine, env.cookie, tmp_path)
    result = cycle(env)
    assert len(result["outcomes"]) == 1 and result["outcomes"][0]["created"], result
    assert len(rows(env, "sa_autonomy_claims")) == len(rows(env, "runs")) == 1
    assert result["outcomes"][0]["claim_id"] == initial["claim_id"]


def test_changed_rule_does_not_rewrite_or_upgrade_a_previously_held_claim(engine, tmp_path):
    env = deferred(engine, tmp_path)
    before = rows(env, "sa_autonomy_claims")
    env.autonomy.save_rule(
        env.cookie,
        env.rule_id,
        AutonomyRuleSave(
            expected_revision=1,
            definition=env.rule.model_copy(update={"cooldown_seconds": 2}),
            review_ref="new-frozen-rule-revision",
        ),
    )
    configure_scheduler(engine, env.cookie, tmp_path)
    result = cycle(env)
    assert not result["outcomes"][0]["created"], result
    assert result["outcomes"][0]["reason_code"] == "AUTONOMY_CLAIM_RULE_CHANGED"
    assert rows(env, "sa_autonomy_claims") == before
    assert rows(env, "runs") == []


def test_maintenance_prefers_actual_running_work_over_old_terminal_audit_rows(engine, tmp_path):
    env = setup(engine, tmp_path)
    from sa16_support import admitted

    first = admitted(env)
    with engine.transaction() as conn:
        conn.execute("UPDATE app.runs SET status='failed' WHERE run_id=%s", (first.run_id,))
    decision(env, topic="other-problem")
    result = cycle(env)
    created = next(x for x in result["outcomes"] if x["created"])
    with engine.transaction() as conn:
        conn.execute("UPDATE app.runs SET status='running' WHERE run_id=%s", (created["run_id"],))
    maintained = env.autonomy.maintain(env.cookie, AutonomyCycleRequest(limit=1))
    assert maintained["outcomes"][0]["attempt_id"] == created["attempt_id"]
    assert maintained["partial"]


def test_original_engine_cancellation_releases_only_undispatched_queue_quota(engine, tmp_path):
    from sa16_support import admitted

    env = setup(engine, tmp_path)
    work = admitted(env)
    engine.cancel(work.run_id)
    assert rows(env, "sa_scheduler_queue")[0]["state"] == "queued"
    env.autonomy.maintain(env.cookie, AutonomyCycleRequest())
    assert rows(env, "sa_scheduler_queue")[0]["state"] == "closed"
    state = env.autonomy.get_attempt(env.cookie, work.attempt_id)
    assert state["original_run_state"] == "cancelled" and not state["terminal_success"]
    assert state["original_model_usage"]["calls"] == 0
    assert state["original_coding_status"] is None
    assert rows(env, "usage_calls") == []
