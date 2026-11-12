"""Controlled original-ledger setup for SA07; no provider or platform requests."""

from datetime import UTC, datetime, timedelta
from itertools import count
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from mgar_v3.domain import TriggerInput
from mgar_v3.live import Authorization, LivePermit, LiveProfile
from mgar_v3.repository_analysis.domain import AnalysisLimits, CreateAnalysisInput
from mgar_v3.repository_analysis.service import RepositoryAnalysisService
from mgar_v3.routing.budget import RouterBudget
from mgar_v3.routing.live import RouterAuthorization, RouterPermit, RouterProfile
from mgar_v3.super_agent.budget_domain import BudgetDefinition, BudgetSave, PriceQuote
from mgar_v3.web_access import LoginInput, WebAccounts

BIG = 10**9
ORGANIZATION = "sa07-controlled-organization"
PRICE = PriceQuote(
    model_id="fixture-chat",
    price_version="fixture-v1",
    input_micros_per_million=1000000,
    output_micros_per_million=1000000,
)
REPO_PRICE = PriceQuote(
    model_id="deepseek-chat",
    price_version="repository-analysis-v1",
    input_micros_per_million=300000,
    output_micros_per_million=1200000,
)


def definition(*, organization_id=None, caps=None, quotas=None, **changes):
    values = {
        "enabled": True,
        "organization_id": organization_id,
        "valid_from": datetime(2020, 1, 1, tzinfo=UTC),
        "valid_until": datetime(2040, 1, 1, tzinfo=UTC),
        "timezone": "Asia/Shanghai",
        "prices": (PRICE, REPO_PRICE),
        "caps": {
            key: BIG
            for key in (
                "total_micros", "hour_micros", "day_micros", "router_day_micros",
                "run_micros", "total_tokens", "hour_tokens", "day_tokens", "run_tokens",
            )
        },
        "quotas": {
            "candidates_hour": 100, "queue": 100, "slots": 2,
            "source_minute": 100, "notifications_hour": 100,
        },
    }
    values["caps"].update(caps or {})
    values["quotas"].update(quotas or {})
    values.update(changes)
    return BudgetDefinition.model_validate(values)


@pytest.fixture
def budget_env(engine):
    engine.settings = engine.settings.model_copy(update={
        "allow_paid": True, "allow_external_write": False,
        "deepseek_api_key": SecretStr("controlled-sa07-key-not-a-real-credential"),
        "graph_version": "r2-mailbox-v1",
    })
    accounts = WebAccounts(engine)
    user = accounts.create_owner(LoginInput(
        username="sa07-owner", password=SecretStr("controlled-sa07-password"),
    ))
    return SimpleNamespace(
        engine=engine, service=engine.project_budgets,
        cookie=accounts.issue_session(user), sequence=count(1),
    )


def save(env, kind, scope_id, value, revision=0):
    return env.service.save(env.cookie, kind, scope_id, BudgetSave(
        expected_revision=revision, definition=value, review_ref="sa07-controlled-review",
    ))


def configure(env, *, project=None, organization=None):
    save(env, "organization", ORGANIZATION, organization or definition())
    save(env, "project", env.engine.settings.project_id,
         project or definition(organization_id=ORGANIZATION))


def view(env, kind="project"):
    key = env.engine.settings.project_id if kind == "project" else ORGANIZATION
    return env.service.inspect(env.cookie, kind, key)


def worker(env, *, issue=None, price=PRICE):
    e = env.engine
    issue = issue or next(env.sequence)
    p = LiveProfile(
        case_id=f"sa07-worker-{issue}", project_id=e.settings.project_id,
        repository=e.settings.repository, issue_number=issue, actor="owner",
        question="Quote controlled documented symptoms", source_ref="a" * 40,
        **price.model_dump(), context_tokens=4096, max_output_tokens=256,
        max_cost_micros=1000000, max_model_calls=3,
    )
    permit = LivePermit(p, Authorization(
        profile_hash=p.profile_hash, approved_by="owner", allow_paid=True,
        allow_external_write=False, expires_at=datetime.now(UTC) + timedelta(hours=1),
    ), e.settings)
    event = TriggerInput(
        source="github", external_event_id=uuid4().hex, project_id=e.settings.project_id,
        repository=e.settings.repository, issue_number=issue, actor="owner",
        body="/mgar investigate " + p.question, execution_mode="live",
    )
    receipt = e.ingest(event, permit)
    run = e.begin_run(receipt.run_id)
    return SimpleNamespace(
        run_id=run["run_id"], amount=p.reservation_micros,
        tokens=p.context_tokens + p.max_output_tokens, price=price,
        reserve=lambda step: e.reserve_call(run["run_id"], run["execution_epoch"], step, permit),
        settle=e.settle_call, ledger_kind="usage", permit=permit,
    )


def router(env):
    e = env.engine
    root = LiveProfile(
        case_id="sa07-router-root", project_id=e.settings.project_id,
        repository=e.settings.repository, issue_number=1, actor="owner",
        question="Quote controlled symptoms", source_ref="a" * 40,
        **PRICE.model_dump(), context_tokens=4096, max_output_tokens=256,
        max_cost_micros=1000000, max_model_calls=3,
    )
    p = RouterProfile(
        project_id=e.settings.project_id, repository=e.settings.repository, actor="owner",
        budget_profile_hash=root.profile_hash, **PRICE.model_dump(),
        context_tokens=4096, max_output_tokens=256,
    )
    permit = RouterPermit(p, RouterAuthorization(
        profile_hash=p.profile_hash, approved_by="owner", allow_paid=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    ), root, e.settings)
    e.ingest(TriggerInput(
        source="github", external_event_id=uuid4().hex, project_id=e.settings.project_id,
        repository=e.settings.repository, issue_number=next(env.sequence), actor="owner",
        body="@mgar Investigate controlled symptoms",
    ))
    batch = e.routing.prepare("live")
    assert batch is not None
    ledger = RouterBudget(e)
    return SimpleNamespace(
        run_id=None, amount=p.reservation_micros,
        tokens=p.context_tokens + p.max_output_tokens, price=PRICE, ledger_kind="router",
        reserve=lambda step: ledger.reserve(batch, permit, "a" * 64),
        settle=ledger.settle,
    )


def repository(env):
    e = env.engine
    service = RepositoryAnalysisService(e, e.settings)
    value = service.create(CreateAnalysisInput(
        repository_url="https://github.com/octo/example", task="Explain controlled startup",
        execution_mode="fixture", client_request_id=uuid4().hex,
        limits=AnalysisLimits(max_model_calls=None, max_tool_calls=None,
                              max_input_tokens=500000, max_total_tokens=500000),
    ))
    service.store.begin_run(value.run_id)
    spec = service.store.spec(value.run_id)
    return SimpleNamespace(
        run_id=spec.run_id, amount=spec.model.reservation_micros,
        tokens=spec.model.context_tokens + spec.model.max_output_tokens,
        price=REPO_PRICE, ledger_kind="usage", store=service.store,
        reserve=lambda step: service.store.reserve_repository_call(
            spec.run_id, spec.execution_epoch, step, spec.profile_hash,
            spec.model.reservation_micros, None,
            token_reservation=spec.model.context_tokens + spec.model.max_output_tokens,
        ),
        settle=service.store.settle_call,
    )


def ledger_snapshot(env):
    with env.engine.transaction() as conn:
        return {
            table: conn.execute("SELECT * FROM app." + table + " ORDER BY 1").fetchall()
            for table in ("usage_calls", "router_calls", "authorization_budgets", "budgets")
        }


def consume(env, kind, operation_key, **kwargs):
    with env.engine.transaction() as conn:
        return env.service.quota(conn, kind, operation_key, **kwargs)


def release(env, kind, operation_key):
    with env.engine.transaction() as conn:
        env.service.release_quota(conn, kind, operation_key)
