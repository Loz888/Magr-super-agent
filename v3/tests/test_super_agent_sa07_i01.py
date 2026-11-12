"""SA07-I01: quota backpressure preserves bodyless ordering without reviving content."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sa04_support import envelope, gateway, rows, setup, signed
from sa07_support import ORGANIZATION, configure, definition, save, view
from test_super_agent_sa04_repair import set_roles

from mgar_v3.domain import DomainError


def source(engine, monkeypatch, **options):
    env = setup(engine, **options)
    env.shared = SimpleNamespace(
        engine=engine, cookie=env.cookie, service=engine.project_budgets,
    )
    # An operation straddling a real minute boundary must not make quota assertions flaky.
    at = datetime(2026, 11, 12, 7, 30, tzinfo=UTC)
    monkeypatch.setattr(env.shared.service, "now", lambda conn: at)
    return env


def block(env, *, enabled=True):
    configure(env.shared, project=definition(
        organization_id=ORGANIZATION, enabled=enabled, quotas={"source_minute": 0},
    ))


def restore(env):
    save(env.shared, "project", env.engine.settings.project_id, definition(
        organization_id=ORGANIZATION, quotas={"source_minute": 16},
    ), revision=1)


def refused(env, message, *, receiver=None, codes=("PROJECT_QUOTA_EXHAUSTED",)):
    acks = []
    with pytest.raises(DomainError) as error:
        (receiver or env.gateway).receive(message, acks.append)
    assert error.value.code in codes
    assert error.value.status == 503 and not acks


def unreadable(env, input_id):
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, input_id)


def no_plaintext(env, marker, caplog):
    assert not rows(env, "bodies")
    assert marker not in caplog.text
    with env.engine.transaction() as conn:
        for table in (
            "source_inbox", "source_ingress_entities", "source_ingress_inputs",
            "source_ingress_deliveries", "source_ingress_audit", "sa_budget_audit",
        ):
            payload = conn.execute(
                "SELECT coalesce(jsonb_agg(to_jsonb(t)),'[]'::jsonb)::text AS value "
                "FROM app." + table + " t"
            ).fetchone()["value"]
            assert marker not in payload


@pytest.mark.parametrize("operation", ["create", "edit"])
@pytest.mark.parametrize("late_version", [1, 2])
def test_quota_refused_higher_version_never_restores_lower_body(
    engine, monkeypatch, caplog, operation, late_version,
):
    env = source(engine, monkeypatch)
    initial = env.gateway.receive(envelope(env, body="initial"))
    block(env)
    marker = "UNRETAINED-QUOTA-REV3-" + uuid4().hex
    refused(env, envelope(
        env, delivery="quota-rev3", version=3, operation=operation, body=marker,
    ))
    entity = rows(env, "entities")[0]
    assert entity["native_order"] == 3 and not entity["ambiguous"]
    assert len(rows(env, "inputs")) == 1
    unreadable(env, initial.input_ids[0])
    no_plaintext(env, marker, caplog)
    restore(env)
    late = env.gateway.receive(envelope(
        env, delivery="late-lower", version=late_version,
        body="initial" if late_version == 1 else "stale second version",
        operation="create" if late_version == 1 else "edit",
    ))
    unreadable(env, late.input_ids[0])
    assert rows(env, "entities")[0]["native_order"] == 3
    assert not rows(env, "bodies")


@pytest.mark.parametrize("operation", ["create", "edit"])
def test_exact_quota_retry_is_readable_then_ack_and_quota_are_idempotent(
    engine, monkeypatch, operation,
):
    env = source(engine, monkeypatch)
    env.gateway.receive(envelope(env))
    block(env)
    message = envelope(
        env, delivery="retry-exact-rev3", version=3, operation=operation, body="third version",
    )
    refused(env, message)
    restore(env)
    acks = []
    accepted = env.gateway.receive(message, acks.append)
    duplicate = env.gateway.receive(message, acks.append)
    assert accepted.disposition == "accepted" and duplicate.disposition == "duplicate"
    assert accepted.input_ids == duplicate.input_ids and len(acks) == 2
    assert env.engine.source_ingress.body(env.cookie, accepted.input_ids[0]) == b"third version"
    assert len(rows(env, "inputs")) == 2 and len(rows(env, "bodies")) == 1
    assert view(env.shared)["quota_usage"]["source"] == 1
    fresh = env.gateway.receive(envelope(
        env, delivery="fresh-rev4", version=4, operation="edit", body="fourth version",
    ))
    assert env.engine.source_ingress.body(env.cookie, fresh.input_ids[0]) == b"fourth version"
    assert rows(env, "entities")[0]["native_order"] == 4


@pytest.mark.parametrize("late_operation", ["create", "edit"])
@pytest.mark.parametrize("late_version", [2, 4])
def test_quota_refused_delete_stays_deleted_after_capacity_returns(
    engine, monkeypatch, late_operation, late_version,
):
    env = source(engine, monkeypatch)
    initial = env.gateway.receive(envelope(env))
    block(env)
    message = envelope(env, delivery="refused-delete", version=3, operation="delete")
    refused(env, message)
    assert rows(env, "entities")[0]["deleted"]
    restore(env)
    retried = env.gateway.receive(message)
    late = env.gateway.receive(envelope(
        env, delivery="after-deletion", version=late_version,
        operation=late_operation, body="must not resurrect",
    ))
    for input_id in (*initial.input_ids, *retried.input_ids, *late.input_ids):
        unreadable(env, input_id)
    assert rows(env, "entities")[0]["deleted"] and not rows(env, "bodies")


@pytest.mark.parametrize("first_order,refused_order", [(1, None), (None, 3)])
@pytest.mark.parametrize("operation", ["create", "edit"])
@pytest.mark.parametrize("tail_version", [2, 4])
def test_quota_observation_does_not_erase_unknown_order_history(
    engine, monkeypatch, first_order, refused_order, operation, tail_version,
):
    env = source(engine, monkeypatch)
    env.gateway.receive(envelope(env, order=first_order))
    block(env)
    message = envelope(
        env, delivery="ambiguous-rev3", version=3, operation=operation,
        order=refused_order, body="cannot establish total order",
    )
    refused(env, message)
    assert rows(env, "entities")[0]["ambiguous"]
    restore(env)
    retry = env.gateway.receive(message)
    tail = env.gateway.receive(envelope(
        env, delivery="ambiguous-tail", version=tail_version,
        operation="edit", body="numeric time does not resolve ambiguity",
    ))
    for input_id in (*retry.input_ids, *tail.input_ids):
        unreadable(env, input_id)
    assert rows(env, "entities")[0]["ambiguous"] and not rows(env, "bodies")


def test_unbound_author_cannot_poison_watermark_through_quota_refusal(engine, monkeypatch):
    env = source(engine, monkeypatch)
    initial = env.gateway.receive(envelope(env, body="trusted original"))
    block(env)
    refused(env, envelope(
        env, delivery="forged-editor", version=999, operation="edit",
        sender="Other", body="unbound sender",
    ))
    assert rows(env, "entities")[0]["native_order"] == 1
    assert not rows(env, "entities")[0]["ambiguous"]
    assert env.engine.source_ingress.body(env.cookie, initial.input_ids[0]) == b"trusted original"
    restore(env)
    fresh = env.gateway.receive(envelope(
        env, delivery="authorized-rev2", version=2, operation="edit", body="trusted second",
    ))
    assert env.engine.source_ingress.body(env.cookie, fresh.input_ids[0]) == b"trusted second"


@pytest.mark.parametrize("changes,code", [
    ({"delivery": "d1"}, "INGRESS_DELIVERY_CONFLICT"),
    ({"delivery": "d2"}, "INGRESS_EVENT_CONFLICT"),
    ({"delivery": "d2", "event": "other-event"}, "INGRESS_VERSION_CONFLICT"),
    ({"delivery": "d2", "event": "other-event", "version": 2, "order": 1},
     "INGRESS_ORDER_CONFLICT"),
])
def test_conflicting_input_cannot_create_quota_ordering_proof(engine, monkeypatch, changes, code):
    env = source(engine, monkeypatch)
    initial = env.gateway.receive(envelope(env, body="initial"))
    block(env)
    acks = []
    with pytest.raises(DomainError) as error:
        env.gateway.receive(envelope(env, body="conflicting content", **changes), acks.append)
    assert error.value.code == code and not acks
    assert rows(env, "entities")[0]["native_order"] == 1
    assert env.engine.source_ingress.body(env.cookie, initial.input_ids[0]) == b"initial"
    assert len(rows(env, "inputs")) == 1
    assert view(env.shared)["quota_usage"]["source"] == 0


@pytest.mark.parametrize("operation", ["create", "edit"])
def test_receive_denial_cannot_borrow_quota_retry_authority(engine, monkeypatch, operation):
    env = source(engine, monkeypatch)
    env.gateway.receive(envelope(env))
    revoked = set_roles(env, env.binding, ("requester",))
    block(env)
    message = envelope(
        env, delivery="receive-denied-rev3", version=3, operation=operation,
        body="Receive itself did not allow this content",
    )
    refused(env, message)
    assert rows(env, "entities")[0]["native_order"] == 3
    restore(env)
    set_roles(env, revoked, ("context_provider", "requester"))
    retry = env.gateway.receive(message)
    for input_id in retry.input_ids:
        unreadable(env, input_id)
    assert not rows(env, "bodies")
    fresh = env.gateway.receive(envelope(
        env, delivery="receive-authorized-rev4", version=4,
        operation="edit", body="genuinely newer authorized content",
    ))
    assert env.engine.source_ingress.body(env.cookie, fresh.input_ids[0])


@pytest.mark.parametrize("operation", ["create", "edit"])
def test_quota_observation_and_privacy_invalidation_rollback_together(
    engine, monkeypatch, operation,
):
    env = source(engine, monkeypatch)
    initial = env.gateway.receive(envelope(env, body="initial"))
    block(env)
    message = envelope(
        env, delivery="rollback-rev3", version=3, operation=operation, body="uncommitted new body",
    )

    def fail(point):
        if point == "before_commit":
            raise RuntimeError("controlled rollback of quota observation")

    with pytest.raises(RuntimeError, match="controlled rollback"):
        gateway(env, checkpoint=fail).receive(message)
    assert rows(env, "entities")[0]["native_order"] == 1
    assert env.engine.source_ingress.body(env.cookie, initial.input_ids[0]) == b"initial"
    assert len(rows(env, "inputs")) == len(rows(env, "deliveries")) == 1
    refused(env, message)
    assert rows(env, "entities")[0]["native_order"] == 3 and not rows(env, "bodies")
    restore(env)
    retry = env.gateway.receive(message)
    assert env.engine.source_ingress.body(env.cookie, retry.input_ids[0]) == b"uncommitted new body"


@pytest.mark.parametrize("versions", [(2, 3), (3, 2)])
def test_refused_multi_item_delivery_preserves_maximum_observed_version(
    engine, monkeypatch, versions,
):
    env = source(engine, monkeypatch)
    env.gateway.receive(envelope(env))
    block(env)
    parts = [json.loads(envelope(
        env, version=version, operation="edit", body=f"body-{version}",
    ).payload) for version in versions]
    raw = parts[0]
    raw["delivery"] = "mixed-order-refused"
    raw["items"] = [part["items"][0] for part in parts]
    message = signed(raw)
    refused(env, message)
    assert rows(env, "entities")[0]["native_order"] == 3
    assert len(rows(env, "inputs")) == 1 and not rows(env, "bodies")
    restore(env)
    lower = env.gateway.receive(envelope(
        env, delivery="late-two", version=2, operation="edit", body="body-2",
    ))
    unreadable(env, lower.input_ids[0])
    retry = env.gateway.receive(message)
    by_version = {row["native_order"]: row["input_id"] for row in rows(env, "inputs")}
    assert set(retry.input_ids) == {by_version[2], by_version[3]}
    unreadable(env, by_version[2])
    assert env.engine.source_ingress.body(env.cookie, by_version[3]) == b"body-3"


def test_first_refused_entity_has_bodyless_watermark_when_fact_capacity_exists(
    engine, monkeypatch, caplog,
):
    env = source(engine, monkeypatch, limits={"max_receipts": 2})
    block(env)
    marker = "FIRST-REFUSED-CONTENT-" + uuid4().hex
    refused(env, envelope(env, delivery="first-rev3", version=3, body=marker))
    entity = rows(env, "entities")[0]
    assert entity["native_order"] == 3 and entity["head_input_id"] is None
    assert not rows(env, "inputs")
    no_plaintext(env, marker, caplog)
    restore(env)
    lower = env.gateway.receive(envelope(
        env, delivery="late-first-rev2", version=2, body="older than first observation",
    ))
    unreadable(env, lower.input_ids[0])


def test_full_fact_capacity_cannot_allocate_entities_from_quota_refusals(engine, monkeypatch):
    env = source(engine, monkeypatch, limits={"max_receipts": 1})
    env.gateway.receive(envelope(env))
    block(env)
    for index in range(3):
        refused(env, envelope(
            env, delivery=f"unallocated-{index}", entity=f"unknown-{index}", version=3,
        ), codes=("PROJECT_QUOTA_EXHAUSTED", "INGRESS_RECEIPT_CAPACITY"))
    assert len(rows(env, "entities")) == len(rows(env, "inputs")) == 1
    assert len(rows(env, "bodies")) == 1
    # Existing-entity invalidation remains necessary even without room for a retry proof.
    refused(env, envelope(
        env, delivery="known-at-capacity", version=3, operation="edit",
    ), codes=("PROJECT_QUOTA_EXHAUSTED", "INGRESS_RECEIPT_CAPACITY"))
    assert rows(env, "entities")[0]["native_order"] == 3
    assert not rows(env, "bodies")


@pytest.mark.parametrize("operation", ["create", "edit"])
def test_quota_observation_does_not_authorize_changed_retry_payload(
    engine, monkeypatch, operation,
):
    env = source(engine, monkeypatch)
    env.gateway.receive(envelope(env))
    block(env)
    refused(env, envelope(
        env, delivery="payload-bound-rev3", version=3, operation=operation, body="observed version",
    ))
    restore(env)
    acks = []
    with pytest.raises(DomainError):
        env.gateway.receive(envelope(
            env, delivery="payload-bound-rev3", version=3,
            operation=operation, body="different content under same delivery",
        ), acks.append)
    assert not acks and not rows(env, "bodies")
    assert rows(env, "entities")[0]["native_order"] == 3


@pytest.mark.parametrize("operation", ["create", "edit"])
def test_disabled_budget_refusal_preserves_the_same_ordering_invariant(
    engine, monkeypatch, operation,
):
    env = source(engine, monkeypatch)
    env.gateway.receive(envelope(env))
    block(env, enabled=False)
    refused(env, envelope(
        env, delivery="disabled-rev3", version=3,
        operation=operation, body="refused while disabled",
    ), codes=("BUDGET_DISABLED_OR_EXPIRED",))
    assert rows(env, "entities")[0]["native_order"] == 3
    restore(env)
    lower = env.gateway.receive(envelope(
        env, delivery="disabled-lower-rev2", version=2, operation="edit", body="late lower",
    ))
    unreadable(env, lower.input_ids[0])


@pytest.mark.parametrize("operation", ["create", "edit"])
def test_equivalent_alias_can_arrive_before_original_quota_transport(
    engine, monkeypatch, operation,
):
    env = source(engine, monkeypatch)
    env.gateway.receive(envelope(env))
    block(env)
    original = envelope(
        env, delivery="original-refused-three", version=3,
        operation=operation, body="same third version",
    )
    refused(env, original)
    restore(env)
    acknowledgements = []
    alias = env.gateway.receive(envelope(
        env, delivery="equivalent-alias-three", event="alias-event-three", version=3,
        operation=operation, body="same third version",
    ), acknowledgements.append)
    later_original = env.gateway.receive(original, acknowledgements.append)
    duplicate_original = env.gateway.receive(original, acknowledgements.append)
    assert alias.input_ids == later_original.input_ids == duplicate_original.input_ids
    assert duplicate_original.disposition == "duplicate" and len(acknowledgements) == 3
    assert env.engine.source_ingress.body(env.cookie, alias.input_ids[0]) == b"same third version"
    assert len(rows(env, "inputs")) == 2 and len(rows(env, "bodies")) == 1


def test_refused_equivalent_alias_binds_its_own_transport_payload(engine, monkeypatch):
    env = source(engine, monkeypatch)
    env.gateway.receive(envelope(env))
    block(env)
    original = envelope(
        env, delivery="refused-transport-A", version=3,
        operation="edit", body="third version",
    )
    alias = envelope(
        env, delivery="refused-transport-B", event="equivalent-event-three", version=3,
        operation="edit", body="third version",
    )
    refused(env, original)
    refused(env, alias)
    restore(env)
    acknowledgements = []
    with pytest.raises(DomainError) as error:
        env.gateway.receive(envelope(
            env, delivery="refused-transport-B", event="different-event-four", version=4,
            operation="edit", body="changed transport under a newer version",
        ), acknowledgements.append)
    assert error.value.code == "INGRESS_DELIVERY_CONFLICT" and not acknowledgements
    assert rows(env, "entities")[0]["native_order"] == 3
    assert len(rows(env, "inputs")) == 1 and not rows(env, "bodies")
    # The conflicting B receipt is sealed under the existing transport semantics.
    # Its original quota-only observation must not relax that durable conflict.
    with pytest.raises(DomainError) as error:
        env.gateway.receive(alias)
    assert error.value.code == "INGRESS_DELIVERY_CONFLICT"
    unchanged_alias = env.gateway.receive(original)
    assert (
        env.engine.source_ingress.body(env.cookie, unchanged_alias.input_ids[0]) == b"third version"
    )


def test_prior_receive_denial_cannot_be_laundered_by_later_quota_observation(engine, monkeypatch):
    env = source(engine, monkeypatch)
    env.gateway.receive(envelope(env))
    changed = set_roles(env, env.binding, ("requester",))
    denied = env.gateway.receive(envelope(
        env, delivery="first-receive-denial", version=3,
        operation="create", body="previously denied third version",
    ))
    assert denied.disposition == "rejected"
    assert rows(env, "entities")[0]["native_order"] == 3
    set_roles(env, changed, ("context_provider", "requester"))
    block(env)
    later_observation = envelope(
        env, delivery="later-quota-only-denial", event="later-third-version-event", version=3,
        operation="create", body="previously denied third version",
    )
    refused(env, later_observation)
    restore(env)
    retry = env.gateway.receive(later_observation)
    for input_id in retry.input_ids:
        unreadable(env, input_id)
    assert not rows(env, "bodies")
    fresh = env.gateway.receive(envelope(
        env, delivery="later-truly-new-four", version=4,
        operation="edit", body="currently authorized genuinely new content",
    ))
    assert env.engine.source_ingress.body(env.cookie, fresh.input_ids[0])


def test_quota_retry_proof_does_not_survive_role_revocation_and_restoration(engine, monkeypatch):
    env = source(engine, monkeypatch)
    env.gateway.receive(envelope(env))
    block(env)
    message = envelope(
        env, delivery="proof-before-role-revocation", version=3,
        operation="edit", body="content observed under previous role revision",
    )
    refused(env, message)
    revoked = set_roles(env, env.binding, ("requester",))
    set_roles(env, revoked, ("context_provider", "requester"))
    restore(env)
    try:
        retry = env.gateway.receive(message)
    except DomainError:
        pass  # Explicitly refusing the obsolete proof is also a safe outcome.
    else:
        for input_id in retry.input_ids:
            unreadable(env, input_id)
    assert rows(env, "entities")[0]["native_order"] == 3 and not rows(env, "bodies")
    fresh = env.gateway.receive(envelope(
        env, delivery="new-after-role-restoration", version=4,
        operation="edit", body="new content under the current restored role",
    ))
    assert env.engine.source_ingress.body(env.cookie, fresh.input_ids[0])


def test_first_unordered_quota_version_can_retry_after_repeated_refusals(engine, monkeypatch):
    env = source(engine, monkeypatch)
    block(env)
    message = envelope(
        env, delivery="first-unordered-repeated", order=None, body="single unordered first version",
    )
    refused(env, message)
    refused(env, message)
    assert not rows(env, "inputs") and not rows(env, "bodies")
    assert all(not entity["ambiguous"] for entity in rows(env, "entities"))
    restore(env)
    acknowledgements = []
    accepted = env.gateway.receive(message, acknowledgements.append)
    duplicate = env.gateway.receive(message, acknowledgements.append)
    assert accepted.disposition == "accepted" and duplicate.disposition == "duplicate"
    assert accepted.input_ids == duplicate.input_ids and len(acknowledgements) == 2
    assert (
        env.engine.source_ingress.body(env.cookie, accepted.input_ids[0])
        == b"single unordered first version"
    )
    assert len(rows(env, "inputs")) == len(rows(env, "bodies")) == 1
    assert not rows(env, "entities")[0]["ambiguous"]
    assert view(env.shared)["quota_usage"]["source"] == 1


@pytest.mark.parametrize("later_order", [None, 4])
def test_first_unordered_quota_version_remains_ambiguous_after_distinct_observation(
    engine, monkeypatch, later_order,
):
    env = source(engine, monkeypatch)
    block(env)
    first = envelope(
        env, delivery="first-unordered-pending", order=None, body="first pending version",
    )
    refused(env, first)
    different = envelope(
        env, delivery="distinct-pending-version", version=4, order=later_order,
        operation="edit", body="different version with uncertain relative order",
    )
    refused(env, different)
    assert rows(env, "entities")[0]["ambiguous"]
    assert not rows(env, "inputs") and not rows(env, "bodies")
    restore(env)
    for message in (first, different):
        retry = env.gateway.receive(message)
        for input_id in retry.input_ids:
            unreadable(env, input_id)
        assert rows(env, "entities")[0]["ambiguous"] and not rows(env, "bodies")
    later = env.gateway.receive(envelope(
        env, delivery="later-numeric-cannot-resolve", version=7,
        operation="edit", body="still no proof of relative order",
    ))
    unreadable(env, later.input_ids[0])
    assert rows(env, "entities")[0]["ambiguous"] and not rows(env, "bodies")
