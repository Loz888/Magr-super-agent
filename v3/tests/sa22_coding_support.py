"""Real Git/PG/Broker resources with controlled supplier HTTP for the owner trial."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
from sa06_support import SENDER_ID, payload, signed
from sa07_support import PRICE
from sa13_runtime_support import BAD, COMMAND, FILES, GOOD, IMAGE, git
from sa22_model_support import model_environment

from mgar_v3.domain import digest
from mgar_v3.sources.feishu_domain import FeishuActivation
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.sources.ingress_domain import IngressConfigure, IngressSettings
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_coding_experiment import install_coding_trial
from mgar_v3.super_agent.source_coding_live_model import ExperimentalDeepSeekModel
from mgar_v3.super_agent.source_notification_domain import NotificationConfigure


def coding_environment(
    engine,
    tmp_path,
    *,
    extra_files=None,
    supplier_hook=None,
    task_status=False,
    token_hook=None,
    write_paths=("src",),
    thinking_mode="disabled",
):
    env = model_environment(engine, task_status=task_status, token_hook=token_hook)
    source = tmp_path / "original-source"
    source.mkdir()
    files = {**FILES, **(extra_files or {})}
    for path, content in files.items():
        p = source / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git(source, "init", "--quiet")
    git(source, "remote", "add", "origin", "https://github.com/example/mgar-test")
    git(source, "add", ".")
    git(source, "commit", "--quiet", "-m", "explicit synthetic trial repository")
    commit = git(source, "rev-parse", "HEAD")
    read_paths = ("src", "tests", "assets") if extra_files else ("src", "tests")
    current = engine.source_policies.read(env.source.cookie, env.source.policy_id)
    raw = current["definition"]
    raw["mode"] = "auto_repair"
    raw["execution"] = {
        "enabled": True,
        "capabilities": ["read_repository", "edit_files", "run_tests", "model_call"],
        "repositories": [
            {
                "repository": engine.settings.repository,
                "base_sha": commit,
                "paths": list(read_paths),
            }
        ],
    }
    saved = engine.source_policies.save(
        env.source.cookie,
        env.source.policy_id,
        PolicySave(
            expected_revision=3,
            definition=PolicyDefinition.model_validate(raw),
            review_ref="SA22-controlled-live-trial",
        ),
    )
    engine.source_ingress.configure(
        env.source.cookie,
        env.source.source_id,
        IngressConfigure(
            expected_generation=3,
            settings=IngressSettings(
                owner="gateway", policy_id=env.source.policy_id, policy_revision=4
            ),
            review_ref="SA22-trial-cutover",
        ),
    )
    if task_status:
        with engine.transaction() as conn:
            control, notification = engine.source_notifications._config(conn, env.source.source_id)
        engine.source_notifications.configure(
            env.source.cookie,
            env.source.source_id,
            NotificationConfigure(
                expected_revision=control["revision"],
                settings=notification.model_copy(update={
                    "policy_revision": 4, "policy_sha256": saved["definition_sha256"],
                }),
                review_ref="SA22-controlled-task-status",
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
            datetime.now(UTC) + timedelta(minutes=10),
            frozenset({"webhook", "socket", "health"}),
            "explicit-controlled-trial",
        ),
        transport=httpx.MockTransport(env.source.handler),
    )
    incoming = runtime.receive(
        signed(
            env.source,
            payload(
                message_id="Om_Code-" + uuid4().hex,
                body="请修复 src/calc.py 中 total 的计算错误，保持测试不变 @_user_1",
            ),
        )
    )
    assert incoming.disposition == "accepted"
    holder, requests = {}, []

    def supplier(request):
        requests.append(request)
        if supplier_hook:
            return supplier_hook(request)
        experiment = holder["experiment"]
        with engine.transaction() as c:
            assignment = c.execute(
                "SELECT assignment_id FROM app.sa_scheduler_assignments WHERE state='active'"
            ).fetchone()
        workspace = experiment.registry.open_task_workspace(
            experiment.runtime_profile.driver.registration, str(assignment["assignment_id"])
        )
        snapshot = workspace.snapshot()
        if workspace.read("src/calc.py") == BAD:
            calls = [
                {
                    "id": "actual-file-edit",
                    "type": "function",
                    "function": {
                        "name": "apply_edits",
                        "arguments": json.dumps(
                            {
                                "expected_tree": snapshot.tree_hash,
                                "edits": [
                                    {
                                        "path": "src/calc.py",
                                        "expected_sha256": __import__("hashlib")
                                        .sha256(BAD.encode())
                                        .hexdigest(),
                                        "content": GOOD,
                                    }
                                ],
                            }
                        ),
                    },
                },
                {
                    "id": "actual-regression",
                    "type": "function",
                    "function": {
                        "name": "run_tests",
                        "arguments": json.dumps(
                            {"argv": list(COMMAND), "cwd": "src", "timeout_seconds": 20}
                        ),
                    },
                },
            ]
            message, finish = (
                {"role": "assistant", "content": None, "tool_calls": calls},
                "tool_calls",
            )
        else:
            message, finish = (
                {"role": "assistant", "content": "Patch proposal ready for review."},
                "stop",
            )
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [{"finish_reason": finish, "message": message}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(supplier), trust_env=False)
    model = ExperimentalDeepSeekModel(
        client, "controlled-not-a-credential", PRICE, controlled_transport=True,
        thinking_mode=thinking_mode,
    )
    root = tmp_path / "experimental-control"
    root.mkdir()
    experiment = install_coding_trial(
        engine,
        env.source.cookie,
        source_id=env.source.source_id,
        actor_id=SENDER_ID,
        source_root=source,
        root=root,
        upstream_commit=commit,
        upstream_tree=git(source, "rev-parse", "HEAD^{tree}"),
        image_id=IMAGE,
        price=PRICE,
        model=model,
        expires=datetime.now(UTC) + timedelta(minutes=10),
        manifest_sha256=digest("controlled-explicit-owner-manifest"),
        h12_evidence_sha256=digest("controlled-R05-evidence-reference"),
        repository_id=1,
        baseline_command=COMMAND,
        working_directory="src",
        read_paths=read_paths,
        write_paths=write_paths,
    )
    holder["experiment"] = experiment
    return SimpleNamespace(
        source=env.source,
        experiment=experiment,
        input_id=incoming.input_ids[0],
        requests=requests,
        original=source,
        root=root,
        client=client,
        runtime=runtime,
        conversation=env,
        close=lambda: (client.close(), env.close()),
    )
