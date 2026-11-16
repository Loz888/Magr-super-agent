from dataclasses import replace

import psycopg
import pytest
from sa13_runtime_support import BAD
from sa22_coding_support import coding_environment

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_coding_experiment import explicit_coding_request


def test_original_trial_factory_model_broker_and_unverified_patch(engine, tmp_path):
    env = coding_environment(engine, tmp_path)
    try:
        target = env.experiment.submit(env.input_id)
        assert env.experiment.submit(env.input_id)["run_id"] == target["run_id"]
        result = env.experiment.run_one()
        assert result and result["changed"]
        assert env.original.joinpath("src/calc.py").read_text() == BAD
        assert (
            "return sum(values)"
            in env.experiment.artifacts.joinpath(str(result["run_id"]), "patch.diff").read_text()
        )
        with engine.transaction() as c:
            calls = c.execute("SELECT * FROM app.usage_calls").fetchall()
            assert calls and all(x["status"] == "known" for x in calls)
            assert len(calls) == len(env.requests)
            assert (
                c.execute("SELECT execution_mode FROM app.sa_coding_bindings").fetchone()[
                    "execution_mode"
                ]
                == "live"
            )
            assert (
                c.execute(
                    "SELECT count(*) AS n FROM app.sa_coding_verifications WHERE result='passed'"
                ).fetchone()["n"]
                == 0
            )
            assert (
                c.execute("SELECT state FROM app.sa_scheduler_assignments").fetchone()["state"]
                == "released"
            )
        assert env.experiment.profile.harness.h12.live_agent == "FAIL"
    finally:
        env.close()


def test_sparse_trial_journal_supports_large_unchanged_repository(engine, tmp_path):
    env = coding_environment(
        engine,
        tmp_path,
        extra_files={"assets/one.txt": "a" * 600000, "assets/two.txt": "b" * 600000},
    )
    try:
        env.experiment.submit(env.input_id)
        result = env.experiment.run_one()
        assert result and result["changed"]
        assert env.original.joinpath("assets/one.txt").read_text() == "a" * 600000
        with engine.transaction() as c:
            sizes = c.execute(
                "SELECT plaintext_bytes FROM app.sa_coding_payloads WHERE payload_kind='checkpoint'"
            ).fetchall()
            assert sizes and all(x["plaintext_bytes"] < 1048576 for x in sizes)
    finally:
        env.close()


def test_live_trial_profile_cannot_use_fixture_registration_or_forged_quality(engine, tmp_path):
    env = coding_environment(engine, tmp_path)
    try:
        trial = env.experiment.profile
        with pytest.raises(DomainError):
            engine.source_coding.register_profile(replace(trial, trial_id=None))
        forged = replace(
            trial,
            harness=trial.harness.model_copy(
                update={
                    "h12": trial.harness.h12.model_copy(
                        update={"live_agent": "PASS", "mvp": "PASS"}
                    )
                }
            ),
        )
        with pytest.raises(DomainError):
            engine.source_coding.register_profile(forged)
        assert env.requests == []
    finally:
        env.close()


def test_trial_permission_is_immutable_and_policy_cutover_fences_new_tasks(engine, tmp_path):
    env = coding_environment(engine, tmp_path)
    try:
        with pytest.raises(psycopg.Error):
            with engine.transaction() as c:
                c.execute(
                    "UPDATE app.sa_coding_trial_authorizations SET maximum_project_micros=4000000"
                )
        current = engine.source_policies.read(env.source.cookie, env.source.policy_id)
        raw = current["definition"]
        raw["execution"]["enabled"] = False
        engine.source_policies.save(
            env.source.cookie,
            env.source.policy_id,
            PolicySave(
                expected_revision=4,
                definition=PolicyDefinition.model_validate(raw),
                review_ref="owner-stops-trial-policy",
            ),
        )
        with pytest.raises(DomainError):
            env.experiment.submit(env.input_id)
        assert env.requests == []
        with engine.transaction() as c:
            assert (
                c.execute("SELECT count(*) AS n FROM app.sa_coding_bindings").fetchone()["n"] == 0
            )
            assert (
                c.execute(
                    "SELECT maximum_project_micros FROM app.sa_coding_trial_authorizations"
                ).fetchone()["maximum_project_micros"]
                == 2000000
            )
    finally:
        env.close()


def test_formal_0054_matches_exact_coordinated_sql_and_installs_guards(engine):
    import hashlib

    from mgar_v3.settings import ROOT

    assert (
        hashlib.sha256((ROOT / "migrations/0054_source_coding_trial.sql").read_bytes()).hexdigest()
        == "094909bf76fe89917c4733882719f389003208ffdec4baa196461ea609ef6f48"
    )
    with engine.transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM app.schema_migrations").fetchone()["n"] == 51
        text = c.execute(
            "SELECT pg_get_functiondef('app.sa_coding_binding_guard()'::regprocedure) AS text"
        ).fetchone()["text"]
        assert "trial.signed_by=NEW.signed_by" in text and "trial.h12_evidence_sha256" in text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("@_user_1 请修改 v3/src/example.py", True),
        ("请解释如何修改代码", False),
        ("仓库日志说：请修改数据库", False),
        ("/code fix a bug", True),
    ],
)
def test_coding_requires_explicit_owner_request_language(text, expected):
    assert explicit_coding_request(text) is expected
