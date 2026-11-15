"""First SA18 preparation checks, not formal Engine/API/publication acceptance."""

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from pydantic import ValidationError
from sa18_support import AFTER, AT, BEFORE, context, current, presentation

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.contracts import WorkspaceEdit
from mgar_v3.harness.workspace import WorkspaceRegistry
from mgar_v3.repair.pr_delivery import PublicationApproval, ReviewBundle, verify_approval
from mgar_v3.repair.workspace import blob, tree
from mgar_v3.super_agent.source_review_bundle import (
    build_bundle,
    encode_bundle,
    publication_metadata,
    tree_sha256,
    validate_approval,
    validate_current,
)
from mgar_v3.super_agent.source_review_capture import H03ReviewCapture, read_tree
from mgar_v3.super_agent.source_review_domain import (
    ReviewApprove,
    ReviewContext,
    ReviewPrepare,
    ReviewPresentation,
    ReviewVerification,
)
from mgar_v3.super_agent.source_review_ports import ReviewProfile

A = Path(__file__).resolve().parents[1]
P = A / "docs/super-agent-v1/sa18-proposal"


def test_full_bundle_reuses_original_hash_and_approval_contract():
    c = context()
    bundle = build_bundle(c, BEFORE, AFTER)
    assert bundle.tree_hash == tree_sha256(AFTER)
    assert bundle.base_sha == c.publication.base_sha and bundle.proposal_id == c.sha256
    assert bundle.diff == (
        "--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
        "-    return a - b\n+    return a + b\n"
    )
    assert {f.path: f.content for f in bundle.files} == {"src/calc.py": AFTER["src/calc.py"]}
    decoded = ReviewBundle.model_validate_json(encode_bundle(bundle))
    assert decoded == bundle
    approval = PublicationApproval.grant(bundle, actor="owner", approved_at=AT.isoformat())
    verify_approval(bundle, approval)
    assert "PRIVATE CHAT" not in bundle.body and str(c.target.ticket_id) in bundle.body
    assert c.origin.input.input_id.hex not in bundle.body


def test_reconciliation_metadata_contains_full_tree_not_only_modified_files():
    c = context()
    bundle = build_bundle(c, BEFORE, AFTER)
    metadata = publication_metadata(bundle, AFTER)
    assert metadata["git_tree"] == tree(AFTER)
    assert metadata["git_blobs"] == {p: blob(v.encode()) for p, v in AFTER.items()}
    assert "tests/test_calc.py" in metadata["git_blobs"]
    assert metadata["body_sha256"] == hashlib.sha256(bundle.body.encode()).hexdigest()
    assert AFTER["src/calc.py"] not in json.dumps(metadata)


@pytest.mark.parametrize("before_end,after_end", [("\n", "\n"), ("", "\n"), ("\n", ""), ("", "")])
def test_exported_patch_applies_to_exact_actual_preimage(tmp_path, before_end, after_end):
    before = {"calc.py": "变量 = 1" + before_end}
    after = {"calc.py": "变量 = 2" + after_end}
    c = context()
    raw = c.model_dump(mode="json")
    raw["baseline_tree_sha256"] = tree_sha256(before)
    raw["verification"]["tree_sha256"] = tree_sha256(after)
    c = ReviewContext.model_validate_json(json.dumps(raw))
    bundle = build_bundle(c, before, after)
    (tmp_path / "calc.py").write_bytes(before["calc.py"].encode())
    patch = tmp_path / "review.patch"
    patch.write_bytes(bundle.diff.encode())
    subprocess.run(
        ["git", "apply", "--check", str(patch)], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(["git", "apply", str(patch)], cwd=tmp_path, check=True, capture_output=True)
    assert (tmp_path / "calc.py").read_bytes() == after["calc.py"].encode()


@pytest.mark.parametrize("change", ["baseline", "tested", "added", "deleted", "unchanged", "nul"])
def test_incomplete_stale_or_unsupported_trees_fail_closed(change):
    before, after = dict(BEFORE), dict(AFTER)
    c = context()
    if change == "baseline":
        before["src/calc.py"] += "# unreviewed\n"
    elif change == "tested":
        after["src/calc.py"] += "# after verification\n"
    elif change == "added":
        after["extra.py"] = "print(1)\n"
    elif change == "deleted":
        del after["tests/test_calc.py"]
    elif change == "nul":
        after["src/calc.py"] += "\0"
    else:
        after = before
    with pytest.raises(DomainError):
        build_bundle(c, before, after)


@pytest.mark.parametrize(
    "path", ["../x", "/etc/passwd", ".env", "src/../a.py", "a//b", "a\\b", "a:b"]
)
def test_hidden_or_traversing_paths_never_enter_review(path):
    with pytest.raises(ValueError):
        build_bundle(context(), {path: "before"}, {path: "after"})


@pytest.mark.parametrize(
    "field",
    [
        "coding_id",
        "attempt_id",
        "mandate_sha256",
        "environment_sha256",
        "profile_sha256",
        "acceptance_sha256",
    ],
)
def test_verification_cannot_borrow_another_target_or_profile(field):
    raw = context().model_dump(mode="json")
    raw["verification"][field] = str(uuid4()) if field.endswith("_id") else "0" * 64
    with pytest.raises(ValidationError):
        ReviewContext.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize(
    "change", ["failed", "incomplete", "zero_tests", "failed_tests", "unbound"]
)
def test_unverified_or_zero_test_result_is_not_reviewable(change):
    raw = context().verification.model_dump(mode="json")
    field, value = {
        "failed": ("result", "failed"),
        "incomplete": ("result", "incomplete"),
        "zero_tests": ("tests_run", 0),
        "failed_tests": ("tests_failed", 1),
        "unbound": ("execution_operation_id", None),
    }[change]
    raw[field] = value
    with pytest.raises(ValidationError):
        ReviewVerification.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize(
    "field",
    [
        "source_current",
        "roles_current",
        "policy_current",
        "target_current",
        "verification_current",
        "primary_current",
        "no_running_jobs",
        "owner_current",
    ],
)
def test_every_current_authority_fence_is_required(field):
    c = context()
    with pytest.raises(DomainError):
        validate_current(c, current(c).model_copy(update={field: False}), at=AT)


@pytest.mark.parametrize(
    "field,value",
    [
        ("epoch", 2),
        ("input_revision", 2),
        ("async_revision", 1),
        ("async_input_revision", 1),
        ("tree_sha256", "0" * 64),
        ("context_sha256", "0" * 64),
        ("profile_sha256", "0" * 64),
        ("actor", "wrong_actor"),
        ("user_id", uuid4()),
        ("run_state", "cancelled"),
        ("mode", "live"),
    ],
)
def test_committed_version_target_actor_and_tree_changes_fence_review(field, value):
    c = context()
    with pytest.raises(DomainError):
        validate_current(c, current(c).model_copy(update={field: value}), at=AT)


@pytest.mark.parametrize("delta", [-1, 600, 601])
def test_review_expiry_cannot_be_extended_implicitly(delta):
    c = context()
    with pytest.raises(DomainError):
        validate_current(c, current(c), at=AT + timedelta(seconds=delta))


def approve_args():
    c = context()
    bundle = build_bundle(c, BEFORE, AFTER)
    p = presentation(c, bundle)
    request = ReviewApprove(
        presentation_id=p.presentation_id,
        bundle_sha256=p.bundle_sha256,
        action_sha256=p.action_sha256,
    )
    return c, current(c), p, request


def do_approve(args, **kw):
    c, facts, p, request = args
    values = dict(
        user_id=c.prepared_by,
        session_sha256=p.session_sha256,
        bundle_sha256=p.bundle_sha256,
        action_sha256=p.action_sha256,
        at=AT,
    )
    values.update(kw)
    return validate_approval(c, facts, p, request, **values)


def test_only_current_owner_exact_presented_bundle_can_approve():
    args = approve_args()
    assert do_approve(args) is None
    # Read access alone permits an inspection but not publication approval.
    c, facts, p, request = args
    with pytest.raises(DomainError) as exc:
        do_approve((c, facts.model_copy(update={"write_grant_current": False}), p, request))
    assert exc.value.code == "SOURCE_REVIEW_WRITE_CREDENTIAL_REQUIRED"


@pytest.mark.parametrize(
    "field", ["user_id", "session_sha256", "bundle_sha256", "action_sha256", "at"]
)
def test_another_user_session_hash_or_expired_presentation_cannot_approve(field):
    value = (
        uuid4()
        if field == "user_id"
        else (AT + timedelta(seconds=120) if field == "at" else "0" * 64)
    )
    with pytest.raises(DomainError):
        do_approve(approve_args(), **{field: value})


@pytest.mark.parametrize("field", ["presentation_id", "bundle_sha256", "action_sha256"])
def test_request_cannot_substitute_another_reviewed_object(field):
    c, facts, p, request = approve_args()
    value = uuid4() if field.endswith("_id") else "0" * 64
    with pytest.raises(DomainError):
        do_approve((c, facts, p, request.model_copy(update={field: value})))


@pytest.mark.parametrize(
    "extra",
    [
        "diff",
        "body",
        "files",
        "actor",
        "approved",
        "provider",
        "credential",
        "remote_url",
        "tests_passed",
    ],
)
def test_http_prepare_does_not_accept_untrusted_evidence_or_authority(extra):
    raw = dict(
        request_id=str(uuid4()),
        coding_id=str(uuid4()),
        verification_id=str(uuid4()),
        expected_epoch=1,
        expected_input_revision=1,
        profile_sha256="a" * 64,
    )
    raw[extra] = "caller claim"
    with pytest.raises(ValidationError):
        ReviewPrepare.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("ttl", [0, 121, 3600])
def test_presentation_is_bounded_and_does_not_roll_approval_expiry(ttl):
    c = context()
    raw = presentation(c, build_bundle(c, BEFORE, AFTER)).model_dump(mode="json")
    raw["expires_at"] = (AT + timedelta(seconds=ttl)).isoformat()
    with pytest.raises(ValidationError):
        ReviewPresentation.model_validate_json(json.dumps(raw))


def test_explicit_profile_never_inherits_a_live_or_read_credential_permission():
    p = ReviewProfile(
        "fixture",
        "a" * 64,
        "example/test",
        1,
        "main",
        "owner",
        AT + timedelta(hours=1),
        object(),
        object(),
        object(),
    )
    assert not p.write_authorized and len(p.sha256) == 64
    assert p.sha256 != replace(p, write_authorized=True).sha256
    for change in ({"mode": "live"}, {"repository_id": True}, {"reader": p.writer}):
        with pytest.raises(ValueError):
            replace(p, **change)


def test_actual_file_reader_retains_exact_unicode_and_crlf_bytes(tmp_path):
    path = tmp_path / "source.py"
    path.write_bytes("变量 = 1\r\n".encode())
    assert read_tree(tmp_path, ("source.py",)) == {"source.py": "变量 = 1\r\n"}


@pytest.mark.parametrize(
    "change",
    [
        "file_symlink",
        "directory_symlink",
        "root_symlink",
        "hardlink",
        "executable",
        "binary",
        "oversize",
    ],
)
def test_actual_capture_rejects_unsafe_or_incomplete_files(tmp_path, change):
    root = tmp_path / "root"
    root.mkdir()
    path = root / "source.py"
    path.write_text("normal")
    paths = ("source.py",)
    if change == "file_symlink":
        other = tmp_path / "secret"
        other.write_text("SECRET")
        path.unlink()
        path.symlink_to(other)
    elif change == "directory_symlink":
        (root / "linked").symlink_to(tmp_path, target_is_directory=True)
        paths = ("linked/outside",)
    elif change == "root_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    elif change == "hardlink":
        os.link(path, tmp_path / "other")
    elif change == "executable":
        path.chmod(0o755)
    elif change == "binary":
        path.write_bytes(b"\xff\xfe")
    else:
        path.write_bytes(b"x" * 262145)
    with pytest.raises((DomainError, OSError)):
        read_tree(root, paths)


def test_h03_capture_binds_actual_baseline_manifest_and_verified_tree(engine, tmp_path):
    source = tmp_path / "controlled"
    source.mkdir()
    (source / "calc.py").write_text("value = 1\n")
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    c = context()
    raw = registry.create_task_workspace(registration, str(c.assignment_id))
    snapshot = raw.snapshot()
    raw.apply(
        [
            WorkspaceEdit(
                path="calc.py",
                content="value = 2\n",
                expected_sha256=hashlib.sha256(b"value = 1\n").hexdigest(),
            )
        ],
        expected_tree=snapshot.tree_hash,
    )
    values = c.model_dump(mode="json")
    values["registration_sha256"] = registration.registration_hash
    values["baseline_tree_sha256"] = snapshot.tree_hash
    values["publication"]["repository"] = registration.repository
    values["publication"]["base_sha"] = registration.base_commit
    values["verification"]["tree_sha256"] = raw.tree_hash()
    c = ReviewContext.model_validate_json(json.dumps(values))
    driver = SimpleNamespace(registration=registration, _registry=lambda: registry)
    capture = H03ReviewCapture(engine, driver)
    before, after = capture.capture(c)
    assert before == {"calc.py": "value = 1\n"} and after == {"calc.py": "value = 2\n"}
    assert tree_sha256(before) == digest(raw._baseline)
    bundle = build_bundle(c, before, after)
    assert "-value = 1\n+value = 2\n" in bundle.diff
    with engine.transaction():
        with pytest.raises(DomainError) as exc:
            capture.capture(c)
        assert exc.value.code == "SOURCE_REVIEW_FILESYSTEM_OUTSIDE_TRANSACTION_REQUIRED"
    (raw.root / "calc.py").write_text("value = 3\n")
    with pytest.raises(DomainError):
        capture.capture(c)


@pytest.fixture
def draft(database):
    with psycopg.connect(database) as conn:
        with conn.transaction(force_rollback=True):
            conn.execute((P / "0050_source_reviews.sql").read_text(), prepare=False)
            yield conn


def test_proposed_sql_installs_six_review_fact_tables_in_real_pg(draft):
    tables = {
        r[0]
        for r in draft.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='app' "
            "AND tablename LIKE 'sa_source_review%'"
        ).fetchall()
    }
    assert tables == {
        "sa_source_reviews",
        "sa_source_review_payloads",
        "sa_source_review_presentations",
        "sa_source_review_approvals",
        "sa_source_review_links",
        "sa_source_review_events",
    }
    assert draft.execute("SELECT app.sa_source_review_current(%s)", (uuid4(),)).fetchone() == (
        False,
    )
    assert draft.execute("SELECT count(*) FROM app.repair_publication_steps").fetchone() == (0,)


def test_draft_only_adds_metadata_and_keeps_original_action_ledger(draft):
    assert draft.execute("SELECT count(*) FROM app.schema_migrations").fetchone() == (46,)
    assert not (A / "migrations/0050_source_reviews.sql").exists()
    for table in ("actions", "approvals", "outbox", "repair_publication_steps"):
        assert draft.execute("SELECT to_regclass(%s)", ("app." + table,)).fetchone()[0]
    names = {
        r[0]
        for r in draft.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname LIKE 'sa_source_review_%'"
        ).fetchall()
    }
    assert {
        "sa_source_review_body_removed",
        "sa_source_review_role_changed",
        "sa_source_review_async_changed",
        "sa_source_review_approval_guard",
    } <= names


def test_proposal_hashes_and_interfaces_match_without_shared_mutation():
    m = json.loads((P / "proposal-manifest.json").read_text())
    assert (
        hashlib.sha256((P / m["migration"]["draft"]).read_bytes()).hexdigest()
        == m["migration"]["sha256"]
    )
    assert hashlib.sha256((P / m["patch"]["path"]).read_bytes()).hexdigest() == m["patch"]["sha256"]
    for f in m["patch"]["files"]:
        assert hashlib.sha256((A.parent / f["path"]).read_bytes()).hexdigest() == f["before_sha256"]
    assert len(m["interfaces"]) == 16
