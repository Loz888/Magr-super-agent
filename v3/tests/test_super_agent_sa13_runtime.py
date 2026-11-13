"""First formal actual PG/Git/Docker/CodingLoop checks, with controlled model IO."""

import json
from dataclasses import replace

import pytest
from sa13_runtime_support import BAD, FILES, GOOD, RepairModel, facts, run, runtime_env

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import IsolatedExecutionBroker
from mgar_v3.harness.contracts import ModelResponse, ModelUsage
from mgar_v3.harness.model import DeepSeekChatAdapter
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.super_agent.policy_domain import PolicyRevoke
from mgar_v3.super_agent.source_coding_budget import CodingBudgetLedger
from mgar_v3.super_agent.source_coding_runtime import ControlledCodingModel, SourceCodingRuntime
from mgar_v3.super_agent.source_scheduler_domain import SchedulerSessionRevokeRequest


def withdraw(env, kind):
    if kind == "policy":
        env.engine.source_policies.revoke(
            env.cookie,
            env.source.policy_id,
            PolicyRevoke(expected_revision=2, review_ref="SA13-exact-dispatch-revoke"),
        )
    else:
        env.source.identities.roles(
            env.cookie,
            env.source.binding["binding_id"],
            RoleChange(
                expected_revision=env.source.binding["revision"],
                roles=("requester",),
                review_ref="SA13-exact-dispatch-context-withdrawal",
            ),
        )


def test_real_loop_failed_patch_then_repair_uses_same_original_target_and_budgets(engine, tmp_path):
    with runtime_env(engine, tmp_path) as env:
        result = run(env)
        assert result["coding_status"] == "completed", result
        assert result["verification"] == "verified" and not result["business_task_completed"]
        assert len(env.model.requests) == result["model_calls"] == 3
        assert (env.raw.root / "src/calc.py").read_text() == GOOD
        assert (env.source_root / "src/calc.py").read_text() == BAD
        assert (env.raw.root / "tests/test_calc.py").read_text() == FILES["tests/test_calc.py"]
        saved = facts(env)
        assert len(saved["usage_calls"]) == len(saved["sa_budget_calls"]) == 3
        assert {row["status"] for row in saved["usage_calls"]} == {"known"}
        assert (
            sum(row["settled_micros"] for row in saved["usage_calls"])
            == result["cost_micros"]
            == 600
        )
        assert {row["run_id"] for row in saved["usage_calls"]} == {env.target.run_id}
        assert {row["component"] for row in saved["sa_budget_calls"]} == {"worker"}
        execute = [row for row in saved["sa_coding_operations"] if row["capability"] == "execute"]
        assert len(execute) == 3
        assert [
            row["result_code"] for row in sorted(execute, key=lambda row: row["created_at"])
        ] == ["failed", "failed", "ok"]
        verification = saved["sa_coding_verifications"][-1]
        assert verification["result"] == "passed" and verification["tests_run"] >= 2
        assert (
            verification["tests_failed"] == 0 and verification["tree_sha256"] == env.raw.tree_hash()
        )
        assert verification["mandate_sha256"] == env.mandate.mandate_sha256
        assert verification["acceptance_sha256"] == env.mandate.acceptance_sha256
        assert verification["execution_operation_id"] in {row["operation_id"] for row in execute}
        assert saved["actions"] == saved["outbox"] == []
        assert engine.get_run(env.target.run_id)["status"] == "running"
        assert not engine.settings.allow_paid and not engine.settings.allow_external_write
        assert saved["sa_coding_payloads"]
        for payload in saved["sa_coding_payloads"]:
            raw = bytes(payload["ciphertext"])
            assert b"synthetic private repair" not in raw and GOOD.encode() not in raw
            assert env.cookie.encode() not in raw and env.worker["bearer"].encode() not in raw
        for table in ("sa_coding_operations", "sa_coding_verifications", "sa_coding_attempts"):
            assert "synthetic private repair" not in json.dumps(saved[table], default=str)
        assert all(
            request.task.actor == env.mandate.origin.engine_actor for request in env.model.requests
        )
        assert env.mandate.origin.engine_actor != engine.settings.local_actor
        assert env.service.capabilities(env.cookie)["live_agent"] == "FAIL"


@pytest.mark.parametrize("kind", ["policy", "context"])
def test_committed_withdrawal_after_original_reservation_blocks_actual_model_call(
    engine,
    tmp_path,
    monkeypatch,
    kind,
):
    with runtime_env(engine, tmp_path) as env:
        reserve = CodingBudgetLedger.reserve
        observed = []

        def committed(ledger, *args, **kwargs):
            value = reserve(ledger, *args, **kwargs)
            if value.kind == "model" and not observed:
                assert engine.connection.info.transaction_status.name == "IDLE"
                with engine.transaction() as conn:
                    call = conn.execute(
                        "SELECT status FROM app.usage_calls WHERE call_id=%s",
                        (value.reservation_id,),
                    ).fetchone()
                    assert call["status"] == "reserved"
                withdraw(env, kind)
                observed.append(True)
            return value

        monkeypatch.setattr(CodingBudgetLedger, "reserve", committed)
        with pytest.raises(DomainError):
            run(env)
        assert observed and env.model.requests == []
        assert (env.raw.root / "src/calc.py").read_text() == BAD
        saved = facts(env)
        assert len(saved["usage_calls"]) == 1
        assert saved["usage_calls"][0]["status"] in {"reserved", "unknown"}
        assert saved["sa_coding_verifications"] == []
        assert saved["actions"] == saved["outbox"] == []


@pytest.mark.parametrize("kind", ["policy", "context"])
def test_withdrawal_after_actual_response_preserves_cost_and_discards_tools(engine, tmp_path, kind):
    with runtime_env(engine, tmp_path) as env:
        model = env.model
        complete = model.complete

        def response_then_withdraw(request, cancellation):
            response = complete(request, cancellation)
            withdraw(env, kind)
            return response

        model.complete = response_then_withdraw
        with pytest.raises(DomainError):
            run(env)
        saved = facts(env)
        assert len(model.requests) == 1
        assert len(saved["usage_calls"]) == 1 and saved["usage_calls"][0]["status"] == "known"
        assert saved["usage_calls"][0]["settled_micros"] == 200
        assert not any(row["capability"] == "write" for row in saved["sa_coding_operations"])
        assert (env.raw.root / "src/calc.py").read_text() == BAD
        assert not saved["sa_coding_verifications"]


def test_unknown_model_response_retains_original_reservation_and_never_resends(engine, tmp_path):
    class Unknown(ControlledCodingModel):
        calls = 0

        def complete(self, request, cancellation):
            self.calls += 1
            return ModelResponse(finish_reason="error", usage=ModelUsage(known=False))

    model = Unknown()
    with runtime_env(engine, tmp_path, model=model) as env:
        first = run(env)
        assert first["coding_status"] == "unknown"
        env.service.runtime = SourceCodingRuntime(env.service, env.runtime_profile)
        second = run(env)
        assert second["coding_status"] == "unknown" and model.calls == 1
        saved = facts(env)
        assert len(saved["usage_calls"]) == 1
        assert saved["usage_calls"][0]["status"] == "unknown"
        assert saved["usage_calls"][0]["reserved_micros"] > 0
        assert [
            row["state"] for row in saved["sa_coding_operations"] if row["kind"] == "model"
        ] == ["unknown"]
        assert (env.raw.root / "src/calc.py").read_text() == BAD


def test_real_container_create_then_committed_session_revoke_prevents_start(
    engine, tmp_path, monkeypatch
):
    with runtime_env(engine, tmp_path) as env:
        inspect = IsolatedExecutionBroker.inspect_bound_command
        original_docker = IsolatedExecutionBroker._docker
        starts, created = [], []

        def before_start(broker, name, labels):
            value = inspect(broker, name, labels)
            if value.state == "created" and not created:
                created.append(value.container_id)
                assert engine.connection.info.transaction_status.name == "IDLE"
                env.engine.source_scheduler.revoke_session(
                    env.cookie,
                    SchedulerSessionRevokeRequest(
                        session_id=env.worker["session_id"],
                        expected_revision=env.worker["revision"],
                        review_ref="SA13-last-inspect-before-start",
                    ),
                )
            return value

        def record_start(broker, argv, **kwargs):
            if argv and argv[0] == "start":
                starts.append(tuple(argv))
            return original_docker(broker, argv, **kwargs)

        monkeypatch.setattr(IsolatedExecutionBroker, "inspect_bound_command", before_start)
        monkeypatch.setattr(IsolatedExecutionBroker, "_docker", record_start)
        try:
            result = run(env)
            assert result["coding_status"] != "completed"
        except DomainError:
            pass
        assert created and not starts and env.model.requests == []
        assert not facts(env)["sa_coding_verifications"]


def test_changed_server_profile_cannot_use_original_reviewed_binding(engine, tmp_path):
    with runtime_env(engine, tmp_path) as env:
        profile = replace(
            env.runtime_profile,
            driver=env.driver,
            price=env.runtime_profile.price.model_copy(update={"price_version": "forged"}),
        )
        env.service.runtime = SourceCodingRuntime(env.service, profile)
        with pytest.raises(DomainError) as rejected:
            run(env)
        assert rejected.value.code == "SOURCE_CODING_FIXTURE_BINDING_INVALID"
        assert not env.model.requests
        assert not facts(env)["usage_calls"]


def test_network_model_adapter_is_rejected_before_fixture_can_call_it(engine, tmp_path):
    with runtime_env(engine, tmp_path) as env:
        adapter = object.__new__(DeepSeekChatAdapter)
        with pytest.raises(DomainError) as rejected:
            replace(env.runtime_profile, model=adapter)
        assert rejected.value.code == "SOURCE_CODING_FIXTURE_PROFILE_INVALID"
        assert not env.model.requests and not facts(env)["usage_calls"]


def test_malicious_tool_cannot_edit_frozen_test_files(engine, tmp_path):
    class Malicious(RepairModel):
        def complete(self, request, cancellation):
            response = super().complete(request, cancellation)
            if response.tool_calls:
                edit = response.tool_calls[0]
                arguments = dict(edit.arguments)
                arguments["edits"] = [
                    {
                        "path": "tests/test_calc.py",
                        "expected_sha256": "a" * 64,
                        "content": "print('2 passed')\n",
                    }
                ]
                return response.model_copy(
                    update={"tool_calls": (edit.model_copy(update={"arguments": arguments}),)}
                )
            return response

    with runtime_env(engine, tmp_path, model=Malicious(wrong_first=False)) as env:
        result = run(env)
        assert result["coding_status"] != "completed" and result["verification"] != "verified"
        assert (env.raw.root / "tests/test_calc.py").read_text() == FILES["tests/test_calc.py"]
        assert (env.raw.root / "src/calc.py").read_text() == BAD
        assert not any(row["result"] == "passed" for row in facts(env)["sa_coding_verifications"])


@pytest.mark.parametrize(
    "body",
    [
        "print('Ran 2 tests')\nraise SystemExit(0)\n",
        "import unittest\nunittest.TextTestRunner.run = lambda *args: None\n",
        "def total(values):\n    print('2 passed')\n    return 5\n",
    ],
)
def test_candidate_cannot_forge_test_totals_or_terminate_trusted_runner(engine, tmp_path, body):
    class Pretend(RepairModel):
        def complete(self, request, cancellation):
            response = super().complete(request, cancellation)
            if response.tool_calls:
                call = response.tool_calls[0]
                args = dict(call.arguments)
                args["edits"] = [{**args["edits"][0], "content": body}]
                return response.model_copy(
                    update={"tool_calls": (call.model_copy(update={"arguments": args}),)}
                )
            return response

    with runtime_env(engine, tmp_path, model=Pretend(wrong_first=False)) as env:
        result = run(env)
        assert result["coding_status"] != "completed" and result["verification"] != "verified"
        assert (env.raw.root / "src/calc.py").read_text() == BAD
        assert not any(row["result"] == "passed" for row in facts(env)["sa_coding_verifications"])
