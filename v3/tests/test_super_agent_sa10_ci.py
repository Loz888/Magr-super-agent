"""CI/PR canonical claims and callback authority on disposable real PostgreSQL."""

import pytest
from sa05_support import payload
from sa10_support import counts, fresh_target, ingest_native, link, resolved, setup

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.ingress_domain import IngressConfigure
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicyRevoke, PolicySave
from mgar_v3.super_agent.source_correlation_domain import CorrelationLinkRequest


def ci_input(env, *, parents=(7,), run_id=701, authorize_creation=False):
    origin = env.sourceenv
    if authorize_creation:
        value = origin.policy.model_dump(mode="json")
        value["admission"].update(
            request_kinds=["explicit_request", "follow_up", "team_rule"],
            rule_ref="SA10-exact-synthetic-CI-rule",
            rule_sha256=digest(["SA10", "synthetic-approved-CI", list(parents)]),
        )
        definition = PolicyDefinition.model_validate(value)
        saved = env.engine.source_policies.save(
            env.cookie,
            origin.policy_id,
            PolicySave(
                expected_revision=origin.policy_revision,
                definition=definition,
                review_ref="SA10-owner-approved-CI-rule",
            ),
        )
        origin.policy, origin.policy_revision = definition, saved["current_revision"]
        origin.control = origin.control.model_copy(
            update={
                "policy_revision": origin.policy_revision,
            }
        )
        configured = env.engine.source_ingress.configure(
            env.cookie,
            origin.source_id,
            IngressConfigure(
                expected_generation=origin.generation,
                settings=origin.control,
                review_ref="SA10-owner-current-CI-ingress",
            ),
        )
        origin.generation = configured["generation"]
    raw = payload("workflow_run")
    raw["workflow_run"].update(
        id=run_id,
        pull_requests=[{"number": number} for number in parents],
    )
    receipt, resource = ingest_native(env, family="workflow_run", raw=raw)
    return receipt.input_ids[0], resource.resource_key


def test_ci_first_claim_also_claims_its_single_native_pr_before_any_later_factory(engine):
    env = setup(engine, create_target=False)
    input_id, key = ci_input(env, authorize_creation=True)
    calls = []

    def factory(conn):
        assert conn is engine.connection
        calls.append(1)
        return fresh_target(env)

    first = env.service.claim_or_create(env.cookie, (input_id,), key, factory)
    assert first["created"] is True and len(calls) == 1
    pr_receipt, pr = ingest_native(env, family="pull_request")
    second = env.service.claim_or_create(
        env.cookie,
        (pr_receipt.input_ids[0],),
        pr.resource_key,
        factory,
    )
    assert second["created"] is False and len(calls) == 1
    assert first["target"]["ticket_id"] == second["target"]["ticket_id"]
    assert counts(engine)["tickets"] == 1
    with engine.transaction() as conn:
        claims = conn.execute(
            "SELECT resource_key,correlation_id FROM app.sa_task_correlation_claims"
        ).fetchall()
    assert {row["resource_key"] for row in claims} == {key, pr.resource_key}
    assert len({row["correlation_id"] for row in claims}) == 1


def test_existing_pr_claim_prevents_a_second_task_via_a_new_ci_resource_key(engine):
    env = setup(engine, family="pull_request")
    linked = link(env)
    before = counts(engine)
    input_id, key = ci_input(env)
    calls = []

    def factory(conn):
        calls.append(1)
        return fresh_target(env, second=True)

    result = env.service.claim_or_create(env.cookie, (input_id,), key, factory)
    assert not calls and result["created"] is False
    assert result["target"]["ticket_id"] == linked["target"]["ticket_id"]
    assert counts(engine) == before
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(DISTINCT correlation_id) AS n FROM app.sa_task_correlation_claims"
            ).fetchone()["n"]
            == 1
        )


def test_multi_pr_ci_cannot_choose_one_target_or_create_one_task_for_both(engine):
    env = setup(engine, create_target=False)
    input_id, key = ci_input(env, parents=(7, 8), authorize_creation=True)
    calls = []

    def factory(conn):
        calls.append(1)
        return fresh_target(env)

    with pytest.raises(DomainError) as caught:
        env.service.claim_or_create(env.cookie, (input_id,), key, factory)
    assert "AMBIGUOUS" in caught.value.code
    assert not calls and counts(engine)["tickets"] == 0
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.sa_task_correlation_claims").fetchone()["n"]
            == 0
        )


def test_ci_child_cannot_be_linked_to_a_different_existing_pr_target(engine):
    env = setup(engine, family="pull_request")
    link(env)
    other = fresh_target(env, second=True)
    input_id, key = ci_input(env)
    snapshot = resolved(env, (input_id,))
    before = counts(engine)
    with engine.transaction() as conn:
        old = conn.execute(
            "SELECT * FROM app.sa_task_correlation_claims ORDER BY resource_key"
        ).fetchall()
    with pytest.raises(DomainError):
        env.service.link(
            env.cookie,
            CorrelationLinkRequest(
                input_ids=(input_id,),
                resource_key=key,
                expected_revision=0,
                snapshot_sha256=snapshot["snapshot_sha256"],
                target=other,
                review_ref="SA10-owner-cannot-override-native-parent-identity",
            ),
        )
    assert counts(engine) == before
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT * FROM app.sa_task_correlation_claims ORDER BY resource_key"
            ).fetchall()
            == old
        )


def test_factory_cannot_smuggle_an_unbound_live_run_out_of_the_fixture_creation_port(engine):
    env = setup(engine, create_target=False)
    before = counts(engine)

    def factory(conn):
        target = fresh_target(env)
        conn.execute("UPDATE app.runs SET execution_mode='live' WHERE run_id=%s", (target.run_id,))
        return target

    with pytest.raises(DomainError):
        env.service.claim_or_create(env.cookie, (env.native_input_id,), env.resource_key, factory)
    assert counts(engine) == before
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.sa_task_correlations").fetchone()["n"] == 0
        )


def test_factory_withdrawal_rolls_back_original_task_and_correlation_atomically(engine):
    env = setup(engine, create_target=False)
    before = counts(engine)

    def factory(conn):
        target = fresh_target(env)
        engine.source_policies.revoke(
            env.cookie,
            env.sourceenv.policy_id,
            PolicyRevoke(
                expected_revision=env.sourceenv.policy_revision,
                review_ref="SA10-trusted-callback-withdraws-source-before-commit",
            ),
        )
        return target

    with pytest.raises(DomainError):
        env.service.claim_or_create(env.cookie, (env.native_input_id,), env.resource_key, factory)
    assert counts(engine) == before
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.sa_task_correlations").fetchone()["n"] == 0
        )
        assert (
            conn.execute(
                "SELECT current_revision FROM app.source_policies WHERE policy_id=%s",
                (env.sourceenv.policy_id,),
            ).fetchone()["current_revision"]
            == env.sourceenv.policy_revision
        )
