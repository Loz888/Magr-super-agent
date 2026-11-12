"""Deterministic correlation over current Engine facts; no model or business writer."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_correlation_domain import (
    MAX_CORRELATION_BYTES,
    CorrelationResolution,
    CorrelationSnapshot,
    ResourceRef,
    TargetRef,
    snapshot_hash,
)

if TYPE_CHECKING:
    from mgar_v3.super_agent.source_eval_domain import SourceDecision

URL = re.compile(r"https://[^\s<>\"'`]{1,2048}")
PATH = re.compile(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(issues|pull)/([1-9][0-9]{0,18})/?")


def _error(code: str, status: int = 409) -> DomainError:
    return DomainError(code, "Source correlation requires current exact references", status)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def _constant(value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def parse_snapshot(raw: str | bytes) -> CorrelationSnapshot:
    """Fixed errors never echo a caller's private text or malformed payload."""
    try:
        if type(raw) not in {str, bytes}:
            raise ValueError("JSON bytes required")
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(encoded) > MAX_CORRELATION_BYTES:
            raise ValueError("Snapshot exceeds bound")
        value = json.loads(encoded, object_pairs_hook=_object, parse_constant=_constant)
        return CorrelationSnapshot.model_validate_json(json.dumps(value))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _error("SOURCE_CORRELATION_CONTRACT_INVALID", 422) from None


def exact_url_resources(text: str, known: tuple[ResourceRef, ...]) -> tuple[ResourceRef, ...]:
    """Return all known exact URL references; ambiguity remains for the resolver.

    The caller supplies currently authorized resource facts. This parser does not
    fetch URLs, establish ownership, recognize bare #numbers or choose an install.
    Two authorized installs with the same visible URL remain two distinct refs.
    """
    try:
        if type(text) is not str or len(text.encode("utf-8")) > MAX_CORRELATION_BYTES:
            raise ValueError("Reference text exceeds bound")
        found: dict[str, ResourceRef] = {}
        for token in URL.findall(text):
            parsed = urlsplit(token.rstrip(".,);]}"))
            if (parsed.scheme != "https" or parsed.netloc != "github.com"
                    or parsed.query or parsed.username or parsed.password):
                continue
            match = PATH.fullmatch(parsed.path)
            if match is None:
                continue
            repository = f"{match[1]}/{match[2]}".lower()
            kind = "issue" if match[3] == "issues" else "pull_request"
            for resource in known:
                if (resource.repository == repository and resource.kind == kind
                        and resource.resource_id == match[4]):
                    found[resource.resource_key] = resource
        return tuple(found[key] for key in sorted(found))
    except (ValueError, TypeError, UnicodeError):
        raise _error("SOURCE_CORRELATION_REFERENCE_INVALID", 422) from None


def _current_target(target: TargetRef, snapshot: CorrelationSnapshot) -> TargetRef | None:
    current = next((item for item in snapshot.targets if item.ticket_id == target.ticket_id), None)
    # Epoch/revision/Mandate are part of equality, not merely the UUID lookup.
    return current if current == target else None


def resolve(snapshot: CorrelationSnapshot, *, at: datetime,
            suggestion: SourceDecision | None = None) -> tuple[CorrelationResolution, ...]:
    """Select only one exact current resource owner, never a model-selected target.

    SourceDecision is optional provenance, not a filter, fallback, capability or
    tie-breaker. The Engine must independently recheck the snapshot before writes.
    """
    try:
        snapshot = CorrelationSnapshot.model_validate(snapshot.model_dump())
        if len(snapshot.model_dump_json().encode("utf-8")) > MAX_CORRELATION_BYTES:
            raise ValueError("Snapshot exceeds bound")
    except (AttributeError, ValueError, UnicodeError):
        raise _error("SOURCE_CORRELATION_CONTRACT_INVALID", 422) from None
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise _error("SOURCE_CORRELATION_TIME_INVALID", 422)
    if snapshot.expires_at <= at:
        raise _error("SOURCE_CORRELATION_SNAPSHOT_EXPIRED")
    fingerprint = snapshot_hash(snapshot)
    heads = {head.resource_key: head for head in snapshot.heads}
    proofs = {(item.input_id, item.resource.resource_key): item for item in snapshot.resources}
    suggestion_key = suggestion.decision_key if suggestion is not None else None
    results = []
    for unit in snapshot.units:
        common = {
            "input_id": unit.input_id, "unit_key": unit.unit_key,
            "snapshot_sha256": fingerprint, "resource_keys": unit.resource_keys,
            "suggestion_decision_key": suggestion_key,
        }

        def result(outcome: str, reason: str, candidates: tuple[TargetRef, ...] = (),
                   *, facts: dict[str, Any] = common) -> CorrelationResolution:
            return CorrelationResolution.model_validate({
                **facts, "outcome": outcome, "reason_code": reason, "candidates": candidates,
            })

        if not unit.resource_keys:
            results.append(result("unmatched", "NO_EXPLICIT_RESOURCE"))
            continue
        evidence = [proofs[unit.input_id, key] for key in unit.resource_keys]
        if any(item.proof_kind == "suggestion" for item in evidence):
            results.append(result("unmatched", "SUGGESTION_ONLY"))
            continue
        resources = [item.resource for item in evidence]
        # A unit spanning different owners/repositories or resource identities is
        # unresolved even if a caller supplies a shared Ticket for all of them.
        if len({item.resource_key for item in resources}) != 1:
            results.append(result("ambiguous", "RESOURCE_AMBIGUOUS"))
            continue
        resource = resources[0]
        head = heads.get(resource.resource_key)
        if head is None or head.state == "unclaimed":
            results.append(result("unmatched", "RESOURCE_UNCLAIMED"))
            continue
        if head.state == "removed":
            results.append(result("blocked", "RESOURCE_REMOVED"))
            continue
        assert head.target is not None
        if not head.target.allowed:
            results.append(result("blocked", "TARGET_DENIED"))
            continue
        current = _current_target(head.target, snapshot)
        if current is None:
            results.append(result("blocked", "TARGET_UNAVAILABLE"))
            continue
        if current.repository != resource.repository:
            results.append(result("blocked", "RESOURCE_CONFLICT"))
            continue
        results.append(result("matched", "EXACT_RESOURCE", (current,)))
    return tuple(results)
