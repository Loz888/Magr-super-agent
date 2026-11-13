"""Actual H03/Git fixtures with explicitly controlled guards, no live Source gate."""

import subprocess
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import WorkspaceEdit, sha256_text
from mgar_v3.harness.workspace import WorkspaceRegistry
from mgar_v3.super_agent.source_coding_workspace import CodingWorkspace


class ControlledGuard:
    """A fixture timing/denial port, never proof of actual Source permissions."""

    def __init__(self):
        self.enabled = True
        self.calls = []

    @contextmanager
    def __call__(self, access):
        self.calls.append(access)
        if not self.enabled:
            raise DomainError("CONTROLLED_AUTHORITY_REVOKED", "Controlled denial", 403)
        yield


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src/one.py").write_bytes(b"value = 1\r\n")
    (source / "README.md").write_text("controlled fixture\n")
    for args in [
        ["init", "--quiet"],
        ["remote", "add", "origin", "https://github.com/example/mgar-test"],
        ["add", "."],
        ["commit", "--quiet", "-m", "controlled source"],
    ]:
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
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    raw = registry.create_task_workspace(registration, str(uuid4()), source_root=source)
    guard = ControlledGuard()
    assignment_workspace_id = uuid4()
    facade = CodingWorkspace(raw, assignment_workspace_id, authority=guard)
    return SimpleNamespace(
        raw=raw,
        facade=facade,
        guard=guard,
        source=source,
        workspace_id=assignment_workspace_id,
    )


def test_real_h03_snapshot_maps_assignment_identity_and_actual_tree(workspace):
    snapshot = workspace.facade.snapshot()
    assert snapshot.workspace_id == str(workspace.workspace_id)
    assert snapshot.workspace_id != workspace.raw.manifest.workspace_id
    assert snapshot.tree_hash == workspace.raw.tree_hash()
    assert snapshot.files["src/one.py"] == "value = 1\r\n"
    assert workspace.guard.calls[-1].operation == "snapshot"
    assert workspace.facade.resolve("src/one.py") == "src/one.py"
    assert workspace.facade.resolve(".") == ""
    assert workspace.facade.list_dir("") == ("README.md", "src")
    assert workspace.facade.list_dir("src") == ("one.py",)


def test_absent_guard_denies_every_provider_method(workspace):
    denied = CodingWorkspace(workspace.raw, uuid4())
    for action in [
        denied.snapshot,
        lambda: denied.resolve("src/one.py"),
        lambda: denied.read("src/one.py"),
        lambda: denied.list_dir("src"),
        lambda: denied.diff("a" * 64),
        lambda: denied.apply([], expected_tree="a" * 64),
    ]:
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.code == "SOURCE_CODING_WORKSPACE_AUTHORITY_REQUIRED"


def test_each_read_rechecks_guard_after_a_previous_authorized_snapshot(workspace):
    workspace.facade.snapshot()
    workspace.guard.enabled = False
    with pytest.raises(DomainError) as error:
        workspace.facade.read("src/one.py")
    assert error.value.code == "CONTROLLED_AUTHORITY_REVOKED"


@pytest.mark.parametrize(
    "path",
    [
        "../README.md",
        "/etc/passwd",
        "src/../README.md",
        ".git/config",
        ".mgar/manifest.json",
    ],
)
def test_relative_resolver_does_not_expose_host_or_workspace_metadata(workspace, path):
    with pytest.raises(DomainError):
        workspace.facade.resolve(path)


def test_diff_adapts_observed_content_instead_of_h03_path_filter(workspace):
    before = workspace.facade.snapshot()
    before.files["src/one.py"] = "caller changed its own returned snapshot"
    (workspace.raw.root / "src/one.py").write_text("value = 2\n")
    diff = workspace.facade.diff(before.tree_hash)
    assert diff.workspace_id == str(workspace.workspace_id)
    assert diff.tree_before == before.tree_hash
    assert diff.tree_after == workspace.raw.tree_hash()
    assert len(diff.changes) == 1
    assert diff.changes[0].path == "src/one.py"
    assert diff.changes[0].before_sha256 == sha256_text("value = 1\r\n")
    assert diff.changes[0].after_sha256 == sha256_text("value = 2\n")
    assert "value = 1" in diff.patch and "value = 2" in diff.patch
    assert (workspace.source / "src/one.py").read_bytes() == b"value = 1\r\n"
    with pytest.raises(DomainError) as error:
        workspace.facade.diff("b" * 64)
    assert error.value.code == "SOURCE_CODING_WORKSPACE_SNAPSHOT_UNKNOWN"


@pytest.mark.parametrize("change", ["new_file", "deleted_file", "symlink"])
def test_first_slice_rejects_layout_changes_and_links(workspace, change):
    file = workspace.raw.root / "src/one.py"
    if change == "new_file":
        (workspace.raw.root / "src/new.py").write_text("new")
    elif change == "deleted_file":
        file.unlink()
    else:
        file.unlink()
        file.symlink_to(workspace.source / "README.md")
    with pytest.raises(DomainError):
        workspace.facade.snapshot()


def test_write_never_calls_h03_apply_without_a_real_original_engine_journal(workspace):
    before = workspace.facade.snapshot()
    edit = WorkspaceEdit(
        path="src/one.py",
        expected_sha256=sha256_text(before.files["src/one.py"]),
        content="value = 3\n",
    )
    for provider, code in [
        (workspace.facade, "SOURCE_CODING_DURABLE_JOURNAL_REQUIRED"),
        (
            CodingWorkspace(
                workspace.raw, uuid4(), authority=workspace.guard, journal=object()
            ),
            "SOURCE_CODING_JOURNAL_EXECUTION_UNAVAILABLE",
        ),
    ]:
        with pytest.raises(DomainError) as error:
            provider.apply([edit], expected_tree=before.tree_hash)
        assert error.value.code == code
    assert (workspace.raw.root / "src/one.py").read_bytes() == b"value = 1\r\n"
