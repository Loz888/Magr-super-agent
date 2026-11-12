"""Additional real-PG API queueing and independent follow-up policy boundaries."""

from fastapi.testclient import TestClient
from sa08_support import original_state, policy
from sa09_support import fast_batch, ingest_case, prepared, proposals, setup
from test_super_agent_sa08 import controller

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError
from mgar_v3.web_access import SESSION_COOKIE, WebAccounts

ORIGIN = "http://127.0.0.1:18080"


def money(env):
    with env.engine.transaction() as conn:
        return {
            table: conn.execute("SELECT * FROM app." + table + " ORDER BY 1,2").fetchall()
            for table in (
                "usage_calls",
                "router_calls",
                "authorization_budgets",
                "budgets",
                "sa_budget_calls",
                "sa_budget_quota",
            )
        }


def test_enabled_api_evaluate_only_enqueues_without_semantic_or_business_execution(engine):
    env = setup(engine)
    receipt = ingest_case(env)
    identifier = fast_batch(env, receipt)
    session = WebAccounts(engine).session(env.cookie)
    assert session is not None
    before_business, before_money = original_state(engine), money(env)
    app = create_app(engine.settings, engine, run_background=False)
    path = "/api/v3/source-eval/" + str(env.installation.scope.source_instance_id) + "/evaluate"
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        client.cookies.set(SESSION_COOKIE, env.cookie)
        response = client.post(
            path,
            json={"fast_batch_ids": [str(identifier)]},
            headers={"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token},
        )
    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    value = response.json()
    assert value["semantic_evaluation"] == "NOT_RUN" and value["executes_tasks"] is False
    assert len(value["evaluations"]) == 1
    assert value["evaluations"][0]["status"] == "prepared"
    assert value["evaluations"][0]["fast_batch_id"] == str(identifier)
    assert original_state(engine) == before_business and money(env) == before_money
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.sa_source_eval_snapshots").fetchone()["n"]
            == 1
        )
        for table in ("sa_source_eval_results", "sa_source_eval_decisions"):
            assert conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"] == 0


def test_exact_controller_without_follow_up_admission_cannot_supply_attach_targets(engine, trigger):
    env = setup(engine)
    task = controller(env, trigger)
    admission = env.definition.admission.model_dump(mode="json")
    admission["request_kinds"] = ["explicit_request"]
    policy(env, admission=admission)
    with engine.transaction() as conn:
        binding = conn.execute(
            "SELECT roles FROM app.source_role_bindings WHERE binding_id=%s AND active",
            (env.binding["binding_id"],),
        ).fetchone()
        assert binding is not None and "task_controller" in binding["roles"]
        assert (
            conn.execute(
                "SELECT 1 FROM app.source_task_controllers WHERE binding_id=%s AND ticket_id=%s "
                "AND project_id=%s AND active",
                (env.binding["binding_id"], task.ticket_id, engine.settings.project_id),
            ).fetchone()
            is not None
        )
    assert "follow_up" not in env.definition.admission.request_kinds
    # Policy changes invalidate earlier source content; evaluate a genuinely fresh input.
    receipt = ingest_case(env, entity="current-explicit-only", delivery="current-explicit-only")
    before_business, before_money = original_state(engine), money(env)
    batch = prepared(env, receipt)
    group = batch["source_snapshot"]["groups"][0]
    denied = None
    try:
        env.service.wrap(batch, proposals(batch, "attach", target=task.ticket_id))
    except DomainError as error:
        denied = error.code
    assert original_state(engine) == before_business and money(env) == before_money
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.sa_source_eval_results").fetchone()["n"]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM app.sa_source_eval_decisions").fetchone()["n"]
            == 0
        )
    assert group["targets"] == [] and "attach" not in group["allowed_actions"]
    assert denied in {"SOURCE_EVAL_ACTION_DENIED", "SOURCE_EVAL_TARGET_DENIED"}
