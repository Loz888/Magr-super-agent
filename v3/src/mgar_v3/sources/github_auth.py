"""Webhook HMAC and short-lived App JWT. Keys stay in trusted server memory."""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from mgar_v3.domain import DomainError


def verify_signature(secret: bytes, payload: bytes, signature: str) -> None:
    if not secret:
        raise DomainError("WEBHOOK_NOT_CONFIGURED", "Webhook verification is not configured", 503)
    expected = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
    try:
        valid = hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii"))
    except (UnicodeError, AttributeError):
        valid = False
    if not valid:
        raise DomainError("SIGNATURE_INVALID", "Webhook signature is invalid", 401)


@dataclass(frozen=True)
class GitHubSecrets:
    webhook_secret: bytes = field(repr=False)
    private_key: bytes = field(repr=False)


@dataclass(frozen=True)
class GitHubActivation:
    """Trusted startup input, never accepted from the settings API or environment flags."""

    source_id: UUID
    config_revision: int
    repository_id: int
    expires_at: datetime
    operations: frozenset[str]
    review_ref: str
    mode: Literal["controlled", "live"] = "controlled"

    def check(self, source_id: UUID, revision: int, repository_id: int, operation: str) -> None:
        if (
            self.source_id != source_id
            or self.config_revision != revision
            or self.repository_id != repository_id
            or operation not in self.operations
            or self.expires_at.tzinfo is None
            or self.expires_at <= datetime.now(UTC)
            or not self.review_ref
            or self.mode not in {"controlled", "live"}
        ):
            raise DomainError(
                "GITHUB_ACTIVATION_REQUIRED", "Exact current startup approval required", 403
            )


def _b64(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def app_jwt(app_id: int, pem: bytes, now: datetime | None = None) -> str:
    at = int((now or datetime.now(UTC)).timestamp())
    try:
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError
        header = _b64(b'{"alg":"RS256","typ":"JWT"}')
        claims = _b64(
            json.dumps(
                {"iat": at - 60, "exp": at + 540, "iss": str(app_id)}, separators=(",", ":")
            ).encode()
        )
        content = header + b"." + claims
        signature = key.sign(content, padding.PKCS1v15(), hashes.SHA256())
        return (content + b"." + _b64(signature)).decode("ascii")
    except (ValueError, TypeError):
        raise DomainError(
            "GITHUB_KEY_INVALID", "App signing key is unavailable or invalid", 503
        ) from None
