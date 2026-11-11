"""Installation/settings/grants on the existing Engine transaction only."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.github import CAPABILITIES
from mgar_v3.sources.github_domain import (
    VERSION,
    GitHubConfigure,
    GitHubRevoke,
    GitHubSettings,
    SnapshotGrant,
    SnapshotSave,
)
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.ingress_domain import IngressSettings
from mgar_v3.super_agent.policy_domain import PolicyDefinition

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine

Row = dict[str, Any]


class GitHubSources:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.identities = SourceIdentities(engine)

    def _audit(
        self,
        conn: Connection,
        source_id: UUID,
        event: str,
        code: str,
        user: UUID | None = None,
        **details: Any,
    ) -> None:
        conn.execute(
            "INSERT INTO app.github_source_audit(source_instance_id,event,reason_code,"
            "actor_user_id,details) VALUES (%s,%s,%s,%s,%s)",
            (source_id, event, code, user, Jsonb(details)),
        )

    def _current(self, conn: Connection, source_id: UUID) -> tuple[Row, GitHubSettings]:
        row = conn.execute(
            "SELECT * FROM app.github_source_settings WHERE source_instance_id=%s AND "
            "project_id=%s",
            (source_id, self.engine.settings.project_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                "GITHUB_NOT_CONFIGURED", "GitHub App installation is not configured", 409
            )
        instance = self.identities.registry._instance(conn, source_id)
        if instance.revision != row["source_revision"]:
            raise DomainError("GITHUB_SOURCE_STALE", "Source configuration changed", 409)
        return row, GitHubSettings.model_validate(row["definition"])

    def current(self, source_id: UUID) -> tuple[Row, GitHubSettings]:
        with self.engine.transaction() as conn:
            return self._current(conn, source_id)

    def configure(self, cookie: str, source_id: UUID, request: GitHubConfigure) -> Row:
        with self.engine.transaction() as conn:
            user = self.identities._console(conn, cookie)
            instance = self.identities.registry._instance(conn, source_id)
            value = request.settings
            if (
                instance.scope.source_kind != "github"
                or instance.capabilities != CAPABILITIES
                or instance.adapter_version != VERSION
                or instance.scope.tenant_id != str(value.account_id)
                or instance.scope.installation_id != str(value.installation_id)
                or instance.credential_ref is None
                or value.repository.full_name.lower() != self.engine.settings.repository.lower()
            ):
                raise DomainError(
                    "GITHUB_INSTALLATION_SCOPE", "Exact installed source/repository required", 403
                )
            old = conn.execute(
                "SELECT * FROM app.github_source_settings WHERE source_instance_id=%s FOR UPDATE",
                (source_id,),
            ).fetchone()
            if (old["revision"] if old else 0) != request.expected_revision:
                raise DomainError("GITHUB_REVISION_CONFLICT", "Reload settings before saving", 409)
            if old:
                prior = GitHubSettings.model_validate(old["definition"])
                if (
                    prior.app_id,
                    prior.account_id,
                    prior.installation_id,
                    prior.repository.repository_id,
                ) != (
                    value.app_id,
                    value.account_id,
                    value.installation_id,
                    value.repository.repository_id,
                ):
                    raise DomainError(
                        "GITHUB_INSTALLATION_IMMUTABLE", "Register a new source identity", 409
                    )
            revision = request.expected_revision + 1
            # Each settings revision also advances the existing Source revision. Its existing
            # triggers fence policies/ingress/content; no parallel revocation state machine.
            updated = instance.model_copy(update={"revision": instance.revision + 1})
            self.identities.registry.register_instance(updated)
            params = (
                source_id,
                self.engine.settings.project_id,
                revision,
                updated.revision,
                Jsonb(value.model_dump(mode="json")),
                user,
                request.review_ref,
            )
            conn.execute(
                "INSERT INTO app.github_source_settings(source_instance_id,project_id,revision,"
                "source_revision,definition,signed_by,review_ref) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(source_instance_id) DO UPDATE SET revision=excluded.revision,"
                "source_revision=excluded.source_revision,definition=excluded.definition,"
                "signed_by=excluded.signed_by,signed_at=now(),review_ref=excluded.review_ref",
                params,
            )
            conn.execute(
                "INSERT INTO "
                "app.github_source_versions(source_instance_id,revision,source_revision,"
                "definition,signed_by,review_ref) VALUES (%s,%s,%s,%s,%s,%s)",
                (source_id, revision, updated.revision, params[4], user, request.review_ref),
            )
            self._audit(
                conn,
                source_id,
                "configure",
                "GITHUB_CONFIG_SAVED",
                user,
                revision=revision,
                review_ref=request.review_ref,
            )
            return {
                "revision": revision,
                "source_revision": updated.revision,
                "settings": value.model_dump(mode="json"),
                "runtime": "NOT_ACTIVATED_BY_SETTINGS",
            }

    def inspect(self, cookie: str, source_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self.identities._console(conn, cookie)
            row, value = self._current(conn, source_id)
            audit = conn.execute(
                "SELECT event,reason_code,details,created_at FROM app.github_source_audit "
                "WHERE source_instance_id=%s ORDER BY audit_id DESC LIMIT 100",
                (source_id,),
            ).fetchall()
            return {
                "revision": row["revision"],
                "source_revision": row["source_revision"],
                "settings": value.model_dump(mode="json"),
                "audit": audit,
                "connection": "disabled",
                "permissions": "unknown",
                "live_source_channel_gate": "NOT_RUN",
                "capability_limits": [
                    "no universal delivery replay",
                    "no native edit ordering",
                    "bounded current issue/PR comment snapshots only",
                ],
            }

    def _grant_check(
        self,
        conn: Connection,
        source_id: UUID,
        value: SnapshotGrant,
        config_revision: int,
        source_revision: int,
        generation: int,
    ) -> None:
        row, settings = self._current(conn, source_id)
        control, ingress = self.engine.source_ingress._control(conn, source_id)
        now = self.engine.source_ingress.now(conn)
        if (
            not settings.enabled
            or settings.repository.repository_id != value.repository_id
            or row["revision"] != config_revision
            or row["source_revision"] != source_revision
            or control["generation"] != generation
            or ingress.owner != "gateway"
            or ingress.policy_id != value.policy_id
            or ingress.policy_revision != value.policy_revision
            or not now < value.expires_at
        ):
            raise DomainError(
                "GITHUB_SNAPSHOT_FENCED", "Snapshot permission expired or changed", 403
            )
        policy = self.engine.source_policies._current(conn, value.policy_id)
        definition = PolicyDefinition.model_validate(policy["definition"])
        self.engine.source_policies._validate_source(conn, definition)
        actor = self.identities._identity(conn, value.identity_id)
        from mgar_v3.sources.domain import ActorRef, SourceThreadRef

        identity = ActorRef.model_validate(actor["actor_ref"])
        thread = SourceThreadRef(
            scope=identity.scope, conversation_id="repository:" + str(value.repository_id)
        )
        roles, _ = self.engine.source_policies._roles(conn, identity, thread, None)
        boundaries = [
            r
            for r in definition.execution.repositories
            if r.repository.lower() == settings.repository.full_name.lower()
            and r.base_sha == value.head_sha
        ]
        if (
            policy["revoked"]
            or policy["current_revision"] != value.policy_revision
            or definition.mode not in {"auto_investigate", "auto_repair"}
            or not definition.valid_from <= now < definition.valid_until
            or value.expires_at > definition.valid_until
            or not definition.execution.enabled
            or "read_repository" not in definition.execution.capabilities
            or actor["revoked_at"] is not None
            or actor["revision"] != value.identity_revision
            or identity.scope.source_instance_id != source_id
            or identity.actor_kind != "human"
            or identity not in definition.selector.actors
            or thread.conversation_id not in definition.selector.conversations
            or (
                definition.selector.native_threads
                and (
                    value.pull_request is None
                    or "pr:" + str(value.pull_request) not in definition.selector.native_threads
                )
            )
            or not {"requester", "context_provider"} <= set(roles)
            or not boundaries
            or not all(
                any(path == p or path.startswith(p + "/") for p in boundaries[0].paths)
                for path in value.paths
            )
        ):
            raise DomainError(
                "GITHUB_SNAPSHOT_GRANT_DENIED",
                "Current explicit repository read grant required",
                403,
            )

    def save_grant(
        self, cookie: str, source_id: UUID, grant_id: UUID, request: SnapshotSave
    ) -> Row:
        with self.engine.transaction() as conn:
            user = self.identities._console(conn, cookie)
            row, _ = self._current(conn, source_id)
            control, _ = self.engine.source_ingress._control(conn, source_id)
            if (
                request.expected_revision != 0
                or conn.execute(
                    "SELECT 1 FROM app.github_snapshot_grants WHERE grant_id=%s", (grant_id,)
                ).fetchone()
            ):
                raise DomainError(
                    "GITHUB_GRANT_IMMUTABLE", "Create a separately reviewed new grant", 409
                )
            self._grant_check(
                conn,
                source_id,
                request.definition,
                row["revision"],
                row["source_revision"],
                control["generation"],
            )
            conn.execute(
                "INSERT INTO "
                "app.github_snapshot_grants(grant_id,source_instance_id,config_revision,"
                "source_revision,owner_generation,revision,definition,signed_by,review_ref) "
                "VALUES (%s,%s,%s,%s,%s,1,%s,%s,%s)",
                (
                    grant_id,
                    source_id,
                    row["revision"],
                    row["source_revision"],
                    control["generation"],
                    Jsonb(request.definition.model_dump(mode="json")),
                    user,
                    request.review_ref,
                ),
            )
            self._audit(
                conn,
                source_id,
                "snapshot_grant",
                "GITHUB_GRANT_SAVED",
                user,
                grant_id=str(grant_id),
                review_ref=request.review_ref,
            )
            return {"grant_id": grant_id, "revision": 1}

    def revoke(self, cookie: str, source_id: UUID, grant_id: UUID, request: GitHubRevoke) -> Row:
        with self.engine.transaction() as conn:
            user = self.identities._console(conn, cookie)
            row = conn.execute(
                "UPDATE app.github_snapshot_grants SET revoked=true,revision=revision+1 "
                "WHERE grant_id=%s AND source_instance_id=%s AND revision=%s AND NOT revoked "
                "AND source_instance_id IN (SELECT source_instance_id FROM app.source_instances "
                "WHERE project_id=%s) RETURNING revision",
                (grant_id, source_id, request.expected_revision, self.engine.settings.project_id),
            ).fetchone()
            if row is None:
                raise DomainError("GITHUB_REVISION_CONFLICT", "Grant changed or was revoked", 409)
            self._audit(
                conn,
                source_id,
                "snapshot_revoke",
                "GITHUB_GRANT_REVOKED",
                user,
                grant_id=str(grant_id),
                review_ref=request.review_ref,
            )
            return row

    def authorize_snapshot(self, source_id: UUID, grant_id: UUID, revision: int) -> SnapshotGrant:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.github_snapshot_grants WHERE grant_id=%s AND "
                "source_instance_id=%s",
                (grant_id, source_id),
            ).fetchone()
            if row is None or row["revoked"] or row["revision"] != revision:
                raise DomainError(
                    "GITHUB_SNAPSHOT_GRANT_REQUIRED",
                    "Explicit current snapshot grant required",
                    403,
                )
            value = SnapshotGrant.model_validate(row["definition"])
            self._grant_check(
                conn,
                source_id,
                value,
                row["config_revision"],
                row["source_revision"],
                row["owner_generation"],
            )
            return value

    def legacy_route(self, payload: bytes, delivery_id: str) -> tuple[UUID, int, str] | None:
        """Untrusted fields select a stricter verifier, never grant permission."""
        import json

        try:
            raw = json.loads(payload)
            repository = raw.get("repository", {}).get("full_name", "")
        except (ValueError, AttributeError):
            return None  # Original endpoint will reject malformed input after signature.
        if not isinstance(repository, str):
            return None
        with self.engine.transaction() as conn:
            schema = conn.execute(
                "SELECT to_regclass('app.source_ingress_controls') AS present"
            ).fetchone()
            if not schema or schema["present"] is None:
                return None
            row = conn.execute(
                "SELECT * FROM app.source_ingress_controls WHERE project_id=%s AND "
                "legacy_repository=%s",
                (self.engine.settings.project_id, repository.lower()),
            ).fetchone()
            if row is None:
                return None
            settings = IngressSettings.model_validate(row["settings"])
            source_id = row["source_instance_id"]
            if settings.owner == "legacy":
                instance = self.identities.registry._instance(conn, source_id)
                if conn.execute(
                    "SELECT 1 FROM app.source_ingress_deliveries WHERE delivery_key=%s "
                    "AND disposition<>'legacy_owned'",
                    (digest([instance.scope.key, delivery_id]),),
                ).fetchone():
                    raise DomainError(
                        "INGRESS_DELIVERY_ALREADY_OWNED", "Delivery already belongs to gateway", 409
                    )
                return None
            return source_id, row["generation"], settings.owner
