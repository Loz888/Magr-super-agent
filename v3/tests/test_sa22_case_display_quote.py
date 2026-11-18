"""Reproduce the copied Feishu request at the frozen-case admission boundary."""

from uuid import uuid4

from sa06_support import payload, signed
from sa22_coding_support import coding_environment

from mgar_v3.super_agent.source_case_experiments import SourceCaseExperiments, canonical_request
from mgar_v3.super_agent.source_coding_experiment import explicit_coding_request


def test_copied_display_quote_dispatches_exact_frozen_request(engine, tmp_path):
    env = coding_environment(engine, tmp_path, task_status=True)
    try:
        request = canonical_request(env.experiment._text(env.input_id))
        manager = SourceCaseExperiments(engine, {"L02": (request, env.experiment)}, env.experiment)
        copied = f"@_user_1 「{request}。"
        incoming = env.runtime.receive(
            signed(
                env.source,
                payload(
                    message_id="om_display_quote_" + uuid4().hex,
                    body=copied,
                ),
            )
        )
        assert incoming.disposition == "accepted"
        [input_id] = incoming.input_ids
        target = manager.admit(input_id)
        assert target is not None

        service = env.conversation.service
        service.coding = manager
        service.deterministic_coding = lambda identifier: manager.case_for(identifier) is not None
        outcome = service.process(input_id)
        assert outcome["action_id"]
        with engine.transaction() as conn:
            binding = conn.execute(
                "SELECT run_id FROM app.sa_coding_bindings WHERE input_id=%s",
                (input_id,),
            ).fetchone()
            assert binding == {"run_id": target["run_id"]}
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM app.sa_coding_operations WHERE run_id=%s "
                    "AND capability='write' AND state='known'",
                    (target["run_id"],),
                ).fetchone()["n"]
                == 1
            )

        assert canonical_request(copied + "」") == request
        assert canonical_request("只是引用" + copied + "」") != request
        assert canonical_request(copied + "」请修改测试") != request
        assert canonical_request(copied.replace("src/calc.py", "src/calc2.py")) != request
        assert not explicit_coding_request(copied)
    finally:
        env.close()
