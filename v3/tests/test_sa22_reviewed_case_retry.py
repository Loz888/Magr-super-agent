"""New Owner-reviewed no-write retry boundary, without repeating historical PASS."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sa06_support import payload, signed
from sa22_coding_support import coding_environment

from mgar_v3.domain import DomainError
from mgar_v3.sources.feishu_domain import FeishuActivation
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.super_agent.source_case_experiments import SourceCaseExperiments
from mgar_v3.super_agent.source_coding_domain import CodingCancelRequest


@pytest.mark.parametrize("reviewed", [False, True])
def test_fresh_case_retry_needs_exact_owner_reviewed_revoked_known_no_write_attempt(
    engine, tmp_path, reviewed
):
    def denied_read(_):
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "controlled-outside-read",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "ungranted.py"}),
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    env = coding_environment(engine, tmp_path, supplier_hook=denied_read)
    try:
        env.experiment(env.input_id)
        with engine.transaction() as c:
            prior = c.execute("SELECT * FROM app.sa_coding_bindings").fetchone()
            epoch = engine._run(c, prior["run_id"])["execution_epoch"]
            message = engine.source_correlations._input(c, env.input_id).text
            calls_before = c.execute("SELECT count(*) n FROM app.usage_calls").fetchone()["n"]
            trials_before = c.execute(
                "SELECT count(*) n FROM app.sa_coding_trial_authorizations"
            ).fetchone()["n"]
        engine.source_coding.cancel(
            env.source.cookie,
            CodingCancelRequest(
                request_id=uuid4(),
                coding_id=prior["coding_id"],
                run_id=prior["run_id"],
                expected_epoch=epoch,
                expected_revision=prior["revision"],
                review_ref="known-failed-no-write-case-review",
            ),
        )
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
                "fresh-explicit-case-retry",
            ),
            transport=httpx.MockTransport(env.source.handler),
        )
        incoming = runtime.receive(
            signed(env.source, payload(message_id="new-case-" + uuid4().hex, body=message))
        )
        fresh = incoming.input_ids[0]
        manager = SourceCaseExperiments(
            engine,
            {"L01": (message, env.experiment)},
            env.experiment,
            reviewed_failed_bindings={"L01": prior["coding_id"]} if reviewed else None,
        )
        if reviewed:
            new = manager.admit(fresh)
            assert new["run_id"] != prior["run_id"]
            assert manager.admit(fresh)["run_id"] == new["run_id"]
            third = runtime.receive(
                signed(
                    env.source, payload(message_id="duplicate-case-" + uuid4().hex, body=message)
                )
            )
            with pytest.raises(DomainError, match="already has an input"):
                manager.admit(third.input_ids[0])
        else:
            with pytest.raises(DomainError, match="already has an input"):
                manager.admit(fresh)
        with engine.transaction() as c:
            assert c.execute("SELECT count(*) n FROM app.sa_coding_bindings").fetchone()["n"] == (
                2 if reviewed else 1
            )
            assert (
                c.execute(
                    "SELECT state FROM app.sa_coding_bindings WHERE coding_id=%s",
                    (prior["coding_id"],),
                ).fetchone()["state"]
                == "revoked"
            )
            assert (
                c.execute("SELECT count(*) n FROM app.usage_calls").fetchone()["n"] == calls_before
            )
            assert (
                c.execute("SELECT count(*) n FROM app.sa_coding_trial_authorizations").fetchone()[
                    "n"
                ]
                == trials_before
            )
    finally:
        env.close()
