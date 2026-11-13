"""Real-PG scheduler fixtures; filesystem-only driver is explicitly controlled.

The formal Runtime tests separately use fixed H03 and the real isolated Broker.
This fixture authorizes no model, arbitrary process, network or external write.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from pydantic import SecretStr
from sa07_support import configure as configure_shared
from sa07_support import definition as shared_definition

from mgar_v3.domain import DomainError, TriggerInput, digest
from mgar_v3.super_agent.source_scheduler import ResourceReceipt
from mgar_v3.super_agent.source_scheduler_domain import (
    ResourceRequest,
    SchedulerClaimRequest,
    SchedulerConfiguration,
    SchedulerConfigureRequest,
    SchedulerEnqueueRequest,
    SchedulerLimits,
    SchedulerReadyRequest,
    SchedulerSessionRegisterRequest,
)
from mgar_v3.web_access import LoginInput, WebAccounts


class FilesystemOnlyDriver:
    """Trusted test port with actual private directories and persistent receipts.

    It has no process launch method. Its absence proof therefore covers this
    controlled driver's empty process set, not a real Harness process backend.
    """

    profile_sha256 = digest({"controlled_driver": "sa12-filesystem-only-v1"})

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.receipts = {}
        self.available = True

    def workspace_identity(self, project_id, assignment_id, workspace_id):
        return digest(
            [project_id, str(assignment_id), str(workspace_id), self.profile_sha256]
        )

    def _receipt(self, ctx, **fields):
        observed_at = fields.pop("observed_at", datetime.now(UTC))
        return ResourceReceipt(
            project_id=ctx.project_id,
            assignment_id=ctx.assignment_id,
            run_id=ctx.run_id,
            session_id=ctx.session_id,
            workspace_id=ctx.workspace_id,
            epoch=ctx.epoch,
            operation_id=ctx.operation_id,
            request_sha256=ctx.request_sha256,
            provider_operation_id=ctx.provider_operation_id,
            observed_at=observed_at,
            **fields,
        )

    def prepare(self, ctx):
        directory = self.root / str(ctx.workspace_id)
        directory.mkdir(mode=0o700)
        content = ("controlled isolated workspace " + str(ctx.workspace_id)).encode()
        target = directory / "fixture.txt"
        target.write_bytes(content)
        target.chmod(0o600)
        receipt = self._receipt(
            ctx,
            state="known_terminal",
            result_code="succeeded",
            tree_sha256=hashlib.sha256(content).hexdigest(),
        )
        self.receipts[ctx.operation_id] = receipt
        path = directory / "prepare-metadata.json"
        path.write_text(
            json.dumps(
                {
                    "operation_id": str(ctx.operation_id),
                    "request_sha256": ctx.request_sha256,
                    "receipt_sha256": receipt.result_sha256,
                    "tree_sha256": receipt.tree_sha256,
                    "observed_at": receipt.observed_at.isoformat(),
                }
            )
        )
        path.chmod(0o600)
        return receipt

    def inspect(self, ctx):
        if not self.available:
            return self._receipt(ctx, state="unknown")
        if ctx.kind == "prepare":
            directory = self.root / str(ctx.workspace_id)
            metadata = json.loads((directory / "prepare-metadata.json").read_text())
            if metadata["operation_id"] != str(ctx.operation_id) or (
                metadata["request_sha256"] != ctx.request_sha256
            ):
                raise DomainError(
                    "SA12_FIXTURE_RECEIPT_MISMATCH", "Fixture receipt mismatch", 409
                )
            actual = hashlib.sha256((directory / "fixture.txt").read_bytes())
            if actual.hexdigest() != metadata["tree_sha256"]:
                raise DomainError(
                    "SA12_FIXTURE_TREE_CHANGED", "Fixture tree changed", 409
                )
            saved = self._receipt(
                ctx,
                state="known_terminal",
                result_code="succeeded",
                tree_sha256=metadata["tree_sha256"],
                observed_at=datetime.fromisoformat(metadata["observed_at"]),
            )
            if saved.result_sha256 != metadata["receipt_sha256"]:
                raise DomainError(
                    "SA12_FIXTURE_RECEIPT_MISMATCH", "Fixture receipt mismatch", 409
                )
            return saved
        directory = self.root / str(ctx.workspace_id)
        directory.mkdir(mode=0o700, exist_ok=True)
        metadata_path = directory / ("resource-" + str(ctx.operation_id) + ".json")
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text())
            if metadata["operation_id"] != str(ctx.operation_id) or (
                metadata["request_sha256"] != ctx.request_sha256
            ):
                raise DomainError(
                    "SA12_FIXTURE_RECEIPT_MISMATCH", "Fixture receipt mismatch", 409
                )
            receipt = self._receipt(
                ctx,
                state="known_terminal",
                result_code="absent",
                processes_stopped=True,
                workspace_fenced=True,
                observed_at=datetime.fromisoformat(metadata["observed_at"]),
            )
            if receipt.result_sha256 != metadata["receipt_sha256"]:
                raise DomainError(
                    "SA12_FIXTURE_RECEIPT_MISMATCH", "Fixture receipt mismatch", 409
                )
            return receipt
        receipt = self._receipt(
            ctx,
            state="known_terminal",
            result_code="absent",
            processes_stopped=True,
            workspace_fenced=True,
        )
        metadata_path.write_text(
            json.dumps(
                {
                    "operation_id": str(ctx.operation_id),
                    "request_sha256": ctx.request_sha256,
                    "observed_at": receipt.observed_at.isoformat(),
                    "receipt_sha256": receipt.result_sha256,
                }
            )
        )
        metadata_path.chmod(0o600)
        return receipt

    def stop(self, ctx):
        return self.inspect(ctx)


def setup(
    engine, tmp_path, *, slots=2, preparing=2, source_slots=2, driver=None, cookie=None
):
    with engine.transaction() as conn:
        row = conn.execute("SELECT current_database() AS name").fetchone()
        assert row["name"].startswith("mgar_v3_test_")
        assert conn.execute(
            "SELECT to_regclass('app.sa_scheduler_assignments') AS name"
        ).fetchone()["name"]
    if cookie is None:
        accounts = WebAccounts(engine)
        user = accounts.create_owner(
            LoginInput(
                username="sa12-owner",
                password=SecretStr("sa12-controlled-password"),
            )
        )
        cookie = accounts.issue_session(user)
    environment = SimpleNamespace(
        engine=engine, service=engine.project_budgets, cookie=cookie
    )
    no_money = {
        key: 0
        for key in (
            "total_micros",
            "hour_micros",
            "day_micros",
            "router_day_micros",
            "run_micros",
            "total_tokens",
            "hour_tokens",
            "day_tokens",
            "run_tokens",
        )
    }
    configure_shared(
        environment,
        organization=shared_definition(caps=no_money),
        project=shared_definition(
            organization_id="sa07-controlled-organization",
            caps=no_money,
        ),
    )
    service = engine.source_scheduler
    driver = driver or FilesystemOnlyDriver(tmp_path / "controlled-scheduler")
    service.driver = driver
    now = datetime.now(UTC)
    configured = service.configure(
        cookie,
        SchedulerConfigureRequest(
            expected_revision=0,
            configuration=SchedulerConfiguration(
                enabled=True,
                valid_from=now - timedelta(minutes=1),
                valid_until=now + timedelta(hours=1),
                limits=SchedulerLimits(
                    slots=slots,
                    preparing=preparing,
                    cpu_millis=4000,
                    memory_mib=4096,
                    queue=32,
                    source_slots=source_slots,
                    lease_seconds=120,
                    preparation_seconds=120,
                    session_ttl_seconds=3600,
                ),
            ),
            review_ref="sa12-explicit-zero-payment-resource-fixture",
        ),
    )
    return SimpleNamespace(
        engine=engine,
        service=service,
        cookie=cookie,
        driver=driver,
        config=configured,
        sequence=1000,
    )


def enqueue(env, *, resources=None):
    env.sequence += 1
    receipt = env.engine.ingest(
        TriggerInput(
            source="web",
            external_event_id=uuid4().hex,
            project_id=env.engine.settings.project_id,
            repository=env.engine.settings.repository,
            issue_number=env.sequence,
            actor=env.engine.settings.local_actor,
            body="/mgar investigate controlled SA12 resource ownership",
        )
    )
    target = env.service.target(env.cookie, receipt.run_id)
    request = SchedulerEnqueueRequest(
        request_id=uuid4(),
        run_id=receipt.run_id,
        expected_epoch=target["epoch"],
        target_sha256=target["target_sha256"],
        resources=resources or ResourceRequest(cpu_millis=1000, memory_mib=256),
        review_ref="sa12-reviewed-existing-original-run",
    )
    return env.service.enqueue(env.cookie, request)


def session(env, ttl=3600):
    return env.service.register_session(
        env.cookie,
        SchedulerSessionRegisterRequest(
            request_id=uuid4(),
            profile_sha256=env.driver.profile_sha256,
            ttl_seconds=ttl,
            review_ref="sa12-explicit-controlled-resource-session",
        ),
    )


def claim(env, worker, *, request_id=None):
    return env.service.claim(
        worker["bearer"],
        SchedulerClaimRequest(
            request_id=request_id or uuid4(),
            session_id=worker["session_id"],
            expected_config_revision=1,
        ),
    )


def ready(env, worker, claimed):
    row = env.service.get_assignment(env.cookie, claimed["assignment_id"])
    operation = claimed["prepare_operation"]
    return env.service.ready(
        worker["bearer"],
        SchedulerReadyRequest(
            assignment_id=row["assignment_id"],
            session_id=worker["session_id"],
            epoch=row["execution_epoch"],
            expected_revision=row["revision"],
            operation_id=operation["operation_id"],
            request_sha256=operation["request_sha256"],
        ),
    )
