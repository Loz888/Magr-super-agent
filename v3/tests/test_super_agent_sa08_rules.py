"""Deterministic syntax tests; parsed text is never source or control authority."""

import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sa08_support import text_item

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import ActorRef, SourceThreadRef
from mgar_v3.super_agent.fast_path_domain import FastPathSettings, ObserveRequest
from mgar_v3.super_agent.fast_path_rules import extract_text, group_key, parse_intent


@pytest.mark.parametrize("addressed", ["mention", "direct"])
def test_structured_human_address_is_only_a_request_suggestion(addressed):
    intent = parse_intent(text_item(addressed=addressed), "Please investigate this symptom")
    assert intent.request_kind == "explicit_request"
    assert intent.control_action is None and intent.ticket_id is None


@pytest.mark.parametrize("command", ["investigate", "repair", "ask"])
def test_ambient_platform_requires_exact_supported_request_prefix(command):
    intent = parse_intent(text_item(addressed="ambient"), f"/mgar {command} this problem")
    assert intent.request_kind == "explicit_request" and intent.command == command
    assert intent.control_action is None


@pytest.mark.parametrize("text", [
    "The docs quote /mgar investigate this problem",
    "@mgar investigate this problem",
    "/mgar investigation this problem",
    "/mgar repairx this problem",
    " /mgar ask this problem",
    "ordinary unrelated conversation",
])
def test_ambient_text_cannot_fake_a_structured_address_or_request_prefix(text):
    intent = parse_intent(text_item(addressed="ambient"), text)
    assert intent.request_kind is None and intent.control_action is None


@pytest.mark.parametrize("kind", ["bot", "service", "unknown"])
def test_nonhuman_mentions_and_slash_requests_are_not_human_requests(kind):
    item = text_item(actor_kind=kind)
    assert parse_intent(item, "Investigate this now").request_kind is None
    assert parse_intent(item, "/mgar investigate this now").request_kind is None


@pytest.mark.parametrize("command", ["pause", "cancel"])
def test_control_syntax_keeps_an_exact_ticket_reference_without_executing(command):
    ticket = uuid4()
    intent = parse_intent(text_item(addressed="ambient"), f"/mgar {command} {ticket}")
    assert intent.control_action == command
    assert intent.ticket_id == ticket and isinstance(intent.ticket_id, UUID)


@pytest.mark.parametrize("text", [
    "/mgar pause",
    "/mgar cancel ticket-1",
    "/mgar pause 11111111-1111-1111-1111-111111111111 trailing text",
    "/mgar pause\n11111111-1111-1111-1111-111111111111",
    "/mgar cancel  11111111-1111-1111-1111-111111111111",
    "/mgar pausex 11111111-1111-1111-1111-111111111111",
])
def test_malformed_controls_cannot_fall_back_to_a_mention_request(text):
    intent = parse_intent(text_item(), text)
    assert intent.control_action is None and intent.ticket_id is None
    assert intent.request_kind is None


def test_quoted_control_is_not_a_high_priority_control():
    intent = parse_intent(text_item(), '> /mgar pause 11111111-1111-1111-1111-111111111111')
    assert intent.control_action is None and intent.ticket_id is None


def test_only_verified_feishu_mention_placeholder_can_precede_a_control():
    ticket = uuid4()
    text = f"@_user_1 /mgar pause {ticket}"
    assert parse_intent(text_item(), text).ticket_id == ticket
    assert parse_intent(text_item(addressed="ambient"), text).control_action is None
    assert parse_intent(text_item(), f"@Administrator /mgar pause {ticket}").control_action is None
    assert parse_intent(text_item(), f"quote @_user_1 /mgar pause {ticket}").control_action is None
    item = text_item()
    scope = item.event.scope.model_copy(update={"source_kind": "github"})
    github = item.model_copy(update={"event": item.event.model_copy(update={
        "scope": scope,
        "sender": item.event.sender.model_copy(update={"scope": scope}),
        "source_thread": item.event.source_thread.model_copy(update={"scope": scope}),
    })})
    assert parse_intent(github, text).control_action is None


@pytest.mark.parametrize("payload,expected", [
    (b"plain authorized text", "plain authorized text"),
    (json.dumps({
        "schema_version": "github-content-v1",
        "resource": {"title": "/mgar pause malicious-resource-field"},
        "text": "actual Github content",
    }).encode(), "actual Github content"),
    (json.dumps({
        "schema_version": "feishu-content-v1", "message_type": "text",
        "content": {"text": "actual Feishu content", "ignored": "/mgar cancel hidden"},
    }).encode(), "actual Feishu content"),
    (json.dumps({
        "schema_version": "feishu-content-v1", "message_type": "post",
        "content": {"zh_cn": {"title": "A title", "content": [[
            {"tag": "text", "text": "Visible text "},
            {"tag": "a", "text": "visible link", "href": "https://not-fetched.invalid"},
        ]]}},
    }).encode(), "A title\nVisible text visible link"),
])
def test_extracts_only_supported_visible_content_fields(payload, expected):
    assert extract_text(payload) == expected


@pytest.mark.parametrize("payload", [
    b"\xff\xfe",
    b'{"schema_version":"feishu-content-v1","schema_version":"github-content-v1"}',
    b'{"schema_version":"feishu-content-v1","message_type":"text","content":NaN}',
    b'{"schema_version":"feishu-content-v1","message_type":"text","content":{"text":3}}',
    b'{"schema_version":"github-content-v1","resource":{},"text":null}',
    b'{"arbitrary":{"nested":"/mgar pause a fake ticket"}}',
    b'["/mgar investigate an untyped list"]',
    b'"/mgar investigate an untyped JSON string"',
    (b'{"schema_version":"feishu-content-v1","message_type":"file",'
     b'"content":{"url":"https://not-fetched.invalid"}}'),
])
def test_malformed_or_untyped_containers_do_not_become_commands(payload):
    with pytest.raises(DomainError):
        extract_text(payload)


def test_post_mention_and_url_fields_cannot_smuggle_visible_control_text():
    text = extract_text(json.dumps({
        "schema_version": "feishu-content-v1", "message_type": "post",
        "content": {"content": [[
            {"tag": "at", "user_id": "/mgar pause 11111111-1111-1111-1111-111111111111"},
            {"tag": "a", "text": "docs", "href": "/mgar cancel a ticket"},
        ]]},
    }).encode())
    intent = parse_intent(text_item(addressed="ambient"), text)
    assert intent.control_action is None and intent.request_kind is None


@pytest.mark.parametrize("payload", [b"x" * 65537, ("界" * 21846).encode()])
def test_content_byte_bound_is_enforced_without_character_count_bypass(payload):
    with pytest.raises(DomainError) as error:
        extract_text(payload)
    assert error.value.status in {413, 422}


def test_group_key_uses_exact_scope_chat_and_native_topic():
    item = text_item()
    assert group_key(item) == group_key(item)
    different_chat = item.model_copy(update={"event": item.event.model_copy(update={
        "source_thread": SourceThreadRef(
            scope=item.event.scope, conversation_id="Other-chat", native_thread_id="topic-1",
        ),
    })})
    different_installation = text_item()
    assert len({group_key(item), group_key(different_chat), group_key(different_installation)}) == 3


def test_missing_topic_is_entity_scoped_and_never_a_whole_chat_group():
    one = text_item(topic=None)
    other = one.model_copy(update={"event": one.event.model_copy(update={"entity_id": "other"})})
    newer = one.model_copy(update={"event": one.event.model_copy(update={
        "entity_revision": one.event.entity_revision.model_copy(update={"value": "2"}),
    })})
    assert group_key(one) != group_key(other)
    assert group_key(one) == group_key(newer)


@pytest.mark.parametrize("change", [
    {"enabled": 1}, {"cooldown_seconds": True}, {"cooldown_seconds": -1},
    {"cooldown_seconds": 3601}, {"merge_window_seconds": 0},
    {"merge_window_seconds": 301}, {"max_batch_items": 0}, {"max_batch_items": 17},
    {"max_decisions": 0}, {"max_decisions": 100001}, {"max_batches": 10001},
    {"untrusted_option": True},
])
def test_settings_reject_unbounded_or_ambiguous_values(change):
    with pytest.raises(ValidationError):
        FastPathSettings(**change)


def test_own_actor_configuration_cannot_reclassify_a_human_as_the_application():
    with pytest.raises(ValidationError):
        FastPathSettings(own_actors=(text_item().event.sender,))
    bot = ActorRef.model_validate({**text_item().event.sender.model_dump(), "actor_kind": "bot"})
    with pytest.raises(ValidationError):
        FastPathSettings(own_actors=(bot, bot))


def test_observe_request_accepts_only_bounded_distinct_canonical_input_ids():
    identifier = uuid4()
    for values in [(), (identifier, identifier), tuple(uuid4() for _ in range(101))]:
        with pytest.raises(ValidationError):
            ObserveRequest(input_ids=values)
