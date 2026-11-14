"""Signed Source/Router inputs for SA16; real PG, zero model/network/publication."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from sa08_support import policy
from sa09_support import apply, ingest_case, prepared
from sa09_support import setup as eval_setup
from sa12_support import FilesystemOnlyDriver

from mgar_v3.domain import digest
from mgar_v3.super_agent.source_autonomy_domain import (
    AutonomyCycleRequest,
    AutonomyRule,
    AutonomyRuleSave,
)
from mgar_v3.super_agent.source_autonomy_profiles import AutonomyProfile
from mgar_v3.super_agent.source_scheduler_domain import (
    ResourceRequest,
    SchedulerConfiguration,
    SchedulerConfigureRequest,
    SchedulerLimits,
)


def configure_scheduler(engine, cookie, tmp_path, driver=None):
    driver = driver or FilesystemOnlyDriver(tmp_path / "sa16-controlled-resource")
    engine.source_scheduler.driver = driver
    now = datetime.now(UTC)
    config = engine.source_scheduler.configure(
        cookie,
        SchedulerConfigureRequest(
            expected_revision=0,
            configuration=SchedulerConfiguration(
                enabled=True,
                valid_from=now - timedelta(minutes=1),
                valid_until=now + timedelta(hours=1),
                limits=SchedulerLimits(
                    slots=2,
                    preparing=2,
                    cpu_millis=4000,
                    memory_mib=4096,
                    queue=32,
                    source_slots=2,
                    lease_seconds=120,
                    preparation_seconds=120,
                    session_ttl_seconds=3600,
                ),
            ),
            review_ref="SA16-explicit-controlled-resource-settings",
        ),
    )
    return SimpleNamespace(
        engine=engine, service=engine.source_scheduler, cookie=cookie, driver=driver, config=config
    )


def setup(engine, tmp_path, *, mode="auto_investigate", rule_changes=None, scheduler=True):
    env = eval_setup(engine, mode=mode)
    profile = AutonomyProfile(
        profile_sha256=digest("sa16-readonly-profile"),
        mode="readonly",
        repository=engine.settings.repository,
        base_sha="a" * 40,
        objective="Quote the approved fixture's documented symptoms",
        budget_profile_sha256=digest("sa16-readonly-zero-payment-profile"),
        resources=ResourceRequest(cpu_millis=1000, memory_mib=256),
    )
    env.autonomy = engine.source_autonomy
    env.autonomy.register_profile(profile)
    env.profile = profile
    policy(
        env,
        admission={
            "enabled": True,
            "request_kinds": ["team_rule"],
            "rule_ref": "sa16-signed-rule",
            "rule_sha256": digest("sa16-team-rule"),
        },
        execution={
            "enabled": True,
            "repositories": [
                {
                    "repository": engine.settings.repository,
                    "base_sha": "a" * 40,
                    "paths": [profile.read_path],
                }
            ],
            "capabilities": ["read_repository", "model_call"],
            "network_hosts": [],
        },
    )
    if scheduler:
        env.scheduler = configure_scheduler(engine, env.cookie, tmp_path)
    with engine.transaction() as conn:
        current = engine.source_policies._current(conn, env.policy_id)
        now = engine.source_ingress.now(conn)
    definition = AutonomyRule(
        source_instance_id=env.installation.scope.source_instance_id,
        policy_id=env.policy_id,
        policy_revision=env.policy_revision,
        policy_sha256=current["definition_sha256"],
        rule_ref="sa16-signed-rule",
        rule_sha256=digest("sa16-team-rule"),
        repository=engine.settings.repository,
        mode=mode,
        profile_sha256=profile.profile_sha256,
        objective_sha256=profile.objective_sha256,
        cooldown_seconds=1,
        task_ttl_seconds=120,
        idle_ttl_seconds=30,
        max_active=2,
        max_attempts=3,
        valid_from=now - timedelta(seconds=1),
        valid_until=min(env.definition.valid_until, now + timedelta(minutes=30)),
    )
    if rule_changes:
        definition = AutonomyRule.model_validate_json(
            __import__("json").dumps({**definition.model_dump(mode="json"), **rule_changes})
        )
    env.rule_id, env.rule = uuid4(), definition
    env.autonomy.save_rule(
        env.cookie,
        env.rule_id,
        AutonomyRuleSave(
            expected_revision=0, definition=definition, review_ref="SA16-frozen-owner-rule-review"
        ),
    )
    return env


def decision(
    env, *, entity=None, topic="same-topic", action="create", body="Unaddressed synthetic signal"
):
    entity = entity or uuid4().hex
    receipt = ingest_case(
        env, delivery=uuid4().hex, entity=entity, body=body, addressed="none", topic=topic
    )
    _, decisions = apply(env, prepared(env, receipt), action=action)
    return receipt, decisions[0]


def cycle(env):
    return env.autonomy.cycle(env.cookie, AutonomyCycleRequest())


def admitted(env):
    receipt, suggestion = decision(env)
    outcome = cycle(env)
    assert len(outcome["outcomes"]) == 1, outcome
    one = outcome["outcomes"][0]
    assert one["created"], one
    return SimpleNamespace(
        receipt=receipt,
        suggestion=suggestion,
        outcome=one,
        attempt_id=one["attempt_id"],
        run_id=one["run_id"],
    )


def rows(env, table):
    with env.engine.transaction() as conn:
        return conn.execute("SELECT * FROM app." + table).fetchall()
