"""Owner console inputs; configuration and secrets never activate a runtime."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, SecretStr, field_validator, model_validator

from mgar_v3.domain import Model
from mgar_v3.sources.domain import OpaqueId, Revision, Sha256
from mgar_v3.sources.feishu_domain import FeishuConfigure
from mgar_v3.sources.github_domain import GitHubConfigure

Counter = Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]
VERSION: Literal["source-console-v1"] = "source-console-v1"


def bounded_secret(value: SecretStr, limit: int, *, multiline: bool = False) -> SecretStr:
    raw = value.get_secret_value()
    if not raw.strip() or len(raw.encode("utf-8")) > limit or "\0" in raw:
        raise ValueError("A bounded nonempty credential is required")
    if not multiline and any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError("Credential contains control characters")
    return value


class GitHubConsoleCredentials(Model):
    app_private_key: SecretStr
    webhook_secret: SecretStr

    @field_validator("app_private_key")
    @classmethod
    def private_key(cls, value: SecretStr) -> SecretStr:
        return bounded_secret(value, 16384, multiline=True)

    @field_validator("webhook_secret")
    @classmethod
    def webhook(cls, value: SecretStr) -> SecretStr:
        return bounded_secret(value, 4096)


class FeishuConsoleCredentials(Model):
    app_secret: SecretStr
    verification_token: SecretStr
    encrypt_key: SecretStr

    @field_validator("app_secret", "verification_token", "encrypt_key")
    @classmethod
    def credential(cls, value: SecretStr) -> SecretStr:
        return bounded_secret(value, 4096)


Credentials = GitHubConsoleCredentials | FeishuConsoleCredentials


class ConsoleSourceSave(Model):
    source_kind: Literal["github", "feishu"]
    expected_source_revision: Counter
    expected_credential_revision: Counter = 0
    display_name: Annotated[str, Field(strict=True, max_length=200)] = ""
    configuration: GitHubConfigure | FeishuConfigure
    credentials: Credentials | None = None
    credential_valid_until: AwareDatetime | None = None

    @model_validator(mode="after")
    def exact_platform(self) -> Self:
        config_type, credential_type = (
            (GitHubConfigure, GitHubConsoleCredentials)
            if self.source_kind == "github"
            else (FeishuConfigure, FeishuConsoleCredentials)
        )
        if not isinstance(self.configuration, config_type):
            raise ValueError("Configuration belongs to another platform")
        if self.credentials is not None and not isinstance(self.credentials, credential_type):
            raise ValueError("Credentials belong to another platform")
        if (self.credentials is None) != (self.credential_valid_until is None):
            raise ValueError("Credential replacement requires its explicit expiry")
        return self


class ConsoleCredentialSave(Model):
    expected_source_revision: Revision
    expected_credential_revision: Counter
    credentials: Credentials
    valid_until: AwareDatetime
    review_ref: OpaqueId


class ConsoleSourceRevoke(Model):
    expected_source_revision: Revision
    review_ref: OpaqueId


class ConsoleDisableSources(Model):
    expected_snapshot_sha256: Sha256
    review_ref: OpaqueId


class ConsoleTaskStop(Model):
    expected_epoch: Counter
    review_ref: OpaqueId


def credential_values(value: Credentials) -> dict[str, str]:
    """Server-only encryption input; never return or hash the plaintext."""
    return {name: getattr(value, name).get_secret_value() for name in type(value).model_fields}


def credential_binding(project: str, source: str, scope: str, kind: str, revision: int) -> str:
    return f"{VERSION}:{project}:{source}:{scope}:{kind}:{revision}"


def expiry_valid(value: datetime, now: datetime) -> bool:
    from datetime import timedelta

    return now < value <= now + timedelta(days=365)
