"""Strict bounded platform decoding; errors contain no remote content."""

from typing import Any

from mgar_v3.domain import DomainError
from mgar_v3.sources.feishu import parsed

MAX_RESPONSE = 2097152


def decode_response(content: bytes) -> dict[str, Any]:
    if len(content) > MAX_RESPONSE:
        raise DomainError("FEISHU_RESPONSE_BOUND", "Feishu response exceeds 2 MiB", 413)
    try:
        return parsed(content)
    except (ValueError, UnicodeError, TypeError, RecursionError, OverflowError):
        raise DomainError(
            "FEISHU_RESPONSE_INVALID", "Malformed Feishu response; no retry", 503
        ) from None
