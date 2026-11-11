"""Native source proof and remote container boundaries, with actual isolated PG."""

from dataclasses import replace

import httpx
import pytest
from sa06_support import CHAT_ID, payload, setup, signed
from test_super_agent_sa06_broker import api_row, counts, history, request_for, serve

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import SourceThreadRef
from mgar_v3.sources.feishu_broker import FeishuHistory
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.sources.gateway import SourceGateway
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.sources.ingress_domain import HistoryRequest, IngressConfigure
from mgar_v3.super_agent.policy_domain import PolicySave

TOPIC = "Thread-Allowed"


def request(env, topic=TOPIC, **kwargs):
    return HistoryRequest(
        source_thread=SourceThreadRef(
            scope=env.instance.scope, conversation_id=CHAT_ID, native_thread_id=topic
        ),
        limit=10,
        **kwargs,
    )


def prove(env, topic=TOPIC, root_only=False):
    raw = payload(event_id="proof-" + topic, message_id="proof-" + topic)
    raw["event"]["message"].update(root_id="Root-" + topic)
    if not root_only:
        raw["event"]["message"]["thread_id"] = topic
    receipt = env.runtime.receive(signed(env, raw))
    assert receipt.committed
    return raw, receipt


def restrict(env):
    policy = env.policy.model_copy(
        update={"selector": env.policy.selector.model_copy(update={"native_threads": (TOPIC,)})}
    )
    env.engine.source_policies.save(
        env.cookie,
        env.policy_id,
        PolicySave(expected_revision=1, definition=policy, review_ref="topic-only"),
    )
    env.engine.source_ingress.configure(
        env.cookie,
        env.source_id,
        IngressConfigure(
            expected_generation=1,
            settings=env.control.model_copy(update={"policy_revision": 2}),
            review_ref="topic-only",
        ),
    )
    env.runtime = FeishuRuntime(
        env.sources,
        env.source_id,
        1,
        2,
        env.secrets,
        replace(env.activation, generation=2),
        transport=httpx.MockTransport(env.handler),
    )
    env.broker = env.runtime.broker
    return SourceGateway(env.engine.source_ingress, env.source_id, 2)


@pytest.mark.parametrize("root_only", [False, True])
def test_no_native_proof_never_mints_or_reads_and_records_gap(engine, root_only):
    env = setup(engine, history=True)
    if root_only:
        raw, _ = prove(env, TOPIC, True)
        event = env.adapter.verify_ingress(env.instance, signed(env, raw)).metadata.items[0].event
        assert event.source_thread.native_thread_id is None
        target = raw["event"]["message"]["root_id"]
    else:
        target = TOPIC
    env.calls.clear()
    with pytest.raises(DomainError) as error:
        history(env, request(env, target))
    assert error.value.code == "FEISHU_HISTORY_THREAD_UNPROVEN" and env.calls == []
    assert counts(env)["source_ingress_gaps"] >= 1


def test_policy_and_request_strings_do_not_prove_container(engine):
    env = setup(engine, history=True)
    gateway = restrict(env)
    with pytest.raises(DomainError) as error:
        gateway.history(FeishuHistory(env.broker), request(env))
    assert error.value.code == "FEISHU_HISTORY_THREAD_UNPROVEN" and env.calls == []


def test_verified_topic_policy_remote_scope_replies_and_terminal_empty(engine):
    env = setup(engine, history=True)
    prove(env)
    gateway = restrict(env)
    raw = payload(event_id="reply-event", message_id="reply-message")
    raw["event"]["message"].update(thread_id=TOPIC, root_id="Root-Allowed", parent_id="Parent")
    serve(env, [api_row(raw)])
    page = gateway.history(FeishuHistory(env.broker), request(env))
    assert page.complete and len(page.deliveries) == 1
    assert dict(env.calls[-1].url.params) == {
        "container_id_type": "thread",
        "container_id": TOPIC,
        "page_size": "10",
    }
    item = page.deliveries[0].metadata.items[0]
    assert item.event.source_thread.native_thread_id == TOPIC
    assert item.event.reply_to_entity_id == "Parent" and item.event.historical_backfill
    with engine.transaction() as conn:
        assert conn.execute(
            "SELECT bool_and(context_only) AS yes FROM app.source_ingress_inputs "
            "WHERE metadata->'event'->>'historical_backfill'='true'"
        ).fetchone()["yes"]
    serve(env, [])
    assert gateway.history(FeishuHistory(env.broker), request(env)).complete


@pytest.mark.parametrize("other", ["Another-Thread", None])
def test_proven_thread_rejects_cross_thread_or_unproven_response(engine, other):
    env = setup(engine, history=True)
    prove(env)
    raw = payload(message_id="outside-topic")
    raw["event"]["message"]["thread_id"] = other
    serve(env, [api_row(raw)])
    before = counts(env)["source_ingress_inputs"]
    with pytest.raises(DomainError) as error:
        history(env, request(env))
    assert error.value.code == "FEISHU_HISTORY_SCOPE"
    assert counts(env)["source_ingress_inputs"] == before
    assert env.calls[-1].url.params["container_id_type"] == "thread"


def test_thread_pages_are_bound_to_scope_and_cannot_be_forged(engine):
    env = setup(engine, history=True)
    prove(env)
    prove(env, "Other")
    serve(env, [], has_more=True, page_token="opaque-page")
    page = history(env, request(env))
    assert not page.complete and page.next_cursor
    valid = page.next_cursor
    before = len(env.calls)
    with pytest.raises(DomainError):
        history(env, request(env, "Other", cursor=valid))
    assert len(env.calls) == before
    with pytest.raises(DomainError):
        history(env, request(env, cursor=valid[:-1] + "X"))
    assert len(env.calls) == before
    with pytest.raises(DomainError):
        history(env, request(env, cursor=valid.rsplit(":", 1)[0] + ":非ASCII"))
    assert len(env.calls) == before
    serve(env, [])
    assert history(env, request(env, cursor=valid)).complete
    assert env.calls[-1].url.params["page_token"] == "opaque-page"


@pytest.mark.parametrize("with_thread", [False, True])
def test_thread_tombstone_requires_exact_platform_thread_provenance(engine, with_thread):
    env = setup(engine, history=True)
    raw, first = prove(env)
    row = api_row(raw)
    row["deleted"] = True
    if not with_thread:
        row.pop("thread_id")
    serve(env, [row])
    if with_thread:
        page = history(env, request(env))
        assert page.complete and page.deliveries[0].bodies == (None,)
        event = page.deliveries[0].metadata.items[0].event
        assert event.source_thread.native_thread_id == TOPIC
        with pytest.raises(DomainError):
            engine.source_ingress.body(env.cookie, first.input_ids[0])
    else:
        with pytest.raises(DomainError):
            history(env, request(env))
        assert counts(env)["source_ingress_gaps"] >= 1


@pytest.mark.parametrize("where", ["mint", "get"])
def test_thread_io_revocation_never_releases_data(engine, where):
    env = setup(engine, history=True)
    prove(env)

    def response(req):
        if req.method == ("POST" if where == "mint" else "GET"):
            env.identities.roles(
                env.cookie,
                env.binding["binding_id"],
                RoleChange(expected_revision=1, roles=("requester",), review_ref="revoked-in-io"),
            )
        if req.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        return None

    env.response = response
    with pytest.raises(DomainError):
        history(env, request(env))
    assert len(env.calls) == (1 if where == "mint" else 2)
    if where == "get":
        assert env.calls[-1].url.params["container_id_type"] == "thread"


def test_whole_chat_exhaustion_does_not_claim_thread_reply_coverage(engine):
    env = setup(engine, history=True)
    serve(env, [])
    page = history(env, request_for(env))
    assert not page.complete and page.next_cursor is None
    assert env.calls[-1].url.params["container_id_type"] == "chat"
    assert counts(env)["source_ingress_gaps"] >= 1
