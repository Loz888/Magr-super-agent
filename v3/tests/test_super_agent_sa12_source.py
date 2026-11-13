"""Signed synthetic Source authority, actual PG fairness and current checks."""

from uuid import uuid4

import pytest
from sa04_support import envelope, gateway
from sa04_support import setup as source_setup
from sa12_support import claim, enqueue, ready, session, setup
from test_super_agent_sa02 import bind
from test_super_agent_sa03 import facts

from mgar_v3.domain import DomainError, TriggerInput
from mgar_v3.sources.ingress_domain import IngressConfigure
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicyRevoke, PolicySave
from mgar_v3.super_agent.source_scheduler_domain import (
    ResourceRequest,
    SchedulerEnqueueRequest,
    SchedulerHeartbeatRequest,
)


def source_environment(engine, tmp_path):
    origin = source_setup(
        engine,
        receive={
            "enabled": True,
            "retain_body": True,
            "model_input": True,
            "body_ttl_seconds": 300,
            "allow_history_context": True,
        },
    )
    binding, credential = bind(
        origin.identities,
        origin.cookie,
        origin.installation,
        roles=("context_provider",),
        actor="CI-bot",
        kind="bot",
    )
    raw = origin.definition.model_dump(mode="json")
    raw["selector"]["actors"].append(binding["actor_ref"])
    raw["admission"] = {
        "enabled": True,
        "request_kinds": ["explicit_request", "team_rule"],
        "rule_ref": "sa12-controlled-ci-fixture",
        "rule_sha256": "e" * 64,
    }
    definition = PolicyDefinition.model_validate(raw)
    engine.source_policies.save(
        origin.cookie,
        origin.policy_id,
        PolicySave(
            expected_revision=1,
            definition=definition,
            review_ref="sa12-explicit-bounded-ci-rule",
        ),
    )
    engine.source_ingress.configure(
        origin.cookie,
        origin.installation.scope.source_instance_id,
        IngressConfigure(
            expected_generation=1,
            settings=origin.settings.model_copy(update={"policy_revision": 2}),
            review_ref="sa12-current-ci-ingress",
        ),
    )
    origin.gateway = gateway(origin, generation=2)
    env = setup(engine, tmp_path, cookie=origin.cookie)
    env.origin, env.bot_credential = origin, credential
    return env


def automation(env, number):
    origin = env.origin
    received = origin.gateway.receive(
        envelope(
            origin,
            delivery=f"sa12-ci-{number}",
            entity=f"sa12-ci-{number}",
            sender="CI-bot",
            actor_kind="bot",
            body="signed synthetic CI resource fixture",
        )
    )
    assert received.committed and len(received.input_ids) == 1
    original = env.engine.ingest(
        TriggerInput(
            source="web",
            external_event_id=uuid4().hex,
            project_id=env.engine.settings.project_id,
            repository=env.engine.settings.repository,
            issue_number=2000 + number,
            actor=env.engine.settings.local_actor,
            body="/mgar investigate reviewed synthetic CI fixture",
        )
    )
    env.engine.source_policies.bind_run(
        env.bot_credential,
        origin.policy_id,
        2,
        original.run_id,
        facts(request_kind="team_rule"),
    )
    with env.engine.transaction() as conn:
        reference = env.engine.source_correlations._input(
            conn, received.input_ids[0]
        ).ref
    target = env.service.target(env.cookie, original.run_id)
    return env.service.enqueue(
        env.cookie,
        SchedulerEnqueueRequest(
            request_id=uuid4(),
            run_id=original.run_id,
            expected_epoch=target["epoch"],
            target_sha256=target["target_sha256"],
            source_input=reference,
            resources=ResourceRequest(cpu_millis=1000, memory_mib=256),
            review_ref="sa12-reviewed-existing-ci-run-not-automatic-creation",
        ),
    )


def test_real_canonical_ci_flood_cannot_take_next_turn_from_authorized_human(
    engine, tmp_path
):
    env = source_environment(engine, tmp_path)
    bots = [automation(env, n) for n in range(1, 7)]
    assert all(q["classification"] == "automation" for q in bots)
    first = session(env)
    taken = claim(env, first)
    assert taken["queue_id"] in {q["queue_id"] for q in bots}
    ready(env, first, taken)
    human = enqueue(env)
    assert human["classification"] == "human"
    selected = claim(env, session(env))
    assert selected["queue_id"] == human["queue_id"]
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.sa_scheduler_queue WHERE classification='automation' "
                "AND state='queued'",
            ).fetchone()["n"]
            == 5
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"]
            == 0
        )
    assert env.service.capacity(env.cookie)["usage"]["slots"] == 2


@pytest.mark.parametrize("change", ["policy_revoke", "source_edit"])
def test_current_source_change_fences_already_claimed_worker_before_new_operation(
    engine,
    tmp_path,
    change,
):
    env = source_environment(engine, tmp_path)
    automation(env, 1)
    worker = session(env)
    active = ready(env, worker, claim(env, worker))
    if change == "policy_revoke":
        engine.source_policies.revoke(
            env.cookie,
            env.origin.policy_id,
            PolicyRevoke(
                expected_revision=2,
                review_ref="sa12-committed-policy-revocation",
            ),
        )
    else:
        receipt = env.origin.gateway.receive(
            envelope(
                env.origin,
                delivery="sa12-ci-edit",
                entity="sa12-ci-1",
                version=2,
                operation="edit",
                sender="CI-bot",
                actor_kind="bot",
                body="new canonical CI facts",
            )
        )
        assert receipt.committed
    with pytest.raises(DomainError):
        env.service.heartbeat(
            worker["bearer"],
            SchedulerHeartbeatRequest(
                assignment_id=active["assignment_id"],
                session_id=worker["session_id"],
                epoch=active["execution_epoch"],
                expected_revision=active["revision"],
            ),
        )
    assert env.service.capacity(env.cookie)["usage"]["slots"] == 1
