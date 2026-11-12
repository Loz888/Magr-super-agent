"""Exercise shared quotas through actual Source, policy and Outbox entry points."""

from types import SimpleNamespace

import pytest
from sa04_support import envelope, rows, setup
from sa07_support import ORGANIZATION, configure, definition, save, view
from test_super_agent_sa03 import cookie as cookie
from test_super_agent_sa03 import (
    definition as policy_definition,
)
from test_super_agent_sa03 import env as env
from test_super_agent_sa03 import facts, notify, proposed, request_for, sign

from mgar_v3.domain import DomainError


def budget_for(env):
    return SimpleNamespace(
        engine=env.engine, cookie=env.cookie, service=env.engine.project_budgets,
    )


def test_source_redelivery_consumes_one_shared_quota_and_refusal_does_not_ack(engine):
    env = setup(engine)
    shared = budget_for(env)
    configure(shared, project=definition(
        organization_id=ORGANIZATION, quotas={"source_minute": 1},
    ))
    acks = []
    message = envelope(env)
    first = env.gateway.receive(message, acks.append)
    duplicate = env.gateway.receive(message, acks.append)
    assert first.receipt_id == duplicate.receipt_id and len(acks) == 2
    assert view(shared)["quota_usage"]["source"] == 1
    with pytest.raises(DomainError) as error:
        env.gateway.receive(
            envelope(env, delivery="new-delivery", entity="new-entity"), acks.append,
        )
    assert error.value.code == "PROJECT_QUOTA_EXHAUSTED"
    assert error.value.status == 503 and len(acks) == 2
    assert len(rows(env, "inputs")) == 1


def test_source_quota_refusal_still_invalidates_trusted_edited_body(engine):
    env = setup(engine)
    first = env.gateway.receive(envelope(env))
    shared = budget_for(env)
    configure(shared, project=definition(
        organization_id=ORGANIZATION, quotas={"source_minute": 0},
    ))
    message = envelope(env, delivery="trusted-edit", version=2, operation="edit", body="new body")
    acks = []
    with pytest.raises(DomainError) as error:
        env.gateway.receive(message, acks.append)
    assert error.value.code == "PROJECT_QUOTA_EXHAUSTED" and not acks
    assert not rows(env, "bodies")
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, first.input_ids[0])
    save(shared, "project", engine.settings.project_id, definition(
        organization_id=ORGANIZATION, quotas={"source_minute": 1},
    ), revision=1)
    accepted = env.gateway.receive(message, acks.append)
    assert accepted.disposition == "accepted" and len(acks) == 1
    assert engine.source_ingress.body(env.cookie, accepted.input_ids[0]) == b"new body"


def test_model_policy_requires_current_shared_capacity_without_starting_model(env):
    raw = policy_definition(env.installation).model_dump()
    raw["execution"]["capabilities"] = ["model_call"]
    raw["budget"]["total_limit_micros"] = 100000
    raw["budget"]["accounting_ref"] = "sa07-reviewed-shared-budget"
    sign(env, type(policy_definition(env.installation)).model_validate(raw))
    model_facts = facts().model_copy(update={"capability": "model_call"})

    def decision():
        return env.policies.evaluate(env.credential, env.policy_id, 1, "execution", model_facts)

    assert decision().reason_code == "SOURCE_SHARED_BUDGET_NOT_AVAILABLE"
    shared = budget_for(env)
    configure(shared)
    assert decision().allowed
    save(shared, "project", env.engine.settings.project_id,
         definition(organization_id=ORGANIZATION, enabled=False), revision=1)
    assert not decision().allowed
    with env.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.router_calls").fetchone()["n"] == 0


@pytest.mark.parametrize("quota", [0, 1])
def test_actual_outbox_claim_enforces_shared_notification_quota(env, trigger, quota):
    receipt, action = proposed(
        env, trigger, policy_definition(env.installation, notification=notify(env.installation)),
    )
    shared = budget_for(env)
    env.policies.authorize_notification(receipt.run_id, request_for(action, env.installation))
    env.engine.approve(action["action_id"], action["payload_hash"], "owner")
    configure(shared, project=definition(
        organization_id=ORGANIZATION, quotas={"notifications_hour": quota},
    ))
    claimed = env.engine.claim_effect("fixture")
    assert (claimed is not None) == bool(quota)
    assert view(shared)["quota_usage"]["notification"] == quota
    if not quota:
        current = env.engine.actions(receipt.run_id)[0]
        assert current["status"] == "expired"
        assert current["reason_code"] == "PROJECT_QUOTA_EXHAUSTED"
