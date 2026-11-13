"""Guarded H03 facade; writes require the original Engine durable journal.

Construction performs no filesystem IO. The supplied authority must hold the
original Engine mutex and recheck current Source/Assignment permissions before
it yields. This facade then takes its private workspace lock, in that order.
"""

import difflib
import os
import stat
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Literal, Protocol
from uuid import UUID

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.contracts import (
    WorkspaceChange,
    WorkspaceDiff,
    WorkspaceEdit,
    WorkspaceSnapshot,
)
from mgar_v3.harness.workspace import TaskWorkspace
from mgar_v3.super_agent.policy_domain import safe_path

_PROTECTED = {".git", ".mgar", ".env", ".harness-tmp"}
_IGNORED = _PROTECTED | {"__pycache__", ".venv", "node_modules", ".mypy_cache"}


def _deny(code: str, status: int = 403) -> DomainError:
    return DomainError(code, "A current guarded Coding workspace is required", status)


@dataclass(frozen=True)
class WorkspaceAccess:
    operation: Literal["snapshot", "resolve", "read", "list", "diff", "apply"]
    paths: tuple[str, ...]
    expected_tree: str | None = None
    baseline_tree: str | None = None


WorkspaceAuthority = Callable[[WorkspaceAccess], AbstractContextManager[None]]


class ExistingFileJournal(Protocol):
    """Future trusted original-Engine port, never an in-memory trust assertion.

    Its implementation must durably record all pre/postimages before IO and
    reconcile partial writes after crashes. Merely implementing this Protocol
    does not enable writes in the read-only facade below.
    """

    def commit_existing_files(
        self,
        workspace_id: UUID,
        edits: Sequence[WorkspaceEdit],
        *,
        expected_tree: str,
    ) -> WorkspaceSnapshot: ...


class DurableExistingFileJournal(ABC):
    """Explicit trusted server port, separate from structural duck typing.

    The Engine adapter implements this port. A random object, including one
    with a similarly named method, cannot turn the read-only facade writable.
    """

    @abstractmethod
    def commit_guarded(
        self,
        workspace: "CodingWorkspace",
        edits: Sequence[WorkspaceEdit],
        *,
        expected_tree: str,
    ) -> WorkspaceSnapshot: ...


class CodingWorkspace:
    def __init__(
        self,
        workspace: TaskWorkspace,
        workspace_id: UUID,
        *,
        authority: WorkspaceAuthority | None = None,
        journal: ExistingFileJournal | DurableExistingFileJournal | None = None,
    ) -> None:
        if not isinstance(workspace, TaskWorkspace) or not isinstance(workspace_id, UUID):
            raise _deny("SOURCE_CODING_WORKSPACE_BINDING_INVALID")
        self._workspace = workspace
        self._root = workspace.root
        self._task_id = workspace.manifest.task_id
        self._registration = workspace.registration.registration_hash
        self._files = tuple(sorted(workspace._baseline))
        self._authority = authority
        self._journal = journal
        self._workspace_id = str(workspace_id)
        self._lock = RLock()
        self._snapshots: dict[str, dict[str, str]] = {}

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @staticmethod
    def _relative(path: str, *, directory: bool = False) -> str:
        if type(path) is not str:
            raise _deny("SOURCE_CODING_WORKSPACE_PATH_DENIED")
        if directory and path in {"", "."}:
            return ""
        try:
            value = safe_path(path)
        except ValueError:
            raise _deny("SOURCE_CODING_WORKSPACE_PATH_DENIED") from None
        if any(part in _PROTECTED for part in value.split("/")):
            raise _deny("SOURCE_CODING_WORKSPACE_PATH_DENIED")
        return value

    @contextmanager
    def _access(self, access: WorkspaceAccess) -> Iterator[None]:
        if self._authority is None:
            raise _deny("SOURCE_CODING_WORKSPACE_AUTHORITY_REQUIRED")
        with self._authority(access), self._lock:
            current = self._workspace
            if (
                current.root != self._root
                or current.manifest.task_id != self._task_id
                or current.registration.registration_hash != self._registration
                or current.manifest.workspace_id != current.registration.workspace_id
                or current.manifest.manifest_hash != current.manifest.computed_hash()
            ):
                raise _deny("SOURCE_CODING_WORKSPACE_BINDING_INVALID")
            self._layout()
            yield

    def _layout(self) -> None:
        if self._root.is_symlink() or not self._root.is_dir():
            raise _deny("SOURCE_CODING_WORKSPACE_PATH_DENIED")
        files: set[str] = set()
        for current, directories, names in os.walk(self._root, followlinks=False):
            parent = Path(current)
            for name in tuple(directories):
                if name in _IGNORED:
                    directories.remove(name)
                elif (parent / name).is_symlink():
                    raise _deny("SOURCE_CODING_WORKSPACE_PATH_DENIED")
            for name in names:
                if name in _IGNORED:
                    continue
                target = parent / name
                info = target.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise _deny("SOURCE_CODING_WORKSPACE_PATH_DENIED")
                files.add(target.relative_to(self._root).as_posix())
        if files != set(self._files):
            raise _deny("SOURCE_CODING_WORKSPACE_LAYOUT_CHANGED", 409)

    @contextmanager
    def _descriptor(self, path: str, *, directory: bool = False) -> Iterator[int]:
        descriptors: list[int] = []
        try:
            descriptor = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            parts = path.split("/") if path else []
            for number, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if number < len(parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                descriptor = os.open(part, flags, dir_fd=descriptor)
                descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise _deny("SOURCE_CODING_WORKSPACE_PATH_DENIED")
            yield descriptor
        except OSError:
            raise _deny("SOURCE_CODING_WORKSPACE_PATH_DENIED") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _read(self, path: str) -> str:
        path = self._relative(path)
        if path not in self._files:
            raise _deny("SOURCE_CODING_WORKSPACE_PATH_DENIED")
        with self._descriptor(path) as descriptor:
            before = os.fstat(descriptor)
            limit = min(self._workspace.limits.max_file_bytes, 8 * 1024 * 1024)
            if before.st_size > limit:
                raise _deny("SOURCE_CODING_WORKSPACE_LIMIT", 413)
            data = bytearray()
            while chunk := os.read(descriptor, min(65536, limit + 1 - len(data))):
                data.extend(chunk)
                if len(data) > limit:
                    raise _deny("SOURCE_CODING_WORKSPACE_LIMIT", 413)
            after = os.fstat(descriptor)
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
                or after.st_nlink != 1
            ):
                raise _deny("SOURCE_CODING_WORKSPACE_CHANGED", 409)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise _deny("SOURCE_CODING_WORKSPACE_BINARY", 415) from None

    def _snapshot(self) -> WorkspaceSnapshot:
        files = {path: self._read(path) for path in self._files}
        if sum(len(value.encode()) for value in files.values()) > min(
            self._workspace.limits.max_total_bytes,
            8 * 1024 * 1024,
        ):
            raise _deny("SOURCE_CODING_WORKSPACE_LIMIT", 413)
        tree = digest({path: sha256(value.encode()).hexdigest() for path, value in files.items()})
        self._snapshots[tree] = dict(files)
        while len(self._snapshots) > 8:
            del self._snapshots[next(iter(self._snapshots))]
        return WorkspaceSnapshot(workspace_id=self.workspace_id, tree_hash=tree, files=dict(files))

    def snapshot(self) -> WorkspaceSnapshot:
        with self._access(WorkspaceAccess("snapshot", self._files)):
            return self._snapshot()

    def resolve(self, path: str) -> str:
        relative = self._relative(path, directory=True)
        directory = relative not in self._files
        with self._access(WorkspaceAccess("resolve", (relative,))):
            with self._descriptor(relative, directory=directory):
                return relative

    def read(self, path: str) -> str:
        relative = self._relative(path)
        with self._access(WorkspaceAccess("read", (relative,))):
            return self._read(relative)

    def list_dir(self, path: str = "") -> tuple[str, ...]:
        relative = self._relative(path, directory=True)
        with self._access(WorkspaceAccess("list", (relative,))):
            with self._descriptor(relative, directory=True):
                prefix = relative + "/" if relative else ""
                return tuple(
                    sorted(
                        {
                            item[len(prefix) :].split("/", 1)[0]
                            for item in self._files
                            if item.startswith(prefix)
                            and item != relative
                            and not any(part in _PROTECTED for part in item.split("/"))
                        }
                    )
                )

    def diff(self, tree_before: str) -> WorkspaceDiff:
        with self._access(WorkspaceAccess("diff", self._files, baseline_tree=tree_before)):
            before = self._snapshots.get(tree_before)
            if before is None:
                raise _deny("SOURCE_CODING_WORKSPACE_SNAPSHOT_UNKNOWN", 409)
            after = self._snapshot()
            changes: list[WorkspaceChange] = []
            patch: list[str] = []
            for path in self._files:
                old, new = before[path], after.files[path]
                if old == new:
                    continue
                changes.append(
                    WorkspaceChange(
                        path=path,
                        change="modified",
                        before_sha256=sha256(old.encode()).hexdigest(),
                        after_sha256=sha256(new.encode()).hexdigest(),
                    )
                )
                patch.extend(
                    difflib.unified_diff(
                        old.splitlines(keepends=True),
                        new.splitlines(keepends=True),
                        fromfile="a/" + path,
                        tofile="b/" + path,
                    )
                )
            return WorkspaceDiff(
                workspace_id=self.workspace_id,
                tree_before=tree_before,
                tree_after=after.tree_hash,
                changes=tuple(changes),
                patch="".join(patch),
            )

    def apply(self, edits: Sequence[WorkspaceEdit], *, expected_tree: str) -> WorkspaceSnapshot:
        paths = tuple(self._relative(edit.path) for edit in edits)
        with self._access(WorkspaceAccess("apply", paths, expected_tree=expected_tree)):
            if self._journal is None:
                raise _deny("SOURCE_CODING_DURABLE_JOURNAL_REQUIRED")
            if not isinstance(self._journal, DurableExistingFileJournal):
                raise _deny("SOURCE_CODING_JOURNAL_EXECUTION_UNAVAILABLE")
            return self._journal.commit_guarded(self, edits, expected_tree=expected_tree)
