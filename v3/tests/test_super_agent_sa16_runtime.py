"""SA16-created Coding task through real original Git/Broker/Docker/ledger.

The existing trusted runtime fixture supplies an explicitly cancelled bootstrap
original target solely to install the fixed profile; it never receives a Worker.
This test runs only the newly delegated Source task. No live model/platform IO.
"""

import json
from datetime import timedelta
from uuid import uuid4

from sa08_support import configure_fast_path, policy
from sa09_support import configure_eval
from sa12_support import claim, session
from sa12_support import ready as scheduler_ready
from sa13_runtime_support import BAD, GOOD, facts, run, runtime_env
from sa13_support import setup as explicit_setup
from sa16_support import cycle, decision

from mgar_v3.domain import digest
from mgar_v3.super_agent.fast_path_domain import FastPathSettings
from mgar_v3.super_agent.source_autonomy_domain import (
    AutonomyCycleRequest,
    AutonomyRule,
    AutonomyRuleSave,
)
from mgar_v3.super_agent.source_autonomy_profiles import AutonomyProfile
from mgar_v3.super_agent.source_coding_domain import CodingMandate, CodingRunRequest
from mgar_v3.super_agent.source_eval_domain import SourceEvalProfile, SourceEvalSettings
from mgar_v3.super_agent.source_eval_model import PROMPT_SHA256, PROMPT_VERSION


def autonomous_setup(engine, tmp_path, *, ready=True, **kwargs):
    env = explicit_setup(engine, tmp_path, ready=False, admitted=False, **kwargs)
    bootstrap = env.target
    engine.cancel(bootstrap.run_id)
    source = env.source
    source.policy_revision, source.generation, source.fast_revision, source.eval_revision = (
        2,
        2,
        0,
        0,
    )
    source.fast_settings = FastPathSettings(
        enabled=True, cooldown_seconds=0, merge_window_seconds=1
    )
    profile = env.profile
    raw = source.definition.execution.model_dump(mode="json")
    raw["capabilities"] = ["read_repository", "edit_files", "run_tests", "model_call"]
    policy(
        source,
        admission={
            "enabled": True,
            "request_kinds": ["team_rule"],
            "rule_ref": "sa16-real-fixture-team-rule",
            "rule_sha256": digest("sa16-real-fixture-rule"),
        },
        execution=raw,
    )
    configure_fast_path(source, source.fast_settings)
    source.service = engine.source_eval
    configure_eval(
        source,
        SourceEvalSettings(
            enabled=True,
            profile=SourceEvalProfile(
                provider_id="fixture",
                profile_ref="sa16-controlled-no-router-model",
                price={
                    "model_id": "fixture-chat",
                    "price_version": "fixture-v1",
                    "input_micros_per_million": 1000000,
                    "output_micros_per_million": 1000000,
                },
                prompt_version=PROMPT_VERSION,
                prompt_sha256=PROMPT_SHA256,
            ),
        ),
    )
    source.autonomy = engine.source_autonomy
    trusted = AutonomyProfile(
        profile_sha256=profile.profile_sha256,
        mode="coding",
        repository=profile.repository.repository,
        base_sha=profile.repository.base_commit,
        objective="Repair total without changing tests",
        budget_profile_sha256=profile.budget.profile_sha256,
        resources=profile.grant.resources,
        coding=profile,
    )
    source.autonomy.register_profile(trusted)
    source.rule_id = uuid4()
    with engine.transaction() as conn:
        now = engine.source_ingress.now(conn)
        current = engine.source_policies._current(conn, source.policy_id)
    source.rule = AutonomyRule(
        source_instance_id=source.installation.scope.source_instance_id,
        policy_id=source.policy_id,
        policy_revision=source.policy_revision,
        policy_sha256=current["definition_sha256"],
        rule_ref="sa16-real-fixture-team-rule",
        rule_sha256=digest("sa16-real-fixture-rule"),
        repository=trusted.repository,
        mode="auto_repair",
        profile_sha256=trusted.profile_sha256,
        objective_sha256=trusted.objective_sha256,
        cooldown_seconds=1,
        task_ttl_seconds=300,
        idle_ttl_seconds=120,
        max_active=1,
        max_attempts=1,
        valid_from=now - timedelta(seconds=1),
        valid_until=min(source.definition.valid_until, now + timedelta(minutes=30)),
    )
    source.autonomy.save_rule(
        source.cookie,
        source.rule_id,
        AutonomyRuleSave(
            expected_revision=0,
            definition=source.rule,
            review_ref="exact-source-autonomous-coding-review",
        ),
    )
    receipt, _ = decision(source)
    result = cycle(source)
    assert len(result["outcomes"]) == 1 and result["outcomes"][0]["created"], result
    one = result["outcomes"][0]
    with engine.transaction() as conn:
        row = engine._run(conn, one["run_id"])
        scope = conn.execute(
            "SELECT scope FROM app.mandates WHERE mandate_id=%s", (row["mandate_id"],)
        ).fetchone()["scope"]
    mandate = CodingMandate.model_validate_json(json.dumps(scope))
    env.bootstrap, env.autonomy_attempt_id = bootstrap, one["attempt_id"]
    env.mandate, env.target, env.input, env.input_id = (
        mandate,
        mandate.target,
        mandate.origin.input,
        receipt.input_ids[0],
    )
    env.binding = engine.source_coding.get(env.cookie, mandate.coding_id)
    if ready:
        env.worker = session(env.scheduler)
        env.claimed = claim(env.scheduler, env.worker)
        env.ready = scheduler_ready(env.scheduler, env.worker, env.claimed)
        env.run_request = CodingRunRequest(
            request_id=uuid4(),
            coding_id=mandate.coding_id,
            run_id=mandate.target.run_id,
            assignment_id=env.ready["assignment_id"],
            session_id=env.worker["session_id"],
            epoch=env.ready["execution_epoch"],
            expected_revision=1,
        )
    return env


def test_autonomous_team_rule_repair_has_actual_diff_tests_usage_and_review_wait(
    engine, tmp_path, monkeypatch
):
    import sa13_runtime_support

    monkeypatch.setattr(sa13_runtime_support, "setup", autonomous_setup)
    with runtime_env(engine, tmp_path) as env:
        assert env.model.requests == []
        result = run(env)
        assert result["verification"] == "verified", result
        assert not result["business_task_completed"]
        assert (env.raw.root / "src/calc.py").read_text() == GOOD
        assert (env.source_root / "src/calc.py").read_text() == BAD
        saved = facts(env)
        assert len(saved["usage_calls"]) == len(env.model.requests) == 3
        assert all(
            u["status"] == "known" and u["run_id"] == env.target.run_id
            for u in saved["usage_calls"]
        )
        assert saved["sa_coding_verifications"][-1]["result"] == "passed"
        assert saved["actions"] == saved["outbox"] == []
        before = engine.source_autonomy.get_attempt(env.cookie, env.autonomy_attempt_id)
        assert before["known_verified_result"] and not before["terminal_success"]
        engine.source_autonomy.maintain(env.cookie, AutonomyCycleRequest())
        state = engine.source_autonomy.get_attempt(env.cookie, env.autonomy_attempt_id)
        assert state["review_state"] == "verification_available_awaiting_review"
        assert state["original_run_state"] in {"waiting_approval", "paused"}
        assert not state["terminal_success"] and not state["published"]


def test_committed_rule_disable_after_original_budget_reservation_prevents_model_send(
    engine, tmp_path, monkeypatch
):
    import sa13_runtime_support

    from mgar_v3.domain import DomainError
    from mgar_v3.super_agent.source_autonomy_domain import AutonomyRevisionRequest
    from mgar_v3.super_agent.source_coding_budget import CodingBudgetLedger

    monkeypatch.setattr(sa13_runtime_support, "setup", autonomous_setup)
    with runtime_env(engine, tmp_path) as env:
        reserve = CodingBudgetLedger.reserve
        boundary = []

        def reserved(ledger, task_id, operation_id, kind, **kwargs):
            value = reserve(ledger, task_id, operation_id, kind, **kwargs)
            if kind == "model" and not boundary:
                assert engine.connection.info.transaction_status.name == "IDLE"
                engine.source_autonomy.disable_rule(
                    env.cookie,
                    env.source.rule_id,
                    AutonomyRevisionRequest(
                        expected_revision=1, review_ref="stop-committed-before-provider-send"
                    ),
                )
                boundary.append(value)
            return value

        monkeypatch.setattr(CodingBudgetLedger, "reserve", reserved)
        try:
            run(env)
        except DomainError:
            pass
        assert len(boundary) == 1
        assert env.model.requests == []
        with engine.transaction() as conn:
            calls = conn.execute(
                "SELECT * FROM app.usage_calls WHERE run_id=%s", (env.target.run_id,)
            ).fetchall()
            assert len(calls) == 1
            assert calls[0]["status"] in {"reserved", "unknown"}
            assert calls[0]["reserved_micros"] > 0 and calls[0]["settled_micros"] is None
            assert engine._run(conn, env.target.run_id)["status"] == "cancelled"
            assert not conn.execute(
                "SELECT app.sa_autonomy_run_current(%s) AS ok", (env.target.run_id,)
            ).fetchone()["ok"]
