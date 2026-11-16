"""Explicit fixture resource driver over the fixed H03 and bound H06 ports.

Nothing is registered or started by importing/constructing this module. Only
trusted server code supplies registrations, profiles and filesystem roots.
Live/Coding execution remains unavailable until the SA13 bridge is accepted.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import UUID, uuid4

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionRequest,
)
from mgar_v3.super_agent.source_scheduler import (
    ResourceContext,
    ResourceOperation,
    ResourceReceipt,
)
from mgar_v3.super_agent.source_scheduler_domain import SchedulerAssignmentRequest

if TYPE_CHECKING:
    from mgar_v3.harness.broker import CapabilityLease, IsolatedExecutionBroker
    from mgar_v3.harness.workspace import Registration, TaskWorkspace, WorkspaceRegistry
    from mgar_v3.super_agent.source_scheduler import SourceScheduler

RUNTIME_VERSION = "source-scheduler-runtime-v1"
MAX_METADATA_BYTES = 1048576
MAX_RELATED = 64
SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _error(code: str) -> DomainError:
    return DomainError(code, "Trusted scheduler resource state could not be established", 409)


def _utc(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise _error("SOURCE_SCHEDULER_RUNTIME_TIME_INVALID")
    return result.astimezone(UTC)


def _json(value: Any) -> bytes:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()


def _root(value: Path) -> Path:
    value = Path(value)
    if not value.is_absolute() or ".." in value.parts:
        raise _error("SOURCE_SCHEDULER_RUNTIME_ROOT_INVALID")
    return value


@dataclass(frozen=True, repr=False)
class SchedulerRuntimeProfile:
    """An explicit server fixture permission, never an Owner/Worker JSON DTO."""

    actor: str
    repository: str
    grant: CapabilityGrant
    request: ExecutionRequest
    image_id: str
    review_ref: str
    provider_generation: str = RUNTIME_VERSION
    execution_mode: Literal["fixture", "live"] = "fixture"

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.actor)
            or self.repository != self.repository.lower()
            or self.grant.actor != self.actor
            or self.grant.repository.lower() != self.repository
            or self.grant.network
            or not self.grant.execute
            or not self.grant.can_execute(self.request.argv)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image_id)
            or not 1 <= len(self.review_ref) <= 256
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", self.provider_generation)
            or (
                self.execution_mode == "live"
                and self.provider_generation != "source-owner-trial-v1"
            )
        ):
            raise _error("SOURCE_SCHEDULER_RUNTIME_PROFILE_INVALID")

    @property
    def sha256(self) -> str:
        values: dict[str, Any] = {
            "version": RUNTIME_VERSION,
            "actor": self.actor,
            "repository": self.repository,
            "grant": self.grant.model_dump(mode="json"),
            "request": self.request.model_dump(mode="json"),
            "image_id": self.image_id,
            "review_ref": self.review_ref,
            "provider_generation": self.provider_generation,
        }
        if self.execution_mode == "live":
            values["execution_mode"] = self.execution_mode
        return digest(values)


@dataclass(frozen=True, repr=False)
class _Stored:
    receipt: ResourceReceipt
    container_id: str | None


class H03SchedulerResourceDriver:
    """Real resource IO is opt-in and always retains original Core authority."""

    def __init__(
        self,
        *,
        scheduler: SourceScheduler,
        registry_root: Path,
        registration: Registration,
        source_root: Path | None,
        profile: SchedulerRuntimeProfile,
        receipt_root: Path,
        image_lock: Path,
        docker: str = "docker",
    ) -> None:
        if (
            registration.registration_hash != registration.computed_hash()
            or not re.fullmatch(r"[a-f0-9]{20}", registration.workspace_id)
            or registration.repository.lower() != profile.repository
            or registration.kind != "local"
            or source_root is None
        ):
            raise _error("SOURCE_SCHEDULER_RUNTIME_REGISTRATION_INVALID")
        self.scheduler = scheduler
        self.registry_root = _root(registry_root)
        self.registration = registration
        self.source_root = _root(source_root)
        self.receipt_root = _root(receipt_root)
        self.image_lock = _root(image_lock)
        if (
            self.registry_root.is_relative_to(self.source_root)
            or self.receipt_root.is_relative_to(self.source_root)
            or self.receipt_root.is_relative_to(self.registry_root)
            or self.registry_root.is_relative_to(self.receipt_root)
        ):
            raise _error("SOURCE_SCHEDULER_RUNTIME_ROOT_OVERLAP")
        self.profile = profile
        self.profile_sha256 = profile.sha256
        self.docker = docker
        self._metadata_lock = threading.RLock()
        self._active: dict[UUID, ResourceContext] = {}
        self._active_lock = threading.RLock()

    def _directory(self, assignment_id: UUID) -> Path:
        key = digest(
            {
                "workspace_id": self.registration.workspace_id,
                "task_id": str(assignment_id),
            }
        )[:20]
        return self.registry_root / self.registration.workspace_id / "tasks" / key

    def _identity(self, project_id: str, assignment_id: UUID, workspace_id: UUID) -> dict[str, str]:
        return {
            "version": RUNTIME_VERSION,
            "project_id": project_id,
            "assignment_id": str(assignment_id),
            "workspace_id": str(workspace_id),
            "registration_id": self.registration.workspace_id,
            "registration_sha256": self.registration.registration_hash,
            "registry_root": str(self.registry_root),
            "source_root": str(self.source_root),
            "task_id": str(assignment_id),
            "task_directory": str(self._directory(assignment_id)),
            "profile_sha256": self.profile_sha256,
            "provider_generation": self.profile.provider_generation,
        }

    def workspace_identity(
        self,
        project_id: str,
        assignment_id: UUID,
        workspace_id: UUID,
    ) -> str:
        """Pure: the assignment UUID is distinct from H03's registration ID."""
        return digest(self._identity(project_id, assignment_id, workspace_id))

    def _check(self, context: ResourceContext, *, executing: bool) -> dict[str, Any]:
        if (
            context.profile_sha256 != self.profile_sha256
            or context.provider_operation_id != self._name(context)
            or context.workspace_sha256
            != self.workspace_identity(
                context.project_id,
                context.assignment_id,
                context.workspace_id,
            )
        ):
            raise _error("SOURCE_SCHEDULER_RUNTIME_BINDING_INVALID")
        facts = self.scheduler.capability_context(context)
        if executing and (
            context.cleanup_only
            or facts.get("execution_authorized") is not True
            or facts.get("execution_mode") != self.profile.execution_mode
            or facts.get("actor") != self.profile.actor
            or str(facts.get("repository", "")).lower() != self.profile.repository
            or facts.get("target_sha256") != context.target_sha256
            or facts.get("workspace_sha256") != context.workspace_sha256
            or (facts.get("read_only") is not False and self.profile.grant.write_paths)
        ):
            raise _error("SOURCE_SCHEDULER_RUNTIME_FIXTURE_AUTHORITY_REQUIRED")
        return facts

    @staticmethod
    def _private_directory(directory: Path) -> None:
        for parent in (directory, *directory.parents):
            if parent.is_symlink():
                raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not directory.is_dir():
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        directory.chmod(0o700)

    def _metadata_directory(self, context: ResourceContext) -> Path:
        directory = self.receipt_root / str(context.assignment_id)
        self._private_directory(directory)
        return directory

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
            raw = handle.read(MAX_METADATA_BYTES + 1)
        if len(raw) > MAX_METADATA_BYTES:
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        return value

    @staticmethod
    def _write(path: Path, value: dict[str, Any], *, immutable: bool = False) -> None:
        raw = _json(value)
        if len(raw) > MAX_METADATA_BYTES:
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        temporary = path.parent / (".receipt-" + uuid4().hex)
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if immutable:
                try:
                    os.link(temporary, path, follow_symlinks=False)
                except FileExistsError:
                    if H03SchedulerResourceDriver._read(path) != value:
                        raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_CONFLICT") from None
            else:
                os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _bind_prepare(self, context: ResourceContext) -> None:
        directory = self._metadata_directory(context)
        self._write(
            directory / "workspace.json",
            {
                **self._identity(context.project_id, context.assignment_id, context.workspace_id),
                "prepare_operation_id": str(context.operation_id),
                "prepare_request_sha256": context.request_sha256,
            },
            immutable=True,
        )

    def _begin_dispatch(self, context: ResourceContext) -> bool:
        """A persistent marker precedes create IO; its presence forbids replay."""
        with self._metadata_lock:
            directory = self._metadata_directory(context) / str(context.operation_id)
            self._private_directory(directory)
            path = directory / "dispatch-intent.json"
            intent = {
                "operation_id": str(context.operation_id),
                "request_sha256": context.request_sha256,
                "profile_sha256": context.profile_sha256,
                "workspace_sha256": context.workspace_sha256,
                "container_name": self._name(context),
                "ownership_labels": self._labels(context),
            }
            previous = self._read(path)
            if previous is not None:
                if {
                    key: value
                    for key, value in previous.items()
                    if key not in {"registered_at", "deadline_at"}
                } != intent:
                    raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_CONFLICT")
                return False
            at = datetime.now(UTC)
            intent["registered_at"] = at.isoformat()
            intent["deadline_at"] = (at + timedelta(seconds=self._timeout())).isoformat()
            self._write(path, intent, immutable=True)
            return True

    def _timeout(self) -> float:
        return min(self.profile.request.timeout_seconds, self.profile.grant.max_seconds, 600)

    def _deadline(self, context: ResourceContext) -> datetime | None:
        directory = self._metadata_directory(context) / str(context.operation_id)
        intent = self._read(directory / "dispatch-intent.json")
        if intent is None:
            return None
        if (
            intent.get("operation_id") != str(context.operation_id)
            or intent.get("request_sha256") != context.request_sha256
            or intent.get("workspace_sha256") != context.workspace_sha256
            or intent.get("profile_sha256") != context.profile_sha256
            or intent.get("container_name") != self._name(context)
            or intent.get("ownership_labels") != self._labels(context)
        ):
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        registered = _utc(intent["registered_at"])
        deadline = _utc(intent["deadline_at"])
        if not 0 < (deadline - registered).total_seconds() <= self._timeout() + 0.000001:
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        return deadline

    def _check_image(self) -> None:
        descriptor = os.open(self.image_lock, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise _error("SOURCE_SCHEDULER_RUNTIME_IMAGE_CHANGED")
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise _error("SOURCE_SCHEDULER_RUNTIME_IMAGE_CHANGED")
        lock = json.loads(raw)
        if not isinstance(lock, dict) or lock.get("image_id") != self.profile.image_id:
            raise _error("SOURCE_SCHEDULER_RUNTIME_IMAGE_CHANGED")

    @staticmethod
    def _decode_receipt(value: dict[str, Any], *, depth: int = 0) -> ResourceReceipt:
        if depth > 1 or len(value.get("related", [])) > MAX_RELATED:
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        data = dict(value)
        for key in ("assignment_id", "run_id", "session_id", "workspace_id", "operation_id"):
            data[key] = UUID(data[key])
        data["observed_at"] = _utc(data["observed_at"])
        data["related"] = tuple(
            H03SchedulerResourceDriver._decode_receipt(item, depth=depth + 1)
            for item in data.get("related", [])
        )
        return ResourceReceipt(**data)

    def _load(self, context: ResourceContext) -> _Stored | None:
        directory = self._metadata_directory(context) / str(context.operation_id)
        self._private_directory(directory)
        head = self._read(directory / "head.json")
        if head is None:
            return None
        key = head.get("sha256")
        if not isinstance(key, str) or not SHA256.fullmatch(key):
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        value = self._read(directory / (key + ".json"))
        if value is None or digest(value) != key:
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        if (
            value.get("version") != RUNTIME_VERSION
            or value.get("profile_sha256") != context.profile_sha256
            or value.get("workspace_sha256") != context.workspace_sha256
        ):
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        receipt = self._decode_receipt(value["receipt"])
        self._match(context, receipt)
        if value.get("receipt_sha256") != receipt.result_sha256:
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        container_id = value.get("container_id")
        if container_id is not None and (
            not isinstance(container_id, str) or not SHA256.fullmatch(container_id)
        ):
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")
        return _Stored(receipt, container_id)

    @staticmethod
    def _match(context: ResourceContext, receipt: ResourceReceipt) -> None:
        if any(
            (
                receipt.project_id != context.project_id,
                receipt.assignment_id != context.assignment_id,
                receipt.run_id != context.run_id,
                receipt.session_id != context.session_id,
                receipt.workspace_id != context.workspace_id,
                receipt.epoch != context.epoch,
                receipt.operation_id != context.operation_id,
                receipt.request_sha256 != context.request_sha256,
                receipt.provider_operation_id != context.provider_operation_id,
            )
        ):
            raise _error("SOURCE_SCHEDULER_RUNTIME_METADATA_INVALID")

    def _save(
        self,
        context: ResourceContext,
        receipt: ResourceReceipt,
        *,
        container_id: str | None = None,
    ) -> ResourceReceipt:
        self._match(context, receipt)
        with self._metadata_lock:
            previous = self._load(context)
            if previous is not None:
                if previous.container_id is not None and container_id not in {
                    None,
                    previous.container_id,
                }:
                    raise _error("SOURCE_SCHEDULER_RUNTIME_CONTAINER_CHANGED")
                container_id = container_id or previous.container_id
                if previous.receipt.state == "known_terminal":
                    if previous.receipt.result_sha256 != receipt.result_sha256:
                        raise _error("SOURCE_SCHEDULER_RUNTIME_RECEIPT_CONFLICT")
                    return previous.receipt
                if previous.receipt.state == receipt.state == "running":
                    return previous.receipt
            value = {
                "version": RUNTIME_VERSION,
                "profile_sha256": context.profile_sha256,
                "workspace_sha256": context.workspace_sha256,
                "receipt": json.loads(_json(asdict(receipt))),
                "receipt_sha256": receipt.result_sha256,
                "container_id": container_id,
            }
            key = digest(value)
            directory = self._metadata_directory(context) / str(context.operation_id)
            self._write(directory / (key + ".json"), value, immutable=True)
            self._write(directory / "head.json", {"sha256": key})
        return receipt

    @staticmethod
    def _receipt(
        context: ResourceContext,
        state: Literal["running", "unknown", "known_terminal"],
        *,
        code: Literal["succeeded", "failed", "cancelled", "absent"] | None = None,
        tree: str | None = None,
        exit_code: int | None = None,
        observed_at: datetime | None = None,
        stopped: bool = False,
        fenced: bool = False,
        related: tuple[ResourceReceipt, ...] = (),
    ) -> ResourceReceipt:
        return ResourceReceipt(
            project_id=context.project_id,
            assignment_id=context.assignment_id,
            run_id=context.run_id,
            session_id=context.session_id,
            workspace_id=context.workspace_id,
            epoch=context.epoch,
            operation_id=context.operation_id,
            request_sha256=context.request_sha256,
            provider_operation_id=context.provider_operation_id,
            state=state,
            observed_at=observed_at or datetime.now(UTC),
            result_code=code,
            tree_sha256=tree,
            exit_code=exit_code,
            processes_stopped=stopped,
            workspace_fenced=fenced,
            related=related,
        )

    def _registry(self) -> WorkspaceRegistry:
        from mgar_v3.harness.workspace import WorkspaceRegistry

        registry = WorkspaceRegistry(self.registry_root, limits=self.registration.limits)
        if registry.root != self.registry_root:
            raise _error("SOURCE_SCHEDULER_RUNTIME_ROOT_CHANGED")
        return registry

    def _open(self, context: ResourceContext) -> TaskWorkspace:
        workspace = self._registry().open_task_workspace(
            self.registration,
            str(context.assignment_id),
        )
        if (
            workspace.root != self._directory(context.assignment_id)
            or workspace.manifest.task_id != str(context.assignment_id)
            or workspace.manifest.workspace_id != self.registration.workspace_id
            or workspace.registration.registration_hash != self.registration.registration_hash
        ):
            raise _error("SOURCE_SCHEDULER_RUNTIME_WORKSPACE_CHANGED")
        return workspace

    def _prepared_receipt(self, context: ResourceContext) -> ResourceReceipt:
        workspace = self._open(context)
        actual = workspace.tree_hash()
        if actual != workspace.manifest.tree_hash:
            raise _error("SOURCE_SCHEDULER_RUNTIME_TREE_CHANGED")
        return self._receipt(
            context,
            "known_terminal",
            code="succeeded",
            tree=actual,
            observed_at=_utc(workspace.manifest.created_at),
        )

    def prepare(self, context: ResourceContext) -> ResourceReceipt:
        """Core's prepare dispatch guard has already committed its IO intent."""
        facts = self._check(context, executing=True)
        if context.kind != "prepare" or facts.get("operation_state") != "running":
            raise _error("SOURCE_SCHEDULER_RUNTIME_PREPARE_NOT_DISPATCHED")
        with self._metadata_lock:
            self._bind_prepare(context)
            stored = self._load(context)
            if stored is not None:
                if stored.receipt.state == "known_terminal":
                    current = self._prepared_receipt(context)
                    if current.result_sha256 != stored.receipt.result_sha256:
                        raise _error("SOURCE_SCHEDULER_RUNTIME_TREE_CHANGED")
                return stored.receipt
            if self._directory(context.assignment_id).exists():
                return self._save(context, self._prepared_receipt(context))
            self._registry().create_task_workspace(
                self.registration,
                str(context.assignment_id),
                source_root=self.source_root,
            )
            return self._save(context, self._prepared_receipt(context))

    def _lease(self, context: ResourceContext, facts: dict[str, Any]) -> CapabilityLease:
        from mgar_v3.harness.broker import issue_lease

        deadline = min(
            facts["session"].expires_at,
            facts["configuration"].configuration.valid_until,
        )
        seconds = int((deadline - datetime.now(UTC)).total_seconds())
        if seconds <= 0:
            raise _error("SOURCE_SCHEDULER_RUNTIME_LEASE_EXPIRED")
        writable = bool(self.profile.grant.write_paths)
        return issue_lease(
            mode="modify_test" if writable else "investigate",
            actor=self.profile.actor,
            workspace_id=str(context.workspace_id),
            repository=self.profile.repository,
            execution_epoch=context.epoch,
            read_paths=self.profile.grant.read_paths,
            write_paths=self.profile.grant.write_paths,
            allowed_commands=self.profile.grant.allowed_commands,
            timeout_seconds=min(600, self.profile.grant.max_seconds),
            memory_mb=context.resources.memory_mib,
            cpus=context.resources.cpu_millis / 1000,
            approved_by=self.profile.actor if writable else "",
            approval_ref=self.profile.review_ref if writable else "",
            ttl_seconds=seconds,
        )

    def _broker(
        self,
        context: ResourceContext,
        facts: dict[str, Any],
        *,
        cleanup: bool = False,
    ) -> IsolatedExecutionBroker:
        from mgar_v3.harness.broker import CapabilityAuthority, IsolatedExecutionBroker, issue_lease

        if cleanup:
            self._private_directory(self.receipt_root)
            lease = issue_lease(
                mode="investigate",
                actor=self.profile.actor,
                workspace_id=str(context.workspace_id),
                repository=self.profile.repository,
                execution_epoch=context.epoch,
                ttl_seconds=1,
            )
            authority = CapabilityAuthority(lease, budget_check=lambda: False)
            root = self.receipt_root
        else:
            lease = self._lease(context, facts)

            def current() -> CapabilityLease:
                # Never reuse facts captured on guard entry: this runs again
                # after strict inspect, directly before the actual H06 start.
                self._check(context, executing=True)
                return lease

            authority = CapabilityAuthority(lease_source=current, budget_check=lambda: True)
            root = self._open(context).root
        return IsolatedExecutionBroker(
            root,
            authority,
            image_id=self.profile.image_id,
            lock_path=self.image_lock,
            docker=self.docker,
        )

    @staticmethod
    def _name(context: ResourceContext) -> str:
        return "mgar-sa12-" + context.operation_id.hex

    @staticmethod
    def _labels(context: ResourceContext) -> dict[str, str]:
        return {
            "mgar.sa12.assignment": str(context.assignment_id),
            "mgar.sa12.operation": str(context.operation_id),
            "mgar.sa12.workspace": str(context.workspace_id),
            "mgar.sa12.project": context.project_id,
        }

    @staticmethod
    def _related_context(context: ResourceContext, operation: ResourceOperation) -> ResourceContext:
        return replace(
            context,
            operation_id=operation.operation_id,
            kind=operation.kind,
            request_sha256=operation.request_sha256,
            provider_operation_id=operation.provider_operation_id,
            operations=(),
            cleanup_only=True,
        )

    def _observe_process(
        self,
        context: ResourceContext,
        broker: IsolatedExecutionBroker,
        *,
        stop: bool,
    ) -> ResourceReceipt:
        if context.provider_operation_id != self._name(context):
            raise _error("SOURCE_SCHEDULER_RUNTIME_BINDING_INVALID")
        stored = self._load(context)
        status = broker.inspect_bound_command(self._name(context), self._labels(context))
        if (
            stored is not None
            and stored.container_id is not None
            and (status.container_id is not None and status.container_id != stored.container_id)
        ):
            raise _error("SOURCE_SCHEDULER_RUNTIME_CONTAINER_CHANGED")
        if stop and status.state != "missing":
            status = broker.stop_bound_command(self._name(context), self._labels(context))
        if status.state == "missing":
            if stored is not None and stored.receipt.state == "known_terminal":
                return stored.receipt
            receipt = self._receipt(context, "known_terminal", code="absent", stopped=True)
        elif status.state in {"exited", "dead"}:
            if stored is not None and stored.receipt.state == "known_terminal":
                return stored.receipt
            receipt = self._receipt(
                context,
                "known_terminal",
                code="succeeded" if status.exit_code == 0 else "failed",
                exit_code=status.exit_code,
                stopped=True,
            )
        else:
            if stored is not None and stored.receipt.state == "known_terminal":
                raise _error("SOURCE_SCHEDULER_RUNTIME_TERMINAL_PROCESS_REAPPEARED")
            return self._save(
                context, self._receipt(context, "running"), container_id=status.container_id
            )
        return self._save(context, receipt, container_id=status.container_id)

    def _fence_workspace(self, context: ResourceContext) -> None:
        root = self._directory(context.assignment_id)
        if root.is_symlink():
            raise _error("SOURCE_SCHEDULER_RUNTIME_WORKSPACE_CHANGED")
        if not root.exists():
            return
        self._open(context)
        for current, directories, files in os.walk(root, followlinks=False):
            directory = Path(current)
            for name in files:
                target = directory / name
                if target.is_symlink():
                    continue
                if not target.is_file():
                    raise _error("SOURCE_SCHEDULER_RUNTIME_WORKSPACE_CHANGED")
                target.chmod(0o400)
            directories[:] = [name for name in directories if not (directory / name).is_symlink()]
            directory.chmod(0o500)

    def _inspect_or_stop(self, context: ResourceContext, *, stop: bool) -> ResourceReceipt:
        facts = self._check(context, executing=False)
        if not context.cleanup_only or context.kind not in {"stop", "inspect"}:
            raise _error("SOURCE_SCHEDULER_RUNTIME_CLEANUP_REQUIRED")
        if len(context.operations) > MAX_RELATED + 1:
            raise _error("SOURCE_SCHEDULER_RUNTIME_OPERATION_CAPACITY")
        stored = self._load(context)
        broker = self._broker(context, facts, cleanup=True)
        related: list[ResourceReceipt] = []
        for operation in context.operations:
            if operation.operation_id == context.operation_id:
                continue
            old = self._related_context(context, operation)
            if operation.kind == "prepare":
                previous = self._load(old)
                if previous is not None and previous.receipt.state == "known_terminal":
                    observed = previous.receipt
                elif self._directory(context.assignment_id).exists():
                    observed = self._save(old, self._prepared_receipt(old))
                else:
                    observed = self._save(
                        old,
                        self._receipt(
                            old,
                            "known_terminal",
                            code="absent",
                            stopped=True,
                        ),
                    )
            elif operation.kind in {"start", "job"}:
                observed = self._observe_process(old, broker, stop=stop)
                # Exited/dead is useful terminal operation evidence, but only
                # strict missing proves the owned container has been removed.
                # An unknown stop is inspected, never automatically repeated.
                status = broker.inspect_bound_command(self._name(old), self._labels(old))
                if status.state != "missing":
                    return self._receipt(context, "unknown")
            else:
                # A previous unresolved stop/inspection must itself be reconciled;
                # a new receipt cannot silently pretend that it was completed.
                if operation.state != "known_terminal":
                    return self._receipt(context, "unknown")
                continue
            if observed.state != "known_terminal":
                return self._receipt(context, "unknown")
            related.append(observed)
        self._fence_workspace(context)
        if stored is not None and stored.receipt.state == "known_terminal":
            return stored.receipt
        return self._save(
            context,
            self._receipt(
                context,
                "known_terminal",
                code="succeeded",
                stopped=True,
                fenced=True,
                related=tuple(related),
            ),
        )

    def inspect(self, context: ResourceContext) -> ResourceReceipt:
        """Actual state is checked before replaying an unchanged terminal receipt."""
        if context.kind == "prepare":
            self._check(context, executing=False)
            stored = self._load(context)
            current = self._prepared_receipt(context)
            if stored is not None:
                if current.result_sha256 != stored.receipt.result_sha256:
                    raise _error("SOURCE_SCHEDULER_RUNTIME_TREE_CHANGED")
                return stored.receipt
            return self._save(context, current)
        if context.kind in {"start", "job"}:
            facts = self._check(context, executing=False)
            broker = self._broker(context, facts, cleanup=True)
            return self._observe_process(context, broker, stop=False)
        return self._inspect_or_stop(context, stop=False)

    def stop(self, context: ResourceContext) -> ResourceReceipt:
        return self._inspect_or_stop(context, stop=True)

    def execute_preconfigured(
        self,
        bearer: str,
        request: SchedulerAssignmentRequest,
        *,
        kind: Literal["start", "job"] = "start",
    ) -> ResourceReceipt:
        """Trusted in-process fixture helper; no HTTP command/grant surface."""
        operation_hash = digest(
            {
                "version": RUNTIME_VERSION,
                "profile_sha256": self.profile_sha256,
                "assignment_id": str(request.assignment_id),
                "session_id": str(request.session_id),
                "epoch": request.epoch,
                "kind": kind,
            }
        )
        context = self.scheduler.begin_operation(bearer, request, kind, operation_hash)
        with self._active_lock:
            self._active[context.operation_id] = context
        try:
            stored = self._load(context)
            if stored is not None:
                # Repeating the helper only queries the original identity; it
                # cannot repeat create/start, including after a lost receipt.
                receipt = self.inspect(context)
            else:
                facts = self._check(context, executing=True)
                if facts.get("operation_state") != "requested" or any(
                    item.kind in {"start", "job"}
                    and item.operation_id != context.operation_id
                    and item.state != "known_terminal"
                    for item in context.operations
                ):
                    raise _error("SOURCE_SCHEDULER_RUNTIME_PROCESS_ALREADY_PENDING")
                self._check_image()
                if not self._begin_dispatch(context):
                    receipt = self.inspect(context)
                else:
                    broker = self._broker(context, facts)
                    execution = self.profile.request.model_copy(
                        update={
                            "operation_id": str(context.operation_id),
                            "execution_epoch": context.epoch,
                        }
                    )
                    grant = self.profile.grant.model_copy(
                        update={
                            "workspace_id": str(context.workspace_id),
                        }
                    )
                    broker.launch(
                        execution,
                        grant,
                        CancellationToken(),
                        container_name=self._name(context),
                        ownership_labels=self._labels(context),
                        dispatch_guard=lambda: self.scheduler.dispatch_guard(context),
                    )
                    receipt = self._observe_process(context, broker, stop=False)
        except (DomainError, OSError, ValueError):
            receipt = self._receipt(context, "unknown")
            try:
                receipt = self._save(context, receipt)
            except (DomainError, OSError, ValueError):
                pass  # Preserve unknown in the original PG operation even if local storage failed.
        self.scheduler.record_resource_receipt(context, receipt)
        if receipt.state == "known_terminal":
            with self._active_lock:
                self._active.pop(context.operation_id, None)
            return receipt
        return self.wait_preconfigured(context)

    def _cleanup_observation(self, context: ResourceContext) -> ResourceReceipt:
        current = self.scheduler.cleanup_context(context.assignment_id, context.operation_id)
        receipt = self.inspect(current)
        self.scheduler.record_resource_receipt(current, receipt)
        return receipt

    def _request_timeout(self, context: ResourceContext) -> None:
        try:
            self.scheduler.request_stop(context, "COMMAND_TIMEOUT")
        except DomainError:
            current = self.scheduler.cleanup_context(context.assignment_id, context.operation_id)
            self.scheduler.request_stop(current, "COMMAND_TIMEOUT")

    def maintain_deadlines(self) -> None:
        """Explicit Supervisor hook; metadata is not a second process registry.

        Identities come from the original PG pending operations as well as the
        current process's issued-context cache. Rebuilding this driver does
        not reset a command's persisted deadline or grant new execution.
        """
        with self._active_lock:
            contexts = {item.operation_id: item for item in self._active.values()}
        for assignment_id, operation_id in self.scheduler.pending_resource_operations():
            if operation_id not in contexts:
                contexts[operation_id] = self.scheduler.cleanup_context(
                    assignment_id,
                    operation_id,
                )
        for context in contexts.values():
            stored = self._load(context)
            if stored is not None and stored.receipt.state == "known_terminal":
                with self._active_lock:
                    self._active.pop(context.operation_id, None)
                continue
            deadline = self._deadline(context)
            if deadline is not None and datetime.now(UTC) >= deadline:
                self._request_timeout(context)

    def wait_preconfigured(
        self,
        context: ResourceContext,
        *,
        interval: float = 0.1,
    ) -> ResourceReceipt:
        """Poll one exact intent; timeout/fencing requests real Core cleanup."""
        _interval(interval)
        while True:
            with self._active_lock:
                current = self._active.get(context.operation_id, context)
            try:
                self._check(current, executing=True)
                deadline = self._deadline(current)
                if deadline is not None and datetime.now(UTC) >= deadline:
                    self._request_timeout(current)
                    self.scheduler.maintain()
                    return self._cleanup_observation(current)
                receipt = self.inspect(current)
                self.scheduler.record_resource_receipt(current, receipt)
                if receipt.state == "known_terminal":
                    with self._active_lock:
                        self._active.pop(context.operation_id, None)
                    return receipt
            except Exception:
                # This may be a lost Worker/expired epoch or an unknown daemon.
                # Core decides whether to dispatch a new stop or only inspect
                # an existing unknown stop; this loop never directly repeats IO.
                try:
                    self.scheduler.maintain()
                    return self._cleanup_observation(current)
                except Exception:
                    stored = self._load(current)
                    if stored is not None and stored.receipt.state == "known_terminal":
                        return stored.receipt
                    return self._receipt(current, "unknown")
            time.sleep(interval)


def _interval(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 0.5:
        raise _error("SOURCE_SCHEDULER_RUNTIME_INTERVAL_INVALID")


class RuntimeLeaseSupervisor:
    """Explicit parent-side resource maintenance; construction starts nothing."""

    def __init__(self, scheduler: SourceScheduler, *, interval: float = 0.25) -> None:
        _interval(interval)
        self.scheduler = scheduler
        self.interval = interval
        self.last_error_code: str | None = None
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        while not self._stopped.is_set():
            observed_error: str | None = None
            try:
                driver = self.scheduler.driver
                if isinstance(driver, H03SchedulerResourceDriver):
                    driver.maintain_deadlines()
            except Exception as error:
                observed_error = (
                    error.code if isinstance(error, DomainError) else type(error).__name__
                )
            try:
                # A metadata/deadline error must not suppress Core's independent
                # lease/session/Source expiry and already-persisted cleanup.
                self.scheduler.maintain()
            except Exception as error:
                observed_error = (
                    error.code if isinstance(error, DomainError) else type(error).__name__
                )
            self.last_error_code = observed_error
            self._stopped.wait(self.interval)

    def start(self) -> Self:
        if self.scheduler.driver is None:
            raise _error("SOURCE_SCHEDULER_RUNTIME_DRIVER_REQUIRED")
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="mgar-sa12-lease-supervisor",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        self._stopped.set()
        thread = self._thread
        if thread is not None:
            if thread is threading.current_thread():
                raise _error("SOURCE_SCHEDULER_RUNTIME_SUPERVISOR_JOIN_INVALID")
            thread.join()
        self._thread = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
