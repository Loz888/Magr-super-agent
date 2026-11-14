"""First additional ACL boundary: private candidates are filtered before decryption."""

import pytest
from sa04_support import envelope
from sa14_support import new_source
from sa15_support import path, ref, snapshot
from test_super_agent_sa02 import bind
from test_super_agent_sa15_quality import setup_from_source

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.policy_domain import PolicyDefinition
from mgar_v3.super_agent.source_recall_domain import RecallResolveRequest


def test_worker_cross_group_reference_never_decrypts_private_candidate(
    engine, tmp_path, monkeypatch
):
    source = new_source(engine)
    bind(
        source.identities,
        source.cookie,
        source.installation,
        roles=("context_provider", "requester"),
        conversation="Chat-B",
    )
    raw = source.definition.model_dump(mode="json")
    raw["selector"]["conversations"] = ["Chat-A", "Chat-B"]
    source.definition = PolicyDefinition.model_validate(raw)
    env = setup_from_source(engine, tmp_path, source)
    receipt = source.gateway.receive(
        envelope(
            source,
            delivery="sa15-predecrypt",
            entity="private-b",
            conversation="Chat-B",
            body="PRIVATE_CANDIDATE",
        )
    )
    private_id = receipt.input_ids[0]
    decrypt = engine.source_ingress.cipher.decrypt
    decrypted = []

    def recording(raw, associated):
        decrypted.append(associated)
        return decrypt(raw, associated)

    monkeypatch.setattr(engine.source_ingress.cipher, "decrypt", recording)
    before = snapshot(engine)
    request = RecallResolveRequest(path=path(ref("source", private_id)))
    with pytest.raises(DomainError):
        engine.source_recall.resolve(env.worker_caller, request)
    assert str(private_id) not in decrypted
    assert engine.source_recall.resolve(env.owner, request)["node"]["state"] == "current"
    assert str(private_id) in decrypted
    assert snapshot(engine) == before
