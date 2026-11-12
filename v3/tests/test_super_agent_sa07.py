"""Shared budget behavior against real PG and the three original call ledgers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sa07_support import (
    ORGANIZATION,
    PRICE,
    configure,
    consume,
    definition,
    ledger_snapshot,
    release,
    repository,
    router,
    save,
    view,
    worker,
)
from sa07_support import budget_env as budget_env
from test_super_agent_sa02 import source

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore


def test_unconfigured_legacy_reservation_and_settlement_still_use_original_ledger(budget_env):
    env = budget_env
    entry = worker(env)
    call = entry.reserve("unconfigured-original")
    entry.settle(call, 2, {"prompt_tokens": 1, "completion_tokens": 1})
    result = view(env)
    assert result["configured"] is False
    assert result["router_semantics"] == "NOT_RUN_NO_SHARED_BUDGET"
    with env.engine.transaction() as conn:
        old = conn.execute("SELECT * FROM app.usage_calls WHERE call_id=%s", (call,)).fetchone()
        assert old["settled_micros"] == 2 and old["status"] == "known"
        assert conn.execute("SELECT count(*) AS n FROM app.sa_budget_calls").fetchone()["n"] == 0


@pytest.mark.parametrize("state", ["disabled", "zero"])
def test_disabled_or_zero_shared_budget_cannot_spend_existing_paid_permit(budget_env, state):
    env = budget_env
    entry = worker(env)
    caps = {"total_micros": 0} if state == "zero" else {}
    configure(env, project=definition(
        organization_id=ORGANIZATION, enabled=state != "disabled", caps=caps,
    ))
    before = ledger_snapshot(env)
    with pytest.raises(DomainError):
        entry.reserve("positive-old-grant-is-insufficient")
    assert ledger_snapshot(env) == before
    assert view(env)["usage"]["total_micros"] == 0


@pytest.mark.parametrize("scope", ["organization", "project"])
@pytest.mark.parametrize("cap", [
    "total_micros", "hour_micros", "day_micros", "run_micros",
    "total_tokens", "hour_tokens", "day_tokens", "run_tokens",
])
def test_each_org_and_project_cap_constrains_original_worker_transaction(budget_env, scope, cap):
    env = budget_env
    entry = worker(env)
    limit = entry.tokens if cap.endswith("tokens") else entry.amount
    tight = definition(
        organization_id=ORGANIZATION if scope == "project" else None, caps={cap: limit},
    )
    configure(env, **{scope: tight})
    entry.reserve("first-within-exact-cap")
    before = ledger_snapshot(env)
    with pytest.raises(DomainError) as error:
        entry.reserve("second-over-shared-cap")
    assert error.value.code == "PROJECT_BUDGET_EXHAUSTED"
    assert ledger_snapshot(env) == before
    assert view(env)["usage"]["total_micros"] == entry.amount
    assert view(env, "organization")["usage"]["total_tokens"] == entry.tokens


@pytest.mark.parametrize("first,second", [
    (worker, router), (router, repository), (repository, worker),
])
def test_worker_router_and_repository_entrypoints_share_one_total(budget_env, first, second):
    env = budget_env
    a, b = first(env), second(env)
    configure(env, project=definition(
        organization_id=ORGANIZATION, caps={"total_micros": a.amount},
    ))
    a.reserve("first-ledger")
    before = ledger_snapshot(env)
    with pytest.raises(DomainError) as error:
        b.reserve("other-ledger-cannot-borrow-new-wallet")
    assert error.value.code == "PROJECT_BUDGET_EXHAUSTED"
    assert ledger_snapshot(env) == before
    assert view(env)["usage"]["total_micros"] == a.amount


def test_router_daily_subcap_is_additional_to_project_total(budget_env):
    env = budget_env
    entry = router(env)
    configure(env, project=definition(
        organization_id=ORGANIZATION, caps={"router_day_micros": entry.amount - 1},
    ))
    before = ledger_snapshot(env)
    with pytest.raises(DomainError) as error:
        entry.reserve("router-subcap")
    assert error.value.code == "PROJECT_BUDGET_EXHAUSTED"
    assert ledger_snapshot(env) == before
    assert view(env)["usage"]["router_day_micros"] == 0


def test_concurrent_preoccupations_cannot_overspend_shared_money(budget_env):
    env = budget_env
    entry = worker(env)
    configure(env, project=definition(
        organization_id=ORGANIZATION, caps={"day_micros": entry.amount * 2},
    ))

    def reserve(index):
        try:
            return entry.reserve(f"concurrent:{index}")
        except DomainError as error:
            assert error.code == "PROJECT_BUDGET_EXHAUSTED"
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        accepted = [value for value in pool.map(reserve, range(8)) if value is not None]
    assert len(accepted) == 2
    usage = view(env)["usage"]
    assert usage["day_micros"] == entry.amount * 2
    assert usage["day_tokens"] == entry.tokens * 2
    with env.engine.transaction() as conn:
        old = conn.execute(
            "SELECT used_calls,reserved_micros FROM app.authorization_budgets"
        ).fetchone()
        assert old == {"used_calls": 2, "reserved_micros": entry.amount * 2}


@pytest.mark.parametrize("component", ["worker", "verification", "recall", "subtask"])
def test_component_attribution_uses_same_real_call_and_conflicts_cannot_relabel_it(
    budget_env, component, monkeypatch,
):
    env = budget_env
    entry = worker(env)
    configure(env)
    reserve = env.service.reserve
    # Exercise the genuine original INSERT, then attribute its ID with the chosen
    # server-side purpose in the same transaction. No synthetic fee row is created.
    with env.engine.transaction() as conn:
        with monkeypatch.context() as patch:
            patch.setattr(env.service, "reserve", lambda *args, **kwargs: None)
            call = entry.reserve("controlled-purpose")
        reserve(conn, "usage", call, price=entry.price, tokens=entry.tokens, component=component)
        reserve(conn, "usage", call, price=entry.price, tokens=entry.tokens, component=component)
    assert view(env)["usage"]["total_micros"] == entry.amount
    with pytest.raises(DomainError) as error, env.engine.transaction() as conn:
        reserve(conn, "usage", call, price=entry.price, tokens=entry.tokens, component="router")
    assert error.value.code == "BUDGET_CALL_CONFLICT"
    with env.engine.transaction() as conn:
        rows = conn.execute(
            "SELECT component FROM app.sa_budget_calls WHERE call_id=%s", (call,),
        ).fetchall()
        assert rows == [{"component": component}]


@pytest.mark.parametrize("change", [
    {"model_id": "different-model"}, {"price_version": "different-price"},
    {"input_micros_per_million": 1000001}, {"output_micros_per_million": 1000001},
])
def test_original_model_permit_cannot_use_unsigned_model_or_price(budget_env, change):
    env = budget_env
    entry = worker(env, price=PRICE.model_copy(update=change))
    configure(env)
    before = ledger_snapshot(env)
    with pytest.raises(DomainError) as error:
        entry.reserve("unsigned-price")
    assert error.value.code == "BUDGET_PRICE_DENIED"
    assert ledger_snapshot(env) == before


@pytest.mark.parametrize("entrypoint", [worker, router, repository])
def test_new_budget_failure_rolls_back_original_fees_and_attribution(
    budget_env, entrypoint, monkeypatch,
):
    env = budget_env
    entry = entrypoint(env)
    configure(env)
    before = ledger_snapshot(env)
    original = env.service.reserve
    attempted = []

    def fail_after_real_attribution(conn, kind, call_id, **kwargs):
        original(conn, kind, call_id, **kwargs)
        assert conn.execute(
            "SELECT 1 FROM app.sa_budget_calls WHERE ledger_kind=%s AND call_id=%s",
            (kind, call_id),
        ).fetchone()
        attempted.append(call_id)
        raise RuntimeError("controlled rollback after actual shared attribution")

    with monkeypatch.context() as patch:
        patch.setattr(env.service, "reserve", fail_after_real_attribution)
        with pytest.raises(RuntimeError, match="controlled rollback"):
            entry.reserve("atomic-rollback")
    assert len(attempted) == 1
    assert ledger_snapshot(env) == before
    assert view(env)["usage"]["total_micros"] == 0
    entry.reserve("atomic-rollback")  # The failed transaction did not consume this step.
    assert view(env)["usage"]["total_micros"] == entry.amount


@pytest.mark.parametrize("period", ["hour", "day"])
@pytest.mark.parametrize("status", ["reserved", "unknown"])
def test_unknown_occupancy_crosses_window_and_late_settlement_releases_once(
    budget_env, monkeypatch, period, status,
):
    env = budget_env
    at = [datetime(2026, 11, 11, 15, 59, 59, tzinfo=UTC)]
    monkeypatch.setattr(env.service, "now", lambda conn: at[0])
    entry = worker(env)
    configure(env)
    call = entry.reserve("cross-midnight")
    if status == "unknown":
        entry.settle(call, None)
    env.engine.cancel(entry.run_id)
    at[0] += timedelta(seconds=1)
    before = view(env)["usage"]
    assert before[period + "_micros"] == entry.amount
    assert before[period + "_tokens"] == entry.tokens
    assert before["unknown_calls"] == before["unknown_token_calls"] == 1
    entry.settle(call, 2, {"prompt_tokens": 1, "completion_tokens": 1})
    entry.settle(call, 2, {"prompt_tokens": 1, "completion_tokens": 1})
    after = view(env)["usage"]
    assert after["total_micros"] == after["total_tokens"] == 2
    assert after[period + "_micros"] == after[period + "_tokens"] == 0
    assert after["unknown_calls"] == after["unknown_token_calls"] == 0
    assert env.engine.get_run(entry.run_id)["status"] == "cancelled"
    with env.engine.transaction() as conn:
        old = conn.execute(
            "SELECT used_calls,settled_micros,reserved_micros FROM app.authorization_budgets"
        ).fetchone()
        assert old == {"used_calls": 1, "settled_micros": 2, "reserved_micros": 0}


def test_dst_repeated_wall_hour_is_a_distinct_known_usage_window(budget_env, monkeypatch):
    env = budget_env
    at = [datetime(2026, 11, 1, 5, 30, tzinfo=UTC)]  # 01:30 EDT.
    monkeypatch.setattr(env.service, "now", lambda conn: at[0])
    entry = worker(env)
    configure(env, project=definition(
        organization_id=ORGANIZATION, timezone="America/New_York",
        caps={"hour_micros": entry.amount},
    ))
    call = entry.reserve("first-fold")
    entry.settle(call, 2, {"prompt_tokens": 1, "completion_tokens": 1})
    assert view(env)["usage"]["hour_micros"] == 2
    at[0] = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 01:30 EST.
    assert view(env)["usage"]["hour_micros"] == 0
    assert view(env)["usage"]["day_micros"] == 2
    entry.reserve("second-fold")
    assert view(env)["usage"]["hour_micros"] == entry.amount


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 1}])
def test_known_zero_cost_without_token_receipt_keeps_token_capacity(budget_env, usage, monkeypatch):
    env = budget_env
    at = [datetime(2026, 11, 11, 15, 59, tzinfo=UTC)]
    monkeypatch.setattr(env.service, "now", lambda conn: at[0])
    entry = worker(env)
    configure(env, project=definition(
        organization_id=ORGANIZATION, caps={"day_tokens": entry.tokens},
    ))
    call = entry.reserve("known-cost-missing-tokens")
    entry.settle(call, 0, usage)
    at[0] += timedelta(minutes=2)
    current = view(env)["usage"]
    assert current["day_micros"] == 0 and current["unknown_calls"] == 0
    assert current["day_tokens"] == entry.tokens and current["unknown_token_calls"] == 1
    before = ledger_snapshot(env)
    with pytest.raises(DomainError) as error:
        entry.reserve("missing-token-receipt-is-not-zero")
    assert error.value.code == "PROJECT_BUDGET_EXHAUSTED"
    assert ledger_snapshot(env) == before


def test_known_legacy_fees_are_not_erased_when_shared_config_is_first_created(budget_env):
    env = budget_env
    entry = worker(env)
    call = entry.reserve("before-project-budget")
    entry.settle(call, 17, {"prompt_tokens": 10, "completion_tokens": 7})
    configure(env, project=definition(
        organization_id=ORGANIZATION, caps={"total_micros": 16},
    ))
    assert view(env)["usage"]["total_micros"] == 17
    before = ledger_snapshot(env)
    with pytest.raises(DomainError) as error:
        entry.reserve("historical-fees-still-occupy-total")
    assert error.value.code == "PROJECT_BUDGET_EXHAUSTED"
    assert ledger_snapshot(env) == before


def test_unknown_and_new_runs_survive_budget_disable_reenable_and_price_revision(budget_env):
    env = budget_env
    entry = worker(env)
    initial = definition(organization_id=ORGANIZATION)
    configure(env, project=initial)
    call = entry.reserve("immutable-old-allocation")
    entry.settle(call, None)
    env.engine.cancel(entry.run_id)
    save(env, "project", env.engine.settings.project_id,
         initial.model_copy(update={"enabled": False}), revision=1)
    changed = initial.model_copy(update={"prices": (PRICE.model_copy(
        update={"price_version": "fixture-new-revision"},
    ),)})
    save(env, "project", env.engine.settings.project_id, changed, revision=2)
    current = view(env)
    assert current["current"]["revision"] == 3
    assert current["usage"]["total_micros"] == entry.amount
    assert current["usage"]["total_tokens"] == entry.tokens
    # Creating another independently permitted Run cannot reset existing occupancy.
    later = worker(env)
    before = ledger_snapshot(env)
    with pytest.raises(DomainError):
        later.reserve("new-run-cannot-reset-old-unknown")
    assert ledger_snapshot(env) == before
    assert view(env)["usage"]["unknown_calls"] == 1


@pytest.mark.parametrize("kind,field", [
    ("candidate", "candidates_hour"), ("source", "source_minute"),
    ("notification", "notifications_hour"),
])
def test_window_quota_is_shared_idempotent_and_not_refundable(budget_env, kind, field, monkeypatch):
    env = budget_env
    at = [datetime(2026, 11, 11, 14, 0, 1, tzinfo=UTC)]
    monkeypatch.setattr(env.service, "now", lambda conn: at[0])
    installation = source(env.engine)
    extra = {"source_id": installation.scope.source_instance_id} if kind == "source" else {}
    configure(env, project=definition(organization_id=ORGANIZATION, quotas={field: 2}))
    assert consume(env, kind, "first", **extra) is True
    assert consume(env, kind, "first", **extra) is False
    with pytest.raises(DomainError) as error:
        consume(env, kind, "first", units=2, **extra)
    assert error.value.code == "PROJECT_QUOTA_CONFLICT"
    assert consume(env, kind, "second", **extra) is True
    with pytest.raises(DomainError) as error:
        consume(env, kind, "third", **extra)
    assert error.value.code == "PROJECT_QUOTA_EXHAUSTED"
    with pytest.raises(DomainError) as error:
        release(env, kind, "first")
    assert error.value.code == "PROJECT_QUOTA_RELEASE_DENIED"
    at[0] += timedelta(hours=1)
    assert consume(env, kind, "first", **extra) is False
    assert consume(env, kind, "new-window", units=2, **extra) is True


@pytest.mark.parametrize("kind,field", [("queue", "queue"), ("slot", "slots")])
def test_persistent_resource_quota_requires_release_and_never_replays_old_key(
    budget_env, kind, field,
):
    env = budget_env
    configure(env, project=definition(organization_id=ORGANIZATION, quotas={field: 1}))
    assert consume(env, kind, "occupied") is True
    with pytest.raises(DomainError):
        consume(env, kind, "blocked")
    # A rollback cannot manufacture a free slot or erase the occupied queue fact.
    with pytest.raises(RuntimeError), env.engine.transaction() as conn:
        env.service.release_quota(conn, kind, "occupied")
        raise RuntimeError("controlled release rollback")
    with pytest.raises(DomainError):
        consume(env, kind, "still-blocked")
    release(env, kind, "occupied")
    release(env, kind, "occupied")
    assert consume(env, kind, "occupied") is False
    assert consume(env, kind, "new-occupant") is True
    with pytest.raises(DomainError):
        consume(env, kind, "cannot-release-twice-for-another-slot")


def test_quota_concurrency_scope_and_organization_cap(budget_env):
    env = budget_env
    configure(env, organization=definition(quotas={"candidates_hour": 2}))

    def claim(index):
        try:
            return consume(env, "candidate", f"parallel:{index}")
        except DomainError as error:
            assert error.code == "PROJECT_QUOTA_EXHAUSTED"
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(claim, range(8))) == 2
    assert view(env, "organization")["quota_usage"]["candidate"] == 2
    for source_id in (None, uuid4()):
        with pytest.raises(DomainError) as error:
            consume(env, "source", "unknown-source", source_id=source_id)
        assert error.value.code == "PROJECT_QUOTA_SCOPE"


def test_real_recovery_and_engine_reopen_preserve_unknown_budget(budget_env):
    env = budget_env
    entry = worker(env)
    configure(env)
    call = entry.reserve("before-process-boundary")
    env.engine.recover()
    before = view(env)["usage"]
    assert before["unknown_calls"] == 1 and before["total_micros"] == entry.amount
    settings = env.engine.settings
    env.engine.close()
    reopened = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        result = reopened.project_budgets.inspect(env.cookie, "project", settings.project_id)
        assert result["usage"] == before
        reopened.settle_call(call, 2, {"prompt_tokens": 1, "completion_tokens": 1})
        current = reopened.project_budgets.inspect(env.cookie, "project", settings.project_id)
        assert current["usage"]["total_micros"] == current["usage"]["total_tokens"] == 2
        assert current["usage"]["unknown_calls"] == 0
        assert reopened.get_run(entry.run_id)["status"] != "running"
    finally:
        reopened.close()
