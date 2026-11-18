"""Developer inputs traverse real Ingress, Fast Path and SourceEval in shadow."""

from datetime import UTC, datetime, timedelta

import pytest
from sa08_support import add_actor, decisions, envelope, original_state
from sa09_support import apply, prepared, setup

from scripts.sa22_routing_inputs import PUBLIC_FIELDS, development_inputs, received_at


def test_all_development_facts_preserve_time_and_exclude_grading_fields():
    rows = development_inputs()
    assert len(rows) == 100
    assert all(set(row) == {*PUBLIC_FIELDS, "event_facts"} for row in rows)
    assert rows[78]["event_facts"]["addressing"]["verified_direct_or_mention"] is False
    assert rows[98]["event_facts"]["scope"]["installation"] == "dev-other-installation"
    assert all(row["event_facts"]["current_policy"]["execution_grant"] is None for row in rows)
    anchor = datetime(2040, 1, 1, tzinfo=UTC)
    assert received_at(rows[0], anchor) == anchor - timedelta(seconds=3)
    assert received_at(rows[97], anchor) == anchor - timedelta(seconds=901)


@pytest.mark.parametrize("number", [1, 61, 81])
def test_development_fixture_reaches_original_shadow_path(engine, number):
    row = development_inputs()[number - 1]
    facts = row["event_facts"]
    actor = facts["trusted_actor"]
    scope = facts["scope"]
    event = facts["event"]
    env = setup(engine)
    # All three cases bind one synthetic Feishu actor and its exact topic.
    add_actor(
        env, actor=actor["actor_id"], kind=actor["kind"],
        conversation=scope["conversation_id"], roles=tuple(actor["current_roles"]),
    )
    before = original_state(engine)
    message = envelope(
        env,
        delivery=event["event_id"], entity=event["canonical_id"], version=event["revision"],
        occurred_at=received_at(row, datetime.now(UTC)).isoformat(),
        conversation=scope["conversation_id"], topic=scope["native_thread_id"],
        sender=actor["actor_id"], actor_kind=actor["kind"], body=row["text"],
        addressed="mention" if facts["addressing"]["verified_direct_or_mention"] else "ambient",
    )
    receipt = env.gateway.receive(message)
    assert receipt.committed and len(receipt.input_ids) == 1
    if number == 1:
        # The response is a controlled proposal to validate the real pipeline.
        batch = prepared(env, receipt)
        assert batch["mode"] == "fixture"
        _, proposed = apply(env, batch, action="create")
        assert len(proposed) == 1 and proposed[0].intended_action == "would_create"
        assert not proposed[0].eligible_for_execution
    elif number == 61:
        [decision] = decisions(env)
        assert decision["intended_action"] == "would_ignore"
        assert env.service.prepare() is None
    else:
        repeated = env.gateway.receive(message)
        assert repeated.committed and tuple(repeated.input_ids) == tuple(receipt.input_ids)
        assert len(decisions(env)) == 1
    assert original_state(engine) == before
