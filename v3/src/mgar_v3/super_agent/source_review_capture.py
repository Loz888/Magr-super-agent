"""Read the actual, already verified H03 workspace; no new Harness task service.

The captured baseline files are checked against H03's original manifest hashes,
including direct preimage hashes (the fixed H08 contract). Every component is
opened with NOFOLLOW and bounded reads. The caller holds the original Engine
mutex and rechecks current SQL facts before and after capture.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from psycopg.pq import TransactionStatus

from mgar_v3.domain import digest
from mgar_v3.repair.pr_delivery import PublicationFile
from mgar_v3.super_agent.source_coding_runtime import _read_h03_manifest
from mgar_v3.super_agent.source_coding_workspace import CodingWorkspace
from mgar_v3.super_agent.source_review_bundle import denied, sha256, tree_sha256
from mgar_v3.super_agent.source_review_domain import MAX_REVIEW_BYTES, ReviewContext
from mgar_v3.super_agent.source_scheduler_runtime import H03SchedulerResourceDriver

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine


def read_tree(root: Path, paths: tuple[str, ...]) -> dict[str, str]:
    """Complete supplied manifest tree, never a caller-supplied filesystem path."""
    files = {}
    total = 0
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for path in paths:
            PublicationFile.repository_relative(path)
            descriptors = []
            descriptor = root_fd
            try:
                parts = path.split("/")
                for position, part in enumerate(parts):
                    flags = os.O_RDONLY | os.O_NOFOLLOW
                    if position < len(parts) - 1:
                        flags |= os.O_DIRECTORY
                    descriptor = os.open(part, flags, dir_fd=descriptor)
                    descriptors.append(descriptor)
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_mode & 0o111
                    or before.st_size > 262144
                ):
                    raise denied("REGULAR_TEXT_TREE_REQUIRED")
                data = bytearray()
                while chunk := os.read(descriptor, min(65536, 262145 - len(data))):
                    data.extend(chunk)
                    if len(data) > 262144:
                        raise denied("FILE_LIMIT")
                after = os.fstat(descriptor)
                if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) or after.st_nlink != 1:
                    raise denied("TREE_CHANGED_DURING_CAPTURE")
                total += len(data)
                if total > MAX_REVIEW_BYTES:
                    raise denied("TREE_LIMIT")
                files[path] = data.decode("utf-8")
            finally:
                for descriptor in reversed(descriptors):
                    os.close(descriptor)
    except (OSError, UnicodeDecodeError):
        raise denied("REGULAR_TEXT_TREE_REQUIRED") from None
    finally:
        os.close(root_fd)
    return files


class H03ReviewCapture:
    def __init__(self, engine: Engine, driver: H03SchedulerResourceDriver) -> None:
        self.engine = engine
        self.driver = driver

    def capture(self, context: ReviewContext) -> tuple[dict[str, str], dict[str, str]]:
        with self.engine._mutex:
            if self.engine.connection.info.transaction_status != TransactionStatus.IDLE:
                raise denied("FILESYSTEM_OUTSIDE_TRANSACTION_REQUIRED")
            registration = self.driver.registration
            if registration.registration_hash != context.registration_sha256:
                raise denied("WORKSPACE_REGISTRATION_CHANGED")
            raw = self.driver._registry().open_task_workspace(
                registration, str(context.assignment_id)
            )
            manifest = _read_h03_manifest(raw.root)
            if (
                manifest != raw.manifest
                or manifest.task_id != str(context.assignment_id)
                or manifest.workspace_id != registration.workspace_id
                or manifest.kind != "local"
                or manifest.repository != context.publication.repository
                or manifest.base_commit != context.publication.base_sha
                or manifest.baseline_hash != context.baseline_tree_sha256
                or digest(raw._baseline) != context.baseline_tree_sha256
            ):
                raise denied("WORKSPACE_BINDING_CHANGED")
            # Use the already hardened facade's complete-layout check, without
            # granting a file write or trusting a new WorkspaceProvider.
            facade = CodingWorkspace(raw, context.workspace_id)
            facade._layout()
            paths = tuple(sorted(raw._baseline))
            metadata = os.open(raw.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                mgar = os.open(
                    ".mgar", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=metadata
                )
                try:
                    # The /proc descriptor path pins the opened parent instead
                    # of following a replaced .mgar directory from the workspace.
                    before = read_tree(Path(f"/proc/self/fd/{mgar}/baseline-files"), paths)
                finally:
                    os.close(mgar)
            finally:
                os.close(metadata)
            after = read_tree(raw.root, paths)
            if {p: sha256(v) for p, v in before.items()} != raw._baseline:
                raise denied("BASELINE_CHANGED")
            if (
                tree_sha256(after) != context.verification.tree_sha256
                or manifest.tree_hash != context.verification.tree_sha256
                or _read_h03_manifest(raw.root) != manifest
            ):
                raise denied("TESTED_TREE_CHANGED")
            return before, after
