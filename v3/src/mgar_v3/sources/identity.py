"""Trusted proof -> explicit owner-reviewed identity binding -> current role checks.

External verification occurs outside SQL transactions. Every role check that can
authorize a mutation holds the existing Engine transaction for that mutation.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import ActorRef
from mgar_v3.sources.identity_domain import (
    BindIdentity,
    ControllerGrant,
    IdentityAction,
    IdentityAuthorization,
    IdentityCredential,
    IdentityVerifier,
    RestoreBinding,
    RestoreIdentity,
    RevisionCommand,
    Role,
    RoleChange,
)
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.sources.registry import SourceRegistry

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine

T = TypeVar("T")
Row = dict[str, Any]
PROOF_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30


class SourceIdentities:
    def __init__(
        self,
        engine: Engine,
        verifiers: Mapping[tuple[str, str], IdentityVerifier] | None = None,
    ):
        self.engine = engine
        self.registry = SourceRegistry(engine)
        self._verifiers = dict(
            verifiers or {}
        )  # Trusted server composition only, no HTTP registration.

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    def _audit(
        self,
        conn: Connection,
        event: str,
        target: UUID | None,
        decision: str,
        code: str,
        user: UUID | None = None,
        **details: Any,
    ) -> None:
        conn.execute(
            "INSERT INTO app.source_identity_audit(project_id,actor_user_id,event,target_id,"
            "decision,reason_code,details) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (self.project, user, event, target, decision, code, Jsonb(details)),
        )

    def _denied(self, event: str, code: str, target: UUID | None = None) -> None:
        safe_code = code if re.fullmatch(r"[A-Z0-9_]{1,100}", code) else "IDENTITY_DENIED"
        with self.engine.transaction() as conn:
            self._audit(conn, event, target, "deny", safe_code)

    def _console(self, conn: Connection, cookie: str) -> UUID:
        if not cookie or len(cookie) > 256:
            raise DomainError("AUTH_REQUIRED", "A current console owner session is required", 401)
        row = conn.execute(
            "SELECT u.user_id FROM app.web_sessions s JOIN app.web_accounts u USING(user_id) "
            "WHERE s.token_hash=%s AND s.expires_at>now() AND u.singleton",
            (hashlib.sha256(cookie.encode()).hexdigest(),),
        ).fetchone()
        if row is None:
            raise DomainError("AUTH_REQUIRED", "A current console owner session is required", 401)
        return UUID(str(row["user_id"]))

    def _manage(
        self,
        cookie: str,
        event: str,
        target: UUID | None,
        command: Callable[[Connection, UUID], T],
        review_ref: str | None = None,
    ) -> T:
        try:
            with self.engine.transaction() as conn:
                user = self._console(
                    conn, cookie
                )  # Recheck after acquiring the mutation transaction.
                result = command(conn, user)
                details = {"review_ref": review_ref} if review_ref is not None else {}
                self._audit(
                    conn, event, target, "allow", "IDENTITY_MANAGEMENT_ALLOWED", user, **details
                )
                return result
        except DomainError as exc:
            self._denied(event, exc.code, target)
            raise

    def _identity(self, conn: Connection, identity_id: UUID) -> Row:
        row = conn.execute(
            "SELECT * FROM app.source_identities WHERE identity_id=%s AND project_id=%s FOR UPDATE",
            (identity_id, self.project),
        ).fetchone()
        if row is None:
            raise DomainError("IDENTITY_NOT_FOUND", "Identity was not found in this project", 404)
        return row

    def _binding(self, conn: Connection, binding_id: UUID) -> Row:
        row = conn.execute(
            "SELECT * FROM app.source_role_bindings WHERE binding_id=%s AND project_id=%s FOR "
            "UPDATE",
            (binding_id, self.project),
        ).fetchone()
        if row is None:
            raise DomainError(
                "IDENTITY_BINDING_NOT_FOUND", "Binding was not found in this project", 404
            )
        return row

    @staticmethod
    def _revision(row: Row, expected: int) -> None:
        if row["revision"] != expected:
            raise DomainError(
                "IDENTITY_REVISION_CONFLICT", "Authorization changed; reload before editing"
            )

    @staticmethod
    def _human_roles(identity: Row, roles: tuple[Role, ...]) -> None:
        if identity["actor_kind"] != "human" and set(roles) != {"context_provider"}:
            raise DomainError(
                "IDENTITY_NONHUMAN_ROLE", "Bots and services can only provide context", 403
            )

    def authenticate(self, instance_id: UUID, envelope: TransportEnvelope) -> IdentityCredential:
        """Called only by a trusted gateway; no public endpoint accepts assertions or proofs.

        Returns an internal bearer, not a business admission/receive grant. Strict
        event replay protection is not a substitute for SA04's ingress protocol.
        """
        try:
            instance = self.registry.get_instance(instance_id)
            if instance.scope.source_kind not in {"github", "feishu"} or (
                instance.scope.installation_id == "legacy-trigger-input-v1"
            ):
                raise DomainError(
                    "IDENTITY_PLATFORM_REQUIRED",
                    "Legacy inputs are not platform identity proofs",
                    403,
                )
            verifier = self._verifiers.get((instance.scope.source_kind, instance.adapter_version))
            if verifier is None:
                raise DomainError(
                    "IDENTITY_VERIFIER_MISSING", "No trusted platform verifier is configured", 503
                )
            if len(envelope.payload) > 262144:
                raise DomainError(
                    "IDENTITY_PROOF_TOO_LARGE", "Identity envelope exceeds its bound", 413
                )
            subject = verifier.verify_identity(
                instance, envelope
            )  # Outside the Engine transaction.
            if (
                subject.actor.scope != instance.scope
                or subject.source_thread.scope != instance.scope
                or subject.payload_sha256 != hashlib.sha256(envelope.payload).hexdigest()
                or subject.actor.actor_kind == "unknown"
                or subject.historical_backfill
            ):
                raise DomainError(
                    "IDENTITY_PROOF_SCOPE",
                    "Identity assertion is not a current verified sender",
                    403,
                )
            now = datetime.now(UTC)
            if (
                not now - timedelta(seconds=PROOF_SECONDS)
                <= subject.occurred_at
                <= now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS)
            ):
                raise DomainError(
                    "IDENTITY_PROOF_EXPIRED", "Identity proof is outside the replay window", 403
                )
            token = secrets.token_urlsafe(48)
            evidence_id = uuid4()
            with self.engine.transaction() as conn:
                current = self.registry._instance(conn, instance_id)
                if current != instance:
                    raise DomainError(
                        "IDENTITY_SOURCE_CHANGED",
                        "Source configuration changed during verification",
                    )
                conn.execute(
                    "INSERT INTO app.source_identities(identity_id,identity_key,source_instance_id,"
                    "project_id,id_type,actor_id,actor_kind,actor_ref) VALUES (%s,%s,%s,%s,%s,"
                    "%s,%s,%s) "
                    "ON CONFLICT(identity_key) DO NOTHING",
                    (
                        uuid4(),
                        subject.actor.key,
                        instance_id,
                        self.project,
                        subject.actor.id_type,
                        subject.actor.actor_id,
                        subject.actor.actor_kind,
                        Jsonb(subject.actor.model_dump(mode="json")),
                    ),
                )
                identity = conn.execute(
                    "SELECT * FROM app.source_identities WHERE identity_key=%s AND "
                    "project_id=%s FOR UPDATE",
                    (subject.actor.key, self.project),
                ).fetchone()
                assert identity is not None
                if ActorRef.model_validate(identity["actor_ref"]) != subject.actor:
                    raise DomainError(
                        "IDENTITY_KIND_CONFLICT", "A verified identity cannot change its kind", 403
                    )
                server_time = conn.execute("SELECT now() AS now").fetchone()
                assert server_time is not None
                conn.execute(
                    "INSERT INTO app.source_identity_evidence(evidence_id,identity_id,"
                    "source_instance_id,"
                    "project_id,source_revision,auth_revision,remote_event_id,payload_sha256,"
                    "credential_hash,"
                    "conversation_key,source_thread,verification_method,verifier_id,"
                    "occurred_at,expires_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        evidence_id,
                        identity["identity_id"],
                        instance_id,
                        self.project,
                        instance.revision,
                        identity["revision"],
                        subject.remote_event_id,
                        subject.payload_sha256,
                        hashlib.sha256(token.encode()).hexdigest(),
                        subject.source_thread.conversation_key,
                        Jsonb(subject.source_thread.model_dump(mode="json")),
                        subject.verification_method,
                        verifier.verifier_id,
                        subject.occurred_at,
                        server_time["now"] + timedelta(seconds=PROOF_SECONDS),
                    ),
                )
                self._audit(
                    conn,
                    "identity_verified",
                    identity["identity_id"],
                    "allow",
                    "IDENTITY_VERIFIED",
                    evidence_id=str(evidence_id),
                )
                return IdentityCredential(
                    evidence_id=evidence_id,
                    identity_id=identity["identity_id"],
                    token=SecretStr(token),
                )
        except UniqueViolation as exc:
            self._denied("identity_verify", "IDENTITY_PROOF_REPLAY", instance_id)
            raise DomainError(
                "IDENTITY_PROOF_REPLAY", "This signed event already has an identity receipt", 409
            ) from exc
        except DomainError as exc:
            self._denied("identity_verify", exc.code, instance_id)
            raise
        except (ValueError, TypeError, AttributeError):
            self._denied("identity_verify", "IDENTITY_ASSERTION_INVALID", instance_id)
            raise DomainError(
                "IDENTITY_ASSERTION_INVALID", "Platform verifier returned an invalid subject", 403
            ) from None

    def _evidence(self, conn: Connection, evidence_id: UUID) -> tuple[Row, Row]:
        proof = conn.execute(
            "SELECT * FROM app.source_identity_evidence WHERE evidence_id=%s AND project_id=%s "
            "AND expires_at>now()",
            (evidence_id, self.project),
        ).fetchone()
        if proof is None:
            raise DomainError(
                "IDENTITY_EVIDENCE_INVALID", "A current verified receipt is required", 403
            )
        identity = self._identity(conn, proof["identity_id"])
        current = self.registry._instance(conn, identity["source_instance_id"])
        if (
            proof["source_revision"] != current.revision
            or proof["auth_revision"] != identity["revision"]
        ):
            raise DomainError(
                "IDENTITY_EVIDENCE_STALE", "Identity or source permissions changed", 409
            )
        return proof, identity

    def _principal(
        self, conn: Connection, identity: Row, request: BindIdentity, user: UUID
    ) -> UUID:
        kind = "human" if identity["actor_kind"] == "human" else "automation"
        selected: Row | None = None
        if request.console_owner:
            if kind != "human":
                raise DomainError(
                    "IDENTITY_NONHUMAN_PRINCIPAL", "A bot cannot become the console owner", 403
                )
            selected = conn.execute(
                "SELECT * FROM app.source_principals WHERE project_id=%s AND console_user_id=%s",
                (self.project, user),
            ).fetchone()
        elif request.principal_id is not None:
            selected = conn.execute(
                "SELECT * FROM app.source_principals WHERE project_id=%s AND principal_id=%s",
                (self.project, request.principal_id),
            ).fetchone()
            if selected is None:
                raise DomainError(
                    "IDENTITY_PRINCIPAL_NOT_FOUND", "Principal was not found in this project", 404
                )
        elif identity["principal_id"] is not None:
            selected = conn.execute(
                "SELECT * FROM app.source_principals WHERE principal_id=%s AND project_id=%s",
                (identity["principal_id"], self.project),
            ).fetchone()
        if selected is not None:
            if selected["principal_kind"] != kind:
                raise DomainError(
                    "IDENTITY_NONHUMAN_PRINCIPAL",
                    "Human and automation identities cannot merge",
                    403,
                )
            principal_id = UUID(str(selected["principal_id"]))
        else:
            principal_id = uuid4()
            conn.execute(
                "INSERT INTO app.source_principals(principal_id,project_id,principal_kind,"
                "engine_actor,console_user_id) "
                "VALUES (%s,%s,%s,%s,%s)",
                (
                    principal_id,
                    self.project,
                    kind,
                    "sa_" + principal_id.hex,
                    user if request.console_owner else None,
                ),
            )
        if identity["principal_id"] is not None and identity["principal_id"] != principal_id:
            raise DomainError(
                "IDENTITY_PRINCIPAL_IMMUTABLE",
                "A bound identity cannot silently change principal",
                409,
            )
        if identity["principal_id"] is None:
            conn.execute(
                "UPDATE app.source_identities SET principal_id=%s,revision=revision+1 WHERE "
                "identity_id=%s",
                (principal_id, identity["identity_id"]),
            )
        return principal_id

    def _view(self, conn: Connection, binding_id: UUID) -> Row:
        row = conn.execute(
            "SELECT b.binding_id,b.identity_id,b.conversation_key,b.roles,b.revision,b.active,"
            "b.revoked_at,"
            "i.actor_ref,i.revision AS identity_revision,i.revoked_at AS identity_revoked_at,"
            "p.principal_id,p.engine_actor,p.console_user_id,p.principal_kind "
            "FROM app.source_role_bindings b JOIN app.source_identities i USING(identity_id,"
            "project_id) "
            "JOIN app.source_principals p ON p.principal_id=i.principal_id AND "
            "p.project_id=i.project_id "
            "WHERE b.binding_id=%s AND b.project_id=%s",
            (binding_id, self.project),
        ).fetchone()
        assert row is not None
        row["controlled_tickets"] = [
            r["ticket_id"]
            for r in conn.execute(
                "SELECT ticket_id FROM app.source_task_controllers WHERE binding_id=%s AND "
                "active ORDER BY ticket_id",
                (binding_id,),
            ).fetchall()
        ]
        return row

    def bind(self, cookie: str, request: BindIdentity) -> Row:
        def command(conn: Connection, user: UUID) -> Row:
            proof, identity = self._evidence(conn, request.evidence_id)
            self._revision(identity, request.expected_identity_revision)
            if identity["revoked_at"] is not None:
                raise DomainError(
                    "IDENTITY_REVOKED", "Restore the identity with a fresh proof first", 403
                )
            self._human_roles(identity, request.roles)
            if conn.execute(
                "SELECT 1 FROM app.source_role_bindings WHERE identity_id=%s AND "
                "conversation_key=%s",
                (identity["identity_id"], proof["conversation_key"]),
            ).fetchone():
                raise DomainError(
                    "IDENTITY_BINDING_EXISTS", "This conversation already has a binding"
                )
            self._principal(conn, identity, request, user)
            binding_id = uuid4()
            conn.execute(
                "INSERT INTO app.source_role_bindings(binding_id,identity_id,project_id,"
                "conversation_key,roles) "
                "VALUES (%s,%s,%s,%s,%s)",
                (
                    binding_id,
                    identity["identity_id"],
                    self.project,
                    proof["conversation_key"],
                    Jsonb(list(request.roles)),
                ),
            )
            return self._view(conn, binding_id)

        return self._manage(
            cookie, "identity_bind", request.evidence_id, command, request.review_ref
        )

    def roles(self, cookie: str, binding_id: UUID, request: RoleChange) -> Row:
        def command(conn: Connection, user: UUID) -> Row:
            binding = self._binding(conn, binding_id)
            self._revision(binding, request.expected_revision)
            identity = self._identity(conn, binding["identity_id"])
            if not binding["active"] or identity["revoked_at"] is not None:
                raise DomainError(
                    "IDENTITY_REVOKED",
                    "Revoked identities or bindings cannot receive new roles",
                    403,
                )
            self._human_roles(identity, request.roles)
            conn.execute(
                "UPDATE app.source_role_bindings SET roles=%s,revision=revision+1 WHERE "
                "binding_id=%s",
                (Jsonb(list(request.roles)), binding_id),
            )
            if "task_controller" not in request.roles:
                conn.execute(
                    "UPDATE app.source_task_controllers SET active=false,revoked_at=now() "
                    "WHERE binding_id=%s AND active",
                    (binding_id,),
                )
            return self._view(conn, binding_id)

        return self._manage(cookie, "identity_roles", binding_id, command, request.review_ref)

    def revoke_binding(self, cookie: str, binding_id: UUID, request: RevisionCommand) -> Row:
        def command(conn: Connection, user: UUID) -> Row:
            binding = self._binding(conn, binding_id)
            self._revision(binding, request.expected_revision)
            if not binding["active"]:
                raise DomainError("IDENTITY_REVOKED", "Binding is already revoked", 409)
            conn.execute(
                "UPDATE app.source_role_bindings SET active=false,revoked_at=now(),"
                "revision=revision+1 "
                "WHERE binding_id=%s",
                (binding_id,),
            )
            conn.execute(
                "UPDATE app.source_task_controllers SET active=false,revoked_at=now() "
                "WHERE binding_id=%s AND active",
                (binding_id,),
            )
            return self._view(conn, binding_id)

        return self._manage(
            cookie, "identity_binding_revoke", binding_id, command, request.review_ref
        )

    def restore_binding(self, cookie: str, binding_id: UUID, request: RestoreBinding) -> Row:
        def command(conn: Connection, user: UUID) -> Row:
            binding = self._binding(conn, binding_id)
            self._revision(binding, request.expected_revision)
            proof, identity = self._evidence(conn, request.evidence_id)
            if (
                binding["active"]
                or identity["identity_id"] != binding["identity_id"]
                or identity["revoked_at"] is not None
                or proof["conversation_key"] != binding["conversation_key"]
                or proof["verified_at"] <= binding["revoked_at"]
            ):
                raise DomainError(
                    "IDENTITY_RESTORE_PROOF",
                    "A new matching proof after revocation is required",
                    403,
                )
            self._human_roles(identity, request.roles)
            conn.execute(
                "UPDATE app.source_role_bindings SET active=true,revoked_at=NULL,roles=%s,"
                "revision=revision+1 "
                "WHERE binding_id=%s",
                (Jsonb(list(request.roles)), binding_id),
            )
            return self._view(conn, binding_id)

        return self._manage(
            cookie, "identity_binding_restore", binding_id, command, request.review_ref
        )

    def revoke_identity(self, cookie: str, identity_id: UUID, request: RevisionCommand) -> Row:
        def command(conn: Connection, user: UUID) -> Row:
            identity = self._identity(conn, identity_id)
            self._revision(identity, request.expected_revision)
            if identity["revoked_at"] is not None:
                raise DomainError("IDENTITY_REVOKED", "Identity is already revoked", 409)
            conn.execute(
                "UPDATE app.source_identities SET revoked_at=now(),revision=revision+1 WHERE "
                "identity_id=%s",
                (identity_id,),
            )
            row = self._identity(conn, identity_id)
            return {"identity_id": identity_id, "revision": row["revision"], "revoked": True}

        return self._manage(cookie, "identity_revoke", identity_id, command, request.review_ref)

    def restore_identity(self, cookie: str, identity_id: UUID, request: RestoreIdentity) -> Row:
        def command(conn: Connection, user: UUID) -> Row:
            proof, identity = self._evidence(conn, request.evidence_id)
            self._revision(identity, request.expected_revision)
            if (
                identity["identity_id"] != identity_id
                or identity["revoked_at"] is None
                or proof["verified_at"] <= identity["revoked_at"]
            ):
                raise DomainError(
                    "IDENTITY_RESTORE_PROOF",
                    "A new matching proof after revocation is required",
                    403,
                )
            conn.execute(
                "UPDATE app.source_identities SET revoked_at=NULL,revision=revision+1 WHERE "
                "identity_id=%s",
                (identity_id,),
            )
            row = self._identity(conn, identity_id)
            return {"identity_id": identity_id, "revision": row["revision"], "revoked": False}

        return self._manage(cookie, "identity_restore", identity_id, command, request.review_ref)

    def _ticket(self, conn: Connection, ticket_id: UUID) -> Row:
        row = conn.execute(
            "SELECT t.ticket_id,t.thread_id FROM app.tickets t JOIN app.threads h USING(thread_id) "
            "WHERE t.ticket_id=%s AND h.project_id=%s",
            (ticket_id, self.project),
        ).fetchone()
        if row is None:
            raise DomainError("IDENTITY_TICKET_DENIED", "Ticket is outside this project", 403)
        return row

    def grant_controller(self, cookie: str, binding_id: UUID, request: ControllerGrant) -> Row:
        def command(conn: Connection, user: UUID) -> Row:
            binding = self._binding(conn, binding_id)
            self._revision(binding, request.expected_revision)
            identity = self._identity(conn, binding["identity_id"])
            if (
                not binding["active"]
                or identity["revoked_at"] is not None
                or "task_controller" not in binding["roles"]
            ):
                raise DomainError(
                    "IDENTITY_CONTROLLER_DENIED",
                    "An active explicit controller role is required",
                    403,
                )
            ticket = self._ticket(conn, request.ticket_id)
            conn.execute(
                "UPDATE app.source_role_bindings SET revision=revision+1 WHERE binding_id=%s",
                (binding_id,),
            )
            conn.execute(
                "INSERT INTO app.source_task_controllers(binding_id,ticket_id,thread_id,"
                "project_id) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(binding_id,ticket_id) DO UPDATE "
                "SET active=true,revoked_at=NULL,granted_at=now()",
                (binding_id, request.ticket_id, ticket["thread_id"], self.project),
            )
            return self._view(conn, binding_id)

        return self._manage(
            cookie, "identity_controller_grant", binding_id, command, request.review_ref
        )

    def revoke_controller(
        self, cookie: str, binding_id: UUID, ticket_id: UUID, request: RevisionCommand
    ) -> Row:
        def command(conn: Connection, user: UUID) -> Row:
            binding = self._binding(conn, binding_id)
            self._revision(binding, request.expected_revision)
            grant = conn.execute(
                "SELECT 1 FROM app.source_task_controllers WHERE binding_id=%s AND "
                "ticket_id=%s AND active",
                (binding_id, ticket_id),
            ).fetchone()
            if grant is None:
                raise DomainError(
                    "IDENTITY_CONTROLLER_DENIED", "No active controller grant exists", 403
                )
            conn.execute(
                "UPDATE app.source_role_bindings SET revision=revision+1 WHERE binding_id=%s",
                (binding_id,),
            )
            conn.execute(
                "UPDATE app.source_task_controllers SET active=false,revoked_at=now() WHERE "
                "binding_id=%s AND ticket_id=%s",
                (binding_id, ticket_id),
            )
            return self._view(conn, binding_id)

        return self._manage(
            cookie, "identity_controller_revoke", binding_id, command, request.review_ref
        )

    def evidence(self, cookie: str) -> list[Row]:
        def query(conn: Connection, user: UUID) -> list[Row]:
            return conn.execute(
                "SELECT e.evidence_id,e.identity_id,e.source_revision,e.conversation_key,"
                "e.source_thread,"
                "e.verified_at,e.expires_at,e.verification_method,e.verifier_id,i.actor_ref,"
                "i.revision AS identity_revision,i.revoked_at FROM app.source_identity_evidence e "
                "JOIN app.source_identities i USING(identity_id,project_id) WHERE e.project_id=%s "
                "ORDER BY e.verified_at DESC,e.evidence_id DESC LIMIT 100",
                (self.project,),
            ).fetchall()

        return self._manage(cookie, "identity_evidence_read", None, query)

    def bindings(self, cookie: str) -> list[Row]:
        def query(conn: Connection, user: UUID) -> list[Row]:
            rows = conn.execute(
                "SELECT binding_id FROM app.source_role_bindings WHERE project_id=%s ORDER BY "
                "binding_id LIMIT 100",
                (self.project,),
            ).fetchall()
            return [self._view(conn, row["binding_id"]) for row in rows]

        return self._manage(cookie, "identity_bindings_read", None, query)

    def principals(self, cookie: str) -> list[Row]:
        def query(conn: Connection, user: UUID) -> list[Row]:
            return conn.execute(
                "SELECT principal_id,principal_kind,engine_actor,console_user_id FROM "
                "app.source_principals "
                "WHERE project_id=%s ORDER BY principal_id LIMIT 100",
                (self.project,),
            ).fetchall()

        return self._manage(cookie, "identity_principals_read", None, query)

    def audit(self, cookie: str) -> list[Row]:
        def query(conn: Connection, user: UUID) -> list[Row]:
            return conn.execute(
                "SELECT audit_id,actor_user_id,event,target_id,decision,reason_code,details,"
                "created_at "
                "FROM app.source_identity_audit WHERE project_id=%s ORDER BY audit_id DESC "
                "LIMIT 100",
                (self.project,),
            ).fetchall()

        return self._manage(cookie, "identity_audit_read", None, query)

    @contextmanager
    def authorized(
        self,
        credential: IdentityCredential,
        action: IdentityAction,
        ticket_id: UUID | None = None,
    ) -> Iterator[IdentityAuthorization]:
        """The protected operation MUST finish inside this context, never cache its result."""
        try:
            with self.engine.transaction() as conn:
                proof = conn.execute(
                    "SELECT * FROM app.source_identity_evidence WHERE evidence_id=%s AND "
                    "identity_id=%s "
                    "AND project_id=%s AND credential_hash=%s AND expires_at>now()",
                    (
                        credential.evidence_id,
                        credential.identity_id,
                        self.project,
                        hashlib.sha256(credential.token.get_secret_value().encode()).hexdigest(),
                    ),
                ).fetchone()
                if proof is None:
                    raise DomainError(
                        "IDENTITY_CREDENTIAL_INVALID",
                        "Current server identity credential required",
                        403,
                    )
                _, identity = self._evidence(conn, credential.evidence_id)
                if identity["revoked_at"] is not None or identity["principal_id"] is None:
                    raise DomainError(
                        "IDENTITY_REVOKED_OR_UNBOUND", "Identity is revoked or unbound", 403
                    )
                if identity["actor_kind"] != "human" and action != "provide_context":
                    raise DomainError(
                        "IDENTITY_NONHUMAN_ROLE",
                        "Bots and services cannot perform control actions",
                        403,
                    )
                binding = conn.execute(
                    "SELECT * FROM app.source_role_bindings WHERE identity_id=%s "
                    "AND conversation_key=%s AND project_id=%s AND active FOR UPDATE",
                    (identity["identity_id"], proof["conversation_key"], self.project),
                ).fetchone()
                required = {
                    "provide_context": "context_provider",
                    "request_task": "requester",
                    "control_task": "task_controller",
                    "manage_rules": "project_administrator",
                }.get(action)
                if binding is None or required is None or required not in binding["roles"]:
                    raise DomainError(
                        "IDENTITY_ROLE_DENIED", "Source role does not authorize this action", 403
                    )
                if ticket_id is not None:
                    self._ticket(conn, ticket_id)
                if action == "control_task":
                    if (
                        ticket_id is None
                        or not conn.execute(
                            "SELECT 1 FROM app.source_task_controllers "
                            "WHERE binding_id=%s AND ticket_id=%s AND active",
                            (binding["binding_id"], ticket_id),
                        ).fetchone()
                    ):
                        raise DomainError(
                            "IDENTITY_CONTROLLER_DENIED",
                            "No explicit control grant for this Ticket",
                            403,
                        )
                principal = conn.execute(
                    "SELECT engine_actor,principal_kind FROM app.source_principals "
                    "WHERE principal_id=%s AND "
                    "project_id=%s",
                    (identity["principal_id"], self.project),
                ).fetchone()
                assert principal is not None
                expected_kind = "human" if identity["actor_kind"] == "human" else "automation"
                if principal["principal_kind"] != expected_kind:
                    raise DomainError(
                        "IDENTITY_PRINCIPAL_KIND",
                        "Principal kind does not match its verified identity",
                        403,
                    )
                self._audit(
                    conn,
                    "source_" + action,
                    identity["identity_id"],
                    "allow",
                    "IDENTITY_ROLE_ALLOWED",
                    ticket_id=str(ticket_id) if ticket_id is not None else None,
                    evidence_id=str(credential.evidence_id),
                )
                yield IdentityAuthorization(
                    identity_id=identity["identity_id"],
                    principal_id=identity["principal_id"],
                    engine_actor=principal["engine_actor"],
                    binding_id=binding["binding_id"],
                    action=action,
                    ticket_id=ticket_id,
                    evidence_id=credential.evidence_id,
                    expires_at=proof["expires_at"],
                )
        except DomainError as exc:
            self._denied("source_" + action, exc.code, credential.identity_id)
            raise
