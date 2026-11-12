"""Original-ledger compatibility for incomplete historical token receipts."""

import json

import pytest
from sa07_support import (
    ORGANIZATION,
    configure,
    definition,
    ledger_snapshot,
    repository,
    router,
    view,
    worker,
)
from sa07_support import budget_env as budget_env

from mgar_v3.domain import DomainError


@pytest.mark.parametrize("entrypoint", [worker, router])
@pytest.mark.parametrize("receipt", [["PRIVATE_LEGACY_USAGE"], "PRIVATE_LEGACY_USAGE", 17, True])
def test_non_object_original_usage_keeps_token_reservation_without_breaking_view(
    budget_env, entrypoint, receipt,
):
    env = budget_env
    entry = entrypoint(env)
    configure(env, project=definition(
        organization_id=ORGANIZATION, caps={"day_tokens": entry.tokens},
    ))
    call = entry.reserve("malformed-historical-usage")
    # Original settlement accepts arbitrary JSON usage; SA07 must conservatively
    # account for existing receipts without rewriting original financial truth.
    entry.settle(call, 0, receipt)
    result = view(env)
    assert result["usage"]["total_micros"] == 0
    assert result["usage"]["unknown_calls"] == 0
    assert result["usage"]["day_tokens"] == entry.tokens
    assert result["usage"]["unknown_token_calls"] == 1
    assert "PRIVATE_LEGACY_USAGE" not in json.dumps(result, default=str)
    blocked = worker(env)
    before = ledger_snapshot(env)
    with pytest.raises(DomainError) as error:
        blocked.reserve("missing-receipt-cannot-free-token-capacity")
    assert error.value.code == "PROJECT_BUDGET_EXHAUSTED"
    assert ledger_snapshot(env) == before


@pytest.mark.parametrize("state", ["reserved", "unknown", "known_missing_usage"])
def test_reliable_original_repository_token_reservation_survives_first_shared_budget(
    budget_env, state,
):
    env = budget_env
    historical = repository(env)
    call = historical.reserve("before-shared-budget")
    if state == "unknown":
        historical.settle(call, None)
    elif state == "known_missing_usage":
        historical.settle(call, 0)
    later = worker(env)
    cap = historical.tokens + later.tokens
    configure(env, project=definition(
        organization_id=ORGANIZATION,
        caps={"total_tokens": cap, "hour_tokens": cap, "day_tokens": cap},
    ))
    before = view(env)["usage"]
    assert before["total_tokens"] == historical.tokens
    assert before["day_tokens"] == historical.tokens
    assert before["unknown_token_calls"] == 1
    later.reserve("new-budget-retains-known-historical-upper-bound")
    assert view(env)["usage"]["total_tokens"] == cap
    blocked = worker(env)
    snapshot = ledger_snapshot(env)
    with pytest.raises(DomainError) as error:
        blocked.reserve("historical-allocation-is-still-charged")
    assert error.value.code == "PROJECT_BUDGET_EXHAUSTED"
    assert ledger_snapshot(env) == snapshot


def test_legacy_call_without_reliable_token_upper_bound_still_blocks_new_spend(budget_env):
    env = budget_env
    historical = worker(env)
    call = historical.reserve("legacy-token-envelope-absent")
    historical.settle(call, 0)
    configure(env)
    before = view(env)["usage"]
    assert before["unknown_token_calls"] == 1
    with pytest.raises(DomainError) as error:
        worker(env).reserve("do-not-invent-missing-old-token-usage")
    assert error.value.code == "PROJECT_BUDGET_EXHAUSTED"
