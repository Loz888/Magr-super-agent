"""New boundary: reviewed, never-started queue attempts must not need fake artifacts."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sa06_support import payload, signed
from test_sa22_case_profiles import cases

from mgar_v3.domain import DomainError
from mgar_v3.sources.feishu_domain import FeishuActivation
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.super_agent.source_case_experiments import SourceCaseExperiments
from mgar_v3.super_agent.source_coding_domain import CodingCancelRequest


@pytest.mark.parametrize("all_reviewed", [False, True])
def test_only_exact_reviewed_never_started_attempts_allow_one_fresh_case_input(
    engine, tmp_path, all_reviewed
):
    env, _, _, manager = cases(engine, tmp_path)
    try:
        first = manager.admit(env.input_id)

        def cancel(target):
            with engine.transaction() as c:
                b = engine.source_coding.binding(c, target["coding_id"])
                run = engine._run(c, target["run_id"])
            engine.source_coding.cancel(
                env.source.cookie,
                CodingCancelRequest(
                    request_id=uuid4(),
                    coding_id=target["coding_id"],
                    run_id=target["run_id"],
                    expected_epoch=run["execution_epoch"],
                    expected_revision=b["revision"],
                    review_ref="explicit review of unstarted budget-blocked input",
                ),
            )

        cancel(first)
        with engine.transaction() as c:
            message = engine.source_correlations._input(c, env.input_id).text
        runtime = FeishuRuntime(
            env.source.sources,
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
                "reviewed-unstarted-case",
            ),
            transport=httpx.MockTransport(env.source.handler),
        )

        def fresh():
            return runtime.receive(
                signed(env.source, payload(message_id="new-" + uuid4().hex, body=message))
            ).input_ids[0]

        reviewed = [first["coding_id"]]
        next_manager = SourceCaseExperiments(
            engine,
            {"L01": (message, env.experiment)},
            env.experiment,
            reviewed_failed_bindings={"L01": tuple(reviewed)},
        )
        second = next_manager.admit(fresh())
        assert second["run_id"] != first["run_id"]
        cancel(second)
        if all_reviewed:
            reviewed.append(second["coding_id"])
        final_manager = SourceCaseExperiments(
            engine,
            {"L01": (message, env.experiment)},
            env.experiment,
            reviewed_failed_bindings={"L01": tuple(reviewed)},
        )
        last = fresh()
        if all_reviewed:
            final = final_manager.admit(last)
            assert final["run_id"] not in (first["run_id"], second["run_id"])
            assert final_manager.admit(last)["run_id"] == final["run_id"]
            with pytest.raises(DomainError, match="already has an input"):
                final_manager.admit(fresh())
        else:
            with pytest.raises(DomainError, match="already has an input"):
                final_manager.admit(last)
        with engine.transaction() as c:
            assert c.execute("SELECT count(*)n FROM app.usage_calls").fetchone()["n"] == 0
            assert c.execute("SELECT count(*)n FROM app.sa_coding_attempts").fetchone()["n"] == 0
        assert env.requests == []
    finally:
        env.close()
