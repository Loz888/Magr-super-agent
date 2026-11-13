"""H03 workspace registration, isolation and change protection.

A local project becomes usable only after an operator explicitly registers its
absolute root.  Every task then receives its own copy; the registered directory
is never used as a worktree and is only written by an explicit ``apply_back``
after every target pre-image hash has been rechecked.  Git metadata, symlinks
and submodule checkouts are never materialized, and Git commands run with hooks,
fsmonitor and optional locks disabled.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.harness.contracts import (
    CancellationToken,
    HarnessToolCall,
    HarnessToolResult,
    ToolContext,
    ToolSpec,
    WorkspaceEdit,
    WorkspaceSnapshot,
    normalize_relative_path,
)
from mgar_v3.repository_analysis.domain import parse_repository_url, validate_commit

MANIFEST_VERSION = "harness-workspace-v1"
REGISTRATION_VERSION = "harness-workspace-registration-v1"
APPLY_VERSION = "harness-apply-receipt-v1"

_SKIP_PARTS = {
    ".git",
    ".mgar",
    ".harness-tmp",
    "__pycache__",
    ".venv",
    "node_modules",
    ".mypy_cache",
}


def _git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_ALLOW_PROTOCOL": "https:file",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


def _git_command(source: Path, args: Sequence[str], *, hooks: Path) -> list[str]:
    """Central Git invocation with host-side repository code disabled.

    Hooks, fsmonitor, untracked cache, submodule recursion and external diff
    are disabled on every command so no repository-configured process runs
    during registration, dirty inventory or materialization.
    """

    return [
        "git",
        "-C",
        str(source),
        "-c",
        f"core.hooksPath={hooks}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "submodule.recurse=false",
        "-c",
        "fetch.recurseSubmodules=false",
        "-c",
        "diff.external=",
        "-c",
        "core.pager=cat",
        *args,
    ]


def _git(source: Path, args: Sequence[str], *, hooks: Path) -> str:
    try:
        result = subprocess.run(
            _git_command(source, args, hooks=hooks),
            capture_output=True,
            env=_git_environment(),
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DomainError("WORKSPACE_GIT_FAILED", "Git command could not run", 502) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise DomainError(
            "WORKSPACE_GIT_FAILED", (detail[-1] if detail else "git command failed")[:300], 502
        )
    return result.stdout.decode("utf-8", "replace").strip()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _source_sha(path: Path) -> str | None:
    if path.is_symlink():
        raise DomainError("WORKSPACE_SYMLINK_DENIED", "Path is a symlink", 403)
    if not path.is_file():
        return None
    return _file_sha256(path)


@contextmanager
def _source_lock(root: Path) -> Iterator[None]:
    """Cross-process exclusive lock on the registered source directory.

    The lock is taken on the directory inode, so separate registries, threads
    and processes that apply back to the same source share one critical
    section without creating a lock file inside the user project.
    """

    descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_text_nofollow(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read().decode("utf-8")


def _write_json_atomic(directory: Path, name: str, payload: dict[str, Any]) -> None:
    """Write JSON through a fresh O_EXCL temp file and atomic replace.

    The temp name is random and created with O_EXCL|O_NOFOLLOW semantics, so a
    pre-existing symlink cannot redirect the write outside the directory.
    """

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(temporary_name, directory / name)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _check_relative(path: str) -> str:
    relative = normalize_relative_path(path)
    if any(part.lower() in {".git", ".mgar"} for part in relative.split("/")):
        raise DomainError(
            "WORKSPACE_PATH_DENIED", "Git or harness metadata is not a workspace path", 403
        )
    return relative


def _safe_target(root: Path, relative: str) -> Path:
    target = root.joinpath(*relative.split("/"))
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise DomainError("WORKSPACE_SYMLINK_DENIED", "Path traverses a symlink", 403)
    parent = target.parent.resolve()
    resolved_root = root.resolve()
    if parent != resolved_root and resolved_root not in parent.parents:
        raise DomainError("WORKSPACE_PATH_DENIED", "Path escapes the workspace", 403)
    return target


def _scan(root: Path, limits: WorkspaceLimits | None = None) -> tuple[dict[str, str], list[str]]:
    """Return path -> sha256 for regular files and the excluded symlink paths."""

    limits = limits or WorkspaceLimits()
    hashes: dict[str, str] = {}
    excluded: list[str] = []
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in _SKIP_PARTS)
        current_path = Path(current)
        for name in sorted(directories):
            if (current_path / name).is_symlink():
                excluded.append((current_path / name).relative_to(root).as_posix())
                directories.remove(name)
        for name in sorted(files):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if any(part in _SKIP_PARTS for part in relative.split("/")):
                continue
            if path.is_symlink():
                excluded.append(relative)
                continue
            if not path.is_file():
                continue
            if len(hashes) >= limits.max_files:
                raise DomainError("WORKSPACE_LIMIT", "Workspace exceeds its file budget", 413)
            size = path.stat().st_size
            if size > limits.max_file_bytes:
                raise DomainError("WORKSPACE_LIMIT", "Workspace file exceeds its byte budget", 413)
            total += size
            if total > limits.max_total_bytes:
                raise DomainError("WORKSPACE_LIMIT", "Workspace exceeds its byte budget", 413)
            hashes[relative] = _file_sha256(path)
    return hashes, sorted(excluded)


def _tree_hash(hashes: Mapping[str, str]) -> str:
    return digest(dict(sorted(hashes.items())))


class WorkspaceLimits(Model):
    max_files: Annotated[int, Field(ge=1, le=200_000)] = 5_000
    max_total_bytes: Annotated[int, Field(ge=1, le=1_000_000_000)] = 50_000_000
    max_file_bytes: Annotated[int, Field(ge=1, le=100_000_000)] = 8_000_000


class DirtyEntry(Model):
    path: str
    status: Literal["modified", "added", "untracked", "deleted", "renamed", "typechange"]
    sha256: str | None = None
    size: Annotated[int, Field(ge=0)] = 0


class DirtyInventory(Model):
    entries: list[DirtyEntry]
    tracked_changes: Annotated[int, Field(ge=0)]
    untracked_files: Annotated[int, Field(ge=0)]
    inventory_hash: str

    @classmethod
    def build(cls, entries: list[DirtyEntry]) -> DirtyInventory:
        payload = [item.model_dump(mode="json") for item in sorted(entries, key=lambda e: e.path)]
        return cls(
            entries=sorted(entries, key=lambda e: e.path),
            tracked_changes=sum(item.status != "untracked" for item in entries),
            untracked_files=sum(item.status == "untracked" for item in entries),
            inventory_hash=digest(payload),
        )


class Registration(Model):
    schema_version: str = REGISTRATION_VERSION
    workspace_id: str
    kind: Literal["local", "github"]
    repository: str
    base_commit: str
    registered_by: str
    registered_at: str
    source_root_sha256: str
    remote: str = ""
    limits: WorkspaceLimits
    registration_hash: str

    def computed_hash(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"registration_hash"}))


class WorkspaceManifest(Model):
    schema_version: str = MANIFEST_VERSION
    workspace_id: str
    task_id: str
    kind: Literal["local", "github"]
    repository: str
    base_commit: str
    tree_hash: str
    baseline_hash: str
    dirty: DirtyInventory
    excluded: list[str]
    created_at: str
    manifest_hash: str

    def computed_hash(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"manifest_hash"}))


class ApplyConflict(Model):
    path: str
    reason: Literal["stale", "missing", "symlink", "denied", "stale_workspace"]
    expected_sha256: str | None
    actual_sha256: str | None


class ApplyReceipt(Model):
    schema_version: str = APPLY_VERSION
    workspace_id: str
    task_id: str
    status: Literal["applied", "conflict"]
    applied: list[str]
    conflicts: list[ApplyConflict]
    tree_before: str
    tree_after: str
    receipt_hash: str

    def computed_hash(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"receipt_hash"}))


class WorkspaceRegistry:
    """Operator-scoped project registration and isolated task workspaces."""

    def __init__(self, root: Path, *, limits: WorkspaceLimits | None = None) -> None:
        self.root = Path(root).resolve()
        self.limits = limits or WorkspaceLimits()
        self.root.mkdir(parents=True, exist_ok=True)
        self._hooks = self.root / ".empty-hooks"
        self._hooks.mkdir(exist_ok=True)
        self._sources: dict[str, Path] = {}

    # -- registration ---------------------------------------------------

    def register_local(self, source: str | Path, *, actor: str) -> Registration:
        path = Path(source)
        if not path.is_absolute():
            raise DomainError(
                "WORKSPACE_REGISTRATION_DENIED",
                "A local project must be registered with an absolute operator path",
                403,
            )
        if path.is_symlink():
            raise DomainError("WORKSPACE_PATH_DENIED", "Registered root is a symlink", 403)
        root = path.resolve()
        if not root.is_dir():
            raise DomainError(
                "WORKSPACE_REGISTRATION_DENIED", "Registered root is not a directory", 404
            )
        base_commit = "0" * 40
        repository = f"local/{root.name.lower() or 'project'}"
        try:
            remote = _git(root, ["remote", "get-url", "origin"], hooks=self._hooks)
            identity, _, _ = parse_repository_url(remote)
            repository = identity.full_name.lower()
        except DomainError:
            remote = ""
        try:
            head = _git(root, ["rev-parse", "HEAD"], hooks=self._hooks)
            if len(head) == 40 and all(character in "0123456789abcdef" for character in head):
                base_commit = head
        except DomainError:
            pass
        workspace_id = digest(
            {
                "kind": "local",
                "root": str(root),
                "repository": repository,
                "base_commit": base_commit,
            }
        )[:20]
        registration = Registration(
            workspace_id=workspace_id,
            kind="local",
            repository=repository,
            base_commit=base_commit,
            registered_by=actor,
            registered_at=datetime.now(UTC).isoformat(),
            source_root_sha256=hashlib.sha256(str(root).encode()).hexdigest(),
            remote=remote,
            limits=self.limits,
            registration_hash="",
        )
        registration = registration.model_copy(
            update={"registration_hash": registration.computed_hash()}
        )
        self._write_registration(registration)
        self._sources[registration.workspace_id] = root
        return registration

    def register_github(self, url: str, commit: str, *, actor: str) -> Registration:
        try:
            identity, _, _ = parse_repository_url(url)
        except ValueError as exc:
            raise DomainError(
                "WORKSPACE_REGISTRATION_DENIED", "GitHub URL is invalid", 403
            ) from exc
        try:
            fixed = validate_commit(commit)
        except ValueError as exc:
            raise DomainError(
                "WORKSPACE_REGISTRATION_DENIED", "A fixed commit is required", 400
            ) from exc
        repository = identity.full_name.lower()
        workspace_id = digest({"kind": "github", "repository": repository, "commit": fixed})[:20]
        registration = Registration(
            workspace_id=workspace_id,
            kind="github",
            repository=repository,
            base_commit=fixed,
            registered_by=actor,
            registered_at=datetime.now(UTC).isoformat(),
            source_root_sha256="",
            remote=f"https://github.com/{repository}",
            limits=self.limits,
            registration_hash="",
        )
        registration = registration.model_copy(
            update={"registration_hash": registration.computed_hash()}
        )
        self._write_registration(registration)
        return registration

    def _write_registration(self, registration: Registration) -> None:
        directory = self.root / registration.workspace_id
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise DomainError("WORKSPACE_PATH_DENIED", "Registration directory is a symlink", 403)
        _write_json_atomic(directory, "registration.json", registration.model_dump(mode="json"))

    def load(self, workspace_id: str) -> Registration:
        directory = self.root / workspace_id
        if directory.is_symlink():
            raise DomainError("WORKSPACE_PATH_DENIED", "Registration directory is a symlink", 403)
        target = _safe_target(directory, "registration.json")
        try:
            registration = Registration.model_validate_json(_read_text_nofollow(target))
        except (OSError, ValueError) as exc:
            raise DomainError(
                "WORKSPACE_NOT_REGISTERED", "Workspace registration is absent or invalid", 404
            ) from exc
        if registration.registration_hash != registration.computed_hash():
            raise DomainError("WORKSPACE_MANIFEST_MISMATCH", "Registration hash changed", 409)
        return registration

    # -- task workspaces ------------------------------------------------

    def create_task_workspace(
        self,
        registration: Registration,
        task_id: str,
        *,
        source_root: Path | None = None,
    ) -> TaskWorkspace:
        key = digest({"workspace_id": registration.workspace_id, "task_id": task_id})[:20]
        directory = self.root / registration.workspace_id / "tasks" / key
        if directory.exists():
            raise DomainError("WORKSPACE_CONFLICT", "Task workspace already exists", 409)
        directory.mkdir(parents=True)
        if registration.kind == "github":
            from mgar_v3.repository_analysis.workspace import RepositoryWorkspace
            from mgar_v3.repository_analysis.workspace import (
                WorkspaceLimits as RepositoryWorkspaceLimits,
            )

            repository_workspace = RepositoryWorkspace(
                self.root / registration.workspace_id / "sources",
                repository=registration.repository,
                commit=registration.base_commit,
                remote=registration.remote,
                run_id=key,
                limits=RepositoryWorkspaceLimits(
                    max_files=self.limits.max_files,
                    max_total_bytes=self.limits.max_total_bytes,
                    max_file_bytes=self.limits.max_file_bytes,
                ),
            )
            repository_manifest = repository_workspace.prepare()
            shutil.copytree(
                repository_workspace.worktree,
                directory,
                ignore=shutil.ignore_patterns(".git"),
                symlinks=False,
                dirs_exist_ok=True,
            )
            excluded: list[str] = [item.path for item in repository_manifest.exclusions]
            base_hashes, discovered = _scan(directory, self.limits)
            excluded = sorted(set(excluded) | set(discovered))
            dirty = DirtyInventory.build([])
        else:
            source_path = self._source_root(registration, source_root)
            excluded = self._copy_local(source_path, directory)
            base_hashes, discovered = _scan(directory, self.limits)
            excluded = sorted(set(excluded) | set(discovered))
            dirty = self._dirty_inventory(source_path)
        baseline = {
            "schema_version": MANIFEST_VERSION,
            "workspace_id": registration.workspace_id,
            "task_id": task_id,
            "files": base_hashes,
        }
        state = directory / ".mgar"
        state.mkdir(exist_ok=True)
        _write_json_atomic(state, "baseline.json", baseline)
        self._store_baseline_text(directory)
        manifest = WorkspaceManifest(
            workspace_id=registration.workspace_id,
            task_id=task_id,
            kind=registration.kind,
            repository=registration.repository,
            base_commit=registration.base_commit,
            tree_hash=_tree_hash(base_hashes),
            baseline_hash=digest(base_hashes),
            dirty=dirty,
            excluded=excluded,
            created_at=datetime.now(UTC).isoformat(),
            manifest_hash="",
        )
        manifest = manifest.model_copy(update={"manifest_hash": manifest.computed_hash()})
        _write_json_atomic(state, "manifest.json", manifest.model_dump(mode="json"))
        return TaskWorkspace(directory, manifest, registration, self.limits)

    def open_task_workspace(self, registration: Registration, task_id: str) -> TaskWorkspace:
        key = digest({"workspace_id": registration.workspace_id, "task_id": task_id})[:20]
        directory = self.root / registration.workspace_id / "tasks" / key
        state = directory / ".mgar"
        if state.is_symlink() or not state.is_dir():
            raise DomainError(
                "WORKSPACE_MANIFEST_MISMATCH", "Workspace metadata directory is unavailable", 409
            )
        try:
            manifest = WorkspaceManifest.model_validate_json(
                _read_text_nofollow(_safe_target(state, "manifest.json"))
            )
        except (OSError, ValueError) as exc:
            raise DomainError(
                "WORKSPACE_NOT_PREPARED", "Task workspace manifest is missing or invalid", 409
            ) from exc
        if manifest.manifest_hash != manifest.computed_hash():
            raise DomainError("WORKSPACE_MANIFEST_MISMATCH", "Workspace manifest hash changed", 409)
        current, _ = _scan(directory, self.limits)
        if _tree_hash(current) != manifest.tree_hash:
            raise DomainError(
                "WORKSPACE_MANIFEST_MISMATCH", "Workspace files no longer match the manifest", 409
            )
        return TaskWorkspace(directory, manifest, registration, self.limits)

    def _source_root(self, registration: Registration, source_root: Path | None = None) -> Path:
        candidate = source_root or self._sources.get(registration.workspace_id)
        if candidate is None:
            raise DomainError(
                "WORKSPACE_REGISTRATION_REQUIRED",
                "Local workspace needs the operator-provided source root",
                409,
            )
        path = Path(candidate)
        if path.is_symlink():
            raise DomainError("WORKSPACE_PATH_DENIED", "Local source root is a symlink", 403)
        root = path.resolve()
        if hashlib.sha256(str(root).encode()).hexdigest() != registration.source_root_sha256:
            raise DomainError(
                "WORKSPACE_REGISTRATION_MISMATCH",
                "Local source root does not match the registration",
                403,
            )
        return root

    def _store_baseline_text(self, directory: Path) -> None:
        target_root = directory / ".mgar" / "baseline-files"
        for current, directories, files in os.walk(directory, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(name for name in directories if name not in _SKIP_PARTS)
            for name in files:
                source = current_path / name
                relative = source.relative_to(directory).as_posix()
                if any(part in _SKIP_PARTS for part in relative.split("/")):
                    continue
                try:
                    safe_source = _safe_target(directory, relative)
                except DomainError:
                    continue
                if safe_source.is_symlink() or not safe_source.is_file():
                    continue
                try:
                    text = safe_source.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                destination = target_root.joinpath(*relative.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text, encoding="utf-8")

    def _copy_local(self, source: Path, destination: Path) -> list[str]:
        excluded: list[str] = []
        total = 0
        copied = 0
        for current, directories, files in os.walk(source, followlinks=False):
            directories[:] = sorted(directories)
            current_path = Path(current)
            for name in list(directories):
                child = current_path / name
                relative = child.relative_to(source).as_posix()
                if name == ".git" or (child / ".git").exists() or child.is_symlink():
                    excluded.append(relative)
                    directories.remove(name)
            for name in sorted(files):
                child = current_path / name
                relative = child.relative_to(source).as_posix()
                if any(part in _SKIP_PARTS for part in relative.split("/")):
                    continue
                if child.is_symlink() or not child.is_file():
                    excluded.append(relative)
                    continue
                size = child.stat().st_size
                if size > self.limits.max_file_bytes:
                    raise DomainError(
                        "WORKSPACE_LIMIT", "Project file exceeds its byte budget", 413
                    )
                copied += 1
                total += size
                if copied > self.limits.max_files or total > self.limits.max_total_bytes:
                    raise DomainError(
                        "WORKSPACE_LIMIT", "Project exceeds its workspace budget", 413
                    )
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(child, target, follow_symlinks=False)
        return sorted(excluded)

    def _dirty_inventory(self, source: Path) -> DirtyInventory:
        """Record staged, unstaged, mixed, renamed and untracked changes.

        The inventory reads HEAD and the index with filter-free plumbing
        commands, then hashes working files directly.  It deliberately does not
        call ``git status``/checkout, so repository hooks, fsmonitor scripts,
        clean/process filters and submodule recursion never run.
        """

        def run(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                _git_command(source, args, hooks=self._hooks),
                capture_output=True,
                env=_git_environment(),
                timeout=120,
                check=False,
            )

        try:
            head_result = run(["ls-tree", "-r", "-z", "HEAD"])
            index_result = run(["ls-files", "-s", "-z"])
            untracked_result = run(["ls-files", "-o", "--exclude-standard", "-z"])
        except (OSError, subprocess.TimeoutExpired):
            return DirtyInventory.build([])
        head: dict[str, tuple[str, str]] = {}
        if head_result.returncode == 0:
            for record in head_result.stdout.split(b"\0"):
                if not record:
                    continue
                meta, separator, path_bytes = record.partition(b"\t")
                if not separator:
                    continue
                fields = meta.decode("ascii", "replace").split()
                if len(fields) < 3:
                    continue
                mode, _kind, blob = fields[0], fields[1], fields[2]
                if mode == "160000":
                    continue
                path = path_bytes.decode("utf-8", "replace")
                if any(part in {".git", ".mgar"} for part in path.split("/")):
                    continue
                head[path] = (mode, blob)
        index: dict[str, tuple[str, str]] = {}
        for record in index_result.stdout.split(b"\0"):
            if not record:
                continue
            meta, separator, path_bytes = record.partition(b"\t")
            if not separator:
                continue
            fields = meta.decode("ascii", "replace").split()
            if len(fields) < 3:
                continue
            mode, blob, stage = fields[0], fields[1], fields[2]
            if stage != "0" or mode == "160000":
                continue
            path = path_bytes.decode("utf-8", "replace")
            if any(part in {".git", ".mgar"} for part in path.split("/")):
                continue
            index[path] = (mode, blob)
        untracked_paths = {
            record.decode("utf-8", "replace")
            for record in untracked_result.stdout.split(b"\0")
            if record
        }
        added = set(index) - set(head)
        deleted = set(head) - set(index)
        renamed: dict[str, str] = {}
        used_deleted: set[str] = set()
        for new_path in sorted(added):
            for old_path in sorted(deleted):
                if old_path in used_deleted:
                    continue
                if head[old_path] == index[new_path]:
                    renamed[new_path] = old_path
                    used_deleted.add(old_path)
                    break

        def worktree(path: str) -> tuple[str, str | None, int, str, str]:
            try:
                target = _safe_target(source, path)
            except DomainError:
                # A symlink in any path component must never be followed for a
                # dirty-inventory read, even when the final file is regular.
                return "typechange", None, 0, "120000", ""
            if target.is_symlink():
                return "typechange", None, 0, "120000", ""
            if not target.exists():
                return "deleted", None, 0, "", ""
            if not target.is_file():
                return "typechange", None, 0, "", ""
            data = target.read_bytes()
            mode = "100755" if os.access(target, os.X_OK) else "100644"
            return (
                "file",
                hashlib.sha256(data).hexdigest(),
                len(data),
                mode,
                _blob_id(data),
            )

        entries: list[DirtyEntry] = []
        for path in sorted(set(head) | set(index)):
            state, digest, size, worktree_mode, worktree_blob = worktree(path)
            if path in renamed:
                entries.append(
                    DirtyEntry(
                        path=path,
                        status="renamed",
                        sha256=digest,
                        size=size,
                    )
                )
                continue
            if path in added:
                entries.append(DirtyEntry(path=path, status="added", sha256=digest, size=size))
                continue
            if path in deleted:
                # A staged deletion may leave the same path as an untracked
                # working file (git rm --cached). Record both facts instead of
                # hiding the dirty state; the file content is still copied.
                entries.append(DirtyEntry(path=path, status="deleted"))
                continue
            staged = head[path] != index[path]
            unstaged = (
                state == "deleted"
                or state == "typechange"
                or worktree_blob != index[path][1]
                or worktree_mode != index[path][0]
            )
            if not staged and not unstaged:
                continue
            if state == "deleted":
                status = "deleted"
                digest = None
                size = 0
            elif state == "typechange":
                status = "typechange"
            else:
                status = "modified"
            entries.append(
                DirtyEntry(
                    path=path,
                    status=status,
                    sha256=digest,
                    size=size,
                )
            )
        for path in sorted(untracked_paths):
            if path in index or path in renamed:
                continue
            if path in head and path not in deleted:
                continue
            if any(part in {".git", ".mgar"} for part in path.split("/")):
                continue
            try:
                target = _safe_target(source, path)
            except DomainError:
                entries.append(DirtyEntry(path=path, status="typechange"))
                continue
            if target.is_symlink():
                entries.append(DirtyEntry(path=path, status="typechange"))
                continue
            if not target.is_file():
                continue
            data = target.read_bytes()
            entries.append(
                DirtyEntry(
                    path=path,
                    status="untracked",
                    sha256=hashlib.sha256(data).hexdigest(),
                    size=len(data),
                )
            )
        return DirtyInventory.build(entries)


class TaskWorkspace:
    """One isolated task copy with read/search/status/diff/edit/apply-back."""

    def __init__(
        self,
        root: Path,
        manifest: WorkspaceManifest,
        registration: Registration,
        limits: WorkspaceLimits,
    ) -> None:
        self.root = Path(root).resolve()
        self.manifest = manifest
        self.registration = registration
        self.limits = limits
        self._baseline = self._load_baseline()

    def _load_baseline(self) -> dict[str, str]:
        path = _safe_target(self._metadata_root(), "baseline.json")
        try:
            payload = json.loads(_read_text_nofollow(path))
            raw = payload["files"]
            if not isinstance(raw, dict):
                raise ValueError("baseline files is not an object")
            baseline = {str(key): str(value) for key, value in raw.items()}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DomainError(
                "WORKSPACE_MANIFEST_MISMATCH",
                "Workspace baseline is missing or invalid",
                409,
            ) from exc
        if digest(baseline) != self.manifest.baseline_hash:
            raise DomainError(
                "WORKSPACE_MANIFEST_MISMATCH",
                "Workspace baseline hash no longer matches the manifest",
                409,
            )
        return baseline

    def _persist_manifest(self) -> None:
        metadata = self._metadata_root()
        _safe_target(metadata, "manifest.json")
        _write_json_atomic(metadata, "manifest.json", self.manifest.model_dump(mode="json"))

    def _metadata_root(self) -> Path:
        path = self.root / ".mgar"
        if path.is_symlink() or not path.is_dir():
            raise DomainError(
                "WORKSPACE_MANIFEST_MISMATCH",
                "Workspace metadata directory is unavailable",
                409,
            )
        return path

    # -- read tools -----------------------------------------------------

    def snapshot(self) -> WorkspaceSnapshot:
        files: dict[str, str] = {}
        for relative in sorted(self._baseline):
            target = _safe_target(self.root, relative)
            try:
                files[relative] = _read_text_nofollow(target)
            except UnicodeDecodeError:
                continue
        return WorkspaceSnapshot(
            workspace_id=self.manifest.workspace_id,
            tree_hash=_tree_hash(_scan(self.root, self.limits)[0]),
            files=files,
        )

    def read(self, path: str) -> str:
        relative = _check_relative(path)
        target = _safe_target(self.root, relative)
        if not target.is_file():
            raise DomainError("WORKSPACE_FILE_NOT_FOUND", "Workspace file was not found", 404)
        try:
            return _read_text_nofollow(target)
        except UnicodeDecodeError as exc:
            raise DomainError("WORKSPACE_BINARY", "Workspace file is not UTF-8 text", 415) from exc

    def list_dir(self, path: str = "") -> tuple[str, ...]:
        relative = _check_relative(path) if path not in {"", "."} else ""
        target = self.root if relative == "" else _safe_target(self.root, relative)
        if not target.is_dir():
            raise DomainError("WORKSPACE_DIR_NOT_FOUND", "Workspace directory was not found", 404)
        return tuple(sorted(child.name for child in target.iterdir()))

    def search(self, query: str, *, path: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if not query:
            raise DomainError("WORKSPACE_SEARCH_EMPTY", "Search query is empty", 422)
        relative = _check_relative(path) if path not in {"", "."} else ""
        base = self.root if relative == "" else _safe_target(self.root, relative)
        if not base.is_dir():
            raise DomainError("WORKSPACE_DIR_NOT_FOUND", "Search root was not found", 404)
        matches: list[dict[str, Any]] = []
        for current, directories, files in os.walk(base, followlinks=False):
            directories[:] = sorted(
                name
                for name in directories
                if name not in _SKIP_PARTS and not (Path(current) / name).is_symlink()
            )
            for name in sorted(files):
                if len(matches) >= limit:
                    return matches
                target = Path(current) / name
                if target.is_symlink() or not target.is_file():
                    continue
                try:
                    text = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for number, line in enumerate(text.splitlines(), start=1):
                    if query in line:
                        matches.append(
                            {
                                "path": target.relative_to(self.root).as_posix(),
                                "line": number,
                                "text": line[:400],
                            }
                        )
                        if len(matches) >= limit:
                            return matches
        return matches

    def status(self) -> list[dict[str, str]]:
        current, _ = _scan(self.root, self.limits)
        result: list[dict[str, str]] = []
        for path in sorted(set(self._baseline) | set(current)):
            before = self._baseline.get(path)
            after = current.get(path)
            if before == after:
                continue
            result.append(
                {
                    "path": path,
                    "status": "added"
                    if before is None
                    else "deleted"
                    if after is None
                    else "modified",
                }
            )
        return result

    def diff(self, path: str = "") -> str:
        import difflib

        relative = _check_relative(path) if path not in {"", "."} else ""
        parts: list[str] = []
        for change in self.status():
            item = change["path"]
            if relative and item != relative and not item.startswith(relative + "/"):
                continue
            before = self._baseline_text(item)
            after = self._read_text(item)
            parts.append(f"diff --git a/{item} b/{item}\n")
            parts.extend(
                difflib.unified_diff(
                    (before or "").splitlines(keepends=True),
                    (after or "").splitlines(keepends=True),
                    fromfile=f"a/{item}",
                    tofile=f"b/{item}",
                )
            )
        return "".join(parts)

    def _baseline_text(self, path: str) -> str | None:
        root = self._metadata_root() / "baseline-files"
        if root.is_symlink() or not root.is_dir():
            raise DomainError(
                "WORKSPACE_MANIFEST_MISMATCH",
                "Workspace baseline directory is unavailable",
                409,
            )
        target = _safe_target(root, path)
        if not target.is_file():
            return None
        try:
            return _read_text_nofollow(target)
        except UnicodeDecodeError:
            return None

    def _read_text(self, path: str) -> str | None:
        target = _safe_target(self.root, path)
        if not target.is_file():
            return None
        try:
            return _read_text_nofollow(target)
        except UnicodeDecodeError:
            return None

    # -- edit and apply back --------------------------------------------

    def apply(self, edits: Sequence[WorkspaceEdit], *, expected_tree: str) -> WorkspaceSnapshot:
        current, _ = _scan(self.root, self.limits)
        if _tree_hash(current) != expected_tree:
            raise DomainError("WORKSPACE_STALE", "Workspace tree changed before the edit", 409)
        staged: dict[str, str] = {}
        for edit in edits:
            relative = _check_relative(edit.path)
            if relative in staged:
                raise DomainError("WORKSPACE_EDIT_DUPLICATED", "Duplicate edit for one path", 422)
            target = _safe_target(self.root, relative)
            if not target.is_file():
                raise DomainError("WORKSPACE_FILE_NOT_FOUND", "Edit target does not exist", 404)
            if _file_sha256(target) != edit.expected_sha256:
                raise DomainError("WORKSPACE_EDIT_STALE", "Edit pre-image hash does not match", 409)
            staged[relative] = edit.content
        for relative, content in staged.items():
            target = _safe_target(self.root, relative)
            if target.is_symlink():
                raise DomainError(
                    "WORKSPACE_SYMLINK_DENIED", "Refusing to write through a symlink", 403
                )
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(target, flags, 0o644), "w", encoding="utf-8") as handle:
                handle.write(content)
        current, _ = _scan(self.root, self.limits)
        updated = self.manifest.model_copy(
            update={"tree_hash": _tree_hash(current), "manifest_hash": ""}
        )
        self.manifest = updated.model_copy(update={"manifest_hash": updated.computed_hash()})
        self._persist_manifest()
        return self.snapshot()

    def apply_back(
        self,
        target_root: Path,
        expected_preimages: Mapping[str, str | None],
    ) -> ApplyReceipt:
        target = Path(target_root)
        if target.is_symlink():
            raise DomainError("WORKSPACE_PATH_DENIED", "Apply-back target is a symlink", 403)
        target = target.resolve()
        if self.registration.kind != "local":
            raise DomainError(
                "WORKSPACE_APPLY_DENIED", "Only local projects can be applied back", 403
            )
        source_root = _registered_root(target, self.registration)
        self._baseline = self._load_baseline()
        changes = self.status()
        conflicts: list[ApplyConflict] = []
        prepared: list[tuple[str, str]] = []
        for change in changes:
            relative = change["path"]
            expected = expected_preimages.get(relative, "__missing__")
            current_path = _safe_target(source_root, relative)
            actual = _source_sha(current_path)
            if relative not in expected_preimages:
                conflicts.append(
                    ApplyConflict(
                        path=relative,
                        reason="missing",
                        expected_sha256=None,
                        actual_sha256=actual,
                    )
                )
                continue
            if expected != self._baseline.get(relative):
                conflicts.append(
                    ApplyConflict(
                        path=relative,
                        reason="stale_workspace",
                        expected_sha256=expected,
                        actual_sha256=actual,
                    )
                )
                continue
            if actual != expected:
                conflicts.append(
                    ApplyConflict(
                        path=relative,
                        reason="stale",
                        expected_sha256=expected,
                        actual_sha256=actual,
                    )
                )
                continue
            prepared.append((relative, change["status"]))
        if conflicts:
            return self._receipt(
                "conflict", [], conflicts, _tree_hash(_scan(self.root, self.limits)[0])
            )

        # Existing files are opened before the critical section so the actual
        # write entry remains observable; the content is revalidated under a
        # cross-process lock on the source directory and no truncation happens
        # until every change still matches its pre-image.
        opened: dict[str, tuple[int, int]] = {}
        try:
            for relative, change_status in prepared:
                if change_status != "modified":
                    continue
                destination = _safe_target(source_root, relative)
                try:
                    writer = os.open(
                        destination,
                        os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                    )
                except FileNotFoundError:
                    conflicts.append(
                        ApplyConflict(
                            path=relative,
                            reason="stale",
                            expected_sha256=expected_preimages[relative],
                            actual_sha256=None,
                        )
                    )
                    continue
                reader = os.open(
                    destination,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                opened[relative] = (writer, reader)
            if conflicts:
                return self._receipt(
                    "conflict", [], conflicts, _tree_hash(_scan(self.root, self.limits)[0])
                )
            with _source_lock(source_root):
                for relative, change_status in prepared:
                    expected = expected_preimages[relative]
                    current_path = _safe_target(source_root, relative)
                    if change_status == "modified":
                        writer, reader = opened[relative]
                        held = os.fstat(reader)
                        named = os.lstat(current_path)
                        writer_stat = os.fstat(writer)
                        if (
                            held.st_dev,
                            held.st_ino,
                        ) != (named.st_dev, named.st_ino) or (
                            writer_stat.st_dev,
                            writer_stat.st_ino,
                        ) != (named.st_dev, named.st_ino):
                            actual = None
                        else:
                            actual = hashlib.sha256(os.pread(reader, held.st_size, 0)).hexdigest()
                    else:
                        actual = _source_sha(current_path)
                    if actual != expected:
                        conflicts.append(
                            ApplyConflict(
                                path=relative,
                                reason="stale",
                                expected_sha256=expected,
                                actual_sha256=actual,
                            )
                        )
                if conflicts:
                    return self._receipt(
                        "conflict",
                        [],
                        conflicts,
                        _tree_hash(_scan(self.root, self.limits)[0]),
                    )
                applied: list[str] = []
                for relative, change_status in prepared:
                    destination = _safe_target(source_root, relative)
                    source = self.root.joinpath(*relative.split("/"))
                    if change_status == "modified":
                        writer, _reader = opened[relative]
                        payload = source.read_bytes()
                        os.ftruncate(writer, 0)
                        os.lseek(writer, 0, os.SEEK_SET)
                        os.write(writer, payload)
                    elif change_status == "added":
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination = _safe_target(source_root, relative)
                        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                        descriptor = os.open(destination, flags, 0o644)
                        try:
                            os.write(descriptor, source.read_bytes())
                        finally:
                            os.close(descriptor)
                    else:
                        destination.unlink(missing_ok=True)
                    applied.append(relative)
                return self._receipt(
                    "applied",
                    applied,
                    [],
                    _tree_hash(_scan(self.root, self.limits)[0]),
                )
        finally:
            for writer, reader in opened.values():
                for descriptor in (writer, reader):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def _receipt(
        self,
        status: Literal["applied", "conflict"],
        applied: list[str],
        conflicts: list[ApplyConflict],
        tree: str,
    ) -> ApplyReceipt:
        payload = ApplyReceipt(
            workspace_id=self.manifest.workspace_id,
            task_id=self.manifest.task_id,
            status=status,
            applied=sorted(applied),
            conflicts=conflicts,
            tree_before=self.manifest.tree_hash,
            tree_after=tree,
            receipt_hash="",
        )
        return payload.model_copy(update={"receipt_hash": payload.computed_hash()})

    def tree_hash(self) -> str:
        return _tree_hash(_scan(self.root, self.limits)[0])


class WorkspaceToolRegistry:
    """H03 read/search/status/diff/edit tools over one isolated task workspace."""

    def __init__(self, workspace: TaskWorkspace) -> None:
        self.workspace = workspace

    def specs(self) -> Sequence[ToolSpec]:
        path = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        return (
            ToolSpec(
                name="read_file",
                description="Read one UTF-8 file.",
                input_schema=path,
                capability="read",
            ),
            ToolSpec(
                name="list_dir",
                description="List one directory.",
                input_schema=path,
                capability="read",
            ),
            ToolSpec(
                name="search_files",
                description="Literal text search inside the workspace.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "query": {"type": "string"}},
                    "required": ["path", "query"],
                },
                capability="read",
            ),
            ToolSpec(
                name="git_status",
                description="Workspace change status.",
                input_schema=path,
                capability="read",
            ),
            ToolSpec(
                name="git_diff",
                description="Unified diff against the task baseline.",
                input_schema=path,
                capability="read",
            ),
            ToolSpec(
                name="apply_edits",
                description="Apply versioned edits after the expected tree hash.",
                input_schema={
                    "type": "object",
                    "properties": {"edits": {"type": "array"}, "expected_tree": {"type": "string"}},
                    "required": ["edits", "expected_tree"],
                },
                capability="write",
            ),
        )

    def invoke(
        self,
        call: HarnessToolCall,
        context: ToolContext,
        cancellation: CancellationToken,
    ) -> HarnessToolResult:
        try:
            cancellation.ensure_active()
            path = call.arguments.get("path", "")
            if not isinstance(path, str):
                raise DomainError("HARNESS_TOOL_ARGUMENT", "path must be a string", 422)
            if call.name == "read_file":
                return self._ok(call, self.workspace.read(path), {"path": path})
            if call.name == "list_dir":
                entries = list(self.workspace.list_dir(path))
                return self._ok(call, "\n".join(entries), {"path": path, "entries": entries})
            if call.name == "search_files":
                query = call.arguments.get("query")
                if not isinstance(query, str):
                    raise DomainError("HARNESS_TOOL_ARGUMENT", "query must be a string", 422)
                matches = self.workspace.search(query, path=path)
                return self._ok(call, json.dumps(matches, ensure_ascii=False), {"matches": matches})
            if call.name == "git_status":
                status_rows = self.workspace.status()
                return self._ok(
                    call,
                    json.dumps(status_rows, ensure_ascii=False),
                    {"status": status_rows},
                )
            if call.name == "git_diff":
                return self._ok(call, self.workspace.diff(path), {"path": path})
            if call.name == "apply_edits":
                raw = call.arguments.get("edits")
                expected_tree = call.arguments.get("expected_tree")
                if not isinstance(raw, list) or not isinstance(expected_tree, str):
                    raise DomainError("HARNESS_TOOL_ARGUMENT", "Invalid edit arguments", 422)
                edits = [WorkspaceEdit.model_validate(item) for item in raw]
                for edit in edits:
                    if not context.grant.can_write(edit.path):
                        raise DomainError("HARNESS_WRITE_DENIED", "Write is outside the grant", 403)
                snapshot = self.workspace.apply(edits, expected_tree=expected_tree)
                return self._ok(
                    call,
                    "edits applied",
                    {"tree_hash": snapshot.tree_hash, "changes": self.workspace.status()},
                )
            return HarnessToolResult(
                call_id=call.call_id,
                status="error",
                code="HARNESS_TOOL_UNKNOWN",
                content="Unknown tool",
            )
        except DomainError as exc:
            tool_status = "denied" if exc.status == 403 else "error"
            return HarnessToolResult(
                call_id=call.call_id,
                status=cast(Any, tool_status),
                code=exc.code,
                content=str(exc),
            )

    @staticmethod
    def _ok(call: HarnessToolCall, content: str, data: dict[str, Any]) -> HarnessToolResult:
        return HarnessToolResult(call_id=call.call_id, status="ok", content=content, data=data)


def _registered_root(target: Path, registration: Registration) -> Path:
    """Reconstruct the registered root from its stored path hash.

    The registration intentionally stores only a hash, so apply-back requires
    the operator-provided target path to hash to the same value.
    """

    actual = hashlib.sha256(str(target).encode()).hexdigest()
    if actual != registration.source_root_sha256:
        raise DomainError(
            "WORKSPACE_APPLY_DENIED", "Apply-back target is not the registered project", 403
        )
    return target
