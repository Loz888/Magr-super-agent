"""New Source disclosure/stream-loss/withdrawal composition, zero physical egress."""

import httpx
import pytest
from sa09_support import ingest_case, prepared, setup
from sa21_support import cleartext_in_database, observation, withdraw
from test_super_agent_sa09 import frames, live

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_eval import SourceEvalProvider
from mgar_v3.super_agent.source_eval_model import SourceEvalModel


@pytest.mark.parametrize("fault", ["lost_stream", "invalid_proposal"])
def test_model_input_audit_never_retains_body_and_withdrawal_cannot_retry_unknown_or_invalid_model(
    engine, fault, caplog
):
    env = setup(engine)
    permit, profile = live(env)
    private = "SA21_MODEL_PRIVATE_BODY_" + fault
    receipt = ingest_case(env, body=private)
    batch = prepared(env, receipt, mode="live")
    sent = []

    def controlled(request):
        sent.append(request)
        assert private in request.content.decode()
        if fault == "lost_stream":
            raise httpx.ReadTimeout("SA21 synthetic stream lost without a usage receipt")
        group = str(batch["source_snapshot"]["groups"][0]["group_id"])
        return httpx.Response(200, content=frames(group, invalid=True))

    with httpx.Client(transport=httpx.MockTransport(controlled), trust_env=False) as client:
        model = SourceEvalModel(profile, permit, client=client, api_key="SA21-synthetic-key")
        provider = SourceEvalProvider(env.service, model)
        with pytest.raises(DomainError):
            provider.propose(batch)
        assert len(sent) == 1 and cleartext_in_database(engine, private) == []
        with engine.transaction() as conn:
            calls_before = conn.execute("SELECT * FROM app.router_calls").fetchall()
            assert len(calls_before) == 1
            assert not conn.execute("SELECT 1 FROM app.tickets").fetchone()
        withdraw(env, receipt.input_ids[0], "policy")
        for _ in range(2):
            with pytest.raises(DomainError):
                provider.propose(batch)
        with engine.transaction() as conn:
            calls_after = conn.execute("SELECT * FROM app.router_calls").fetchall()
            assert calls_after == calls_before
            assert not conn.execute("SELECT 1 FROM app.actions").fetchone()
        assert len(sent) == 1 and cleartext_in_database(engine, private) == []
        assert private not in caplog.text
    observation(
        "model_loss_and_withdrawal", fault=fault, physical_requests=0, controlled_requests=1
    )
