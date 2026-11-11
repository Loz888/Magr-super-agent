"""SA04 independent findings retained as registered regressions; no scorer edits."""

import pytest
from sa04_support import envelope, gateway, rows, setup

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.sources.ingress_crypto import BodyCipher


@pytest.mark.parametrize("operation", ["create", "edit", "delete"])
def test_ack_callback_loss_then_restart_is_idempotent(engine, operation):
    env = setup(engine)
    if operation != "create":
        env.gateway.receive(envelope(env, delivery="original", version=1))
    incoming = envelope(
        env, delivery="ack-lost", version=2, operation=operation, body="latest body"
    )
    committed_ids = []

    def broken_ack(receipt):
        committed_ids.append(receipt.receipt_id)
        raise ConnectionError("controlled ACK transport failure after durable commit")

    with pytest.raises(ConnectionError):
        env.gateway.receive(incoming, broken_ack)
    original_inputs = {r["input_id"] for r in rows(env, "inputs")}
    assert len(original_inputs) == (1 if operation == "create" else 2)
    engine.close()
    recovered = Engine(engine.settings, ArtifactStore(engine.settings.artifact_root))
    try:
        env.engine = recovered
        recovered.source_ingress.cipher = BodyCipher(env.key)
        ack = []
        result = gateway(env).receive(incoming, ack.append)
        assert result.disposition == "duplicate" and result.committed
        assert result.receipt_id == committed_ids[0] and len(ack) == 1
        assert {r["input_id"] for r in rows(env, "inputs")} == original_inputs
        if operation == "delete":
            assert rows(env, "entities")[0]["deleted"] and not rows(env, "bodies")
            gateway(env).receive(
                envelope(env, delivery="late-new-version", version=99, body="must not revive")
            )
            assert not rows(env, "bodies")
            assert all(r["unavailable_at"] is not None for r in rows(env, "inputs"))
        else:
            assert recovered.source_ingress.body(env.cookie, result.input_ids[0]) == b"latest body"
    finally:
        recovered.close()


def test_equivalent_unordered_edit_does_not_erase_canonical_body(engine):
    env = setup(engine)
    first = env.gateway.receive(envelope(env, operation="edit", order=None, body="same edit"))
    assert engine.source_ingress.body(env.cookie, first.input_ids[0]) == b"same edit"
    second = env.gateway.receive(
        envelope(env, delivery="same-edit-alias", operation="edit", order=None, body="same edit")
    )
    assert first.input_ids == second.input_ids
    assert engine.source_ingress.body(env.cookie, first.input_ids[0]) == b"same edit"


def test_denied_newer_edit_keeps_watermark_against_late_older_version(engine):
    from mgar_v3.sources.identity_domain import RoleChange

    env = setup(engine)
    env.gateway.receive(envelope(env, version=1))
    changed = env.identities.roles(
        env.cookie,
        env.binding["binding_id"],
        RoleChange(
            expected_revision=env.binding["revision"],
            roles=("requester",),
            review_ref="independent-deny-context",
        ),
    )
    denied = env.gateway.receive(
        envelope(env, delivery="newer-denied", version=3, operation="edit", body="newer denied")
    )
    assert denied.disposition == "rejected" and not rows(env, "bodies")
    env.identities.roles(
        env.cookie,
        env.binding["binding_id"],
        RoleChange(
            expected_revision=changed["revision"],
            roles=("context_provider", "requester"),
            review_ref="independent-restore-context",
        ),
    )
    late = env.gateway.receive(
        envelope(
            env,
            delivery="late-old-edit",
            version=2,
            operation="edit",
            body="older must not become current",
        )
    )
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, late.input_ids[0])


def set_roles(env, binding, roles):
    from mgar_v3.sources.identity_domain import RoleChange

    return env.identities.roles(
        env.cookie,
        binding["binding_id"],
        RoleChange(
            expected_revision=binding["revision"], roles=roles, review_ref="SA04-regression-roles"
        ),
    )


@pytest.mark.parametrize("had_original", [False, True])
@pytest.mark.parametrize("ordered", [False, True])
def test_denied_observation_is_monotonic_without_retaining_body(
    engine, had_original, ordered, caplog
):
    from uuid import uuid4

    env = setup(engine)
    if had_original:
        env.gateway.receive(envelope(env))
    changed = set_roles(env, env.binding, ("requester",))
    secret = "REJECTED-NOT-FOR-STORAGE-" + uuid4().hex
    denied = env.gateway.receive(
        envelope(
            env,
            delivery="denied-3",
            version=3,
            operation="edit",
            order=3 if ordered else None,
            body=secret,
        )
    )
    assert denied.disposition == "rejected" and not denied.input_ids
    observed = rows(env, "entities")[0]
    assert observed["native_order"] == (3 if ordered else 1 if had_original else None)
    assert observed["ambiguous"] == (not ordered)
    assert not rows(env, "bodies")
    assert len(rows(env, "inputs")) == int(had_original)
    set_roles(env, changed, ("context_provider", "requester"))
    late = env.gateway.receive(
        envelope(
            env,
            delivery="late-2",
            version=2,
            operation="edit" if had_original else "create",
            body="known older content",
        )
    )
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, late.input_ids[0])
    assert not rows(env, "bodies")
    assert rows(env, "entities")[0]["native_order"] == observed["native_order"]
    assert secret not in str(rows(env, "inputs")) + str(rows(env, "entities")) + caplog.text
    if ordered:
        fresh = env.gateway.receive(
            envelope(
                env,
                delivery="fresh-4",
                version=4,
                operation="edit",
                body="fresh authorized version",
            )
        )
        assert (
            engine.source_ingress.body(env.cookie, fresh.input_ids[0])
            == b"fresh authorized version"
        )
        assert rows(env, "entities")[0]["native_order"] == 4


@pytest.mark.parametrize("historical", [False, True])
def test_denied_equal_watermark_and_replay_do_not_reauthorize_rejected_content(engine, historical):
    env = setup(engine)
    env.gateway.receive(envelope(env))
    changed = set_roles(env, env.binding, ("requester",))
    incoming = envelope(
        env, delivery="denied-3", version=3, operation="edit", body="denied version"
    )
    env.gateway.receive(incoming)
    set_roles(env, changed, ("context_provider", "requester"))
    replay = env.gateway.receive(incoming)
    assert not replay.input_ids and not rows(env, "bodies")
    equal = env.gateway.receive(
        envelope(
            env,
            delivery="replayed-equal-3",
            version=3,
            operation="edit",
            body="denied version",
            historical=historical,
        )
    )
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, equal.input_ids[0])
    assert not rows(env, "bodies")
    assert rows(env, "entities")[0]["native_order"] == 3


def test_unbound_editor_cannot_advance_another_authors_watermark(engine):
    env = setup(engine)
    original = env.gateway.receive(envelope(env))
    denied = env.gateway.receive(
        envelope(
            env,
            delivery="other-author",
            version=999,
            operation="edit",
            sender="Other",
            body="forged edit",
        )
    )
    assert denied.disposition == "rejected"
    assert rows(env, "entities")[0]["native_order"] == 1
    assert (
        engine.source_ingress.body(env.cookie, original.input_ids[0])
        == b"synthetic private content"
    )


@pytest.mark.parametrize("alias_event", [False, True])
def test_equivalent_unordered_version_concurrent_aliases_keep_one_readable_body(
    engine, alias_event
):
    from concurrent.futures import ThreadPoolExecutor

    env = setup(engine)
    first = env.gateway.receive(envelope(env, operation="edit", order=None, body="same edit"))

    def submit(index):
        return env.gateway.receive(
            envelope(
                env,
                delivery=f"alias-{index}",
                operation="edit",
                order=None,
                body="same edit",
                event=f"alias-event-{index}" if alias_event else "m1-1",
            )
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, range(12)))
    assert all(row.input_ids == first.input_ids for row in results)
    assert len(rows(env, "inputs")) == len(rows(env, "bodies")) == 1
    assert not rows(env, "entities")[0]["ambiguous"]
    assert engine.source_ingress.body(env.cookie, first.input_ids[0]) == b"same edit"


def test_unordered_distinct_denied_edit_stays_ambiguous_after_replay(engine):
    env = setup(engine)
    env.gateway.receive(envelope(env, operation="edit", order=None, body="first edit"))
    changed = set_roles(env, env.binding, ("requester",))
    denied = envelope(
        env, delivery="unknown-newer", version=3, operation="edit", order=None, body="unretained"
    )
    env.gateway.receive(denied)
    assert rows(env, "entities")[0]["ambiguous"] and not rows(env, "bodies")
    set_roles(env, changed, ("context_provider", "requester"))
    env.gateway.receive(denied)
    late = env.gateway.receive(
        envelope(
            env,
            delivery="unknown-late",
            version=2,
            operation="edit",
            order=None,
            body="not current",
        )
    )
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, late.input_ids[0])
    assert rows(env, "entities")[0]["ambiguous"] and not rows(env, "bodies")


def test_denied_watermark_observation_and_receipt_rollback_together(engine):
    env = setup(engine)
    env.gateway.receive(envelope(env))
    set_roles(env, env.binding, ("requester",))
    incoming = envelope(env, delivery="denied-3", version=3, operation="edit", body="not retained")

    def fail(point):
        if point == "before_commit":
            raise RuntimeError("controlled transaction rollback")

    with pytest.raises(RuntimeError):
        gateway(env, checkpoint=fail).receive(incoming)
    assert rows(env, "entities")[0]["native_order"] == 1
    assert len(rows(env, "deliveries")) == 1
    env.gateway.receive(incoming)
    assert rows(env, "entities")[0]["native_order"] == 3
    assert len(rows(env, "deliveries")) == 2 and not rows(env, "bodies")
