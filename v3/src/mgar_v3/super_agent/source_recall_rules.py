"""Bounded read planning; the resolver must authorize every current node/edge."""

from collections import deque
from collections.abc import Callable, Iterable

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_recall_domain import (
    RecallEdge,
    RecallNode,
    RecallRef,
    RecallWalkRequest,
    RecallWalkResult,
)


def denied() -> DomainError:
    # Uniform across missing and inaccessible identities, including derived CAS IDs.
    return DomainError("SOURCE_RECALL_UNAVAILABLE", "Reference is unavailable in this scope", 404)


def walk(
    request: RecallWalkRequest,
    resolve: Callable[[RecallRef], RecallNode],
    neighbours: Callable[[RecallRef], Iterable[RecallEdge]],
) -> RecallWalkResult:
    root = request.path.refs[-1]
    pending: deque[tuple[RecallRef, int, RecallEdge | None]] = deque([(root, 0, None)])
    visited: set[str] = set()
    nodes: list[RecallNode] = []
    edges: list[RecallEdge] = []
    examined = 0
    truncated = False
    while pending:
        ref, depth, via = pending.popleft()
        if ref.key in visited:
            continue
        visited.add(ref.key)
        try:
            node = resolve(ref)
        except DomainError:
            if ref == root:
                raise denied() from None
            continue
        if node.ref != ref:
            raise denied()
        if len(nodes) >= request.max_nodes:
            truncated = True
            break
        candidate = RecallWalkResult(
            nodes=(*nodes, node), edges=(*edges, via) if via else tuple(edges), truncated=True
        )
        if len(candidate.model_dump_json().encode()) > request.max_bytes:
            truncated = True
            break
        nodes.append(node)
        if via is not None:
            edges.append(via)
        if depth >= request.max_hops:
            # Do not claim complete reachability beyond the requested depth.
            truncated = True
            continue
        for edge in neighbours(ref):
            examined += 1
            if examined > request.max_edges:
                truncated = True
                break
            if edge.source != ref:
                raise denied()
            if edge.target.key not in visited:
                pending.append((edge.target, depth + 1, edge))
        if examined > request.max_edges:
            break
    result = RecallWalkResult(nodes=tuple(nodes), edges=tuple(edges), truncated=truncated)
    if len(result.model_dump_json().encode()) > request.max_bytes:
        raise denied()
    return result


def utf8_page(raw: bytes, offset: int, maximum: int) -> tuple[str, int]:
    if type(offset) is not int or type(maximum) is not int or not 128 <= maximum <= 4096:
        raise DomainError("SOURCE_RECALL_CURSOR_INVALID", "Page boundary is invalid", 403)
    if not 0 <= offset <= len(raw) or len(raw) > 2_000_000:
        raise DomainError("SOURCE_RECALL_CURSOR_INVALID", "Page boundary is invalid", 403)
    try:
        raw.decode("utf-8")
        raw[:offset].decode("utf-8")
    except UnicodeDecodeError:
        raise DomainError(
            "SOURCE_RECALL_ENCODING", "Only complete UTF8 text is pageable", 403
        ) from None
    end = min(len(raw), offset + maximum)
    while end > offset:
        try:
            return raw[offset:end].decode("utf-8"), end
        except UnicodeDecodeError:
            end -= 1
    if offset == len(raw):
        return "", offset
    raise DomainError("SOURCE_RECALL_ENCODING", "A full UTF8 character is required", 403)
