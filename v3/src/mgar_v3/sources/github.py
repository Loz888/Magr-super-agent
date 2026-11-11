"""GitHub App verifier/normalizer. Signed resource facts are never execution grants."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.domain import (
    ActorRef,
    EntityRevision,
    RepositoryHint,
    SourceCapabilities,
    SourceEvent,
    SourceHealth,
    SourceInstance,
    SourceThreadRef,
)
from mgar_v3.sources.github_auth import verify_signature
from mgar_v3.sources.github_domain import VERSION, GitHubResource, GitHubSettings, utc
from mgar_v3.sources.identity_domain import VerifiedSubject
from mgar_v3.sources.ingress_domain import DeliveryMetadata, ItemMetadata, VerifiedDelivery
from mgar_v3.sources.ports import (
    ContextPage,
    ContextRequest,
    TransportEnvelope,
    VerifiedEnvelope,
)

Row = dict[str, Any]
CAPABILITIES = SourceCapabilities(
    history_read="supported",
    incremental_cursor="unsupported",
    reconnect_replay="unsupported",
    edit_events="supported",
    delete_events="supported",
    native_entity_revision="unsupported",
)
ACTIONS = {
    "issues": {"opened", "edited", "deleted", "closed", "reopened", "labeled", "unlabeled"},
    "issue_comment": {"created", "edited", "deleted"},
    "pull_request": {
        "opened",
        "edited",
        "closed",
        "reopened",
        "synchronize",
        "ready_for_review",
        "converted_to_draft",
        "labeled",
        "unlabeled",
    },
    "pull_request_review": {"submitted", "edited", "dismissed"},
    "pull_request_review_comment": {"created", "edited", "deleted"},
    "workflow_run": {"requested", "in_progress", "completed"},
    "check_run": {"created", "completed", "rerequested", "requested_action"},
    "check_suite": {"requested", "completed", "rerequested"},
}


def _id(value: object) -> int:
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ValueError("Invalid GitHub immutable ID")
    return value


def _headers(values: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in values.items():
        key = name.lower()
        if key in result:
            raise ValueError("Duplicate authentication header")
        result[key] = value
    return result


def _json(raw: bytes) -> Row:
    def pairs(items: list[tuple[str, Any]]) -> Row:
        result: Row = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("Object required")
    return value


class GitHubAdapter:
    source_kind = "github"
    adapter_version = VERSION
    verifier_id = "github-app-signature-v1"
    capabilities = CAPABILITIES

    def __init__(
        self,
        settings: GitHubSettings,
        webhook_secret: bytes,
        check_current: Callable[[], object] | None = None,
    ):
        self.settings = settings
        self._secret = webhook_secret
        self._check = check_current

    def configuration_schema(self) -> Row:
        return GitHubSettings.model_json_schema()

    def _verified(
        self, instance: SourceInstance, envelope: TransportEnvelope
    ) -> tuple[Row, str, str]:
        if self._check:
            self._check()
        if len(envelope.payload) > 262144:
            raise DomainError("INGRESS_ENVELOPE_BOUND", "GitHub envelope exceeds 256 KiB", 413)
        try:
            headers = _headers(envelope.headers)
            verify_signature(self._secret, envelope.payload, headers.get("x-hub-signature-256", ""))
            raw = _json(envelope.payload)
            family, delivery = headers["x-github-event"], headers["x-github-delivery"]
            if (
                not delivery
                or len(delivery) > 200
                or any(ord(c) < 33 or ord(c) > 126 for c in delivery)
            ):
                raise ValueError
            installation, repository = raw["installation"], raw["repository"]
            if (
                not self.settings.enabled
                or instance.adapter_version != VERSION
                or instance.scope.source_kind != "github"
                or instance.scope.installation_id != str(_id(installation["id"]))
                or instance.scope.tenant_id != str(_id(repository["owner"]["id"]))
                or _id(installation["id"]) != self.settings.installation_id
                or _id(repository["owner"]["id"]) != self.settings.account_id
                or _id(repository["id"]) != self.settings.repository.repository_id
                or repository["full_name"].lower() != self.settings.repository.full_name.lower()
                or type(repository["private"]) is not bool
                or repository["private"] != self.settings.repository.private
            ):
                raise DomainError(
                    "GITHUB_INSTALLATION_DENIED", "Unapproved installation/repository", 403
                )
            if family not in self.settings.events:
                raise DomainError("GITHUB_EVENT_DISABLED", "Event family is not selected", 403)
            if raw.get("action") not in ACTIONS[family]:
                raise DomainError(
                    "GITHUB_ACTION_UNSUPPORTED", "Action has no normalized contract", 422
                )
            sender = raw["sender"]
            sender_id = _id(sender["id"])
            selected = (sender.get("type") == "User" and sender_id in self.settings.sender_ids) or (
                sender.get("type") == "Bot" and sender_id in self.settings.context_bot_ids
            )
            mutation = raw["action"] in {"edited", "deleted", "dismissed", "synchronize"}
            if sender.get("type") not in {"User", "Bot"}:
                raise DomainError("GITHUB_SENDER_DENIED", "Unknown sender type", 403)
            if not selected and not mutation:
                if sender_id in self.settings.own_bot_ids:
                    raise DomainError(
                        "GITHUB_SELF_RESULT", "Own App output is not a new input", 403
                    )
                raise DomainError(
                    "GITHUB_SENDER_DENIED", "Sender/type is outside approved scope", 403
                )
            # Trusted mutations outside Receive must still invalidate an existing body.
            # Runtime validates policy actors are a subset of the App ceiling; SA04 then
            # rejects their new body while retaining only the minimal version/tombstone.
            return raw, family, delivery
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            raise DomainError(
                "GITHUB_INVALID_WEBHOOK", "Malformed GitHub App delivery", 422
            ) from None

    def verify(self, instance: SourceInstance, envelope: TransportEnvelope) -> VerifiedEnvelope:
        _, family, delivery = self._verified(instance, envelope)
        # In-memory capsule retains original authenticated transport fields; it is not a DTO.
        capsule = json.dumps(
            {"family": family, "delivery": delivery, "payload": envelope.payload.decode()}
        ).encode()
        return VerifiedEnvelope(instance.scope, "github-app-signature", capsule)

    def normalize(
        self, instance: SourceInstance, envelope: VerifiedEnvelope
    ) -> tuple[SourceEvent, ...]:
        if envelope.scope != instance.scope or envelope.verification_ref != "github-app-signature":
            raise DomainError("GITHUB_SCOPE", "Verified capsule scope mismatch", 403)
        capsule = _json(envelope.payload)
        item, _ = self._normalize(
            instance, _json(capsule["payload"].encode()), capsule["family"], capsule["delivery"]
        )
        return (item.event,)

    def _normalize(
        self,
        instance: SourceInstance,
        raw: Row,
        family: str,
        delivery: str,
        *,
        historical: bool = False,
    ) -> tuple[ItemMetadata, bytes | None]:
        try:
            action = raw["action"]
            relation: Row = {
                "repository": self.settings.repository.model_dump(),
                "kind": family,
                "action": action,
            }
            obj: Row
            number = None
            pr: Row | None = None
            if family == "issues":
                obj = raw["issue"]
                number = _id(obj["number"])
                if "pull_request" in obj:
                    raise ValueError("PR must use its explicit family")
                entity = "issue:" + str(_id(obj["id"]))
            elif family == "issue_comment":
                obj = raw["comment"]
                number = _id(raw["issue"]["number"])
                entity = "comment:" + str(_id(obj["id"]))
            elif family == "pull_request":
                pr = obj = raw["pull_request"]
                number = _id(obj["number"])
                entity = "pull:" + str(_id(obj["id"]))
            elif family == "pull_request_review":
                pr = raw["pull_request"]
                obj = raw["review"]
                number = _id(pr["number"])
                entity = "review:" + str(_id(obj["id"]))
            elif family == "pull_request_review_comment":
                pr = raw["pull_request"]
                obj = raw["comment"]
                number = _id(pr["number"])
                entity = "review-comment:" + str(_id(obj["id"]))
            else:
                obj = raw[family]
                pulls = obj.get("pull_requests", [])
                relation["pull_requests"] = tuple(_id(p["number"]) for p in pulls)
                # No first-PR guessing and no synthetic Issue for a repository workflow.
                if len(pulls) == 1:
                    number = _id(pulls[0]["number"])
                relation["head_sha"] = obj["head_sha"]
                if family == "workflow_run":
                    relation["run_id"] = _id(obj["id"])
                    relation["run_attempt"] = _id(obj["run_attempt"])
                    head_repo = obj.get("head_repository")
                    if head_repo:
                        relation["head_repository_id"] = _id(head_repo["id"])
                else:
                    relation[family + "_id"] = _id(obj["id"])
                    if family == "check_run" and obj.get("check_suite"):
                        relation["check_suite_id"] = _id(obj["check_suite"]["id"])
                    if obj.get("app"):
                        relation["producer_app_id"] = _id(obj["app"]["id"])
                        if relation["producer_app_id"] == self.settings.app_id:
                            raise DomainError(
                                "GITHUB_SELF_RESULT", "Own App check is not new work", 403
                            )
                entity = (
                    family
                    + ":"
                    + str(_id(obj["id"]))
                    + ":"
                    + digest(
                        [
                            relation.get("run_attempt"),
                            obj["head_sha"],
                            action,
                            obj.get("status"),
                            obj.get("conclusion"),
                        ]
                    )[:32]
                )
            is_pr_comment = family == "issue_comment" and "pull_request" in raw["issue"]
            is_pr = (
                pr is not None
                or is_pr_comment
                or (family in {"workflow_run", "check_run", "check_suite"} and number is not None)
            )
            if pr is not None:
                if _id(pr["base"]["repo"]["id"]) != self.settings.repository.repository_id:
                    raise DomainError(
                        "GITHUB_PR_BASE_SCOPE", "PR base belongs to another repository", 403
                    )
                relation.update(
                    head_sha=pr["head"]["sha"],
                    base_sha=pr["base"]["sha"],
                    head_repository_id=_id(pr["head"]["repo"]["id"]),
                )
            relation["number"] = number
            if pr is not None or is_pr_comment:
                relation["pull_requests"] = (number,)
            author = obj.get("user")
            if author:
                relation["author_id"] = _id(author["id"])
            resource = GitHubResource.model_validate(relation)
            if family in {"workflow_run", "check_run", "check_suite"}:
                text = json.dumps(
                    {
                        k: obj.get(k)
                        for k in ["name", "status", "conclusion", "event", "run_number"]
                    },
                    sort_keys=True,
                )
            elif family in {"issues", "pull_request"}:
                text = str(obj.get("title", "")) + "\n" + str(obj.get("body") or "")
            else:
                text = str(obj.get("body") or "")
            encoded_body = json.dumps(
                {
                    "schema_version": "github-content-v1",
                    "resource": resource.model_dump(mode="json"),
                    "text": text,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if len(encoded_body) > 65536:
                raise DomainError(
                    "INGRESS_BODY_BOUND", "GitHub normalized content exceeds 64 KiB", 413
                )
            operation = (
                "delete"
                if action == "deleted"
                else ("edit" if action in {"edited", "dismissed", "synchronize"} else "create")
            )
            # updated_at is not a native monotonic revision: two edits may share one timestamp.
            # Include the authenticated content/state hash; genuinely different unknown-order
            # versions stay ambiguous in SA04 instead of using arrival time.
            version = digest(
                [
                    resource.model_dump(mode="json"),
                    operation,
                    hashlib.sha256(encoded_body).hexdigest(),
                    obj.get("updated_at"),
                    obj.get("state"),
                ]
            )
            occurred = next(
                (
                    obj[k]
                    for k in [
                        "updated_at",
                        "completed_at",
                        "submitted_at",
                        "started_at",
                        "created_at",
                    ]
                    if obj.get(k)
                ),
                None,
            )
            at = utc(occurred)
            body: bytes | None = None if operation == "delete" else encoded_body
            sender = raw["sender"]
            actor = ActorRef(
                scope=instance.scope,
                id_type="github_id",
                actor_id=str(_id(sender["id"])),
                actor_kind="bot" if sender["type"] == "Bot" else "human",
            )
            event_kind = family + "." + action
            topic = ("pr:" if is_pr else "issue:") + str(number) if number is not None else None
            hint_kind = (
                "workflow"
                if family == "workflow_run"
                else "check"
                if family in {"check_run", "check_suite"}
                else "pull_request"
                if is_pr
                else "issue"
            )
            event = SourceEvent(
                scope=instance.scope,
                project_id=instance.project_id,
                source_revision=instance.revision,
                delivery_id=delivery,
                remote_event_id=family + ":" + digest([entity, version]),
                entity_id=entity,
                entity_revision=EntityRevision(origin="derived", value=version),
                event_kind=event_kind,
                source_thread=SourceThreadRef(
                    scope=instance.scope,
                    conversation_id="repository:" + str(resource.repository.repository_id),
                    native_thread_id=topic,
                ),
                reply_to_entity_id=str(obj["in_reply_to_id"])
                if obj.get("in_reply_to_id")
                else None,
                sender=actor,
                addressed="ambient",
                occurred_at=at,
                received_at=datetime.now(UTC),
                repository_hint=RepositoryHint(
                    full_name=resource.repository.full_name,
                    resource_kind=hint_kind,
                    number=number,
                    head_sha=resource.head_sha,
                ),
                body_ref=hashlib.sha256(body).hexdigest() if body else None,
                verification_ref="github-installation-api"
                if historical
                else "github-app-signature",
                historical_backfill=historical,
                provenance="verified_source",
            )
            return ItemMetadata(
                event=event,
                entity_namespace="github:" + str(resource.repository.repository_id),
                operation=operation,
                native_order=None,
                platform_moderation=bool(
                    resource.author_id
                    and resource.author_id != _id(sender["id"])
                    and operation in {"edit", "delete"}
                ),
            ), body
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            raise DomainError(
                "GITHUB_NORMALIZATION_INVALID", "GitHub resource relationships are invalid", 422
            ) from None

    def verify_ingress(
        self, instance: SourceInstance, envelope: TransportEnvelope
    ) -> VerifiedDelivery:
        raw, family, delivery = self._verified(instance, envelope)
        item, body = self._normalize(instance, raw, family, delivery)
        return VerifiedDelivery(
            DeliveryMetadata(
                source_instance_id=instance.scope.source_instance_id,
                source_revision=instance.revision,
                delivery_id=delivery,
                payload_sha256=hashlib.sha256(envelope.payload).hexdigest(),
                verified_at=datetime.now(UTC),
                verification_method="platform_signature",
                items=(item,),
            ),
            (body,),
        )

    def verify_identity(
        self, instance: SourceInstance, envelope: TransportEnvelope
    ) -> VerifiedSubject:
        delivery = self.verify_ingress(instance, envelope)
        event = delivery.metadata.items[0].event
        return VerifiedSubject(
            actor=event.sender,
            source_thread=event.source_thread,
            remote_event_id=event.delivery_id,
            payload_sha256=delivery.metadata.payload_sha256,
            occurred_at=event.occurred_at,
            verification_method="platform_signature",
        )

    def fetch_context(self, instance: SourceInstance, request: ContextRequest) -> ContextPage:
        raise DomainError(
            "GITHUB_HISTORY_GATEWAY_REQUIRED",
            "Use current Receive-authorized Gateway.history with GitHubHistory",
            409,
        )

    def health(self, instance: SourceInstance) -> SourceHealth:
        return SourceHealth(
            scope=instance.scope,
            observed_at=datetime.now(UTC),
            connection="disabled",
            permissions="unknown",
        )
