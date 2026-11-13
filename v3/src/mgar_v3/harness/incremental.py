"""Full-content-only SA13 compatibility port over a fixed H11 module.

The source and exact compatibility diff are recorded in the SA13 import
proposal. Shared R5 replacement helpers are not imported or overwritten.
Replacement payloads are rejected by WorkspaceEdit before this module is
invoked, and direct replacement calls fail closed. Historical full-content
output limits and the remaining fixed H11 helpers are retained.
"""

from __future__ import annotations

from collections.abc import Sequence

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import TextReplacement, WorkspaceEdit

DEFAULT_MAX_OUTPUT_BYTES = 65_536
DEFAULT_MAX_REPLACEMENTS = 64
MAX_FILE_BYTES = 2_000_000


def apply_replacements(content: str, replacements: Sequence[TextReplacement]) -> str:
    """SA13's reviewed bridge exposes only required, full-content edits."""
    raise DomainError(
        "SOURCE_WORKER_INCREMENTAL_NOT_AVAILABLE",
        "Incremental replacement edits are outside this Source Worker capability",
        403,
    )

def output_bytes(edits: Sequence[WorkspaceEdit]) -> int:
    """Bytes the model had to emit for these edits (not the resulting file size)."""
    total = 0
    for edit in edits:
        if edit.replacements is not None:
            total += sum(len(item.new_text.encode()) for item in edit.replacements)
        elif edit.content is not None:
            total += len(edit.content.encode())
    return total


def check_output_budget(
    edits: Sequence[WorkspaceEdit],
    *,
    max_output_bytes: int,
    max_replacements: int,
) -> None:
    """Enforce per-edit replacement count and the emitted-byte budget."""
    for edit in edits:
        if edit.replacements is not None and len(edit.replacements) > max_replacements:
            raise DomainError("PATCH_LIMIT", "Patch carries too many replacements", 413)
    if output_bytes(edits) > max_output_bytes:
        raise DomainError("PATCH_LIMIT", "Patch exceeds the total byte budget", 413)


def protocol(edits: Sequence[WorkspaceEdit]) -> str:
    """Report which wire protocol one patch used."""
    full = any(edit.content is not None for edit in edits)
    incremental = any(edit.replacements is not None for edit in edits)
    if full and incremental:
        return "mixed"
    return "incremental" if incremental else "full"


def staged_content(edit: WorkspaceEdit, before: str) -> str:
    """Resolve one edit against its pre-image into the full updated content."""
    if edit.replacements is not None:
        return apply_replacements(before, edit.replacements)
    if edit.content is None:  # pragma: no cover - the contract enforces one form
        raise DomainError("HARNESS_TOOL_ARGUMENT", "Edit carries no content", 422)
    return edit.content
