"""Budget administration isolation and atomic signed changes on real PostgreSQL."""

import pytest
from test_super_agent_sa07_api import (
    PREFIX,
    authenticate,
    budget_counts,
    budget_request,
    save_project,
)
from test_super_agent_sa07_api import (
    budget_browser as budget_browser,
)

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.budget import SharedBudgets


def signed_rows(env):
    with env.engine.transaction() as conn:
        return {
            table: conn.execute("SELECT * FROM app." + table + " ORDER BY 1,2").fetchall()
            for table in ("sa_budget_policies", "sa_budget_versions", "sa_budget_audit")
        }


def test_budget_unconfigured_view_reports_no_semantic_evaluation_without_writes(budget_browser):
    env = budget_browser
    authenticate(env)
    before = budget_counts(env)
    response = env.client.get(PREFIX)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    view = response.json()
    assert view["configured"] is False
    assert view["current"] == {"revision": 0, "definition": None}
    assert view["router_semantics"] == "NOT_RUN_NO_SHARED_BUDGET"
    assert budget_counts(env) == before


@pytest.mark.parametrize("method", ["get", "put"])
def test_budget_other_existing_organization_is_inaccessible(budget_browser, method):
    env = budget_browser
    authenticate(env)
    foreign = PREFIX + "/organization/Other-Reviewed-Organization"
    response = env.client.put(foreign, json=budget_request())
    assert response.status_code == 200, response.text
    save_project(env)
    before = signed_rows(env)
    response = (
        env.client.get(foreign)
        if method == "get"
        else env.client.put(foreign, json=budget_request(revision=1))
    )
    assert response.status_code == 403
    assert response.json()["code"] == "BUDGET_SCOPE_DENIED"
    assert signed_rows(env) == before


def test_budget_project_organization_cannot_be_rebound(budget_browser):
    env = budget_browser
    authenticate(env)
    foreign_id = "Other-Reviewed-Organization"
    assert env.client.put(
        PREFIX + "/organization/" + foreign_id, json=budget_request()
    ).status_code == 200
    save_project(env)
    before = signed_rows(env)
    response = env.client.put(
        env.project_path, json=budget_request(organization_id=foreign_id, revision=1)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "BUDGET_ORGANIZATION_IMMUTABLE"
    assert signed_rows(env) == before


@pytest.mark.parametrize("organization_id", [None, "Not-Configured-Organization"])
def test_budget_project_requires_existing_organization_without_partial_history(
    budget_browser, organization_id
):
    env = budget_browser
    authenticate(env)
    before = signed_rows(env)
    response = env.client.put(
        env.project_path, json=budget_request(organization_id=organization_id)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "BUDGET_ORGANIZATION_MISSING"
    assert signed_rows(env) == before


def test_budget_organization_cannot_receive_parent_project_binding(budget_browser):
    env = budget_browser
    authenticate(env)
    before = signed_rows(env)
    response = env.client.put(
        env.organization_path, json=budget_request(organization_id="Untrusted-Parent")
    )
    assert response.status_code == 422
    assert response.json()["code"] == "BUDGET_SCOPE_INVALID"
    assert signed_rows(env) == before


@pytest.mark.parametrize("existing", [False, True])
def test_budget_failed_signature_audit_rolls_back_head_and_history(
    budget_browser, monkeypatch, existing
):
    env = budget_browser
    authenticate(env)
    if existing:
        save_project(env)
    before = signed_rows(env)

    def fail_audit(service, conn, event, **details):
        # The real SQL head and history inserts must have happened before this fault.
        row = conn.execute(
            "SELECT revision FROM app.sa_budget_policies "
            "WHERE scope_type='organization' AND scope_id=%s",
            (env.organization,),
        ).fetchone()
        assert row["revision"] == (2 if existing else 1)
        raise DomainError("CONTROLLED_AUDIT_FAILURE", "Budget signing did not commit", 503)

    monkeypatch.setattr(SharedBudgets, "_audit", fail_audit)
    response = env.client.put(
        env.organization_path, json=budget_request(revision=1 if existing else 0)
    )
    assert response.status_code == 503
    assert response.json()["code"] == "CONTROLLED_AUDIT_FAILURE"
    assert signed_rows(env) == before
    assert budget_counts(env)["usage_calls"] == budget_counts(env)["router_calls"] == 0


def test_budget_positive_signed_plan_does_not_create_payment_authority(budget_browser):
    env = budget_browser
    authenticate(env)
    for path, organization_id in (
        (env.organization_path, None),
        (env.project_path, env.organization),
    ):
        request = budget_request(organization_id=organization_id)
        request["definition"].update(
            {
                "enabled": True,
                "caps": {"total_micros": 1000, "day_micros": 1000},
                "prices": [
                    {
                        "model_id": "controlled-api-plan",
                        "price_version": "controlled-v1",
                        "input_micros_per_million": 1,
                        "output_micros_per_million": 1,
                    }
                ],
            }
        )
        response = env.client.put(path, json=request)
        assert response.status_code == 200, response.text
        assert response.json()["current"]["definition"]["enabled"] is True
    assert not env.engine.settings.allow_paid
    assert not env.engine.settings.allow_external_write
    counts = budget_counts(env)
    for name in ("usage_calls", "router_calls", "sa_budget_calls", "sa_budget_quota"):
        assert counts[name] == 0
    with env.engine.transaction() as conn:
        for table in ("runs", "authorization_budgets", "actions", "outbox"):
            assert conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"] == 0
