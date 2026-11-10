"""Lossless opt-in wrapping of TriggerInput; this is not a GitHub App verifier."""

import hashlib
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from mgar_v3.domain import DomainError, TriggerInput, digest
from mgar_v3.sources.domain import (
    ActorRef,
    EntityRevision,
    LegacyTriggerRef,
    RepositoryHint,
    SourceEvent,
    SourceScope,
    SourceThreadRef,
)


def legacy_scope(trigger: TriggerInput) -> SourceScope:
    return SourceScope(
        source_kind=trigger.source,
        source_instance_id=uuid5(
            NAMESPACE_URL, digest(["legacy-trigger-v1", trigger.project_id, trigger.source])
        ),
        tenant_id="legacy-project:" + digest(trigger.project_id),
        installation_id="legacy-trigger-input-v1",
    )


class LegacyTriggerAdapter:
    @staticmethod
    def wrap(trigger: TriggerInput, *, received_at: datetime) -> SourceEvent:
        scope = legacy_scope(trigger)
        entity = "legacy-issue:" + digest([trigger.repository, trigger.issue_number])
        remote = "legacy-event:" + digest(trigger.external_event_id)
        return SourceEvent(
            scope=scope,
            project_id=trigger.project_id,
            source_revision=1,
            delivery_id=remote,
            remote_event_id=remote,
            entity_id=entity,
            entity_revision=EntityRevision(origin="legacy", value="v1"),
            event_kind="legacy.trigger",
            source_thread=SourceThreadRef(
                scope=scope,
                conversation_id=entity,
                native_thread_id=entity,
            ),
            sender=ActorRef(
                scope=scope,
                id_type="legacy_engine_principal",
                actor_id=trigger.actor,
                actor_kind="human",
            ),
            addressed="unknown",
            occurred_at=received_at,  # Legacy TriggerInput has no remote occurrence timestamp.
            received_at=received_at,
            repository_hint=RepositoryHint(
                full_name=trigger.repository, resource_kind="issue", number=trigger.issue_number
            ),
            body_ref=hashlib.sha256(trigger.body.encode()).hexdigest(),
            verification_ref="legacy-trigger-input-v1",  # Never a platform signature claim.
            provenance="legacy_trigger",
            legacy_context=LegacyTriggerRef.from_trigger(trigger),
        )

    @staticmethod
    def restore(event: SourceEvent, *, body: str) -> TriggerInput:
        hint = event.repository_hint
        context = event.legacy_context
        if (
            event.provenance != "legacy_trigger"
            or event.scope.source_kind not in {"github", "cli", "web"}
            or event.event_kind != "legacy.trigger"
            or hint is None
            or hint.resource_kind != "issue"
            or hint.number is None
            or context is None
            or event.sender.id_type != "legacy_engine_principal"
        ):
            raise DomainError(
                "LEGACY_EVENT_REQUIRED", "Only legacy TriggerInput wrappers can be restored", 422
            )
        if event.body_ref != hashlib.sha256(body.encode()).hexdigest():
            raise DomainError(
                "SOURCE_BODY_MISMATCH", "Body does not match the wrapped legacy input", 409
            )
        trigger = TriggerInput.model_validate(
            {**context.model_dump(), "project_id": event.project_id, "body": body}
        )
        # Compare the entire versioned wrapper, not just a user-provided provenance label.
        if event != LegacyTriggerAdapter.wrap(trigger, received_at=event.received_at):
            raise DomainError(
                "LEGACY_WRAPPER_MISMATCH", "Legacy identity or event fields changed", 409
            )
        return trigger
