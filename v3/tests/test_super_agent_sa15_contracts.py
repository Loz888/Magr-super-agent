"""First SA15 contract/plan checks. These do not certify PG/API integration."""

import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_recall_cursor import RecallCursors
from mgar_v3.super_agent.source_recall_domain import (
    RecallCursor,
    RecallEdge,
    RecallFact,
    RecallNode,
    RecallPageRequest,
    RecallPath,
    RecallRef,
    RecallSearchRequest,
    RecallWalkRequest,
)
from mgar_v3.super_agent.source_recall_rules import denied, utf8_page, walk

KINDS = [
    "source",
    "thread",
    "ticket",
    "run",
    "job",
    "evidence",
    "artifact",
    "memory",
    "review",
    "delivery",
    "gap",
]
NOW = datetime(2026, 11, 14, 11, 0, tzinfo=UTC)


def ref(kind="source"):
    return RecallRef(kind=kind, id="a" * 64 if kind == "artifact" else str(uuid4()))


def node(value, **extra):
    return RecallNode(
        ref=value, version_sha256="b" * 64, state="observed", availability="available", **extra
    )


def request(root, **kwargs):
    return RecallWalkRequest(path=RecallPath(refs=(root,)), **kwargs)


@pytest.mark.parametrize("kind", KINDS)
def test_exact_typed_identity_preserves_kind_without_alias_or_authority(kind):
    value = ref(kind)
    assert RecallRef.model_validate_json(value.model_dump_json()) == value
    assert node(value).grants_authority is False


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("identity", ["../../private", "https://private.invalid/body"])
def test_reference_never_treats_path_or_url_as_an_authorized_identity(kind, identity):
    with pytest.raises(ValidationError):
        RecallRef(kind=kind, id=identity)


@pytest.mark.parametrize("value", ["A" * 64, "a" * 63, "a" * 65, "g" * 64])
def test_artifact_id_is_strict_hash_not_a_locator(value):
    with pytest.raises(ValidationError):
        RecallRef(kind="artifact", id=value)


@pytest.mark.parametrize("kind", ["artifact", "memory", "review", "delivery"])
def test_derived_object_needs_provenance_path(kind):
    with pytest.raises(ValidationError):
        RecallPath(refs=(ref(kind),))


def test_cycle_in_submitted_path_is_rejected_before_any_resolution():
    root = ref()
    with pytest.raises(ValidationError):
        RecallPath(refs=(root, ref("run"), root))


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_hops", -1),
        ("max_hops", 5),
        ("max_hops", True),
        ("max_nodes", 0),
        ("max_nodes", 33),
        ("max_nodes", True),
        ("max_edges", 0),
        ("max_edges", 129),
        ("max_bytes", 16385),
        ("max_bytes", 1023),
    ],
)
def test_untrusted_budget_cannot_remove_traversal_bounds(field, value):
    with pytest.raises(ValidationError):
        request(ref(), **{field: value})


@pytest.mark.parametrize(
    "field", ["actor", "project_id", "trusted", "paid", "body", "grants_authority"]
)
def test_caller_cannot_self_assert_authority_in_read_request(field):
    with pytest.raises(ValidationError):
        RecallPageRequest.model_validate({"path": RecallPath(refs=(ref(),)), field: "forged"})


def test_conversation_selection_requires_installation_identity():
    with pytest.raises(ValidationError):
        RecallSearchRequest(query="登录", conversation_key="a" * 64)


@pytest.mark.parametrize("query", ["", "  ", "a\x00b", "a" * 257])
def test_query_is_bounded_untrusted_data(query):
    with pytest.raises(ValidationError):
        RecallSearchRequest(query=query)


@pytest.mark.parametrize(
    "after,before",
    [
        (NOW, None),
        (None, NOW),
        (NOW, NOW),
        (NOW, NOW - timedelta(seconds=1)),
        (NOW, NOW + timedelta(days=32)),
    ],
)
def test_time_filters_are_explicit_bounded_intervals(after, before):
    with pytest.raises(ValidationError):
        RecallSearchRequest(query="昨天登录", after=after, before=before)


def test_each_target_is_resolved_again_and_denied_reference_does_not_leak():
    root, allowed, private = ref(), ref("run"), ref("evidence")
    calls = []

    def resolve(value):
        calls.append(value)
        if value == private:
            raise denied()
        return node(value)

    def adjacent(value):
        if value == root:
            return (
                RecallEdge(source=root, target=allowed, relation="attempt"),
                RecallEdge(source=root, target=private, relation="evidence"),
            )
        return ()

    result = walk(request(root), resolve, adjacent)
    assert set(calls) == {root, allowed, private}
    assert {n.ref for n in result.nodes} == {root, allowed}
    assert private.id not in result.model_dump_json()


def test_relation_cannot_bypass_root_acl():
    root = ref()
    with pytest.raises(DomainError) as failure:
        walk(request(root), lambda value: (_ for _ in ()).throw(denied()), lambda value: ())
    assert failure.value.code == "SOURCE_RECALL_UNAVAILABLE"


def test_relation_cannot_substitute_an_authorized_object_for_requested_identity():
    root = ref()
    with pytest.raises(DomainError):
        walk(request(root), lambda value: node(ref()), lambda value: ())


def test_cycle_and_many_edges_stop_with_finite_unique_nodes():
    first, second = ref(), ref("run")
    edges = {
        first: (RecallEdge(source=first, target=second, relation="attempt"),),
        second: (RecallEdge(source=second, target=first, relation="origin"),),
    }
    result = walk(request(first), node, lambda value: edges[value])
    assert len(result.nodes) == 2 and not result.truncated


def test_node_and_edge_caps_are_hard_limits_not_silent_complete_claims():
    root, child, other = ref(), ref("run"), ref("job")
    links = (
        RecallEdge(source=root, target=child, relation="attempt"),
        RecallEdge(source=root, target=other, relation="resource"),
    )
    result = walk(request(root, max_nodes=1), node, lambda value: links if value == root else ())
    assert len(result.nodes) == 1 and result.truncated
    result = walk(request(root, max_edges=1), node, lambda value: links if value == root else ())
    assert len(result.edges) <= 1 and result.truncated


def test_serialized_response_including_edges_fits_byte_budget():
    root, child = ref(), ref("run")
    facts = (RecallFact(name="state", value="x" * 256),)
    link = RecallEdge(source=root, target=child, relation="attempt")
    result = walk(
        request(root, max_bytes=1024),
        lambda value: node(value, facts=facts),
        lambda value: (link,) if value == root else (),
    )
    assert len(result.model_dump_json().encode()) <= 1024 and result.truncated


@pytest.mark.parametrize("maximum", [128, 129, 256, 4096])
def test_utf8_pages_reconstruct_original_without_split_or_duplicate(maximum):
    raw = ("登录问题：🧭详细日志\n" * 300).encode()
    position = 0
    chunks = []
    while position < len(raw):
        text, end = utf8_page(raw, position, maximum)
        assert 0 < end - position <= maximum
        chunks.append(text)
        position = end
    assert "".join(chunks).encode() == raw


@pytest.mark.parametrize("offset", [-1, True, 1, 999])
def test_cursor_cannot_inject_negative_boolean_or_partial_utf8_offset(offset):
    with pytest.raises(DomainError):
        utf8_page("汉字".encode(), offset, 128)


def claim(**kwargs):
    return RecallCursor(
        purpose="page",
        caller_sha256="a" * 64,
        request_sha256="b" * 64,
        version_sha256="c" * 64,
        offset=128,
        after_id=uuid4(),
        expires_at=NOW + timedelta(seconds=60),
        **kwargs,
    )


def test_cursor_hides_private_skipped_id_and_binds_exact_caller():
    cursors = RecallCursors("server-only-secret-for-test")
    value = claim()
    token = cursors.encode(value)
    assert cursors.decode(token, "a" * 64, now=NOW) == value
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert str(value.after_id).encode() not in raw
    with pytest.raises(DomainError):
        cursors.decode(token, "d" * 64, now=NOW)


@pytest.mark.parametrize(
    "mutation", ["tamper", "expired", "another_server", "oversize", "malformed"]
)
def test_cursor_replay_tampering_and_expiry_fail_closed(mutation):
    cursors = RecallCursors("server-only-secret-for-test")
    token = cursors.encode(claim())
    clock = NOW
    if mutation == "tamper":
        token = ("A" if token[0] != "A" else "B") + token[1:]
    elif mutation == "expired":
        clock += timedelta(seconds=60)
    elif mutation == "another_server":
        cursors = RecallCursors("another-server-only-secret")
    elif mutation == "oversize":
        token = "a" * 4097
    elif mutation == "malformed":
        token = "%%invalid%%"
    with pytest.raises(DomainError) as failure:
        cursors.decode(token, "a" * 64, now=clock)
    assert failure.value.code == "SOURCE_RECALL_CURSOR_INVALID"


def test_depth_limit_never_claims_entire_reachable_graph_is_complete():
    values = [ref("run") for _ in range(6)]
    links = {
        value: (RecallEdge(source=value, target=values[index + 1], relation="attempt"),)
        for index, value in enumerate(values[:-1])
    }
    result = walk(request(values[0], max_hops=2), node, lambda value: links.get(value, ()))
    assert {n.ref for n in result.nodes} == set(values[:3]) and result.truncated
