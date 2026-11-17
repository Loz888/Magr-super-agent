"""New owner ceiling revision: strict types, original PG and preserved money."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from pydantic import TypeAdapter, ValidationError
from sa22_coding_support import coding_environment
from sa22_model_support import model_environment

from mgar_v3.domain import DomainError, digest
from mgar_v3.super_agent.budget_domain import BudgetSave
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_coding_trial import CodingTrial, authorize_trial, current_trial
from mgar_v3.super_agent.source_trial_limits import OwnerTrialProjectLimit

PROPOSAL = (
    Path(__file__).resolve().parents[1] / "docs/super-agent-v1/sa22-proposal/"
    "budget-10-20261117/0055_source_trial_budget_ceiling.sql"
)


@pytest.mark.parametrize("value", [2_000_000, 10_000_000])
def test_explicit_supported_integer_ceiling(value):
    assert TypeAdapter(OwnerTrialProjectLimit).validate_python(value) == value


@pytest.mark.parametrize("value", [True, False, "10000000", 10000000.0, -1, 0, 7500000, 10000001])
def test_noninteger_or_unsupported_ceiling_cannot_create_authority(value):
    with pytest.raises(ValidationError):
        TypeAdapter(OwnerTrialProjectLimit).validate_python(value)


def increase_original_budget(engine, cookie):
    with engine.transaction() as conn:
        configs = engine.project_budgets._configs(conn)
    for row, definition in configs:
        caps = definition.caps.model_copy(
            update={
                "total_micros": 10_000_000,
                "hour_micros": 10_000_000,
                "day_micros": 10_000_000,
            }
        )
        engine.project_budgets.save(
            cookie,
            row["scope_type"],
            row["scope_id"],
            BudgetSave(
                expected_revision=row["revision"],
                definition=definition.model_copy(update={"caps": caps}),
                review_ref="new controlled SA22 owner USD10 ceiling; no ledger reset",
            ),
        )


@pytest.mark.parametrize("outcome", ["known", "unknown"])
def test_new_cap_and_profile_leave_original_known_or_unknown_usage_unchanged(engine, outcome):
    def supplier(request):
        raise httpx.ReadTimeout("controlled missing response", request=request)

    env = model_environment(engine, handler=supplier if outcome == "unknown" else None)
    try:
        if outcome == "unknown":
            with pytest.raises(DomainError):
                env.service.execute(env.input_id)
        else:
            env.service.execute(env.input_id)
        assert env.service.profile.maximum_project_micros == 2_000_000
        with engine.transaction() as conn:
            before = conn.execute("SELECT * FROM app.usage_calls").fetchall()
            attributions = conn.execute("SELECT * FROM app.sa_budget_calls").fetchall()
        increase_original_budget(engine, env.source.cookie)
        new_profile = type(env.service.profile).model_validate(
            {
                **env.service.profile.model_dump(),
                "maximum_project_micros": 10_000_000,
            }
        )
        assert new_profile.profile_hash != env.service.profile.profile_hash
        with engine.transaction() as conn:
            assert conn.execute("SELECT * FROM app.usage_calls").fetchall() == before
            assert conn.execute("SELECT * FROM app.sa_budget_calls").fetchall() == attributions
            configs = engine.project_budgets._configs(conn)
        assert len(env.model_calls) == 1
        for row, _ in configs:
            viewed = engine.project_budgets.inspect(
                env.source.cookie, row["scope_type"], row["scope_id"]
            )
            assert viewed["current"]["definition"]["caps"]["total_micros"] == 10_000_000
            expected = (
                before[0]["settled_micros"] if outcome == "known" else before[0]["reserved_micros"]
            )
            assert viewed["usage"]["total_micros"] == expected > 0
            assert before[0]["status"] == outcome
    finally:
        env.close()


def test_owned_pg_draft_requires_new_signed_trial_and_keeps_old_facts_immutable(engine, tmp_path):
    env = coding_environment(engine, tmp_path)
    try:
        with engine.transaction() as conn:
            assert (
                conn.execute("SELECT current_database() AS name")
                .fetchone()["name"]
                .startswith("mgar_v3_test_")
            )
            old_trial = conn.execute("SELECT * FROM app.sa_coding_trial_authorizations").fetchone()
            original = conn.execute(
                "SELECT pg_get_functiondef('app.sa_coding_binding_guard()'::regprocedure) AS text"
            ).fetchone()["text"]
            conn.execute(PROPOSAL.read_text())
            changed = conn.execute(
                "SELECT pg_get_functiondef('app.sa_coding_binding_guard()'::regprocedure) AS text"
            ).fetchone()["text"]
        assert changed == original.replace(
            "AND trial.maximum_project_micros=2000000",
            "AND trial.maximum_project_micros IN (2000000,10000000)",
        )
        assert old_trial["maximum_project_micros"] == 2_000_000
        increase_original_budget(engine, env.source.cookie)
        # A larger project config does not upgrade an old trial by implication.
        with engine.transaction() as conn, pytest.raises(DomainError):
            current_trial(engine, conn, env.experiment.profile)
        current = engine.source_policies.read(env.source.cookie, env.source.policy_id)
        document = current["definition"]
        document["budget"]["total_limit_micros"] = 10_000_000
        engine.source_policies.save(
            env.source.cookie,
            env.source.policy_id,
            PolicySave(
                expected_revision=current["current_revision"],
                definition=PolicyDefinition.model_validate(document),
                review_ref="controlled owner USD10 source policy; new signed trial required",
            ),
        )
        signed = CodingTrial.model_validate(
            {
                **{name: old_trial[name] for name in CodingTrial.model_fields},
                "trial_id": uuid4(),
                "policy_revision": current["current_revision"] + 1,
                "maximum_project_micros": 10_000_000,
                "review_sha256": digest("new-owner-ceiling"),
            }
        )
        authorize_trial(engine, env.source.cookie, signed)
        profile = replace(env.experiment.profile, trial_id=signed.trial_id, limit_micros=10_000_000)
        with engine.transaction() as conn:
            assert current_trial(engine, conn, profile)["maximum_project_micros"] == 10_000_000
            assert (
                conn.execute(
                    "SELECT * FROM app.sa_coding_trial_authorizations WHERE trial_id=%s",
                    (old_trial["trial_id"],),
                ).fetchone()
                == old_trial
            )
        with pytest.raises(psycopg.errors.RaiseException), engine.transaction() as conn:
            conn.execute(
                "DELETE FROM app.sa_coding_trial_authorizations WHERE trial_id=%s",
                (old_trial["trial_id"],),
            )
        malformed = signed.model_copy(
            update={
                "trial_id": uuid4(),
                "profile_sha256": digest("unsupported-budget"),
                "maximum_project_micros": 7_500_000,
            }
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            authorize_trial(engine, env.source.cookie, malformed)
        assert env.requests == []  # No model request or worker was run.
    finally:
        env.close()
