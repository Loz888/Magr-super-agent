"""First per-case source/profile isolation checks on real original PG/H03 state."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sa06_support import SENDER_ID, payload, signed
from sa22_coding_support import coding_environment

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.feishu_domain import FeishuActivation
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.sources.feishu_store import FeishuSources
from mgar_v3.super_agent.policy_domain import PolicyRevoke
from mgar_v3.super_agent.source_case_experiments import (
    SourceCaseExperiments,
    build_case_experiment,
)
from mgar_v3.super_agent.source_scheduler_domain import (
    SchedulerClaimRequest,
    SchedulerOwnerDrainRequest,
    SchedulerSessionRegisterRequest,
)


def cases(engine, tmp_path):
    env = coding_environment(engine, tmp_path)
    original = env.experiment
    second = build_case_experiment(
        engine,
        env.source.cookie,
        source_id=env.source.source_id,
        actor_id=SENDER_ID,
        source_root=env.original,
        root=tmp_path / "second-case",
        upstream_commit=original.profile.repository.base_commit,
        upstream_tree=original.profile.repository.upstream_tree,
        image_id=original.runtime_profile.driver.profile.image_id,
        price=original.runtime_profile.price,
        model=original.runtime_profile.model,
        expires=datetime.now(UTC) + timedelta(minutes=5),
        manifest_sha256=digest("second-case"),
        h12_evidence_sha256=original.profile.harness.h12.evidence_sha256,
        objective="完成本条受控来源第二项请求，测试保持不变。",
        baseline_command=(*original.runtime_profile.baseline_command, "-v"),
        working_directory="src",
        read_paths=original.profile.grant.read_paths,
        write_paths=original.profile.grant.write_paths,
        repository_id=1,
    )
    message = "请实现 src/calc.py 的第二项独立请求，保持测试不变 @_user_1"
    runtime = FeishuRuntime(
        FeishuSources(engine),
        env.source.source_id,
        2,
        4,
        env.source.secrets,
        FeishuActivation(
            env.source.source_id,
            2,
            4,
            datetime.now(UTC) + timedelta(minutes=5),
            frozenset({"webhook", "socket", "health"}),
            "case-profile-controlled",
        ),
        transport=httpx.MockTransport(env.source.handler),
    )
    received = runtime.receive(
        signed(env.source, payload(message_id="om_second_case_" + uuid4().hex, body=message))
    )
    assert received.disposition == "accepted"
    with engine.transaction() as conn:
        first = engine.source_correlations._input(conn, env.input_id).text
    manager = SourceCaseExperiments(
        engine, {"L01": (first, original), "L02": (message, second)}, original
    )
    return env, second, received.input_ids[0], manager


def test_second_profile_session_never_claims_older_first_profile_task(engine, tmp_path):
    env, second, second_input, manager = cases(engine, tmp_path)
    assignment_id = None
    try:
        first_target = manager.admit(env.input_id)
        second_target = manager.admit(second_input)
        assert first_target and second_target and first_target["run_id"] != second_target["run_id"]
        scheduler = engine.source_scheduler
        scheduler.driver = second.runtime_profile.driver
        session = scheduler.register_session(
            env.source.cookie,
            SchedulerSessionRegisterRequest(
                request_id=uuid4(),
                profile_sha256=second.profile.profile_sha256,
                ttl_seconds=300,
                review_ref="new second-case session cannot take older unrelated case",
            ),
        )
        with engine.transaction() as conn:
            revision = scheduler._configuration(conn, active=True).revision
        claimed = scheduler.claim(
            session["bearer"],
            SchedulerClaimRequest(
                request_id=uuid4(),
                session_id=session["session_id"],
                expected_config_revision=revision,
            ),
        )
        assignment_id = claimed["assignment_id"]
        assignment = scheduler.get_assignment(env.source.cookie, assignment_id)
        assert assignment["run_id"] == second_target["run_id"]
        with engine.transaction() as conn:
            first = conn.execute(
                "SELECT state FROM app.sa_scheduler_queue WHERE run_id=%s",
                (first_target["run_id"],),
            ).fetchone()
            assert first["state"] == "queued"
            assert (
                conn.execute("SELECT count(*) AS n FROM app.sa_coding_bindings").fetchone()["n"]
                == 2
            )
        assert env.requests == []
    finally:
        if assignment_id is not None:
            assignment = engine.source_scheduler.get_assignment(env.source.cookie, assignment_id)
            engine.source_scheduler.drain(
                env.source.cookie,
                SchedulerOwnerDrainRequest(
                    assignment_id=assignment_id,
                    expected_revision=assignment["revision"],
                    reason_code="OWNER_DRAIN",
                    review_ref="owned first-case-profile check cleanup",
                ),
            )
            engine.source_scheduler.maintain()
        env.close()


def test_two_inputs_persist_distinct_profiles_without_switching_active_driver_or_recreating(
    engine, tmp_path
):
    env, second, second_input, manager = cases(engine, tmp_path)
    try:
        driver = engine.source_scheduler.driver
        first = manager.admit(env.input_id)
        second_target = manager.admit(second_input)
        assert first and second_target
        assert manager.admit(second_input)["run_id"] == second_target["run_id"]
        assert engine.source_scheduler.driver is driver
        with engine.transaction() as conn:
            rows = conn.execute(
                "SELECT input_id,profile_sha256 FROM app.sa_coding_bindings"
            ).fetchall()
        assert {row["input_id"]: row["profile_sha256"] for row in rows} == {
            env.input_id: env.experiment.profile.profile_sha256,
            second_input: second.profile.profile_sha256,
        }
        assert env.requests == []
    finally:
        env.close()


def test_current_source_revocation_blocks_case_admission_before_any_task_or_model(engine, tmp_path):
    env, _, second_input, manager = cases(engine, tmp_path)
    try:
        engine.source_policies.revoke(
            env.source.cookie,
            env.source.policy_id,
            PolicyRevoke(expected_revision=4, review_ref="case admission revocation boundary"),
        )
        with pytest.raises(DomainError):
            manager.admit(second_input)
        with engine.transaction() as conn:
            assert not conn.execute("SELECT 1 FROM app.sa_coding_bindings").fetchone()
        assert env.requests == []
    finally:
        env.close()


@pytest.mark.parametrize("changed_manifest", [False, True])
def test_case_restart_reuses_exact_trial_without_minting_authority(
    engine, tmp_path, changed_manifest
):
    env, second, _, _ = cases(engine, tmp_path)
    try:
        with engine.transaction() as conn:
            trial = conn.execute(
                "SELECT * FROM app.sa_coding_trial_authorizations WHERE trial_id=%s",
                (second.profile.trial_id,),
            ).fetchone()
            before = conn.execute(
                "SELECT * FROM app.sa_coding_trial_authorizations ORDER BY trial_id"
            ).fetchall()
        arguments = dict(
            source_id=env.source.source_id,
            actor_id=SENDER_ID,
            source_root=env.original,
            root=tmp_path / "second-case",
            upstream_commit=second.profile.repository.base_commit,
            upstream_tree=second.profile.repository.upstream_tree,
            image_id=second.runtime_profile.driver.profile.image_id,
            price=second.runtime_profile.price,
            model=second.runtime_profile.model,
            expires=trial["valid_until"],
            manifest_sha256=digest("tampered") if changed_manifest else trial["manifest_sha256"],
            h12_evidence_sha256=second.profile.harness.h12.evidence_sha256,
            objective=second.runtime_profile.objective,
            baseline_command=second.runtime_profile.baseline_command,
            working_directory="src",
            read_paths=second.profile.grant.read_paths,
            write_paths=second.profile.grant.write_paths,
            repository_id=1,
            existing_trial=trial,
        )
        if changed_manifest:
            with pytest.raises(DomainError, match="Exact signed case"):
                build_case_experiment(engine, env.source.cookie, **arguments)
        else:
            restored = build_case_experiment(engine, env.source.cookie, **arguments)
            assert restored.profile == second.profile
            assert (
                restored.runtime_profile.acceptance_sha256
                == second.runtime_profile.acceptance_sha256
            )
        with engine.transaction() as conn:
            assert (
                conn.execute(
                    "SELECT * FROM app.sa_coding_trial_authorizations ORDER BY trial_id"
                ).fetchall()
                == before
            )
        assert env.requests == []
    finally:
        env.close()
