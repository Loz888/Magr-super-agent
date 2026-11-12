"""Deterministic syntax only; these helpers never confer source authority.

A standalone /mgar pause|cancel command names one canonical UUID. Request syntax
starts with /mgar investigate|repair|ask. Only a verified Feishu mention may put
one leading platform @_user_N placeholder and whitespace before either command.
Arbitrary @names, Markdown quotes and unrelated prefixes are never stripped.
GitHub issues and pull requests normalize title + newline + body; only the body
after that first newline is command syntax. Their title alone is never a command.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.ingress_domain import ItemMetadata
from mgar_v3.super_agent.fast_path_domain import FAST_PATH_VERSION

MAX_CONTENT_BYTES = 65536
MAX_POST_ELEMENTS = 4096
REQUEST = re.compile(r"\A/mgar (investigate|repair|ask)(?=\s|\Z)")
CONTROL = re.compile(
    r"/mgar (pause|cancel) "
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
FEISHU_MENTION = re.compile(r"\A@_user_[0-9]+\s+")

Command = Literal["investigate", "repair", "ask", "pause", "cancel"]


@dataclass(frozen=True)
class ParsedIntent:
    request_kind: Literal["explicit_request", "team_rule"] | None = None
    command: Command | None = None
    control_action: Literal["pause", "cancel"] | None = None
    ticket_id: UUID | None = None


def _invalid() -> DomainError:
    return DomainError("FAST_PATH_CONTENT_INVALID", "Source content structure is invalid", 422)


def _unsupported() -> DomainError:
    return DomainError(
        "FAST_PATH_CONTENT_UNSUPPORTED", "Source content type is not supported", 422
    )


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate content field")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise _invalid()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise _invalid() from None
    if size > MAX_CONTENT_BYTES:
        raise DomainError("FAST_PATH_CONTENT_BOUND", "Source content exceeds its bound", 413)
    return value


def _post(value: Any) -> str:
    if not isinstance(value, dict):
        raise _invalid()
    # Receive normalizes the platform container, preserving either a direct post
    # or a language-keyed post. Never walk arbitrary nested fields as commands.
    if "content" in value:
        posts = [value]
    else:
        if not value or not all(isinstance(post, dict) for post in value.values()):
            raise _invalid()
        posts = list(value.values())
    output: list[str] = []
    count = 0
    for post in posts:
        title, lines = post.get("title", ""), post.get("content")
        if not isinstance(title, str) or not isinstance(lines, list):
            raise _invalid()
        if title:
            output.append(title)
        for line in lines:
            if not isinstance(line, list):
                raise _invalid()
            pieces: list[str] = []
            for element in line:
                count += 1
                if count > MAX_POST_ELEMENTS:
                    raise _invalid()
                if not isinstance(element, dict) or not isinstance(element.get("tag"), str):
                    raise _invalid()
                if element["tag"] in {"text", "a"}:
                    text = element.get("text")
                    if not isinstance(text, str):
                        raise _invalid()
                    pieces.append(text)
                elif element["tag"] == "at":
                    # Mention labels/IDs and image/file URLs are not command text.
                    pieces.append(" ")
                else:
                    raise _unsupported()
            output.append("".join(pieces))
    return "\n".join(output)


def extract_text(plaintext: bytes) -> str:
    """Decode already-authorized retained bytes; callers must check Receive and TTL first."""
    if not isinstance(plaintext, bytes):
        raise _invalid()
    if len(plaintext) > MAX_CONTENT_BYTES:
        raise DomainError("FAST_PATH_CONTENT_BOUND", "Source content exceeds its bound", 413)
    try:
        text = plaintext.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid() from None
    if not text.lstrip().startswith(("{", "[", '"')):
        return text  # Explicitly supported synthetic/plain-text source fixtures.
    try:
        value = json.loads(text, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, TypeError, RecursionError):
        raise _invalid() from None
    if not isinstance(value, dict):
        raise _unsupported()
    schema = value.get("schema_version")
    if schema == "github-content-v1":
        if not isinstance(value.get("resource"), dict) or not isinstance(value.get("text"), str):
            raise _invalid()
        return _text(value["text"])
    if schema == "feishu-content-v1":
        content = value.get("content")
        if value.get("message_type") == "text":
            if not isinstance(content, dict) or not isinstance(content.get("text"), str):
                raise _invalid()
            return _text(content["text"])
        if value.get("message_type") == "post":
            return _text(_post(content))
    raise _unsupported()


def parse_intent(item: ItemMetadata, text: str) -> ParsedIntent:
    """Recognize exact syntax; a control suggestion is never a verified permission."""
    _text(text)
    if item.event.scope.source_kind == "github":
        family, separator, _ = item.event.event_kind.partition(".")
        if separator and family in {"issues", "pull_request"}:
            _, newline, body = text.partition("\n")
            text = body if newline else ""
    if item.event.scope.source_kind == "feishu" and item.event.addressed == "mention":
        mention = FEISHU_MENTION.match(text)
        if mention is not None and text[mention.end():].startswith("/mgar "):
            text = text[mention.end():]
    control = CONTROL.fullmatch(text)
    if control is not None:
        action: Literal["pause", "cancel"] = "pause" if control[1] == "pause" else "cancel"
        return ParsedIntent(None, action, action, UUID(control[2]))
    if text.startswith(("/mgar pause", "/mgar cancel")):
        return ParsedIntent()  # Malformed control cannot fall back to an explicit request.
    if item.event.sender.actor_kind != "human":
        return ParsedIntent()
    request = REQUEST.match(text)
    if request is not None:
        command: Command
        if request[1] == "investigate":
            command = "investigate"
        elif request[1] == "repair":
            command = "repair"
        else:
            command = "ask"
        return ParsedIntent("explicit_request", command)
    if item.event.addressed in {"mention", "direct"}:
        return ParsedIntent("explicit_request")
    return ParsedIntent()


def group_key(item: ItemMetadata) -> str:
    """Keep unknown topics entity-scoped; a whole chat never becomes a fallback task."""
    thread = item.event.source_thread
    return digest([
        FAST_PATH_VERSION,
        item.event.scope.key,
        thread.conversation_key,
        ["topic", thread.topic_key]
        if thread.topic_key is not None else ["entity", item.entity_key],
    ])
