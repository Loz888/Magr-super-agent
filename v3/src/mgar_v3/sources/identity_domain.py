"""Trusted identity port and management commands; source text never supplies authority."""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, SecretStr, model_validator

from mgar_v3.domain import Model
from mgar_v3.sources.domain import (
    ActorRef,
    OpaqueId,
    Revision,
    Sha256,
    SourceInstance,
    SourceThreadRef,
)
from mgar_v3.sources.ports import TransportEnvelope

IDENTITY_CONTRACT_VERSION = "source-identity-v1"
Role = Literal["context_provider", "requester", "task_controller", "project_administrator"]
IdentityAction = Literal[
    "provide_context",
    "request_task",
    "control_task",
    "manage_rules",
    "manage_identities",
    "approve_publication",
    "expand_execution",
]
Roles = Annotated[tuple[Role, ...], Field(min_length=1, max_length=4)]


class VerifiedSubject(Model):
    """Only the registered server verifier can supply this assertion to authenticate.

    It must extract the actual author from a verified platform envelope, never a
    quoted request, display name, user-controlled body, or model inference.
    """

    actor: ActorRef
    source_thread: SourceThreadRef
    remote_event_id: OpaqueId
    payload_sha256: Sha256
    occurred_at: AwareDatetime
    verification_method: Literal["platform_signature", "installation_proof"]
    historical_backfill: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def same_installation(self) -> Self:
        if self.actor.scope != self.source_thread.scope:
            raise ValueError("Verified sender and conversation must share one installation")
        return self


class IdentityVerifier(Protocol):
    @property
    def verifier_id(self) -> str: ...

    def verify_identity(
        self, instance: SourceInstance, envelope: TransportEnvelope
    ) -> VerifiedSubject: ...


class IdentityCredential(Model):
    """Internal gateway bearer; never returned by management APIs or put in a message."""

    evidence_id: UUID
    identity_id: UUID
    token: SecretStr = Field(repr=False)


class BindIdentity(Model):
    evidence_id: UUID
    expected_identity_revision: Revision
    roles: Roles
    principal_id: UUID | None = None
    console_owner: bool = Field(default=False, strict=True)
    review_ref: OpaqueId

    @model_validator(mode="after")
    def explicit_target(self) -> Self:
        if self.console_owner and self.principal_id is not None:
            raise ValueError("Choose the console owner or an existing principal, not both")
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("Duplicate role")
        return self


class RoleChange(Model):
    expected_revision: Revision
    roles: Roles
    review_ref: OpaqueId

    @model_validator(mode="after")
    def distinct_roles(self) -> Self:
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("Duplicate role")
        return self


class RevisionCommand(Model):
    expected_revision: Revision
    review_ref: OpaqueId


class RestoreIdentity(RevisionCommand):
    evidence_id: UUID


class RestoreBinding(RoleChange):
    evidence_id: UUID


class ControllerGrant(RevisionCommand):
    ticket_id: UUID


@dataclass(frozen=True)
class IdentityAuthorization:
    identity_id: UUID
    principal_id: UUID
    engine_actor: str
    binding_id: UUID
    action: IdentityAction
    ticket_id: UUID | None
    evidence_id: UUID
    expires_at: datetime
