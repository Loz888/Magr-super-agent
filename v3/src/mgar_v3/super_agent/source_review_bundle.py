"""Build the original ReviewBundle from complete, bounded worker snapshots.

This module does no SQL, filesystem, network, or publication. The trusted capture
adapter supplies whole text trees; HTTP and model inputs cannot supply evidence.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from mgar_v3.domain import DomainError, digest
from mgar_v3.repair.pr_delivery import (
    PublicationFile,
    ReviewBundle,
    seal_bundle,
)
from mgar_v3.repair.workspace import blob, tree
from mgar_v3.super_agent.source_review_domain import (
    MAX_REVIEW_BYTES,
    ReviewApprove,
    ReviewContext,
    ReviewCurrent,
    ReviewPresentation,
)


def denied(code: str, status: int = 409) -> DomainError:
    return DomainError("SOURCE_REVIEW_" + code, "Current exact reviewed result required", status)


def sha256(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def tree_sha256(files: Mapping[str, str]) -> str:
    return digest({path: sha256(content) for path, content in files.items()})


def _files(files: Mapping[str, str]) -> dict[str, str]:
    if not files or len(files) > 512:
        raise denied("TREE_LIMIT")
    total = 0
    result = {}
    for path, content in sorted(files.items()):
        # PublicationFile also protects .git/.mgar/hidden paths and traversal.
        PublicationFile.repository_relative(path)
        if not isinstance(content, str) or "\0" in content:
            raise denied("TEXT_TREE_REQUIRED")
        size = len(content.encode())
        if size > 262144:
            raise denied("FILE_LIMIT")
        total += size
        if total > MAX_REVIEW_BYTES:
            raise denied("TREE_LIMIT")
        result[path] = content
    return result


def build_bundle(
    context: ReviewContext,
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> ReviewBundle:
    baseline, tested = _files(before), _files(after)
    if set(baseline) != set(tested):
        raise denied("EXISTING_TEXT_FILES_ONLY")
    if tree_sha256(baseline) != context.baseline_tree_sha256:
        raise denied("BASELINE_CHANGED")
    if tree_sha256(tested) != context.verification.tree_sha256:
        raise denied("TESTED_TREE_CHANGED")
    changed = [path for path in baseline if baseline[path] != tested[path]]
    if not changed or len(changed) > 16:
        raise denied("DIFF_LIMIT")
    diffs = []
    for path in changed:
        # newline="" preserves the actual preimage; Git's no-final-newline
        # marker is needed to avoid presenting a misleading patch export.
        lines = list(
            difflib.unified_diff(
                baseline[path].splitlines(keepends=True),
                tested[path].splitlines(keepends=True),
                fromfile="a/" + path,
                tofile="b/" + path,
            )
        )
        for line in lines:
            diffs.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    diff = "".join(diffs)
    if len(diff.encode()) > 131072:
        raise denied("DIFF_LIMIT")
    target = context.publication
    return seal_bundle(
        repository=target.repository,
        base_branch=target.base_branch,
        base_sha=target.base_sha,
        tree_hash=context.verification.tree_sha256,
        proposal_id=context.sha256,
        branch=target.branch,
        title=f"[Draft] Verified repair {context.target.ticket_id}",
        # Public text contains no source message, original objective, log or diff.
        body=(
            f"Controlled fixture result. Ticket: {context.target.ticket_id}\n"
            f"Review: {context.review_id}\nVerified tree: {context.verification.tree_sha256}\n"
            f"Verification: {context.verification.verification_id}\n"
        ),
        diff=diff,
        files=[PublicationFile(path=path, content=tested[path]) for path in changed],
    )


def publication_metadata(bundle: ReviewBundle, after: Mapping[str, str]) -> dict[str, object]:
    """Sufficient for full-tree, parent and PR reconciliation after body erasure."""
    tested = _files(after)
    if tree_sha256(tested) != bundle.tree_hash:
        raise denied("TESTED_TREE_CHANGED")
    return {
        "repository": bundle.repository,
        "base_branch": bundle.base_branch,
        "base_sha": bundle.base_sha,
        "branch": bundle.branch,
        "git_tree": tree(tested),
        "git_blobs": {path: blob(content.encode()) for path, content in tested.items()},
        "title_sha256": sha256(bundle.title),
        "body_sha256": sha256(bundle.body),
        "diff_sha256": sha256(bundle.diff),
        "bundle_sha256": bundle.payload_hash,
    }


def encode_bundle(bundle: ReviewBundle) -> bytes:
    raw = json.dumps(
        bundle.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if len(raw) > MAX_REVIEW_BYTES:
        raise denied("BUNDLE_LIMIT")
    return raw


def validate_current(context: ReviewContext, current: ReviewCurrent, *, at: datetime) -> None:
    if not context.prepared_at <= at < context.expires_at:
        raise denied("EXPIRED")
    if current.mode != context.publication.mode:
        raise denied("LIVE_NOT_AUTHORIZED", 403)
    if (
        current.context_sha256 != context.context_sha256
        or current.epoch != context.epoch
        or current.input_revision != context.input_revision
        or current.async_revision != context.async_revision
        or current.async_input_revision != context.async_input_revision
        or current.tree_sha256 != context.verification.tree_sha256
        or current.profile_sha256 != context.profile_sha256
        or current.actor != context.actor
        or current.user_id != context.prepared_by
        or current.run_state not in {"running", "waiting_approval"}
    ):
        raise denied("FENCED")
    if not all(
        (
            current.source_current,
            current.roles_current,
            current.policy_current,
            current.target_current,
            current.verification_current,
            current.primary_current,
            current.no_running_jobs,
            current.owner_current,
        )
    ):
        raise denied("AUTHORITY_LOST", 403)


def validate_approval(
    context: ReviewContext,
    current: ReviewCurrent,
    presentation: ReviewPresentation,
    request: ReviewApprove,
    *,
    user_id: UUID,
    session_sha256: str,
    bundle_sha256: str,
    action_sha256: str,
    at: datetime,
) -> None:
    validate_current(context, current, at=at)
    if not current.write_grant_current:
        raise denied("WRITE_CREDENTIAL_REQUIRED", 403)
    if not presentation.presented_at <= at < min(presentation.expires_at, context.expires_at):
        raise denied("PRESENTATION_EXPIRED")
    if (
        presentation.review_id != context.review_id
        or presentation.user_id != user_id
        or context.prepared_by != user_id
        or presentation.session_sha256 != session_sha256
        or presentation.context_sha256 != context.sha256
        or request.presentation_id != presentation.presentation_id
        or presentation.bundle_sha256 != bundle_sha256
        or request.bundle_sha256 != bundle_sha256
        or presentation.action_sha256 != action_sha256
        or request.action_sha256 != action_sha256
    ):
        raise denied("EXACT_OWNER_PRESENTATION_REQUIRED", 403)
