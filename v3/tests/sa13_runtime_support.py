"""Actual local Git/H03/H06 fixtures; controlled models authorize no live gate."""

import json
import os
import subprocess
from contextlib import contextmanager
from hashlib import sha256

from sa07_support import PRICE
from sa13_support import new_source, setup

from mgar_v3.domain import digest
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.contracts import (
    BudgetLimits,
    CapabilityGrant,
    ExecutionRequest,
    HarnessToolCall,
    ModelResponse,
    ModelUsage,
    VerificationRequirement,
)
from mgar_v3.harness.workspace import WorkspaceRegistry
from mgar_v3.super_agent.source_coding_domain import CodingGrant, CodingRepository
from mgar_v3.super_agent.source_coding_runtime import (
    ControlledCodingModel,
    SourceCodingFixtureProfile,
    SourceCodingRuntime,
)
from mgar_v3.super_agent.source_scheduler_domain import (
    ResourceRequest,
    SchedulerOwnerDrainRequest,
)
from mgar_v3.super_agent.source_scheduler_runtime import (
    H03SchedulerResourceDriver,
    SchedulerRuntimeProfile,
)

IMAGE = "sha256:efad10932c12f9d6f2408fe960e9a292c4bdbac0f8365387838acbe4f0fdd2b6"
COMMAND = ("python3", "-m", "unittest", "discover", "-s", "../tests", "-p", "test_*.py")
BAD = "def total(values):\n    return sum(values) - 2\n"
WRONG = "def total(values):\n    return sum(values) - 1\n"
GOOD = "def total(values):\n    return sum(values)\n"
FILES = {
    "src/__init__.py": "",
    "src/calc.py": BAD,
    "src/other.py": "value = 0\n",
    "tests/test_calc.py": (
        "import sys\nimport unittest\nsys.path.insert(0, '..')\nfrom src.calc import total\n"
        "class Regression(unittest.TestCase):\n"
        "    def test_sum(self):\n        self.assertEqual(total([2, 3]), 5)\n"
        "    def test_empty(self):\n        self.assertEqual(total([]), 0)\n"
    ),
}


def docker(*args):
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        timeout=30,
        check=False,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DOCKER_HOST": os.environ.get("DOCKER_HOST", ""),
        },
    )


def git(source, *args):
    return (
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=SA13 Fixture",
                "-c",
                "user.email=sa13@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        .stdout.decode()
        .strip()
    )


class RepairModel(ControlledCodingModel):
    """Explicit deterministic adapter; edits actual source through the real loop."""

    def __init__(self, *, wrong_first=True, two_files=False):
        self.requests = []
        self.wrong_first = wrong_first
        self.two_files = two_files

    def complete(self, request, cancellation):
        cancellation.ensure_active()
        self.requests.append(request)
        repairs = 2 if self.wrong_first else 1
        if len(self.requests) > repairs:
            return ModelResponse(
                text="The actual regression tests now pass.",
                usage=ModelUsage(input_tokens=100, output_tokens=100, cost_micros=200),
                provider="explicit-sa13-controlled",
                model_id=PRICE.model_id,
                price_version=PRICE.price_version,
            )
        previous = BAD if len(self.requests) == 1 else WRONG
        content = WRONG if self.wrong_first and len(self.requests) == 1 else GOOD
        edits = [
            {
                "path": "src/calc.py",
                "expected_sha256": sha256(previous.encode()).hexdigest(),
                "content": content,
            }
        ]
        if self.two_files and len(self.requests) == 1:
            edits.append(
                {
                    "path": "src/other.py",
                    "expected_sha256": sha256(b"value = 0\n").hexdigest(),
                    "content": "value = 1\n",
                }
            )
        return ModelResponse(
            tool_calls=(
                HarnessToolCall(
                    call_id="repair-" + str(len(self.requests)),
                    name="apply_edits",
                    arguments={"expected_tree": request.workspace_tree, "edits": edits},
                ),
                HarnessToolCall(
                    call_id="test-" + str(len(self.requests)),
                    name="run_tests",
                    arguments={"argv": list(COMMAND), "cwd": "src", "timeout_seconds": 20},
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=100, output_tokens=100, cost_micros=200),
            provider="explicit-sa13-controlled",
            model_id=PRICE.model_id,
            price_version=PRICE.price_version,
        )


@contextmanager
def runtime_env(engine, tmp_path, *, model=None, ready=True):
    assert docker("image", "inspect", IMAGE).returncode == 0
    source_env = new_source(engine)
    with engine.transaction() as conn:
        actor = conn.execute(
            "SELECT engine_actor FROM app.source_principals WHERE project_id=%s",
            (engine.settings.project_id,),
        ).fetchone()["engine_actor"]
    source = tmp_path / "registered-source"
    source.mkdir()
    for path, content in FILES.items():
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(source, "init", "--quiet")
    git(source, "remote", "add", "origin", "https://github.com/example/mgar-test")
    git(source, "add", ".")
    git(source, "commit", "--quiet", "-m", "controlled regression fixture")
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor=engine.settings.local_actor)
    lock = tmp_path / "image-lock.json"
    lock.write_text(json.dumps({"image_id": IMAGE}))
    lock.chmod(0o600)
    grant = CapabilityGrant(
        actor=actor,
        workspace_id=registration.workspace_id,
        repository=engine.settings.repository,
        read_paths=("src", "tests"),
        write_paths=("src",),
        execute=True,
        allowed_commands=("python3",),
        network=False,
        max_seconds=120,
    )
    profile = SchedulerRuntimeProfile(
        actor=actor,
        repository=engine.settings.repository,
        grant=grant,
        request=ExecutionRequest(
            operation_id="sa13-server-controlled", argv=COMMAND, timeout_seconds=20
        ),
        image_id=IMAGE,
        review_ref="explicit-SA13-real-resource-fixture",
    )
    driver_kwargs = dict(
        scheduler=engine.source_scheduler,
        registry_root=registry.root,
        registration=registration,
        source_root=source,
        profile=profile,
        receipt_root=tmp_path / "private-receipts",
        image_lock=lock,
    )
    driver = H03SchedulerResourceDriver(**driver_kwargs)
    tree = digest({path: sha256(value.encode()).hexdigest() for path, value in FILES.items()})
    lease = issue_lease(
        mode="investigate",
        actor=actor,
        workspace_id=registration.workspace_id,
        repository=engine.settings.repository,
        execution_epoch=1,
    )
    environment = IsolatedExecutionBroker.environment_hash(None, lease)
    repository = CodingRepository(
        repository=engine.settings.repository,
        repository_id=1,
        source_kind="fixture_git",
        base_commit=registration.base_commit,
        upstream_tree=git(source, "rev-parse", "HEAD^{tree}"),
        materialized_commit=registration.base_commit,
        materialized_tree_sha256=tree,
        registration_sha256=registration.registration_hash,
        snapshot_sha256=digest({"registration": registration.registration_hash, "tree": tree}),
        model_disclosure="controlled_fixture",
    )
    coding_grant = CodingGrant(
        read_paths=("src", "tests"),
        write_paths=("src",),
        allowed_commands=("python3",),
        resources=ResourceRequest(cpu_millis=1000, memory_mib=256),
        environment_sha256=environment,
        max_seconds=120,
    )
    model = model or RepairModel()
    fixture = SourceCodingFixtureProfile(
        driver=driver,
        model=model,
        objective="Repair total without changing tests",
        requirements=(
            VerificationRequirement(
                requirement_id="sum-regression", command_contains=("unittest",), min_tests=2
            ),
        ),
        baseline_command=COMMAND,
        price=PRICE,
        limits=BudgetLimits(
            max_model_calls=4, max_tool_calls=16, max_cost_micros=20000, max_seconds=120
        ),
    )
    env = setup(
        engine,
        tmp_path,
        source_env=source_env,
        driver=driver,
        repository=repository,
        grant=coding_grant,
        ready=ready,
        acceptance_sha256=fixture.acceptance_sha256,
    )
    env.service.runtime = SourceCodingRuntime(env.service, fixture)
    env.model, env.runtime_profile = model, fixture
    env.registry, env.registration, env.driver_kwargs = registry, registration, driver_kwargs
    env.source_root = source
    env.raw = (
        registry.open_task_workspace(registration, str(env.ready["assignment_id"]))
        if ready
        else None
    )
    try:
        yield env
    finally:
        # Only identities read from this isolated test's original SA12 journal.
        for row in engine.source_scheduler.assignments(env.cookie)["items"]:
            if row["state"] != "released":
                engine.source_scheduler.drain(
                    env.cookie,
                    SchedulerOwnerDrainRequest(
                        assignment_id=row["assignment_id"],
                        expected_revision=row["revision"],
                        reason_code="OWNER_DRAIN",
                        review_ref="SA13-actual-resource-cleanup",
                    ),
                )
        engine.source_scheduler.maintain()
        with engine.transaction() as conn:
            operations = conn.execute(
                "SELECT * FROM app.sa_scheduler_operations WHERE kind IN ('start','job')"
            ).fetchall()
        for op in operations:
            probe = docker("container", "inspect", op["provider_operation_id"])
            if probe.returncode == 0:
                actual = json.loads(probe.stdout)[0]
                labels = actual["Config"]["Labels"]
                assert labels["mgar.sa12.project"] == engine.settings.project_id
                assert labels["mgar.sa12.assignment"] == str(op["assignment_id"])
                assert docker("rm", "-f", actual["Id"]).returncode == 0


def run(env):
    return env.service.run(env.worker["bearer"], env.run_request)


def facts(env):
    with env.engine.transaction() as conn:
        return {
            name: conn.execute("SELECT * FROM app." + name + " ORDER BY 1").fetchall()
            for name in (
                "usage_calls",
                "sa_budget_calls",
                "sa_coding_attempts",
                "sa_coding_operations",
                "sa_coding_payloads",
                "sa_coding_verifications",
                "sa_scheduler_operations",
                "actions",
                "outbox",
            )
        }
