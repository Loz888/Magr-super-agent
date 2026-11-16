"""Pure current-fact Coding validation; these functions create no authority."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.policy_domain import safe_path
from mgar_v3.super_agent.source_coding_domain import (
    CODING_VERSION,
    CodingCurrentFacts,
    CodingMandate,
    CodingReceiptFacts,
    CodingRunRequest,
    within,
)


def _deny(code: str) -> DomainError:
    return DomainError(code, "Coding requires current scoped original Run authority", 403)


def validate_coding_current(
    mandate: CodingMandate,
    facts: CodingCurrentFacts,
    request: CodingRunRequest,
    *,
    at: datetime,
) -> None:
    """Recheck this at each read/edit/model/command/result boundary on actual facts."""
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise _deny("SOURCE_CODING_TIME_INVALID")
    if (
        facts.original_scope_kind != CODING_VERSION
        or facts.original_read_only
        or facts.source_mode != "auto_repair"
    ):
        raise _deny("SOURCE_CODING_NEW_MANDATE_REQUIRED")
    if not mandate.valid_from <= at < min(mandate.valid_until, facts.origin.expires_at):
        raise _deny("SOURCE_CODING_MANDATE_EXPIRED")
    if (
        facts.target != mandate.target
        or facts.origin != mandate.origin
        or facts.repository != mandate.repository
        or facts.budget != mandate.budget
        or facts.mandate_sha256 != mandate.mandate_sha256
        or facts.coding_revision != mandate.revision
        or facts.profile_sha256 != mandate.worker_profile_sha256
        or facts.context_sha256 != mandate.context_sha256
        or facts.harness != mandate.harness
    ):
        raise _deny("SOURCE_CODING_BINDING_STALE")
    if (
        request.coding_id != mandate.coding_id
        or request.run_id != mandate.target.run_id
        or request.expected_revision != mandate.revision
        or request.assignment_id != facts.assignment_id
        or request.session_id != facts.session_id
        or request.epoch != facts.epoch
        or facts.run_epoch != facts.epoch
        or facts.assignment_epoch != facts.epoch
    ):
        raise _deny("SOURCE_CODING_EPOCH_FENCED")
    if (
        facts.run_state != "running"
        or facts.assignment_state != "active"
        or not facts.assignment_created_at
        <= at
        < min(
            facts.lease_until,
            facts.session_expires_at,
            facts.configuration_expires_at,
        )
    ):
        raise _deny("SOURCE_CODING_LEASE_FENCED")
    if not all(
        (
            facts.source_current,
            facts.identity_current,
            facts.policy_current,
            facts.canonical_current,
            facts.admission_allowed,
            facts.model_input_allowed,
            facts.execution_allowed,
            facts.budget_current,
            facts.snapshot_current,
            facts.target_current,
            facts.runtime_profile_current,
            facts.assignment_current,
            facts.session_current,
        )
    ):
        raise _deny("SOURCE_CODING_AUTHORITY_FENCED")
    if facts.environment_sha256 != mandate.grant.environment_sha256:
        raise _deny("SOURCE_CODING_ENVIRONMENT_CHANGED")
    h11, h12 = mandate.harness.h11, mandate.harness.h12
    if h11.engineering != "PASS" or h11.recovery != "PASS" or h12.engineering != "PASS":
        raise _deny("SOURCE_CODING_HARNESS_ENGINEERING_REQUIRED")
    if facts.mode == "live":
        if (h12.live_agent != "PASS" or h12.mvp != "PASS") and not facts.owner_trial_current:
            raise _deny("SOURCE_CODING_HARNESS_LIVE_REQUIRED")
        if not all(
            (
                facts.live_source_authorized,
                facts.live_model_authorized,
                facts.live_execution_authorized,
            )
        ):
            raise _deny("SOURCE_CODING_LIVE_AUTHORIZATION_REQUIRED")
        disclosure = "approved_live"
    else:
        disclosure = "controlled_fixture"
    if mandate.repository.model_disclosure != disclosure:
        raise _deny("SOURCE_CODING_MODEL_DISCLOSURE_DENIED")


def validate_coding_tool(
    mandate: CodingMandate,
    facts: CodingCurrentFacts,
    request: CodingRunRequest,
    *,
    at: datetime,
    capability: Literal["read", "write", "execute"],
    path: str | None = None,
    command: str | None = None,
    expected_tree_sha256: str | None = None,
) -> None:
    validate_coding_current(mandate, facts, request, at=at)
    if expected_tree_sha256 is not None and expected_tree_sha256 != facts.current_tree_sha256:
        raise _deny("SOURCE_CODING_TREE_STALE")
    grant = mandate.grant
    if capability == "execute":
        if expected_tree_sha256 is None:
            raise _deny("SOURCE_CODING_TREE_REQUIRED")
        if (
            path is not None
            or command is None
            or command not in grant.allowed_commands
            or command not in facts.policy_commands
        ):
            raise _deny("SOURCE_CODING_COMMAND_DENIED")
        return
    if capability not in {"read", "write"} or path is None or command is not None:
        raise _deny("SOURCE_CODING_TOOL_SCOPE_INVALID")
    try:
        path = "" if path == "" and capability == "read" else safe_path(path)
    except ValueError:
        raise _deny("SOURCE_CODING_PATH_DENIED") from None
    roots = grant.write_paths if capability == "write" else grant.read_paths
    current = facts.policy_write_paths if capability == "write" else facts.policy_read_paths
    if not within(path, roots) or not within(path, current) or within(path, grant.protected_paths):
        raise _deny("SOURCE_CODING_PATH_DENIED")
    if capability == "write" and expected_tree_sha256 is None:
        raise _deny("SOURCE_CODING_TREE_REQUIRED")


def validate_coding_receipt(
    mandate: CodingMandate,
    facts: CodingCurrentFacts,
    request: CodingRunRequest,
    receipt: CodingReceiptFacts,
    *,
    at: datetime,
    operation_id: UUID,
    operation_sha256: str,
    minimum_tests: int = 1,
) -> None:
    """Only actual current trusted receipt facts can promote a Coding outcome."""
    validate_coding_current(mandate, facts, request, at=at)
    if (
        type(minimum_tests) is not int
        or minimum_tests < 1
        or receipt.provenance != "broker_verifier"
        or not receipt.journaled
        or receipt.result != "passed"
        or receipt.tests_run < minimum_tests
    ):
        raise _deny("SOURCE_CODING_TRUSTED_VERIFICATION_REQUIRED")
    if (
        receipt.coding_id != mandate.coding_id
        or receipt.coding_revision != mandate.revision
        or receipt.mandate_sha256 != mandate.mandate_sha256
        or receipt.target != mandate.target
        or receipt.assignment_id != facts.assignment_id
        or receipt.session_id != facts.session_id
        or receipt.workspace_id != facts.workspace_id
        or receipt.epoch != facts.epoch
        or receipt.operation_id != operation_id
        or receipt.operation_sha256 != operation_sha256
        or receipt.tree_sha256 != facts.current_tree_sha256
        or receipt.environment_sha256 != facts.environment_sha256
        or receipt.profile_sha256 != mandate.worker_profile_sha256
        or receipt.acceptance_sha256 != mandate.acceptance_sha256
        or not facts.assignment_created_at <= receipt.observed_at <= at
    ):
        raise _deny("SOURCE_CODING_RECEIPT_BINDING_DENIED")
