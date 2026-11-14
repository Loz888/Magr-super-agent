"""Opaque bounded continuations; never expose private skipped candidate IDs."""

import base64
import hashlib
import os
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_recall_domain import RECALL_VERSION, RecallCursor


class RecallCursors:
    def __init__(self, secret: str) -> None:
        if len(secret) < 16:
            raise ValueError("A server-owned cursor secret is required")
        self._cipher = AESGCM(hashlib.sha256((RECALL_VERSION + "\0" + secret).encode()).digest())

    def encode(self, value: RecallCursor) -> str:
        nonce = os.urandom(12)
        raw = value.model_dump_json().encode()
        if len(raw) > 2048:
            raise ValueError("Cursor exceeds metadata bound")
        encrypted = self._cipher.encrypt(nonce, raw, value.caller_sha256.encode())
        return base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")

    def decode(self, token: str, caller: str, *, now: datetime) -> RecallCursor:
        try:
            if not 1 <= len(token) <= 4096 or now.utcoffset() is None:
                raise ValueError("bound")
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            clear = self._cipher.decrypt(raw[:12], raw[12:], caller.encode())
            value = RecallCursor.model_validate_json(clear)
            if value.caller_sha256 != caller or now >= value.expires_at:
                raise ValueError("scope")
            return value
        except Exception:
            raise DomainError(
                "SOURCE_RECALL_CURSOR_INVALID", "Continuation is unavailable", 403
            ) from None
