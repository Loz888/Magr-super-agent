"""Controlled Source-only transport and disclosure boundaries; no PG or live calls."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from mgar_v3.domain import DomainError, digest
from mgar_v3.live import LiveProfile
from mgar_v3.routing.live import RouterAuthorization, RouterPermit, RouterProfile
from mgar_v3.settings import Settings
from mgar_v3.sources.domain import ActorRef, SourceScope
from mgar_v3.super_agent import source_eval_model as transport
from mgar_v3.super_agent.budget_domain import PriceQuote
from mgar_v3.super_agent.source_eval_domain import (
    CanonicalInputRef,
    ModelContext,
    ModelContextGroup,
    SourceEvalProfile,
    SourceSnapshot,
    SourceSnapshotGroup,
    SourceTargetRef,
)
from mgar_v3.super_agent.source_eval_model import (
    ENDPOINT,
    MAX_FRAME_BYTES,
    MAX_RESPONSE_BYTES,
    PROMPT,
    PROMPT_SHA256,
    PROMPT_VERSION,
    SourceEvalModel,
    render_request,
)

PRIVATE = "PRIVATE_SOURCE_SENTINEL_8e64"
ACTOR = "Private-opaque-open-id_1987"


def setup():
    now = datetime.now(UTC)
    root = LiveProfile(
        case_id="synthetic-source-eval", project_id="orders-demo", repository="example/project",
        issue_number=1, actor="owner", question="Synthetic model permission only",
        source_ref="a" * 40, model_id="synthetic-deepseek", price_version="synthetic-price-v1",
        input_micros_per_million=300000, output_micros_per_million=1200000,
        context_tokens=4096, max_output_tokens=512, max_cost_micros=100000,
    )
    router = RouterProfile(
        project_id=root.project_id, repository=root.repository, actor=root.actor,
        budget_profile_hash=root.profile_hash, model_id=root.model_id,
        price_version=root.price_version, input_micros_per_million=300000,
        output_micros_per_million=1200000, context_tokens=4096, max_output_tokens=512,
    )
    settings = Settings(project_id=root.project_id, repository=root.repository,
                        local_actor="owner", allowed_actors=["owner"], allow_paid=True)
    permit = RouterPermit(router, RouterAuthorization(
        profile_hash=router.profile_hash, approved_by="owner", allow_paid=True,
        expires_at=now + timedelta(minutes=5),
    ), root, settings)
    price = PriceQuote(model_id=root.model_id, price_version=root.price_version,
                       input_micros_per_million=300000, output_micros_per_million=1200000)
    profile = SourceEvalProfile(
        provider_id="deepseek", profile_ref=router.profile_hash, price=price,
        prompt_version=PROMPT_VERSION, prompt_sha256=PROMPT_SHA256,
    )
    scope = SourceScope(source_kind="feishu", source_instance_id=uuid4(),
                        installation_id="Private-installation", tenant_id="Private-tenant")
    actor = ActorRef(scope=scope, id_type="open_id", actor_id=ACTOR, actor_kind="human")
    group = SourceSnapshotGroup(
        group_id=uuid4(), fast_batch_id=uuid4(), fast_batch_revision=1, topic_key="d" * 64,
        actors=(actor,), inputs=(CanonicalInputRef(
            input_id=uuid4(), version_fingerprint="e" * 64, identity_id=uuid4(),
            identity_revision=1, actor_key=actor.key,
        ),), targets=(SourceTargetRef(
            ticket_id=uuid4(), run_id=uuid4(), thread_id=uuid4(), thread_revision=1,
            execution_epoch=1, mandate_id=uuid4(), mandate_sha256="f" * 64,
            project_id=root.project_id, repository=root.repository, allowed=True,
        ),), repository=root.repository, context_complete=True,
        allowed_actions=("ignore", "observe", "attach", "create", "ask"),
    )
    snapshot = SourceSnapshot(
        batch_id=uuid4(), project_id=root.project_id, scope=scope,
        source_revision=1, generation=1, fast_config_revision=1, eval_config_revision=1,
        policy_id=uuid4(), policy_revision=1, prompt_version=PROMPT_VERSION,
        prompt_sha256=PROMPT_SHA256, profile_sha256=digest(profile.model_dump(mode="json")),
        price_sha256=digest(price.model_dump(mode="json")), budget_snapshot_sha256="a" * 64,
        expires_at=now + timedelta(minutes=2),
        groups=(group,),
    )
    context = ModelContext(groups=(ModelContextGroup(group_id=group.group_id, text=PRIVATE),))
    request = render_request(snapshot, context)
    return SimpleNamespace(snapshot=snapshot, context=context, request=request,
                           profile=profile, permit=permit)


def candidate(env, **changes):
    return {"schema_version": "source-router-proposals-v1", "proposals": [{
        "group_id": str(env.snapshot.groups[0].group_id), "action": "create",
        "target_id": None, "reason_code": "ACTIONABLE", **changes,
    }]}


def frame(payload):
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def events(env, *, text=None, finish="stop", usage=None):
    text = json.dumps(candidate(env), ensure_ascii=False) if text is None else text
    result = [frame({"choices": [{"index": 0, "delta": {"content": text}}]})]
    if finish is not None:
        result.append(frame({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}))
    if usage is not False:
        result.append(frame({"choices": [], "usage": usage or {
            "prompt_tokens": 100, "completion_tokens": 20,
        }}))
    return result


class Chunks(httpx.SyncByteStream):
    def __init__(self, values, *, broken=False):
        self.values, self.broken = values, broken

    def __iter__(self):
        yield from self.values
        if self.broken:
            raise httpx.ReadError(PRIVATE + " transport lost")


def model(env, payload=None, *, chunks=None, broken=False, status=200, **settings):
    posts = []
    payload = b"".join(events(env)) + b"data: [DONE]\n\n" if payload is None else payload

    def handle(request):
        posts.append(request)
        return httpx.Response(status, stream=Chunks(chunks or [payload], broken=broken))

    client = httpx.Client(transport=httpx.MockTransport(handle))
    value = SourceEvalModel(env.profile, env.permit, client=client,
                            api_key="controlled-no-network-key", **settings)
    return value, posts


def test_renderer_discloses_only_current_text_and_minimal_trusted_references():
    env = setup()
    value, posts = model(env)
    body = value.request_body(env.request)
    assert not posts and "tools" not in body and "tool_choice" not in body
    assert body["messages"][0]["content"] == PROMPT
    content = body["messages"][1]["content"]
    assert PRIVATE in content and ACTOR not in content
    assert "Private-installation" not in content and "Private-tenant" not in content
    assert "run_id" not in content and "workspace_id" not in content
    assert "thread_id" not in content and "mandate_id" not in content
    assert str(env.snapshot.groups[0].targets[0].ticket_id) in content
    assert env.snapshot.groups[0].actors[0].key in content
    assert PRIVATE not in repr(env.request) and ACTOR not in repr(env.request)
    assert PROMPT_SHA256 == hashlib.sha256(PROMPT.encode()).hexdigest()


def test_context_cannot_invent_or_omit_a_frozen_group():
    env = setup()
    with pytest.raises(DomainError) as error:
        render_request(env.snapshot, ModelContext(groups=(
            ModelContextGroup(group_id=uuid4(), text=PRIVATE),
        )))
    assert error.value.code == "SOURCE_EVAL_MODEL_CONTEXT_INVALID"
    assert PRIVATE not in str(error.value)


@pytest.mark.parametrize("changed", [
    "provider", "profile_ref", "model", "price_version", "input_price", "output_price",
    "profile_hash", "price_hash", "prompt", "project", "repository", "missing_repository",
    "expired", "revoked_permit", "changed_permit_context",
])
def test_profile_price_prompt_scope_and_current_permit_mismatches_never_post(changed):
    env = setup()
    if changed in {"provider", "profile_ref"}:
        env.profile = env.profile.model_copy(update={
            "provider_id" if changed == "provider" else "profile_ref": "unreviewed",
        })
    elif changed in {"model", "price_version", "input_price", "output_price"}:
        key, val = {
            "model": ("model_id", "wrong-model"), "price_version": ("price_version", "wrong-price"),
            "input_price": ("input_micros_per_million", 1),
            "output_price": ("output_micros_per_million", 1),
        }[changed]
        env.profile = env.profile.model_copy(update={
            "price": env.profile.price.model_copy(update={key: val}),
        })
    elif changed == "revoked_permit":
        env.permit.authorization = env.permit.authorization.model_copy(update={"allow_paid": False})
    elif changed == "changed_permit_context":
        env.permit.authorization = env.permit.authorization.model_copy(update={
            "context_snapshot_hash": "0" * 64,
        })
    else:
        update = {
            "profile_hash": {"profile_sha256": "0" * 64},
            "price_hash": {"price_sha256": "0" * 64},
            "prompt": {"prompt_sha256": "0" * 64},
            "project": {"project_id": "foreign-project"},
            "expired": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        }.get(changed)
        if update is None:
            update = {"groups": (env.snapshot.groups[0].model_copy(update={
                "repository": None if changed == "missing_repository" else "foreign/repository",
                "targets": (),
            }),)}
        env.snapshot = env.snapshot.model_copy(update=update)
        env.request = env.request.model_copy(update={"snapshot": env.snapshot})
    value, posts = model(env)
    with pytest.raises(DomainError):
        value.propose(env.request)
    assert not posts


def test_request_input_limit_is_checked_before_any_post():
    env = setup()
    value, posts = model(env, max_context_bytes=1)
    with pytest.raises(DomainError) as error:
        value.propose(env.request)
    assert error.value.code == "SOURCE_EVAL_MODEL_INPUT_BOUND" and not posts


def test_success_uses_one_fixed_https_request_no_tools_and_known_billing_only():
    env = setup()
    value, posts = model(env)
    outcome = value.propose(env.request)
    assert outcome.status == "ok" and outcome.reason_code == "SOURCE_EVAL_MODEL_OK"
    assert outcome.proposals is not None and outcome.proposals.proposals[0].action == "create"
    assert (outcome.input_tokens, outcome.output_tokens, outcome.cost_micros) == (100, 20, 54)
    assert outcome.known_usage and len(posts) == 1
    assert str(posts[0].url) == ENDPOINT and posts[0].method == "POST"
    assert json.loads(posts[0].content)["model"] == "synthetic-deepseek"
    assert "tools" not in json.loads(posts[0].content)
    assert PRIVATE not in outcome.model_dump_json()


def test_split_utf8_json_and_single_byte_crlf_frames_are_reassembled():
    env = setup()
    # Whitespace is valid JSON; every transport byte, including CRLF, is split.
    raw = events(env)
    raw[0] = frame({"choices": [{"delta": {"content": " "}}]}) + raw[0]
    raw = b"".join(raw).replace(b"\n\n", b"\r\n\r\n") + b"data: [DONE]\r\n\r\n"
    value, posts = model(env, chunks=[bytes([byte]) for byte in raw])
    result = value.propose(env.request)
    assert result.status == "ok" and len(posts) == 1


def test_actual_multibyte_utf8_metadata_survives_single_byte_transport_splits():
    env = setup()
    raw = events(env)
    first = json.loads(raw[0][6:])
    first["id"] = "读取"
    raw[0] = frame(first)
    payload = b"".join(raw)
    assert "读取".encode() in payload
    value, posts = model(env, chunks=[bytes([byte]) for byte in payload])
    result = value.propose(env.request)
    assert result.status == "ok" and result.known_usage and len(posts) == 1
    assert result.response_sha256 == hashlib.sha256(payload).hexdigest()
    assert "读取" not in result.model_dump_json()


@pytest.mark.parametrize("text", [
    "{", '{"schema_version":"source-router-proposals-v1","proposals":NaN}',
    '{"schema_version":"bad","schema_version":"source-router-proposals-v1","proposals":[]}',
    '```json\n{}\n```',
])
def test_candidate_schema_failure_preserves_known_cost_without_echoing_text(text):
    env = setup()
    value, posts = model(env, payload=b"".join(events(env, text=text)))
    result = value.propose(env.request)
    assert result.status == "error" and result.reason_code == "SOURCE_EVAL_PROPOSALS_INVALID"
    assert result.proposals is None and result.known_usage and result.cost_micros == 54
    assert len(posts) == 1
    assert all(text not in value for value in result.model_dump().values()
               if isinstance(value, str))


@pytest.mark.parametrize("finish", ["length", "tool_calls", "content_filter", "refusal"])
def test_nonstop_finish_cannot_return_proposals_but_known_usage_can_settle(finish):
    env = setup()
    value, posts = model(env, payload=b"".join(events(env, finish=finish)))
    result = value.propose(env.request)
    assert result.status == "error" and result.proposals is None
    assert result.reason_code == "SOURCE_EVAL_MODEL_FINISH_REJECTED"
    assert result.known_usage and result.cost_micros == 54 and len(posts) == 1


@pytest.mark.parametrize("case", [
    "tools", "function", "refusal", "choices", "index", "duplicate", "nan",
    "after_finish", "after_done", "model",
])
def test_untrusted_stream_protocol_cannot_authorize_proposals(case):
    env = setup()
    values = events(env)
    delta = {"content": ""}
    if case in {"tools", "function", "refusal"}:
        delta[{"tools": "tool_calls", "function": "function_call", "refusal": "refusal"}[case]] = (
            PRIVATE
        )
        values.insert(0, frame({"choices": [{"delta": delta}]}))
    elif case == "choices":
        values.insert(0, frame({"choices": [{"delta": {}}, {"delta": {}}]}))
    elif case == "index":
        values.insert(0, frame({"choices": [{"index": 1, "delta": {}}]}))
    elif case == "duplicate":
        values.insert(0, b'data: {"choices":[],"choices":[]}\n\n')
    elif case == "nan":
        values.insert(0, b'data: {"choices":[],"extra":NaN}\n\n')
    elif case == "after_finish":
        values.insert(2, frame({"choices": [{"delta": {"content": PRIVATE}}]}))
    elif case == "after_done":
        values.append(b"data: [DONE]\n\n")
        values.append(frame({"choices": [{"delta": {"content": PRIVATE}}]}))
    else:
        values.insert(0, frame({"model": "unreviewed-model", "choices": []}))
    value, posts = model(env, payload=b"".join(values))
    result = value.propose(env.request)
    assert result.status != "ok" and result.proposals is None and len(posts) == 1
    assert PRIVATE not in result.model_dump_json()
    if case == "model":
        assert not result.known_usage and result.cost_micros is None
    else:
        assert result.known_usage and result.cost_micros == 54


@pytest.mark.parametrize("container", ["outer_refusal", "choice_tools"])
def test_refusal_or_tools_outside_delta_cannot_be_ignored(container):
    env = setup()
    invalid = ({"choices": [], "refusal": PRIVATE} if container == "outer_refusal" else {
        "choices": [{"delta": {}, "tool_calls": [{"function": PRIVATE}]}],
    })
    value, posts = model(env, payload=frame(invalid) + b"".join(events(env)))
    result = value.propose(env.request)
    assert result.status == "error" and result.proposals is None
    assert result.known_usage and result.cost_micros == 54 and len(posts) == 1
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize("usage", [
    False, {"prompt_tokens": 100}, {"prompt_tokens": True, "completion_tokens": 20},
    {"prompt_tokens": -1, "completion_tokens": 20},
    {"prompt_tokens": 100, "completion_tokens": "20"},
    {"prompt_tokens": 100, "completion_tokens": 513},
])
def test_absent_invalid_or_out_of_profile_usage_is_unknown_not_known_zero(usage):
    env = setup()
    value, posts = model(env, payload=b"".join(events(env, usage=usage)))
    result = value.propose(env.request)
    assert result.status == "unknown" and result.proposals is None
    assert not result.known_usage
    assert result.cost_micros is result.input_tokens is result.output_tokens is None
    assert len(posts) == 1


def test_missing_authoritative_finish_retains_billing_but_never_a_proposal():
    env = setup()
    value, posts = model(env, payload=b"".join(events(env, finish=None)))
    result = value.propose(env.request)
    assert result.status == "unknown" and result.reason_code == "SOURCE_EVAL_MODEL_FINISH_UNKNOWN"
    assert result.known_usage and result.cost_micros == 54
    assert result.proposals is None and len(posts) == 1


@pytest.mark.parametrize("has_known_usage", [False, True])
def test_transport_loss_never_retries_or_echoes_private_provider_exception(has_known_usage):
    env = setup()
    payload = b"".join(events(env)) if has_known_usage else b"data: "
    value, posts = model(env, chunks=[payload], broken=True)
    result = value.propose(env.request)
    assert result.status == "unknown"
    assert result.reason_code == "SOURCE_EVAL_MODEL_TRANSPORT_UNKNOWN"
    assert result.known_usage is has_known_usage and result.proposals is None
    assert (result.cost_micros == 54) if has_known_usage else (result.cost_micros is None)
    assert len(posts) == 1 and PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize("status", [401, 429, 500, 302])
def test_provider_rejection_or_redirect_is_not_followed_or_retried(status):
    env = setup()
    payload = json.dumps({"error": {"message": PRIVATE}}).encode()
    value, posts = model(env, payload=payload, status=status)
    result = value.propose(env.request)
    assert result.status == "unknown" and result.proposals is None
    assert result.reason_code == "SOURCE_EVAL_MODEL_PROVIDER_ERROR"
    assert not result.known_usage and result.cost_micros is None
    assert len(posts) == 1 and PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize("case", ["wire", "frame", "output"])
def test_response_frame_and_accumulated_output_are_bounded(case):
    env = setup()
    payload = {
        "wire": b": comment" + b"x" * 4000 + b"\n\n",
        "frame": b"data: " + b"x" * MAX_FRAME_BYTES + b"\n\n",
        "output": b"".join(events(env)),
    }[case]
    if case == "wire":
        payload *= MAX_RESPONSE_BYTES // len(payload) + 1
    value, posts = model(
        env, payload=payload, **({"max_output_bytes": 1} if case == "output" else {}),
    )
    result = value.propose(env.request)
    assert result.status == "unknown" and result.proposals is None and len(posts) == 1
    assert result.reason_code == {
        "wire": "SOURCE_EVAL_MODEL_RESPONSE_BOUND", "frame": "SOURCE_EVAL_MODEL_FRAME_BOUND",
        "output": "SOURCE_EVAL_MODEL_OUTPUT_BOUND",
    }[case]


def test_total_elapsed_limit_stops_a_slow_stream_without_retry(monkeypatch):
    env = setup()
    value, posts = model(env, max_seconds=1)
    times = iter([0, 2])
    monkeypatch.setattr(transport, "monotonic", lambda: next(times))
    result = value.propose(env.request)
    assert result.status == "unknown" and result.reason_code == "SOURCE_EVAL_MODEL_TIMEOUT"
    assert len(posts) == 1 and result.proposals is None


def test_utf8_input_bytes_bound_cost_before_character_estimate_can_underreserve():
    env = setup()
    context = ModelContext(groups=(ModelContextGroup(
        group_id=env.snapshot.groups[0].group_id, text="界" * 1000,
    ),))
    request = render_request(env.snapshot, context)
    value, posts = model(env)
    profile = env.permit.profile
    wire = {
        "model": env.profile.price.model_id, "max_tokens": profile.max_output_tokens,
        "temperature": 0, "stream": True, "stream_options": {"include_usage": True},
        "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"},
        "messages": [message.model_dump() for message in request.messages],
    }
    encoded = json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) + profile.max_output_tokens > profile.context_tokens
    assert transport.estimate_tokens(encoded.decode()) + 1024 + profile.max_output_tokens < (
        profile.context_tokens
    )
    with pytest.raises(DomainError) as error:
        value.request_body(request)
    assert error.value.code == "SOURCE_EVAL_MODEL_INPUT_BOUND" and not posts
