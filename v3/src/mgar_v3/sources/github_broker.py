"""Trusted REST/credential broker. Tokens never enter DTOs, SQL, workers or URLs."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from psycopg.pq import TransactionStatus

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.domain import SourceInstance
from mgar_v3.sources.github import GitHubAdapter, _id
from mgar_v3.sources.github_auth import GitHubActivation, GitHubSecrets, app_jwt
from mgar_v3.sources.github_domain import GitHubSettings, SnapshotGrant, utc
from mgar_v3.sources.github_store import GitHubSources
from mgar_v3.sources.ingress_domain import (
    DeliveryMetadata,
    HistoryPage,
    HistoryRequest,
    VerifiedDelivery,
)
from mgar_v3.super_agent.policy_domain import safe_path

API = "https://api.github.com"
API_VERSION = "2026-03-10"
MAX_RESPONSE = 2097152


class GitHubReadBroker:
    def __init__(
        self,
        sources: GitHubSources,
        source_id: UUID,
        revision: int,
        secrets: GitHubSecrets,
        activation: GitHubActivation | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.sources, self.source_id, self.revision = sources, source_id, revision
        self._secrets, self.activation = secrets, activation
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise ValueError("Only explicit MockTransport is accepted for controlled engineering")
        if (
            activation
            and activation.mode == "controlled"
            and not isinstance(transport, httpx.MockTransport)
        ):
            raise ValueError(
                "Controlled activation requires MockTransport; real network is disabled"
            )
        self._transport = transport
        self._blocked_until: datetime | None = None

    def current(self, operation: str) -> GitHubSettings:
        row, settings = self.sources.current(self.source_id)
        if self.activation is None:
            raise DomainError("GITHUB_ACTIVATION_REQUIRED", "GitHub runtime is not activated", 403)
        self.activation.check(
            self.source_id, row["revision"], settings.repository.repository_id, operation
        )
        if not settings.enabled or row["revision"] != self.revision:
            raise DomainError("GITHUB_CONFIG_FENCED", "GitHub settings changed", 403)
        if operation in {"webhook", "history"}:
            from mgar_v3.super_agent.policy_domain import PolicyDefinition

            with self.sources.engine.transaction() as conn:
                _, ingress = self.sources.engine.source_ingress._control(conn, self.source_id)
                if ingress.policy_id is None:
                    raise DomainError("GITHUB_POLICY_SCOPE", "Exact source policy is required", 403)
                policy = self.sources.engine.source_policies._current(conn, ingress.policy_id)
                definition = PolicyDefinition.model_validate(policy["definition"])
                for actor in definition.selector.actors:
                    allowed = (
                        settings.sender_ids
                        if actor.actor_kind == "human"
                        else settings.context_bot_ids
                        if actor.actor_kind == "bot"
                        else ()
                    )
                    if actor.id_type != "github_id" or actor.actor_id not in {
                        str(i) for i in allowed
                    }:
                        raise DomainError(
                            "GITHUB_POLICY_SCOPE", "Policy actor exceeds App receive ceiling", 403
                        )
        if self._blocked_until and datetime.now(UTC) < self._blocked_until:
            raise DomainError(
                "GITHUB_RATE_LIMITED", "Wait for the recorded GitHub rate window", 429
            )
        return settings

    def _request(
        self, method: str, path: str, token: str, body: dict[str, Any] | None = None
    ) -> Any:
        if not path.startswith("/") or "://" in path or ".." in path or "\\" in path:
            raise DomainError(
                "GITHUB_ENDPOINT_DENIED", "Only fixed GitHub REST endpoints allowed", 403
            )
        if self.sources.engine.connection.info.transaction_status != TransactionStatus.IDLE:
            raise DomainError(
                "GITHUB_NETWORK_IN_TRANSACTION", "Remote reads must be outside SQL", 409
            )
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "MAGR-source-github-v1",
        }
        try:
            with httpx.Client(
                transport=self._transport, timeout=10, follow_redirects=False, trust_env=False
            ) as client:
                with client.stream(method, API + path, headers=headers, json=body) as response:
                    status = response.status_code
                    if status in {403, 429} and (
                        "Retry-After" in response.headers
                        or response.headers.get("X-RateLimit-Remaining") == "0"
                        or status == 429
                    ):
                        now = datetime.now(UTC)
                        try:
                            delay = int(response.headers.get("Retry-After", "60"))
                            reset = int(response.headers.get("X-RateLimit-Reset", "0"))
                            self._blocked_until = max(
                                now + timedelta(seconds=max(1, min(delay, 86400))),
                                datetime.fromtimestamp(reset, UTC),
                            )
                        except (ValueError, OverflowError, OSError):
                            self._blocked_until = now + timedelta(seconds=60)
                        self._record("rate_limit", "GITHUB_RATE_LIMITED")
                        raise DomainError(
                            "GITHUB_RATE_LIMITED", "GitHub limited request; no automatic retry", 429
                        )
                    if status not in {200, 201}:
                        code = (
                            "GITHUB_PERMISSION_DENIED"
                            if status in {401, 403, 404}
                            else "GITHUB_REMOTE_FAILED"
                        )
                        self._record("read_denied", code)
                        raise DomainError(
                            code, "GitHub request unavailable; no automatic retry", 503
                        )
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        content.extend(chunk)
                        if len(content) > MAX_RESPONSE:
                            raise DomainError(
                                "GITHUB_RESPONSE_BOUND", "GitHub response exceeded bound", 413
                            )
                    return json.loads(content)
        except (httpx.HTTPError, OSError, ValueError, RecursionError):
            self._record("request_unknown", "GITHUB_REQUEST_UNKNOWN")
            raise DomainError(
                "GITHUB_REQUEST_UNKNOWN",
                "Request has no usable response; do not blindly retry",
                503,
            ) from None

    def _record(self, event: str, code: str) -> None:
        with self.sources.engine.transaction() as conn:
            self.sources._audit(conn, self.source_id, event, code)

    def _token(self, operation: str, permissions: dict[str, str]) -> str:
        if operation not in {"snapshot", "history", "health"} or (
            permissions.get("metadata") != "read"
            or not permissions
            or set(permissions)
            - {"metadata", "contents", "issues", "pull_requests", "actions", "checks"}
            or any(level != "read" for level in permissions.values())
        ):
            raise DomainError(
                "GITHUB_TOKEN_PERMISSION", "Only bounded read permissions allowed", 403
            )
        settings = self.current(operation)
        jwt = app_jwt(settings.app_id, self._secrets.private_key)
        value = self._request(
            "POST",
            f"/app/installations/{settings.installation_id}/access_tokens",
            jwt,
            {"repository_ids": [settings.repository.repository_id], "permissions": permissions},
        )
        self.current(operation)  # Revocation during minting must not release a token to the caller.
        try:
            expiry = utc(value["expires_at"])
            repos = value["repositories"]
            granted = value["permissions"]
            token = value["token"]
            if (
                not isinstance(token, str)
                or not 1 <= len(token) <= 16384
                or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or not datetime.now(UTC) + timedelta(seconds=5)
                < expiry
                <= datetime.now(UTC) + timedelta(hours=1, seconds=30)
                or len(repos) != 1
                or _id(repos[0]["id"]) != settings.repository.repository_id
                or repos[0]["full_name"].lower() != settings.repository.full_name.lower()
                or granted != permissions
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DomainError(
                "GITHUB_TOKEN_SCOPE", "Installation token scope/expiry was not exact", 403
            ) from None
        return token  # Private broker call only; no public issuance endpoint, no token cache.

    def _read(
        self,
        operation: str,
        suffix: str,
        permissions: dict[str, str],
        authority: Callable[[], object] | None = None,
    ) -> Any:
        if operation in {"snapshot", "history"} and authority is None:
            raise DomainError(
                "GITHUB_READ_AUTHORITY_REQUIRED", "Current explicit read authority required", 403
            )
        if authority:
            authority()
        settings = self.current(operation)
        token = self._token(operation, permissions)
        if authority:
            authority()  # Minting is remote too; withdrawal must block the data request.
        value = self._request("GET", "/repos/" + settings.repository.full_name + suffix, token)
        self.current(operation)
        if authority:
            authority()  # In-flight data remains private on revocation.
        return value

    def probe_permissions(self) -> dict[str, Any]:
        settings = self.current("health")
        permissions = {"metadata": "read"}
        needed = {
            "issues": "issues",
            "issue_comment": "issues",
            "pull_request": "pull_requests",
            "pull_request_review": "pull_requests",
            "pull_request_review_comment": "pull_requests",
            "workflow_run": "actions",
            "check_run": "checks",
            "check_suite": "checks",
        }
        for event in settings.events:
            permissions[needed[event]] = "read"
        token = self._token("health", permissions)
        value = self._request("GET", "/installation/repositories?per_page=2", token)
        self.current("health")
        try:
            repositories = value["repositories"]
            repository = repositories[0]
            if (
                len(repositories) != 1
                or _id(repository["id"]) != settings.repository.repository_id
                or _id(repository["owner"]["id"]) != settings.account_id
                or repository["full_name"].lower() != settings.repository.full_name.lower()
                or repository["private"] != settings.repository.private
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DomainError(
                "GITHUB_PERMISSION_PROOF", "Repository permission proof mismatch", 403
            ) from None
        self._record("health", "GITHUB_READ_PERMISSION_VERIFIED")
        return {
            "observed_at": datetime.now(UTC),
            "connection": "online",
            "permissions": "granted",
            "permission_subset": permissions,
            "mode": self.activation.mode if self.activation else "disabled",
            "config_revision": self.revision,
            "live_source_channel_gate": "NOT_RUN",
        }

    def _pull_head(self, value: SnapshotGrant, authority: Callable[[], object]) -> None:
        if value.pull_request is None:
            return
        settings = self.current("snapshot")
        pull = self._read(
            "snapshot",
            "/pulls/" + str(value.pull_request),
            {"metadata": "read", "pull_requests": "read"},
            authority,
        )
        try:
            if (
                _id(pull["base"]["repo"]["id"]) != value.repository_id
                or _id(pull["head"]["repo"]["id"]) != settings.repository.repository_id
                or pull["head"]["sha"] != value.head_sha
                or _id(pull["number"]) != value.pull_request
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DomainError(
                "GITHUB_PR_HEAD_CHANGED", "Fork/current PR head differs from approved snapshot", 409
            ) from None

    def snapshot(self, grant_id: UUID, revision: int, paths: tuple[str, ...]) -> dict[str, bytes]:
        value = self.sources.authorize_snapshot(self.source_id, grant_id, revision)
        if not paths or len(paths) > 100 or len(set(paths)) != len(paths):
            raise DomainError(
                "GITHUB_SNAPSHOT_BOUND", "Provide 1..100 distinct authorized files", 422
            )
        try:
            for path in paths:
                safe_path(path)
                if not any(path == p or path.startswith(p + "/") for p in value.paths):
                    raise ValueError
        except ValueError:
            raise DomainError(
                "GITHUB_SNAPSHOT_PATH", "Snapshot file outside explicit path grant", 403
            ) from None

        def authority() -> SnapshotGrant:
            return self.sources.authorize_snapshot(self.source_id, grant_id, revision)

        self._pull_head(value, authority)
        if any(len(path.split("/")) > 16 for path in paths):
            raise DomainError("GITHUB_SNAPSHOT_TREE_BOUND", "Snapshot depth exceeds 16", 413)
        permissions = {"metadata": "read", "contents": "read"}
        commit = self._read("snapshot", "/git/commits/" + value.head_sha, permissions, authority)
        try:
            root = commit["tree"]["sha"]
            if commit["sha"] != value.head_sha or not re.fullmatch(r"[a-f0-9]{40}", root):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DomainError("GITHUB_SNAPSHOT_TREE", "Commit/tree proof invalid", 403) from None
        trees: dict[str, dict[str, Any]] = {}
        result: dict[str, bytes] = {}
        total = 0
        for path in paths:
            tree_sha = root
            pieces = path.split("/")
            entry: dict[str, Any] = {}
            for index, part in enumerate(pieces):
                if tree_sha not in trees:
                    if len(trees) >= 128:
                        raise DomainError(
                            "GITHUB_SNAPSHOT_TREE_BOUND", "At most 128 tree objects", 413
                        )
                    tree = self._read("snapshot", "/git/trees/" + tree_sha, permissions, authority)
                    try:
                        if tree["sha"] != tree_sha or tree["truncated"] is not False:
                            raise ValueError
                        members = tree["tree"]
                        if not isinstance(members, list) or len(members) > 10000:
                            raise ValueError
                        by_name = {member["path"]: member for member in members}
                        if len(by_name) != len(members):
                            raise ValueError
                        trees[tree_sha] = by_name
                    except (ValueError, KeyError, TypeError, AttributeError):
                        raise DomainError(
                            "GITHUB_SNAPSHOT_TREE", "Tree proof incomplete or invalid", 403
                        ) from None
                entry = trees[tree_sha].get(part, {})
                if index < len(pieces) - 1:
                    if entry.get("mode") != "040000" or entry.get("type") != "tree":
                        raise DomainError(
                            "GITHUB_SNAPSHOT_LINK", "Parent must be an ordinary Git tree", 403
                        )
                    tree_sha = entry.get("sha", "")
                    if not isinstance(tree_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", tree_sha):
                        raise DomainError("GITHUB_SNAPSHOT_TREE", "Invalid parent tree SHA", 403)
                elif entry.get("mode") not in {"100644", "100755"} or entry.get("type") != "blob":
                    raise DomainError(
                        "GITHUB_SNAPSHOT_LINK", "Symlinks/submodules/nonfiles denied", 403
                    )
            blob_sha = entry.get("sha", "")
            if not isinstance(blob_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", blob_sha):
                raise DomainError("GITHUB_SNAPSHOT_TREE", "Invalid file blob SHA", 403)
            raw = self._read("snapshot", "/git/blobs/" + blob_sha, permissions, authority)
            try:
                if raw["encoding"] != "base64":
                    raise ValueError
                content = base64.b64decode(raw["content"].replace("\n", ""), validate=True)
                total += len(content)
                blob = hashlib.sha1(
                    b"blob " + str(len(content)).encode() + b"\0" + content
                ).hexdigest()
                if (
                    raw["sha"] != blob
                    or blob != blob_sha
                    or type(raw["size"]) is not int
                    or raw["size"] != len(content)
                    or total > value.max_bytes
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError):
                raise DomainError(
                    "GITHUB_SNAPSHOT_INVALID", "Blob/size proof invalid; no snapshot released", 403
                ) from None
            result[path] = content
        self._pull_head(value, authority)
        self.sources.authorize_snapshot(self.source_id, grant_id, revision)
        self._record("snapshot", "GITHUB_SNAPSHOT_READ")
        return result


class GitHubHistory:
    """Bounded issue/PR conversation comment snapshots, not webhook delivery replay."""

    def __init__(self, broker: GitHubReadBroker):
        self.broker = broker

    def _authorized(self, instance: SourceInstance, request: HistoryRequest) -> None:
        from mgar_v3.sources.domain import ActorRef
        from mgar_v3.super_agent.policy_domain import PolicyDefinition

        sources = self.broker.sources
        with sources.engine.transaction() as conn:
            control, settings = sources.engine.source_ingress._control(conn, self.broker.source_id)
            if settings.owner != "gateway" or settings.policy_id is None:
                raise DomainError(
                    "GITHUB_HISTORY_RECEIVE_DENIED", "History owner/Receive changed", 403
                )
            row = sources.engine.source_policies._current(conn, settings.policy_id)
            definition = PolicyDefinition.model_validate(row["definition"])
            sources.engine.source_policies._validate_source(conn, definition)
            thread, selector = request.source_thread, definition.selector
            now = sources.engine.source_ingress.now(conn)
            if (
                row["revoked"]
                or row["current_revision"] != settings.policy_revision
                or not definition.valid_from <= now < definition.valid_until
                or not definition.receive.enabled
                or not definition.receive.allow_history_context
                or thread.scope != instance.scope
                or thread.scope != selector.scope
                or thread.conversation_id not in selector.conversations
                or selector.native_threads
                and thread.native_thread_id not in selector.native_threads
            ):
                raise DomainError(
                    "GITHUB_HISTORY_RECEIVE_DENIED", "History scope/Receive changed", 403
                )
            for actor in selector.actors:
                identity = conn.execute(
                    "SELECT actor_ref FROM app.source_identities WHERE identity_key=%s "
                    "AND revoked_at IS NULL AND project_id=%s",
                    (actor.key, sources.engine.settings.project_id),
                ).fetchone()
                roles, _ = sources.engine.source_policies._roles(conn, actor, thread, None)
                if (
                    identity
                    and ActorRef.model_validate(identity["actor_ref"]) == actor
                    and "context_provider" in roles
                ):
                    return
            raise DomainError("GITHUB_HISTORY_RECEIVE_DENIED", "No current context identity", 403)

    def fetch(self, instance: SourceInstance, request: HistoryRequest) -> HistoryPage:
        from mgar_v3.sources.ingress_domain import GapDeclaration

        try:
            return self._fetch(instance, request)
        except DomainError as error:
            if error.code in {
                "GITHUB_HISTORY_UNAVAILABLE",
                "GITHUB_PERMISSION_DENIED",
                "GITHUB_HISTORY_RECEIVE_DENIED",
            }:
                self.broker.sources.engine.source_ingress.gap(
                    self.broker.source_id,
                    GapDeclaration(
                        stream_id="github-history",
                        epoch="bounded-api",
                        reason="no_replay"
                        if error.code == "GITHUB_HISTORY_UNAVAILABLE"
                        else "permission",
                        started_at=datetime.now(UTC),
                    ),
                )
            raise

    def _fetch(self, instance: SourceInstance, request: HistoryRequest) -> HistoryPage:
        self._authorized(instance, request)
        settings = self.broker.current("history")
        topic = request.source_thread.native_thread_id or ""
        parts = topic.split(":")
        if (
            request.source_thread.scope != instance.scope
            or instance.scope.source_instance_id != self.broker.source_id
            or request.source_thread.conversation_id
            != "repository:" + str(settings.repository.repository_id)
            or len(parts) != 2
            or parts[0] not in {"issue", "pr"}
            or not parts[1].isdigit()
            or not 0 < int(parts[1]) <= 9223372036854775807
            or request.cursor is not None
            and (
                not request.cursor.isascii()
                or not request.cursor.isdigit()
                or not 1 <= int(request.cursor) <= 100
            )
        ):
            raise DomainError(
                "GITHUB_HISTORY_UNAVAILABLE", "Only bounded issue/PR comment pages supported", 409
            )
        number = int(parts[1])
        page = int(request.cursor or "1")
        permission = "pull_requests" if parts[0] == "pr" else "issues"
        rows = self.broker._read(
            "history",
            f"/issues/{number}/comments?per_page={request.limit}&page={page}",
            {"metadata": "read", permission: "read"},
            lambda: self._authorized(instance, request),
        )
        if not isinstance(rows, list) or len(rows) > request.limit:
            raise DomainError("GITHUB_HISTORY_BOUND", "History page exceeded requested bound", 413)
        adapter = GitHubAdapter(settings, b"")  # Installation API proof, no fake webhook signature.
        deliveries = []
        for row in rows:
            try:
                author = row["user"]  # API proves authorship, not who last edited.
                author_id = _id(author["id"])
                actor_type = author["type"]
                if actor_type not in {"User", "Bot"}:
                    raise ValueError("Unknown resource author type")
                selected = (actor_type == "User" and author_id in settings.sender_ids) or (
                    actor_type == "Bot" and author_id in settings.context_bot_ids
                )
                if "issue_comment" not in settings.events:
                    raise DomainError(
                        "GITHUB_EVENT_DISABLED", "Comment history is not selected", 403
                    )
                issue: dict[str, Any] = {"number": number}
                if parts[0] == "pr":
                    issue["pull_request"] = {}
                raw = {"action": "created", "comment": row, "issue": issue, "sender": author}
                identifier = "history:" + digest(
                    [row["id"], row.get("updated_at"), row.get("body")]
                )
                item, body = adapter._normalize(
                    instance, raw, "issue_comment", identifier, historical=True
                )
                if not selected:
                    # Keep only the trusted snapshot/version observation. The actual
                    # author remains outside Receive; no invented editor, role, body
                    # retention or identity evidence. Engine records minimal denial
                    # and invalidates stale retained content in its original transaction.
                    item = item.model_copy(
                        update={"event": item.event.model_copy(update={"body_ref": None})}
                    )
                    body = None
                deliveries.append(
                    VerifiedDelivery(
                        DeliveryMetadata(
                            source_instance_id=instance.scope.source_instance_id,
                            source_revision=instance.revision,
                            delivery_id=identifier,
                            payload_sha256=digest(raw),
                            verified_at=datetime.now(UTC),
                            verification_method="installation_proof",
                            items=(item,),
                        ),
                        (body,),
                    )
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                raise DomainError(
                    "GITHUB_HISTORY_INVALID", "History item has invalid provenance", 403
                ) from None
        next_page = str(page + 1) if len(rows) == request.limit and page < 100 else None
        return HistoryPage(tuple(deliveries), next_page, complete=len(rows) < request.limit)
