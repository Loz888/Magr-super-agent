"""Trusted filtered resource observations invalidate old text without retaining new text."""
import copy

import pytest
from sa06_support import CHAT_ID, payload, setup, signed

from mgar_v3.domain import DomainError


def accepted(engine):
    env = setup(engine)
    raw = payload(event_id="privacy-original")
    result = env.runtime.receive(signed(env, raw))
    assert result.committed
    with engine.transaction() as conn:
        identifier = conn.execute(
            "SELECT input_id FROM app.source_ingress_inputs WHERE source_instance_id=%s",
            (env.source_id,)).fetchone()["input_id"]
    original = engine.source_ingress.body(env.cookie, identifier)
    return env, raw, identifier, original


def changed(original, kind):
    value = copy.deepcopy(original)
    value["header"]["event_id"] = "privacy-changed"
    value["event"]["message"]["content"] = '{"text":"UNAUTHORIZED-NEW-BODY-DO-NOT-STORE"}'
    if kind == "unsupported":
        value["event"]["message"]["message_type"] = "image"
    elif kind == "lost_mention":
        value["event"]["message"]["mentions"] = []
    else:
        raise AssertionError("Unknown controlled fixture change")
    return value


def counts(engine, source_id):
    with engine.transaction() as conn:
        return {
            name: conn.execute(
                f"SELECT count(*) AS n FROM app.{name} WHERE source_instance_id=%s",
                (source_id,)).fetchone()["n"]
            for name in ("source_ingress_inputs", "source_ingress_entities",
                         "source_identity_evidence", "feishu_transport_filters")
        }


@pytest.mark.parametrize("kind", ["unsupported", "lost_mention"])
def test_live_filtered_resource_change_invalidates_old_text_without_new_input(engine, kind):
    env, raw, identifier, _ = accepted(engine)
    before = counts(engine, env.source_id)
    value = changed(raw, kind)
    result = env.runtime.receive(signed(env, value))
    assert result.committed and result.disposition == "filtered"
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, identifier)
    after = counts(engine, env.source_id)
    assert after["source_ingress_inputs"] == before["source_ingress_inputs"] == 1
    assert after["source_identity_evidence"] == before["source_identity_evidence"]
    assert after["feishu_transport_filters"] == before["feishu_transport_filters"] + 1
    with engine.transaction() as conn:
        inputs = conn.execute(
            "SELECT metadata,sender_ref,body_sha256,unavailable_at FROM app.source_ingress_inputs "
            "WHERE source_instance_id=%s", (env.source_id,)).fetchall()
        filters = conn.execute(
            "SELECT * FROM app.feishu_transport_filters WHERE source_instance_id=%s",
            (env.source_id,)).fetchall()
    assert inputs[0]["unavailable_at"] is not None
    assert "UNAUTHORIZED-NEW-BODY-DO-NOT-STORE" not in str(inputs) + str(filters)
    # Same filtered redelivery is durable and creates no new minimal fact/input.
    duplicate = env.runtime.receive(signed(env, value))
    assert duplicate.receipt_id == result.receipt_id
    assert counts(engine, env.source_id) == after


@pytest.mark.parametrize("kind", ["unsupported", "lost_mention"])
def test_unknown_filtered_resource_does_not_create_identity_input_or_entity(engine, kind):
    env = setup(engine)
    before = counts(engine, env.source_id)
    value = changed(payload(event_id="privacy-unknown-initial"), kind)
    result = env.runtime.receive(signed(env, value))
    assert result.committed and result.disposition == "filtered"
    after = counts(engine, env.source_id)
    assert after["source_ingress_inputs"] == before["source_ingress_inputs"] == 0
    assert after["source_ingress_entities"] == before["source_ingress_entities"] == 0
    assert after["source_identity_evidence"] == before["source_identity_evidence"]
    assert after["feishu_transport_filters"] == before["feishu_transport_filters"] + 1


@pytest.mark.parametrize("kind", ["unsupported", "lost_mention"])
def test_filtered_same_event_conflict_cannot_delete_canonical_text(engine, kind):
    env, raw, identifier, original = accepted(engine)
    value = changed(raw, kind)
    value["header"]["event_id"] = raw["header"]["event_id"]
    before = counts(engine, env.source_id)
    with pytest.raises(DomainError) as error:
        env.runtime.receive(signed(env, value))
    assert error.value.code in {"INGRESS_DELIVERY_CONFLICT", "INGRESS_EVENT_CONFLICT",
                                "FEISHU_FILTER_CONFLICT"}
    assert engine.source_ingress.body(env.cookie, identifier) == original
    assert counts(engine, env.source_id) == before


def test_filtered_invalidation_and_receipt_rollback_together(engine, monkeypatch):
    env, raw, identifier, original = accepted(engine)
    value = changed(raw, "unsupported")
    before = counts(engine, env.source_id)
    actual = engine.source_ingress._privacy_delete
    observed = []
    def fail_after_invalidation(*args, **kwargs):
        actual(*args, **kwargs)
        observed.append(True)
        raise RuntimeError("controlled-after-privacy-invalidation")
    with monkeypatch.context() as patch:
        patch.setattr(engine.source_ingress, "_privacy_delete", fail_after_invalidation)
        with pytest.raises(RuntimeError, match="controlled-after-privacy-invalidation"):
            env.runtime.receive(signed(env, value))
    assert observed == [True]
    assert engine.source_ingress.body(env.cookie, identifier) == original
    assert counts(engine, env.source_id) == before
    # Retry after rollback is a first committed observation and now invalidates.
    result = env.runtime.receive(signed(env, value))
    assert result.committed
    with pytest.raises(DomainError):
        engine.source_ingress.body(env.cookie, identifier)


@pytest.mark.parametrize("wrong", ["signature", "tenant", "group", "private"])
def test_untrusted_or_cross_conversation_observation_cannot_delete_text(engine, wrong):
    env, raw, identifier, original = accepted(engine)
    value = changed(raw, "unsupported")
    headers = None
    if wrong == "signature":
        headers = {"X-Lark-Signature": "0" * 64}
    elif wrong == "tenant":
        value["header"]["tenant_key"] = "other-tenant"
    elif wrong == "group":
        value["event"]["message"]["chat_id"] = CHAT_ID + "-other"
    else:
        value["event"]["message"]["chat_type"] = "p2p"
    envelope = signed(env, value, headers=headers)
    if wrong in {"signature", "tenant"}:
        with pytest.raises(DomainError):
            env.runtime.receive(envelope)
    else:
        result = env.runtime.receive(envelope)
        assert result.committed and result.disposition == "filtered"
    assert engine.source_ingress.body(env.cookie, identifier) == original
    assert counts(engine, env.source_id)["source_ingress_inputs"] == 1
