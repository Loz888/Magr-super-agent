"""A durable reservation is an attempted call, not proof of a physical POST."""

from sa08_support import original_state
from sa09_support import prepared, setup
from test_super_agent_sa09 import live

from mgar_v3.routing.budget import RouterBudget


def test_reserved_before_send_is_reported_as_unconfirmed_attempt_not_physical_call(engine):
    env = setup(engine)
    permit, _ = live(env)
    batch = prepared(env, mode="live")
    before = original_state(engine)
    posts = []
    # This is the actual durable financial boundary used before a Provider POST.
    # No model/client is constructed, so this test cannot issue a request.
    call_id = RouterBudget(engine).reserve(batch, permit, "a" * 64)
    after = original_state(engine)
    assert not posts
    assert {k: v for k, v in before.items() if k != "router_calls"} == {
        k: v for k, v in after.items() if k != "router_calls"
    }
    with engine.transaction() as conn:
        call = conn.execute(
            "SELECT * FROM app.router_calls WHERE call_id=%s", (call_id,),
        ).fetchone()
        assert call["status"] == "reserved" and call["settled_micros"] is None
    metrics = env.service.metrics(env.cookie, env.installation.scope.source_instance_id)
    assert "physical_model_calls" not in metrics
    assert metrics["recorded_model_attempts"] == 1
    assert metrics["known_billed_calls"] == 0
    assert metrics["unconfirmed_model_attempts"] == 1
    assert metrics["calls_by_status"] == {"reserved": 1}
    assert metrics["settled_cost_micros"] == 0 and metrics["held_micros"] > 0
