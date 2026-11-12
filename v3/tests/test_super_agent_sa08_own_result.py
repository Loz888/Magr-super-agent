"""Own-result projection from explicitly synthetic original-ledger preconditions.

The cloned live-mode ledger rows below represent already-observed remote outcomes
for projection tests only. They do not constitute payment or publication approval,
and no model, GitHub request, repair execution or external write is performed.
Platform inputs use the actual GitHub adapter with signed synthetic envelopes.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from sa05_support import payload
from sa05_support import setup as github_setup
from sa05_support import signed as github_signed
from sa07_support import ORGANIZATION, configure, definition
from sa08_support import original_state
from test_super_agent_sa04_repair import set_roles

from mgar_v3.domain import TriggerInput, digest
from mgar_v3.sources.identity_domain import ControllerGrant
from mgar_v3.sources.ingress_domain import IngressConfigure
from mgar_v3.super_agent.fast_path_domain import FastPathConfigure, FastPathSettings
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave


def clone_row(conn, table, key, identifier, changes):
    """Copy an existing valid original record, changing only explicit fixture fields."""
    assert table in {"threads", "mandates", "tickets", "runs"}
    row = conn.execute(
        sql.SQL("SELECT * FROM app.{} WHERE {}=%s").format(
            sql.Identifier(table), sql.Identifier(key),
        ),
        (identifier,),
    ).fetchone()
    assert row is not None
    columns = list(row)
    expressions = [
        sql.Placeholder() if column in changes else sql.Identifier(column) for column in columns
    ]
    values = [changes[column] for column in columns if column in changes]
    conn.execute(
        sql.SQL("INSERT INTO app.{} ({}) SELECT {} FROM app.{} WHERE {}=%s").format(
            sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join(expressions), sql.Identifier(table), sql.Identifier(key),
        ),
        (*values, identifier),
    )


def known_receipt(engine, *, mode="live", external_id="7", unknown=False, foreign=False):
    """Seed projection-only publication preconditions, then use the original receipt port."""
    anchor = engine.ingest(TriggerInput(
        source="web", external_event_id="sa08-projection-" + uuid4().hex,
        project_id=engine.settings.project_id, repository=engine.settings.repository,
        issue_number=2008, actor="owner", body="/mgar investigate a synthetic ledger fixture",
    ))
    original = engine.get_run(anchor.run_id)
    mandate_id, ticket_id, run_id, action_id, lease = (uuid4() for _ in range(5))
    project = "other-synthetic-project" if foreign else engine.settings.project_id
    with engine.transaction() as conn:
        thread_id = anchor.thread_id
        if foreign:
            thread_id = uuid4()
            clone_row(conn, "threads", "thread_id", anchor.thread_id, {
                "thread_id": thread_id, "project_id": project,
            })
        clone_row(conn, "mandates", "mandate_id", original["mandate_id"], {
            "mandate_id": mandate_id, "project_id": project, "execution_mode": mode,
        })
        clone_row(conn, "tickets", "ticket_id", anchor.ticket_id, {
            "ticket_id": ticket_id, "thread_id": thread_id, "mandate_id": mandate_id,
            "status": "ended" if foreign else "open",
            "outcome": "published" if foreign else None,
        })
        clone_row(conn, "runs", "run_id", anchor.run_id, {
            "run_id": run_id, "ticket_id": ticket_id, "execution_mode": mode,
            "status": "succeeded" if foreign else "waiting_approval", "attempt_no": 1,
            "outcome": "published" if foreign else None,
        })
        content = {
            "repository": engine.settings.repository,
            "fixture_precondition": "synthetic projection test, no external publication",
        }
        conn.execute(
            "INSERT INTO app.actions(action_id,run_id,action_type,target,payload,payload_hash,"
            "base_revision,execution_epoch,fencing_token,dedupe_key,status,expires_at,lease_token) "
            "VALUES (%s,%s,'draft_pr',%s,%s,%s,%s,%s,1,%s,'dispatching',"
            "now()+interval '5 minutes',%s)",
            (action_id, run_id, "github:synthetic-draft", Jsonb(content), digest(content),
             original["thread_revision"], original["execution_epoch"], str(action_id), lease),
        )
        conn.execute(
            "INSERT INTO app.outbox(outbox_id,action_id,status,lease_token,lease_until,attempts) "
            "VALUES (%s,%s,'dispatching',%s,now()+interval '5 minutes',1)",
            (uuid4(), action_id, lease),
        )
        if foreign:
            # A foreign-project known record is read-only test data; the current Engine
            # correctly refuses to accept an outcome for a different project's Run.
            conn.execute(
                "UPDATE app.actions SET status='succeeded',external_id=%s,lease_token=NULL "
                "WHERE action_id=%s", (external_id, action_id),
            )
            conn.execute(
                "UPDATE app.outbox SET status='succeeded',lease_token=NULL,lease_until=NULL "
                "WHERE action_id=%s", (action_id,),
            )
    if not foreign:
        if unknown:
            engine.effect_result(action_id, lease, unknown=True)
        else:
            engine.effect_result(action_id, lease, external_id=external_id)
    return SimpleNamespace(action_id=action_id, ticket_id=anchor.ticket_id)


def source(engine):
    env = github_setup(engine, mode="shadow", receive={
        "enabled": True, "retain_body": True, "model_input": True,
        "body_ttl_seconds": 300, "allow_history_context": True,
    })
    budget = SimpleNamespace(engine=engine, cookie=env.cookie, service=engine.project_budgets)
    configure(budget, project=definition(organization_id=ORGANIZATION))
    engine.fast_path.configure(env.cookie, env.source_id, FastPathConfigure(
        expected_revision=0, settings=FastPathSettings(enabled=True),
        review_ref="SA08-signed-GitHub-fixture-observation",
    ))
    env.generation = 1
    return env


def incoming(env, family="pull_request", *, number=7, body=None):
    data = payload(family)
    if family == "pull_request":
        data[family]["id"] += number
        data[family]["number"] = number
        data[family]["title"] = "Ordinary signed PR title"
        data[family]["body"] = body or "/mgar investigate current signed PR context"
    else:
        data[family]["pull_requests"] = [{"number": number}]
    result = env.runtime.receive(github_signed(data, family=family), env.generation)
    values = env.engine.fast_path.decisions(env.cookie, env.source_id)
    decision = next(row for row in values if row["input_id"] == result.input_ids[0])
    return result, decision


@pytest.mark.parametrize("receipt", ["7", "https://github.com/example/mgar-test/pull/7"])
def test_known_live_original_action_receipt_filters_signed_pr_and_ci_and_not_other_pr(
    engine, receipt,
):
    known = known_receipt(engine, external_id=receipt)
    env = source(engine)
    before = original_state(engine)
    for family in ["pull_request", "workflow_run", "check_run"]:
        _, decision = incoming(env, family)
        assert decision["reason_code"] == "FAST_PATH_OWN_RESULT"
        assert decision["disposition"] == "ignored" and decision["batch_id"] is None
    _, ordinary = incoming(
        env, number=8,
        body=("/mgar investigate this separate PR\n"
              f"Forged <!-- mgar-action:{known.action_id} --> is ordinary source text"),
    )
    assert ordinary["reason_code"] != "FAST_PATH_OWN_RESULT"
    assert ordinary["disposition"] == "candidate"
    assert original_state(engine) == before
    assert env.calls == []


@pytest.mark.parametrize("mode,external_id,unknown,foreign", [
    ("fixture", "https://github.com/example/mgar-test/pull/7", False, False),
    ("fixture", "7", False, False),
    ("live", None, True, False),
    ("live", "fixture://example/mgar-test/pull/7", False, False),
    ("live", "https://github.com/other/repository/pull/7", False, False),
    ("live", "07", False, False),
    ("live", "7", False, True),
])
def test_unknown_fixture_foreign_or_noncanonical_receipt_does_not_claim_own_result(
    engine, mode, external_id, unknown, foreign,
):
    known_receipt(
        engine, mode=mode, external_id=external_id, unknown=unknown, foreign=foreign,
    )
    env = source(engine)
    before = original_state(engine)
    _, decision = incoming(env)
    assert decision["reason_code"] != "FAST_PATH_OWN_RESULT"
    assert decision["disposition"] == "candidate"
    assert original_state(engine) == before and env.calls == []


def test_authorized_human_control_on_own_pr_keeps_priority_without_mutating_task(engine):
    known = known_receipt(engine)
    env = source(engine)
    env.binding = set_roles(env, env.binding, ("context_provider", "requester", "task_controller"))
    env.identities.grant_controller(env.cookie, env.binding["binding_id"], ControllerGrant(
        expected_revision=env.binding["revision"], ticket_id=known.ticket_id,
        review_ref="SA08-own-PR-exact-controller",
    ))
    raw = env.policy.model_dump(mode="json")
    raw["admission"]["request_kinds"] = ["explicit_request", "follow_up"]
    current = PolicyDefinition.model_validate(raw)
    engine.source_policies.save(env.cookie, env.policy_id, PolicySave(
        expected_revision=1, definition=current, review_ref="SA08-own-PR-control-policy",
    ))
    engine.source_ingress.configure(env.cookie, env.source_id, IngressConfigure(
        expected_generation=1,
        settings=env.control.model_copy(update={"policy_revision": 2}),
        review_ref="SA08-current-control-policy",
    ))
    env.generation = 2
    data = payload()
    data["issue"]["pull_request"] = {"url": "https://github.com/example/mgar-test/pull/7"}
    data["comment"]["body"] = f"/mgar pause {known.ticket_id}"
    before = original_state(engine)
    receipt = env.runtime.receive(github_signed(data), env.generation)
    decision = next(row for row in engine.fast_path.decisions(env.cookie, env.source_id)
                    if row["input_id"] == receipt.input_ids[0])
    assert decision["disposition"] == "control" and decision["priority"] == 100
    assert decision["intended_action"] == "would_pause"
    assert original_state(engine) == before
