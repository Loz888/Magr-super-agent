"""H04 capability leases and the single real-container execution broker.

Every built-in or future tool that needs a process goes through
``IsolatedExecutionBroker``.  The lease is the authority: actor, workspace,
read/write scope, command allowlist, network intent, timeout, resource limits,
credentials and external-write permission.  The project process receives none
of the host environment or credentials and runs in a real container with a
read-only root, no network, a non-root uid and cgroup limits.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionReceipt,
    ExecutionRequest,
    normalize_relative_path,
    sha256_text,
)

LEASE_VERSION = "harness-capability-lease-v1"
SANDBOX_UID = "65532:65532"
DEFAULT_IMAGE_LOCK = Path(".local/r03/sandbox-lock.json")


_MASK_LOCK = threading.Lock()
_MASK_FILE: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


class CapabilityLease(Model):
    """One explicit, scoped capability grant for a task workspace."""

    schema_version: str = LEASE_VERSION
    lease_id: str
    mode: Literal["investigate", "modify_test", "publish"]
    actor: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    workspace_id: str = Field(min_length=1, max_length=100)
    repository: str
    execution_epoch: int = Field(ge=1)
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...] = ()
    allowed_commands: tuple[str, ...]
    network_hosts: tuple[str, ...] = ()
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_output_bytes: int = Field(default=65536, ge=1024, le=1_048_576)
    memory_mb: int = Field(default=256, ge=32, le=4096)
    cpus: float = Field(default=1.0, gt=0, le=8)
    pids: int = Field(default=64, ge=8, le=1024)
    scratch_mb: int = Field(default=64, ge=16, le=2048)
    credentials: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = (".git", ".mgar", ".env")
    max_calls: int = Field(default=100, ge=1, le=100_000)
    external_write: bool = False
    approved_by: str = ""
    approval_ref: str = ""
    issued_at: str
    expires_at: str
    lease_hash: str

    def computed_hash(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"lease_hash"}))

    def expired(self, moment: datetime | None = None) -> bool:
        return datetime.fromisoformat(self.expires_at) <= (moment or _now())


def issue_lease(
    *,
    mode: Literal["investigate", "modify_test", "publish"],
    actor: str,
    workspace_id: str,
    repository: str,
    execution_epoch: int,
    read_paths: Sequence[str] = ("",),
    write_paths: Sequence[str] = (),
    allowed_commands: Sequence[str] = ("cat", "ls"),
    network_hosts: Sequence[str] = (),
    timeout_seconds: int = 60,
    max_output_bytes: int = 65536,
    memory_mb: int = 256,
    cpus: float = 1.0,
    pids: int = 64,
    scratch_mb: int = 64,
    credentials: Sequence[str] = (),
    protected_paths: Sequence[str] = (".git", ".mgar", ".env"),
    max_calls: int = 100,
    approved_by: str = "",
    approval_ref: str = "",
    ttl_seconds: int = 3600,
) -> CapabilityLease:
    """Issue one preset lease and fail closed for unsafe combinations."""

    if network_hosts:
        raise DomainError(
            "HARNESS_LEASE_DENIED",
            "H04 does not authorize network egress",
            403,
        )
    if mode == "investigate":
        if write_paths:
            raise DomainError("HARNESS_LEASE_DENIED", "Investigate leases cannot write", 403)
        if approved_by or approval_ref:
            raise DomainError(
                "HARNESS_LEASE_DENIED", "Investigate leases do not accept write approval", 422
            )
        external_write = False
    elif mode == "modify_test":
        if not write_paths:
            raise DomainError(
                "HARNESS_LEASE_DENIED", "Writable mode requires explicit write paths", 403
            )
        if not approved_by or not approval_ref:
            raise DomainError(
                "HARNESS_LEASE_DENIED", "First writable scope needs explicit approval", 403
            )
        external_write = False
    else:
        if not approved_by or not approval_ref:
            raise DomainError("HARNESS_LEASE_DENIED", "Publish mode needs explicit approval", 403)
        external_write = True
    issued = _now()
    lease = CapabilityLease(
        lease_id="",
        mode=mode,
        actor=actor,
        workspace_id=workspace_id,
        repository=repository.lower(),
        execution_epoch=execution_epoch,
        read_paths=tuple(read_paths),
        write_paths=tuple(write_paths),
        allowed_commands=tuple(allowed_commands),
        network_hosts=tuple(network_hosts),
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        memory_mb=memory_mb,
        cpus=cpus,
        pids=pids,
        scratch_mb=scratch_mb,
        credentials=tuple(credentials),
        protected_paths=tuple(protected_paths),
        max_calls=max_calls,
        external_write=external_write,
        approved_by=approved_by,
        approval_ref=approval_ref,
        issued_at=issued.isoformat(),
        expires_at=(issued + timedelta(seconds=ttl_seconds)).isoformat(),
        lease_hash="",
    )
    lease = lease.model_copy(update={"lease_id": lease.computed_hash()[:32]})
    return lease.model_copy(update={"lease_hash": lease.computed_hash()})


def _docker_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DOCKER_HOST": os.environ.get("DOCKER_HOST", ""),
    }


def _masked_file() -> str:
    """A host file used to mask an existing protected file.

    ``/dev/null`` accepted writes even through a read-only bind, so a project
    command could "write" its way past a protected file.  An unreadable empty
    regular file gives both reads and writes ``EACCES`` while the real file
    stays untouched.
    """

    global _MASK_FILE
    with _MASK_LOCK:
        if _MASK_FILE is None:
            directory = Path(tempfile.mkdtemp(prefix="mgar-harness-mask-"))
            path = directory / "masked"
            path.write_bytes(b"")
            path.chmod(0o000)
            _MASK_FILE = str(path)
        return _MASK_FILE


def _within(path: str, roots: Sequence[str]) -> bool:
    normalized = normalize_relative_path(path)
    for root in roots:
        prefix = normalize_relative_path(root)
        if prefix == "" or normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def _intersect(paths: Sequence[str], other: Sequence[str]) -> tuple[str, ...]:
    """Return the most specific root shared by both authorization scopes."""

    result: set[str] = set()
    for path in paths:
        left = normalize_relative_path(path)
        for candidate in other:
            right = normalize_relative_path(candidate)
            if _within(left, (right,)):
                result.add(left)
            elif _within(right, (left,)):
                result.add(right)
    return tuple(sorted(result))


def _is_protected(path: str, protected_paths: Sequence[str]) -> bool:
    normalized = normalize_relative_path(path)
    for protected in protected_paths:
        prefix = normalize_relative_path(protected)
        if prefix and (normalized == prefix or normalized.startswith(prefix + "/")):
            return True
    return False


class CapabilityAuthority:
    """Current trusted lease plus revocation and budget checks.

    The Broker and every built-in tool read the lease through this object on
    each call, so an expired, revoked or replaced lease cannot be bypassed by
    holding an old context or a previously constructed Broker.  A production
    integration can pass ``lease_source`` so the current lease is re-read from
    the trusted authority on every call; ``replace`` supports local wiring.
    """

    def __init__(
        self,
        lease: CapabilityLease | None = None,
        *,
        lease_source: Callable[[], CapabilityLease] | None = None,
        budget_check: Callable[[], bool],
        revocation_check: Callable[[], bool] | None = None,
    ) -> None:
        if (lease is None) == (lease_source is None):
            raise DomainError(
                "HARNESS_AUTHORITY_REQUIRED",
                "Provide exactly one of lease or lease_source",
                409,
            )
        self._lease = lease
        self._lease_source = lease_source
        self._budget_check = budget_check
        self._revocation_check = revocation_check
        self._revoked = False

    def revoke(self) -> None:
        self._revoked = True

    def replace(self, lease: CapabilityLease) -> None:
        if self._lease_source is not None:
            raise DomainError(
                "HARNESS_AUTHORITY_DENIED",
                "A dynamic lease source cannot be replaced locally",
                409,
            )
        self._lease = lease

    def current(self) -> CapabilityLease:
        lease = self.peek()
        if not self._budget_check():
            raise DomainError("HARNESS_BUDGET_EXHAUSTED", "Tool budget is exhausted", 429)
        return lease

    def peek(self) -> CapabilityLease:
        """Return the validated lease without charging the call budget.

        Runtime liveness checks (a command handle noticing that its lease was
        revoked, expired or replaced) must not spend the model/actor budget:
        they observe state, they do not perform work.
        """

        lease = self._lease_source() if self._lease_source is not None else self._lease
        if lease is None:
            raise DomainError("HARNESS_AUTHORITY_REQUIRED", "No current lease", 409)
        if lease.lease_hash != lease.computed_hash():
            raise DomainError("HARNESS_LEASE_DENIED", "Lease hash changed", 409)
        if self._revoked or (self._revocation_check and self._revocation_check()):
            raise DomainError("HARNESS_LEASE_REVOKED", "Capability lease was revoked", 403)
        if lease.expired():
            raise DomainError("HARNESS_LEASE_EXPIRED", "Capability lease has expired", 403)
        return lease

    def check_scope(
        self,
        *,
        actor: str,
        workspace_id: str,
        repository: str,
        execution_epoch: int,
        paths: Sequence[str],
        write: bool,
    ) -> CapabilityLease:
        lease = self.current()
        if actor != lease.actor:
            raise DomainError("HARNESS_ACTOR_MISMATCH", "Actor does not match lease", 403)
        if workspace_id != lease.workspace_id:
            raise DomainError("HARNESS_WORKSPACE_MISMATCH", "Workspace does not match lease", 403)
        if repository.lower() != lease.repository:
            raise DomainError("HARNESS_REPOSITORY_MISMATCH", "Repository does not match lease", 403)
        if execution_epoch != lease.execution_epoch:
            raise DomainError("HARNESS_EPOCH_MISMATCH", "Execution epoch is stale", 409)
        if write and lease.mode == "investigate":
            raise DomainError("HARNESS_WRITE_DENIED", "Investigate lease cannot write", 403)
        roots = lease.write_paths if write else lease.read_paths
        for path in paths:
            if _is_protected(path, lease.protected_paths):
                raise DomainError(
                    "HARNESS_PROTECTED_PATH_DENIED",
                    "Path is protected workspace metadata",
                    403,
                )
            if not _within(path, roots):
                raise DomainError(
                    "HARNESS_WRITE_DENIED" if write else "HARNESS_READ_DENIED",
                    "Path is outside the leased scope",
                    403,
                )
        return lease


@dataclass(frozen=True)
class LaunchedCommand:
    """One container that was created and started by the broker.

    The container name is an internal capability: callers poll, page logs and
    stop only through the broker, never with their own docker command.
    """

    name: str
    lease_id: str
    execution_epoch: int
    timeout_seconds: float
    max_output_bytes: int
    tree_hash: str
    environment_hash: str


@dataclass(frozen=True)
class BoundCommandStatus:
    """An owner-checked observation; only ``missing`` proves current absence."""

    name: str
    container_id: str | None
    state: str
    exit_code: int | None


class IsolatedExecutionBroker:
    """The only process execution entry for H04 leases."""

    def __init__(
        self,
        workspace_root: Path,
        lease: CapabilityLease | CapabilityAuthority,
        *,
        image_id: str | None = None,
        docker: str = "docker",
        credentials: Mapping[str, str] | None = None,
        budget_check: Callable[[], bool] | None = None,
        lock_path: Path = DEFAULT_IMAGE_LOCK,
    ) -> None:
        self.root = Path(workspace_root).resolve()
        if self.root.is_symlink() or not self.root.is_dir():
            raise DomainError("HARNESS_WORKSPACE_DENIED", "Workspace root is unavailable", 409)
        if isinstance(lease, CapabilityAuthority):
            self.authority = lease
        else:
            if budget_check is None:
                # A missing external ledger does not mean unlimited spending:
                # the lease's own bounded call counter still fails closed.
                remaining = {"calls": 0}

                def lease_budget() -> bool:
                    remaining["calls"] += 1
                    return remaining["calls"] <= lease.max_calls

                budget_check = lease_budget
            self.authority = CapabilityAuthority(lease, budget_check=budget_check)
        self.docker = docker
        self.image_id = image_id or self._image_from_lock(lock_path)
        # Credentials stay in the Broker; only names appear in the lease and
        # no value is ever added to the project container environment.
        self._credentials = dict(credentials or {})
        # Mount targets that the broker had to create inside the workspace so a
        # mask can cover a missing protected path.  They are owned by the
        # workspace user, empty and removed as soon as the container is
        # removed, so docker never leaves root-owned placeholders behind.
        self._placeholders: dict[str, list[Path]] = {}
        self._placeholder_lock = threading.Lock()

    @staticmethod
    def _image_from_lock(lock_path: Path) -> str:
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            image_id = str(lock["image_id"])
        except (OSError, ValueError, KeyError) as exc:
            raise DomainError(
                "SANDBOX_NOT_PREPARED", "A pinned sandbox image is required", 409
            ) from exc
        if not image_id.startswith("sha256:"):
            raise DomainError("SANDBOX_NOT_PREPARED", "Sandbox image id is not pinned", 409)
        return image_id

    def execute(
        self,
        request: ExecutionRequest,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
    ) -> ExecutionReceipt:
        cancellation.ensure_active()
        lease, read_paths, write_paths = self._authorize(request, grant)
        name = "mgar-h04-" + uuid.uuid4().hex[:16]
        create_args, placeholders = self._create_args(
            name, request, lease, read_paths, write_paths
        )
        self._register_placeholders(name, placeholders)
        created = self._docker(create_args)
        if created.returncode != 0:
            self._remove_placeholders(name)
            raise DomainError(
                "HARNESS_SANDBOX_ERROR", "Restricted container could not be created", 502
            )
        started = time.monotonic()
        status: str = "error"
        stdout = b""
        exit_code = 1
        try:
            try:
                completed = subprocess.run(
                    [self.docker, "start", "-a", name],
                    capture_output=True,
                    env=_docker_environment(),
                    timeout=min(
                        request.timeout_seconds,
                        lease.timeout_seconds,
                        grant.max_seconds,
                    ),
                    check=False,
                )
                stdout = completed.stdout + completed.stderr
                exit_code = completed.returncode
                status = "passed" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired as exc:
                stdout = (exc.stdout or b"")[: lease.max_output_bytes]
                status = "timeout"
                exit_code = 124
        finally:
            subprocess.run(
                [self.docker, "rm", "-f", name],
                capture_output=True,
                env=_docker_environment(),
                check=False,
            )
            self._remove_placeholders(name)
        duration = time.monotonic() - started
        text = stdout.decode("utf-8", "replace")[: lease.max_output_bytes]
        return ExecutionReceipt(
            receipt_id=digest(
                {
                    "lease_id": lease.lease_id,
                    "operation_id": request.operation_id,
                    "argv": list(request.argv),
                    "status": status,
                    "exit_code": exit_code,
                    "output": sha256_text(text),
                }
            ),
            operation_id=request.operation_id,
            purpose=request.purpose,
            status=cast(Any, status),
            argv=request.argv,
            cwd=request.cwd,
            exit_code=exit_code,
            stdout=text,
            stderr="",
            duration_seconds=duration,
            tree_hash=digest({"workspace": str(self.root), "files": sorted(os.listdir(self.root))}),
            log_sha256=sha256_text(text),
            environment_hash=digest({"env": ["PATH", "HOME", "LANG"], "credentials": []}),
        )

    def launch(
        self,
        request: ExecutionRequest,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
        *,
        container_name: str | None = None,
        ownership_labels: Mapping[str, str] | None = None,
        dispatch_guard: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> LaunchedCommand:
        """Create and start a container without waiting for it to finish.

        H06 uses this primitive for run/poll/cancel handles and controlled
        background services.  Authorization is identical to ``execute``: the
        lease, grant intersection, cwd, command allowlist and epoch are checked
        before any container exists.

        Optional bound launch is a trusted server-only extension. The caller
        commits name/labels as an operation intent before calling and provides
        a current-authority context spanning the actual Docker start dispatch.
        All three options must be supplied together; all-None keeps H06 behavior.
        """

        cancellation.ensure_active()
        bound = any(
            value is not None for value in (container_name, ownership_labels, dispatch_guard)
        )
        labels: dict[str, str] = {}
        if bound:
            if container_name is None or ownership_labels is None or dispatch_guard is None:
                raise DomainError(
                    "HARNESS_COMMAND_BINDING_REQUIRED",
                    "Bound launch requires a server name, ownership labels and dispatch guard",
                    422,
                )
            labels = self._bound_identity(container_name, ownership_labels)
        lease, read_paths, write_paths = self._authorize(request, grant)
        name = container_name or "mgar-h06-" + uuid.uuid4().hex[:16]
        create_args, placeholders = self._create_args(
            name, request, lease, read_paths, write_paths, ownership_labels=labels
        )
        self._register_placeholders(name, placeholders)
        created = self._docker(create_args)
        if created.returncode != 0:
            self._remove_placeholders(name)
            raise DomainError(
                "HARNESS_SANDBOX_ERROR", "Restricted container could not be created", 502
            )
        start_target = name
        if bound:
            try:
                start_target = created.stdout.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise DomainError(
                    "HARNESS_COMMAND_OUTCOME_UNKNOWN", "Created container ID is unavailable", 503
                ) from exc
            if not re.fullmatch(r"[a-f0-9]{64}", start_target):
                raise DomainError(
                    "HARNESS_COMMAND_OUTCOME_UNKNOWN", "Created container ID is unavailable", 503
                )
        # The trusted controller persists name/labels before launch. Its guard
        # holds the Engine mutex across actual start, after committing the short
        # current-authority transaction. No PG transaction may span Docker IO.
        # Guard/transport failure preserves that intent for owner-bound recovery.
        with (nullcontext() if dispatch_guard is None else dispatch_guard()):
            if bound:
                cancellation.ensure_active()
                try:
                    observed = self.inspect_bound_command(name, labels)
                except DomainError as exc:
                    raise DomainError(
                        "HARNESS_COMMAND_OUTCOME_UNKNOWN",
                        "Created container ownership could not be established",
                        503,
                    ) from exc
                if observed.container_id != start_target or observed.state != "created":
                    raise DomainError(
                        "HARNESS_COMMAND_OUTCOME_UNKNOWN",
                        "Created container identity or launch state changed",
                        503,
                    )
                # Inspection may outlast lease/session/policy expiry. This final
                # noncharging source must re-read current PG authority/time and
                # finish its short transaction; a cached guard-entry lease is
                # insufficient. No slow IO may intervene before actual start.
                current = self.authority.peek()
                if (
                    current.lease_id != lease.lease_id
                    or current.lease_hash != lease.lease_hash
                    or current.execution_epoch != request.execution_epoch
                ):
                    raise DomainError(
                        "HARNESS_EPOCH_MISMATCH", "Command authority changed before dispatch", 409
                    )
            # Bound dispatch uses the immutable ID from create. A replacement at
            # the same name can never become the process started by this call.
            started = self._docker(["start", start_target])
        if started.returncode != 0:
            if bound:
                self.stop_bound_command(name, labels)
            else:
                self._docker(["rm", "-f", name])
                self._remove_placeholders(name)
            raise DomainError(
                "HARNESS_SANDBOX_ERROR", "Restricted container could not be started", 502
            )
        return LaunchedCommand(
            name=name,
            lease_id=lease.lease_id,
            execution_epoch=lease.execution_epoch,
            timeout_seconds=min(request.timeout_seconds, lease.timeout_seconds, grant.max_seconds),
            max_output_bytes=lease.max_output_bytes,
            tree_hash=self.workspace_tree_hash(),
            environment_hash=self.environment_hash(lease),
        )

    def inspect_command(self, name: str) -> tuple[str, int | None]:
        """Return the container state and exit code, or ``missing``."""

        probe = self._docker(
            ["inspect", "--format", "{{.State.Status}}|{{.State.ExitCode}}", name]
        )
        if probe.returncode:
            return "missing", None
        status, _, code = probe.stdout.decode("utf-8", "replace").strip().partition("|")
        parsed = int(code) if code.lstrip("-").isdigit() else None
        return status or "missing", parsed

    @staticmethod
    def _bound_identity(name: str, ownership_labels: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name
        ):
            raise DomainError("HARNESS_COMMAND_BINDING_INVALID", "Invalid server command name", 422)
        labels = dict(ownership_labels)
        required = {
            "mgar.sa12.assignment", "mgar.sa12.operation",
            "mgar.sa12.workspace", "mgar.sa12.project",
        }
        if set(labels) != required:
            raise DomainError(
                "HARNESS_COMMAND_BINDING_INVALID", "Complete SA12 ownership labels are required", 422
            )
        for key, value in labels.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"mgar\.[a-z0-9][a-z0-9_.-]{0,126}", key)
                or key == "mgar.v4.h04.broker"
                or not isinstance(value, str)
                or not 1 <= len(value) <= 256
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise DomainError(
                    "HARNESS_COMMAND_BINDING_INVALID", "Invalid server ownership labels", 422
                )
        try:
            for key in ("mgar.sa12.assignment", "mgar.sa12.operation", "mgar.sa12.workspace"):
                if str(uuid.UUID(labels[key])) != labels[key]:
                    raise ValueError("Noncanonical ownership UUID")
            if name != "mgar-sa12-" + uuid.UUID(labels["mgar.sa12.operation"]).hex:
                raise ValueError("Command name does not bind its operation")
        except ValueError as exc:
            raise DomainError(
                "HARNESS_COMMAND_BINDING_INVALID", "Invalid SA12 execution identity", 422
            ) from exc
        return labels

    def inspect_bound_command(
        self, name: str, ownership_labels: Mapping[str, str]
    ) -> BoundCommandStatus:
        """Inspect only a fully matched server identity; ambiguity raises.

        The trusted controller supplies every ownership label from its durable
        Assignment/operation intent. Raw client names/labels are not authority.
        Unrelated image labels are permitted; the complete mgar.sa12.* label
        set, original Broker label and canonical operation name match exactly.
        """

        labels = self._bound_identity(name, ownership_labels)
        template = (
            '{"name":{{json .Name}},"id":{{json .Id}},'
            '"labels":{{json .Config.Labels}},"state":{{json .State.Status}},'
            '"exit_code":{{json .State.ExitCode}}}'
        )
        probe = self._docker(["container", "inspect", "--format", template, name])
        if probe.returncode:
            # A transport/daemon/permission error is never proof of absence.
            expected = {
                f"Error response from daemon: No such container: {name}",
                f"Error: No such container: {name}",
                f"Error: No such object: {name}",
            }
            if (
                probe.returncode == 1
                and not probe.stdout.strip()
                and probe.stderr.decode("utf-8", "replace").strip() in expected
            ):
                return BoundCommandStatus(name, None, "missing", None)
            raise DomainError(
                "HARNESS_COMMAND_OUTCOME_UNKNOWN", "Container absence could not be established", 503
            )
        try:
            observed = json.loads(probe.stdout)
            if not isinstance(observed, dict):
                raise ValueError("Invalid container observation")
            actual_labels = observed.get("labels")
            if (
                observed.get("name") != "/" + name
                or not isinstance(actual_labels, dict)
                or actual_labels.get("mgar.v4.h04.broker") != "1"
                or {
                    key: value for key, value in actual_labels.items()
                    if isinstance(key, str) and key.startswith("mgar.sa12.")
                } != labels
            ):
                raise DomainError(
                    "HARNESS_COMMAND_OWNER_MISMATCH", "Container ownership does not match", 409
                )
            identifier = observed.get("id")
            state = observed.get("state")
            exit_code = observed.get("exit_code")
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(r"[a-f0-9]{64}", identifier)
                or state not in {
                    "created", "running", "paused", "restarting", "removing", "exited", "dead"
                }
                or type(exit_code) is not int
            ):
                raise ValueError("Incomplete container observation")
        except (ValueError, TypeError, KeyError) as exc:
            raise DomainError(
                "HARNESS_COMMAND_OUTCOME_UNKNOWN", "Container state could not be established", 503
            ) from exc
        return BoundCommandStatus(name, identifier, state, exit_code)

    def stop_bound_command(
        self, name: str, ownership_labels: Mapping[str, str]
    ) -> BoundCommandStatus:
        """Remove a matched immutable container ID and prove name absence.

        A replacement at the same name is never deleted blindly. Unknown
        transport results, owner mismatch or non-absence retain the controller's
        operation intent/resource reservation for later reconciliation.
        """

        observed = self.inspect_bound_command(name, ownership_labels)
        if observed.state == "missing":
            self._remove_placeholders(name)
            return observed
        assert observed.container_id is not None
        stopped = self._docker(["rm", "-f", observed.container_id])
        if stopped.returncode:
            raise DomainError(
                "HARNESS_COMMAND_OUTCOME_UNKNOWN", "Container stop could not be established", 503
            )
        after = self.inspect_bound_command(name, ownership_labels)
        if after.state != "missing":
            raise DomainError(
                "HARNESS_COMMAND_OUTCOME_UNKNOWN", "Container quiescence could not be established", 503
            )
        self._remove_placeholders(name)
        return after

    def command_logs(self, name: str, *, max_bytes: int) -> tuple[bytes, int]:
        """Read the container's combined output, bounded by the lease.

        Returns the retained prefix and the exact number of bytes the container
        wrote, so a caller can report that the tail was clipped instead of
        silently presenting a shortened log as complete.  Reading is streamed
        in fixed-size chunks: a hostile process cannot make the harness buffer
        an unbounded amount of output.
        """

        limit = max(0, max_bytes)
        try:
            process = subprocess.Popen(
                [self.docker, "logs", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=_docker_environment(),
            )
        except OSError as exc:
            raise DomainError(
                "HARNESS_SANDBOX_ERROR", "Docker could not read command logs", 502
            ) from exc
        kept = bytearray()
        total = 0
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                room = limit - len(kept)
                if room > 0:
                    kept.extend(chunk[:room])
            process.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            process.kill()
            raise DomainError(
                "HARNESS_SANDBOX_ERROR", "Docker could not read command logs", 502
            ) from exc
        finally:
            if process.stdout is not None:
                process.stdout.close()
        return bytes(kept), total

    def stop_command(self, name: str) -> None:
        """Stop and remove one owned container.  Safe to call repeatedly."""

        self._docker(["rm", "-f", name])
        self._remove_placeholders(name)

    def command_revoked(self, lease_id: str, execution_epoch: int) -> bool:
        """True when a running command's lease is no longer the current one.

        Revocation, expiry and epoch replacement all invalidate the authority
        for a command that is already running; the command session polls this
        without charging the call budget.
        """

        try:
            lease = self.authority.peek()
        except DomainError:
            return True
        return lease.lease_id != lease_id or lease.execution_epoch != execution_epoch

    def _register_placeholders(self, name: str, paths: Sequence[Path]) -> None:
        if not paths:
            return
        with self._placeholder_lock:
            self._placeholders[name] = list(paths)

    def _remove_placeholders(self, name: str) -> None:
        with self._placeholder_lock:
            paths = self._placeholders.pop(name, [])
        self._remove_paths(paths)

    def exec_probe(
        self, name: str, argv: Sequence[str], *, timeout: float = 2.0
    ) -> tuple[int, bytes]:
        """Run a fixed readiness probe inside an owned container.

        The argv is supplied by trusted adapter code, never by model output.
        ``docker exec`` does not create a new authorization surface: it only
        reaches a container that the broker already authorized.
        """

        result = self._docker(["exec", name, *argv], timeout=timeout)
        return result.returncode, result.stdout + result.stderr

    def workspace_tree_hash(self) -> str:
        """Hash the visible worktree so a command is bound to one version."""

        files: dict[str, str] = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.root).as_posix()
            if any(part in {".git", ".mgar"} for part in relative.split("/")):
                continue
            try:
                files[relative] = sha256_text(path.read_bytes().decode("utf-8", "replace"))
            except OSError:
                continue
        return digest(files)

    def environment_hash(self, lease: CapabilityLease) -> str:
        """Summarize the fixed container environment; never its values."""

        return digest(
            {
                "PATH": (
                    "/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:"
                    "/usr/sbin:/usr/bin:/sbin:/bin"
                ),
                "HOME": "/tmp",
                "LANG": "C.UTF-8",
                "TMPDIR": "/scratch",
                "GOCACHE": "/scratch/go-cache",
                "GOTMPDIR": "/scratch",
                "credentials": sorted(lease.credentials),
            }
        )

    def _authorize(
        self,
        request: ExecutionRequest,
        grant: CapabilityGrant,
    ) -> tuple[CapabilityLease, tuple[str, ...], tuple[str, ...]]:
        lease = self.authority.check_scope(
            actor=grant.actor,
            workspace_id=grant.workspace_id,
            repository=grant.repository,
            execution_epoch=request.execution_epoch,
            paths=(),
            write=False,
        )
        if lease.external_write or lease.mode == "publish":
            raise DomainError(
                "HARNESS_EXTERNAL_WRITE_DENIED",
                "External writes must use Action/Approval/Outbox",
                403,
            )
        if lease.network_hosts or grant.network:
            raise DomainError(
                "HARNESS_NETWORK_DENIED",
                "H04 project processes have no network authorization",
                403,
            )
        if not grant.execute:
            raise DomainError("HARNESS_EXECUTE_DENIED", "Execution is not granted", 403)
        cwd = normalize_relative_path(request.cwd)
        read_paths = _intersect(lease.read_paths, grant.read_paths)
        write_paths = _intersect(lease.write_paths, grant.write_paths)
        if lease.mode == "modify_test" and not write_paths:
            raise DomainError(
                "HARNESS_WRITE_DENIED",
                "Writable lease and grant scopes do not intersect",
                403,
            )
        allowed_paths = tuple(sorted(set(read_paths) | set(write_paths)))
        if _is_protected(cwd, lease.protected_paths):
            raise DomainError(
                "HARNESS_PROTECTED_PATH_DENIED",
                "Working directory is protected workspace metadata",
                403,
            )
        if not _within(cwd, allowed_paths):
            raise DomainError("HARNESS_READ_DENIED", "Working directory is outside the scopes", 403)
        program = Path(request.argv[0]).name
        if program not in lease.allowed_commands:
            raise DomainError(
                "HARNESS_COMMAND_DENIED", f"Command {program!r} is not in the lease", 403
            )
        if not grant.allowed_commands or program not in grant.allowed_commands:
            raise DomainError(
                "HARNESS_COMMAND_DENIED", f"Command {program!r} is not in the grant", 403
            )
        return lease, read_paths, write_paths

    def prepare_dependencies(
        self,
        plan: Mapping[str, Any],
        grant: CapabilityGrant,
        cancellation: CancellationToken,
    ) -> None:
        """Dependency preparation is a separate phase from task execution.

        H04 does not authorize any package source or network egress; the port
        exists so callers cannot smuggle installation into the run phase.
        """

        cancellation.ensure_active()
        del plan, grant
        raise DomainError(
            "HARNESS_DEPENDENCY_SOURCE_DENIED",
            "Dependency preparation needs a separately approved source phase",
            403,
        )

    def _scope_source(self, relative: str) -> Path:
        """Resolve one approved scope without following host symlinks."""

        relative = normalize_relative_path(relative)
        target = self.root if relative == "" else self.root.joinpath(*relative.split("/"))
        current = self.root
        if relative:
            for part in relative.split("/"):
                current = current / part
                if current.is_symlink():
                    raise DomainError(
                        "HARNESS_PATH_DENIED",
                        "Approved scope traverses a host symlink",
                        403,
                    )
        resolved = target.resolve()
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise DomainError(
                "HARNESS_PATH_DENIED", "Approved scope escapes the workspace", 403
            )
        return target

    def _mount_args(
        self,
        read_paths: Sequence[str],
        write_paths: Sequence[str],
        protected_paths: Sequence[str],
    ) -> tuple[list[str], list[Path]]:
        """Mount exactly ``read ∪ write`` and hide protected paths.

        Every bind target must already exist on the host.  Mounting a scope and
        then masking a child used to duplicate the target, so the earlier
        implementation expanded a read-only scope into per-child mounts.  That
        still conflicted with file masks, and a missing protected path forced
        the runtime to create a root-owned placeholder inside the workspace.

        Instead the declared scopes are mounted once, exactly as leased, and a
        protected path is masked only when it would otherwise be visible:

        * existing file -> ``/dev/null`` bind (the target already exists);
        * existing directory -> read-only tmpfs with mode ``000``;
        * missing path under a read-only mount -> no mask, because the
          read-only parent already makes it impossible to create or read;
        * missing path under a writable mount -> the broker creates the mount
          target itself (owned by the workspace user) and removes it together
          with the container, so docker never writes into the workspace.
        """

        read_set = {normalize_relative_path(path) for path in read_paths}
        write_set = {normalize_relative_path(path) for path in write_paths}
        if "" in write_set:
            read_only: set[str] = set()
        else:
            read_only = {
                path
                for path in read_set - write_set
                if not any(path.startswith(writable + "/") for writable in write_set)
            }
        plan: list[tuple[str, bool]] = [(path, True) for path in sorted(read_only)]
        plan.extend((path, False) for path in sorted(write_set))
        mounts: list[str] = []
        mounted: list[str] = []
        for path, readonly in plan:
            source = self._scope_source(path)
            if not source.is_dir():
                continue
            target = "/workspace" + (f"/{path}" if path else "")
            suffix = ",readonly" if readonly else ""
            mounts.extend(["--mount", f"type=bind,source={source},target={target}{suffix}"])
            mounted.append(path)
        masks, placeholders = self._mask_args(mounted, write_set, protected_paths)
        return mounts + masks, placeholders

    def _mask_args(
        self,
        mounted: Sequence[str],
        write_set: set[str],
        protected_paths: Sequence[str],
    ) -> tuple[list[str], list[Path]]:
        """Hide protected paths without letting docker create host mountpoints."""

        args: list[str] = []
        placeholders: list[Path] = []
        try:
            for protected in protected_paths:
                relative = normalize_relative_path(protected)
                if relative == "" or not _within(relative, tuple(mounted)):
                    continue
                target = f"/workspace/{relative}"
                source = self.root.joinpath(*relative.split("/"))
                if source.is_symlink():
                    raise DomainError(
                        "HARNESS_PROTECTED_PATH_DENIED",
                        "Protected path is a host symlink",
                        403,
                    )
                if source.exists():
                    if source.is_dir():
                        args.extend(["--tmpfs", f"{target}:ro,size=1m,mode=000"])
                    else:
                        args.extend(
                            [
                                "--mount",
                                f"type=bind,source={_masked_file()},target={target},readonly",
                            ]
                        )
                    continue
                writable = "" in write_set or any(
                    relative.startswith(path + "/") for path in write_set
                )
                if not writable:
                    # The parent mount is read-only; the path stays absent and
                    # the project process can neither read nor create it.
                    continue
                placeholders.extend(self._create_placeholder(source))
                args.extend(["--tmpfs", f"{target}:ro,size=1m,mode=000"])
        except BaseException:
            self._remove_paths(placeholders)
            raise
        return args, placeholders

    def _create_placeholder(self, target: Path) -> list[Path]:
        """Create the empty, user-owned mount target a mask needs."""

        missing: list[Path] = []
        current = target
        while not current.exists():
            missing.append(current)
            current = current.parent
        if current.is_symlink() or not current.is_dir():
            raise DomainError(
                "HARNESS_PATH_DENIED",
                "A protected path has a non-directory host parent",
                403,
            )
        created: list[Path] = []
        for path in reversed(missing):
            try:
                path.mkdir(mode=0o700)
            except OSError as exc:
                self._remove_paths(created)
                raise DomainError(
                    "HARNESS_SANDBOX_ERROR",
                    "A protected path mount target could not be created",
                    502,
                ) from exc
            created.append(path)
        return created

    def _remove_paths(self, paths: Sequence[Path]) -> None:
        for path in reversed(list(paths)):
            try:
                path.rmdir()
            except OSError:
                continue

    def _create_args(
        self,
        name: str,
        request: ExecutionRequest,
        lease: CapabilityLease,
        read_paths: Sequence[str],
        write_paths: Sequence[str],
        *,
        ownership_labels: Mapping[str, str] | None = None,
    ) -> tuple[list[str], list[Path]]:
        memory = lease.memory_mb * 1024 * 1024
        cwd = normalize_relative_path(request.cwd)
        mounts, placeholders = self._mount_args(
            read_paths, write_paths, lease.protected_paths
        )
        label_args: list[str] = []
        for key, value in sorted((ownership_labels or {}).items()):
            label_args.extend(["--label", f"{key}={value}"])
        args = [
            "create",
            "--name",
            name,
            "--label",
            "mgar.v4.h04.broker=1",
            *label_args,
            "--network=none",
            "--read-only",
            "--user=" + SANDBOX_UID,
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=" + str(lease.pids),
            "--memory=" + str(memory),
            "--memory-swap=" + str(memory),
            "--cpus=" + f"{lease.cpus:g}",
            "--ipc=none",
            "--init",
            *mounts,
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--tmpfs",
            (
                f"/scratch:rw,exec,nosuid,nodev,size={lease.scratch_mb}m,mode=1777"
            ),
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            "core=0:0",
            "--env",
            "PATH=/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "--env",
            "HOME=/tmp",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "TMPDIR=/scratch",
            "--env",
            "GOCACHE=/scratch/go-cache",
            "--env",
            "GOTMPDIR=/scratch",
            "--workdir",
            "/workspace" + (f"/{cwd}" if cwd else ""),
            "--entrypoint",
            "",
            self.image_id,
            *request.argv,
        ]
        return args, placeholders

    def _docker(
        self, args: Sequence[str], *, timeout: float = 60
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self.docker, *args],
                capture_output=True,
                env=_docker_environment(),
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DomainError(
                "HARNESS_SANDBOX_ERROR", "Docker could not run the sandbox", 502
            ) from exc
