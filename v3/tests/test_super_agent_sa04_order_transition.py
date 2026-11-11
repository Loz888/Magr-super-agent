"""SA04 order transition regression; original independent function retained unchanged."""

import pytest
from sa04_support import envelope, rows, setup
from test_super_agent_sa04_repair import set_roles

from mgar_v3.domain import DomainError


@pytest.mark.parametrize("deny_middle", [False, True])
def test_unordered_then_distinct_ordered_versions_keep_ambiguity(engine, deny_middle):
    env = setup(engine)
    first = env.gateway.receive(
        envelope(env, operation="edit", order=None, body="first without native order")
    )
    assert engine.source_ingress.body(env.cookie, first.input_ids[0])
    if deny_middle:
        binding = set_roles(env, env.binding, ("requester",))
    middle = env.gateway.receive(
        envelope(
            env, delivery="middle-3", version=3, operation="edit", body="distinct ordered version"
        )
    )
    if deny_middle:
        assert middle.disposition == "rejected"
        set_roles(env, binding, ("context_provider", "requester"))
    fresh = env.gateway.receive(
        envelope(
            env,
            delivery="later-4",
            version=4,
            operation="edit",
            body="cannot resolve unknown first version",
        )
    )
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, fresh.input_ids[0])
    assert rows(env, "entities")[0]["ambiguous"]


@pytest.mark.parametrize("first_operation", ["create", "edit"])
@pytest.mark.parametrize("middle_operation", ["create", "edit"])
@pytest.mark.parametrize("deny_middle", [False, True])
@pytest.mark.parametrize("tail_version", [2, 4])
def test_unknown_history_is_preserved_across_receive_decisions(
    engine, first_operation, middle_operation, deny_middle, tail_version
):
    env = setup(engine)
    first = env.gateway.receive(
        envelope(
            env,
            operation=first_operation,
            order=None,
            body="first unordered",
        )
    )
    assert engine.source_ingress.body(env.cookie, first.input_ids[0]) == b"first unordered"
    if deny_middle:
        binding = set_roles(env, env.binding, ("requester",))
    middle = env.gateway.receive(
        envelope(
            env,
            delivery="middle-3",
            version=3,
            operation=middle_operation,
            body="middle content",
        )
    )
    assert middle.disposition == ("rejected" if deny_middle else "accepted")
    assert rows(env, "entities")[0]["ambiguous"]
    assert not rows(env, "bodies")
    if deny_middle:
        set_roles(env, binding, ("context_provider", "requester"))
    tail = env.gateway.receive(
        envelope(
            env,
            delivery="tail",
            version=tail_version,
            operation="edit",
            body="not provably newer",
        )
    )
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, tail.input_ids[0])
    assert rows(env, "entities")[0]["ambiguous"] and not rows(env, "bodies")


@pytest.mark.parametrize("middle_operation", ["create", "edit"])
@pytest.mark.parametrize("deny_middle", [False, True])
def test_fully_ordered_history_still_admits_provably_newer_content(
    engine, middle_operation, deny_middle
):
    env = setup(engine)
    env.gateway.receive(envelope(env))
    if deny_middle:
        binding = set_roles(env, env.binding, ("requester",))
    env.gateway.receive(
        envelope(
            env,
            delivery="middle-3",
            version=3,
            operation=middle_operation,
            body="ordered three",
        )
    )
    if deny_middle:
        set_roles(env, binding, ("context_provider", "requester"))
    fresh = env.gateway.receive(
        envelope(
            env,
            delivery="later-4",
            version=4,
            operation="edit",
            body="ordered four",
        )
    )
    assert engine.source_ingress.body(env.cookie, fresh.input_ids[0]) == b"ordered four"
    assert not rows(env, "entities")[0]["ambiguous"]


@pytest.mark.parametrize("tail_version", [2, 4])
def test_preserved_unordered_input_defeats_a_preexisting_numeric_watermark(engine, tail_version):
    env = setup(engine)
    env.gateway.receive(envelope(env, operation="edit", order=None, body="unordered origin"))
    # Historical state fixture for the old bug, not a simulated process/transport failure.
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE app.source_ingress_entities SET native_order=3 WHERE source_instance_id=%s",
            (env.installation.scope.source_instance_id,),
        )
    fresh = env.gateway.receive(
        envelope(
            env,
            delivery="after-old-watermark",
            version=tail_version,
            operation="edit",
            body="cannot establish native order retroactively",
        )
    )
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, fresh.input_ids[0])
    assert rows(env, "entities")[0]["ambiguous"] and not rows(env, "bodies")


def test_ambiguity_and_denied_receipt_commit_or_rollback_together(engine):
    from sa04_support import gateway

    env = setup(engine)
    env.gateway.receive(envelope(env, operation="edit", order=None))
    set_roles(env, env.binding, ("requester",))
    incoming = envelope(env, delivery="middle-3", version=3, operation="edit", body="denied body")

    def fail(point):
        if point == "before_commit":
            raise RuntimeError("controlled rollback")

    with pytest.raises(RuntimeError):
        gateway(env, checkpoint=fail).receive(incoming)
    entity = rows(env, "entities")[0]
    assert entity["native_order"] is None and not entity["ambiguous"]
    assert len(rows(env, "deliveries")) == 1
    env.gateway.receive(incoming)
    entity = rows(env, "entities")[0]
    assert entity["native_order"] == 3 and entity["ambiguous"]
    assert len(rows(env, "deliveries")) == 2 and not rows(env, "bodies")
