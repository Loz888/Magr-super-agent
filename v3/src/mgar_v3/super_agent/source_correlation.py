"""Current, owner-reviewed Source relationships on the original Engine connection.

Relationships do not grant execution, control, model input or notification rights.
Plaintext is temporary; persisted facts contain exact typed references and hashes.
Only an explicitly injected trusted factory can create original fixture records;
new live Source execution belongs to the later authorized coding bridge.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.sources.github_domain import GitHubResource
from mgar_v3.sources.github_store import GitHubSources
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.ingress_domain import ItemMetadata
from mgar_v3.super_agent.fast_path_rules import extract_text, parse_intent
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicyFacts
from mgar_v3.super_agent.source_correlation_domain import (
    MAX_CORRELATION_BYTES,
    CodeRef,
    CorrelationCorrectRequest,
    CorrelationHead,
    CorrelationInputRef,
    CorrelationLinkRequest,
    CorrelationRemoveRequest,
    CorrelationResolveRequest,
    CorrelationSnapshot,
    InputUnit,
    ResourceEvidence,
    ResourceKind,
    ResourceRef,
    TargetRef,
    input_unit_key,
    snapshot_hash,
)
from mgar_v3.super_agent.source_correlation_rules import exact_url_resources
from mgar_v3.super_agent.source_correlation_rules import resolve as resolve_facts
from mgar_v3.super_agent.source_eval_domain import SourceDecision

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine

Row = dict[str, Any]


def _error(code: str, status: int = 409) -> DomainError:
    return DomainError(
        code, "Source correlation requires current exact authorized references", status
    )


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _hash(value: Any) -> str:
    return digest(_json(value))


def _decode[T: Model](model: type[T], value: Any) -> T:
    try:
        return model.model_validate_json(json.dumps(value, default=str))
    except (ValueError, TypeError, RecursionError):
        raise _error("SOURCE_CORRELATION_FACT_INVALID", 403) from None


@dataclass
class _Input:
    row: Row
    item: ItemMetadata
    definition: PolicyDefinition
    ref: CorrelationInputRef
    text: str
    clear: bytes
    expires: datetime
    native: tuple[ResourceEvidence, ...] = ()


class SourceCorrelations:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.identities = SourceIdentities(engine)
        self.github = GitHubSources(engine)

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    def _console(self, conn: Connection, cookie: str) -> UUID:
        user = self.identities._console(conn, cookie)
        row = conn.execute("SELECT to_regclass('app.sa_task_correlations') AS ready").fetchone()
        if row is None or row["ready"] is None:
            raise _error("SOURCE_CORRELATION_NOT_MIGRATED")
        return user

    def _now(self, conn: Connection) -> datetime:
        return self.engine.source_ingress.now(conn)

    def _input(self, conn: Connection, input_id: UUID) -> _Input:
        row = conn.execute(
            "SELECT * FROM app.source_ingress_inputs WHERE input_id=%s AND project_id=%s",
            (input_id, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_CORRELATION_INPUT_NOT_FOUND", 404)
        ingress = self.engine.source_ingress
        item, grant = ingress._readable(conn, row)
        body = conn.execute(
            "SELECT ciphertext,expires_at FROM app.source_ingress_bodies WHERE input_id=%s "
            "AND expires_at>clock_timestamp()",
            (input_id,),
        ).fetchone()
        if body is None or ingress.cipher is None:
            raise _error("SOURCE_CORRELATION_CONTENT_UNAVAILABLE", 403)
        clear = ingress.cipher.decrypt(bytes(body["ciphertext"]), str(input_id))
        if (
            len(clear) > MAX_CORRELATION_BYTES
            or hashlib.sha256(clear).hexdigest() != row["body_sha256"]
        ):
            raise _error("SOURCE_CORRELATION_CONTENT_INVALID", 403)
        policy = self.engine.source_policies._current(conn, grant["policy_id"])
        definition = PolicyDefinition.model_validate(policy["definition"])
        instance = self.identities.registry._instance(conn, row["source_instance_id"])
        control, _ = ingress._control(conn, row["source_instance_id"])
        identity = conn.execute(
            "SELECT to_jsonb(i) AS identity,to_jsonb(b) AS binding FROM app.source_identities i "
            "JOIN app.source_role_bindings b USING(identity_id,project_id) "
            "WHERE i.identity_id=%s AND i.project_id=%s AND b.conversation_key=%s",
            (row["identity_id"], self.project, item.event.source_thread.conversation_key),
        ).fetchone()
        if identity is None:
            raise _error("SOURCE_CORRELATION_IDENTITY_DENIED", 403)
        controllers = conn.execute(
            "SELECT ticket_id,active,granted_at,revoked_at FROM app.source_task_controllers "
            "WHERE binding_id=%s AND project_id=%s ORDER BY ticket_id LIMIT 101",
            (identity["binding"]["binding_id"], self.project),
        ).fetchall()
        if len(controllers) > 100:
            raise _error("SOURCE_CORRELATION_CAPACITY")
        entity = conn.execute(
            "SELECT * FROM app.source_ingress_entities WHERE entity_key=%s",
            (row["entity_key"],),
        ).fetchone()
        authority = _hash(
            {
                "source": instance.model_dump(mode="json"),
                "ingress": control,
                "identity": identity,
                "controllers": controllers,
                "entity": entity,
                "policy_id": policy["policy_id"],
                "policy_revision": policy["current_revision"],
                "policy_sha256": policy["definition_sha256"],
                "revoked": policy["revoked"],
                "input": {
                    key: row[key]
                    for key in (
                        "input_id",
                        "source_instance_id",
                        "project_id",
                        "version_fingerprint",
                        "identity_revision",
                        "policy_revision",
                        "context_only",
                        "unavailable_at",
                        "metadata_expires_at",
                        "body_sha256",
                    )
                },
                "body_expires_at": body["expires_at"],
            }
        )
        ref = CorrelationInputRef(
            input_id=input_id,
            source_instance_id=row["source_instance_id"],
            project_id=self.project,
            version_fingerprint=row["version_fingerprint"],
            state_sha256=authority,
        )
        result = _Input(
            row,
            item,
            definition,
            ref,
            extract_text(clear),
            clear,
            min(row["metadata_expires_at"], body["expires_at"], definition.valid_until),
        )
        if result.expires <= self._now(conn):
            raise _error("SOURCE_CORRELATION_INPUT_EXPIRED", 403)
        result.native = self._native(conn, result)
        return result

    def _native(self, conn: Connection, value: _Input) -> tuple[ResourceEvidence, ...]:
        event = value.item.event
        if event.scope.source_kind != "github":
            return ()
        _, settings = self.github._current(conn, event.scope.source_instance_id)
        try:
            raw = json.loads(value.clear)
            if (
                not settings.enabled
                or set(raw) != {"schema_version", "resource", "text"}
                or raw["schema_version"] != "github-content-v1"
                or type(raw["text"]) is not str
            ):
                raise ValueError("Unverified resource shape")
            resource = GitHubResource.model_validate(raw["resource"])
            if (
                event.scope.tenant_id != str(settings.account_id)
                or event.scope.installation_id != str(settings.installation_id)
                or resource.repository.repository_id != settings.repository.repository_id
                or resource.repository.private != settings.repository.private
                or resource.repository.full_name.lower() != settings.repository.full_name.lower()
                or resource.kind not in settings.events
                or event.event_kind != resource.kind + "." + resource.action
            ):
                raise ValueError("Resource owner differs")
            selected = []
            if resource.kind in {"issues", "issue_comment"} and not resource.pull_requests:
                if resource.number is not None:
                    selected.append(("issue", resource.number))
            elif resource.kind in {
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "issue_comment",
            }:
                if resource.number is not None:
                    selected.append(("pull_request", resource.number))
            else:
                own = getattr(
                    resource,
                    {
                        "workflow_run": "run_id",
                        "check_run": "check_run_id",
                        "check_suite": "check_suite_id",
                    }[resource.kind],
                )
                if own is not None:
                    selected.append((resource.kind, own))
                selected.extend(("pull_request", number) for number in resource.pull_requests)
            if len(selected) > 16:
                raise ValueError("Too many explicit resources")
            proof = _hash(
                {
                    "input_id": value.ref.input_id,
                    "version": value.ref.version_fingerprint,
                    "body_sha256": value.row["body_sha256"],
                    "resource": raw["resource"],
                }
            )
            result = []
            for kind, number in sorted(set(selected)):
                ref = ResourceRef(
                    project_id=self.project,
                    scope=event.scope,
                    repository_id=resource.repository.repository_id,
                    repository=resource.repository.full_name.lower(),
                    kind=cast(ResourceKind, kind),
                    resource_id=str(number),
                )
                code = CodeRef(
                    resource_key=ref.resource_key,
                    proof_sha256=proof,
                    head_repository_id=resource.head_repository_id,
                    head_sha=resource.head_sha,
                    base_sha=resource.base_sha,
                    run_id=resource.run_id,
                    run_attempt=resource.run_attempt,
                    check_run_id=resource.check_run_id,
                    check_suite_id=resource.check_suite_id,
                )
                result.append(
                    ResourceEvidence(
                        input_id=value.ref.input_id,
                        resource=ref,
                        proof_kind="verified_resource",
                        proof_sha256=proof,
                        code_ref=code,
                    )
                )
            return tuple(result)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise _error("SOURCE_CORRELATION_RESOURCE_INVALID", 403) from None

    def _target(self, conn: Connection, ticket_id: UUID) -> TargetRef:
        row = conn.execute(
            "SELECT h.thread_id,h.revision,h.project_id,h.repository,t.ticket_id,m.mandate_id,"
            "to_jsonb(m) AS mandate,r.run_id,r.execution_epoch FROM app.tickets t "
            "JOIN app.threads h USING(thread_id) JOIN app.mandates m USING(mandate_id) "
            "JOIN LATERAL(SELECT * FROM app.runs x WHERE x.ticket_id=t.ticket_id "
            "ORDER BY attempt_no DESC LIMIT 1) r ON true WHERE t.ticket_id=%s "
            "AND h.project_id=%s AND m.project_id=%s AND lower(h.repository)=%s",
            (ticket_id, self.project, self.project, self.engine.settings.repository.lower()),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_CORRELATION_TARGET_NOT_FOUND", 404)
        return TargetRef(
            ticket_id=row["ticket_id"],
            run_id=row["run_id"],
            thread_id=row["thread_id"],
            thread_revision=row["revision"],
            execution_epoch=row["execution_epoch"],
            mandate_id=row["mandate_id"],
            mandate_sha256=digest(row["mandate"]),
            project_id=self.project,
            repository=row["repository"].lower(),
            allowed=True,
        )

    def target(self, cookie: str, ticket_id: UUID) -> TargetRef:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            return self._target(conn, ticket_id)

    def _record(self, conn: Connection, correlation_id: UUID, revision: int | None = None) -> Row:
        row = conn.execute(
            "SELECT v.*,h.revision AS current_revision FROM app.sa_task_correlations h "
            "JOIN app.sa_task_correlation_versions v ON v.correlation_id=h.correlation_id "
            "AND v.project_id=h.project_id AND v.revision=coalesce(%s,h.revision) "
            "WHERE h.correlation_id=%s AND h.project_id=%s",
            (revision, correlation_id, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_CORRELATION_NOT_FOUND", 404)
        if digest(row["payload"]) != row["payload_sha256"]:
            raise _error("SOURCE_CORRELATION_HISTORY_INVALID", 403)
        _decode(TargetRef, row["payload"]["target"])
        return row

    def _claims(self, conn: Connection, correlation_id: UUID) -> list[Row]:
        return conn.execute(
            "SELECT * FROM app.sa_task_correlation_claims WHERE correlation_id=%s "
            "AND project_id=%s ORDER BY resource_key LIMIT 65",
            (correlation_id, self.project),
        ).fetchall()

    def _head(self, conn: Connection, key: str) -> CorrelationHead:
        claim = conn.execute(
            "SELECT c.*,h.revision FROM app.sa_task_correlation_claims c JOIN "
            "app.sa_task_correlations h USING(correlation_id,project_id) "
            "WHERE c.project_id=%s AND c.resource_key=%s",
            (self.project, key),
        ).fetchone()
        if claim is None or claim["revision"] == 0:
            return CorrelationHead(
                resource_key=key,
                revision=0,
                head_sha256=digest([key, "unclaimed"]),
                state="unclaimed",
            )
        row = self._record(conn, claim["correlation_id"])
        state = row["payload"]["state"]
        return CorrelationHead(
            resource_key=key,
            revision=row["revision"],
            head_sha256=_hash([claim, row["payload_sha256"]]),
            state=state,
            target=_decode(TargetRef, row["payload"]["target"]) if state == "bound" else None,
            link_id=row["correlation_id"],
        )

    @staticmethod
    def _context(value: _Input, target: TargetRef) -> None:
        if not any(
            boundary.repository.lower() == target.repository
            for boundary in value.definition.execution.repositories
        ):
            raise _error("SOURCE_CORRELATION_TARGET_DENIED", 403)

    def _known(
        self, conn: Connection, loaded: dict[UUID, _Input]
    ) -> dict[str, tuple[_Input, ResourceEvidence]]:
        known = {
            proof.resource.resource_key: (value, proof)
            for value in loaded.values()
            for proof in value.native
        }
        rows = conn.execute(
            "SELECT DISTINCT input_id FROM app.sa_correlation_resources WHERE project_id=%s "
            "ORDER BY input_id LIMIT 257",
            (self.project,),
        ).fetchall()
        if len(rows) > 256:
            raise _error("SOURCE_CORRELATION_CAPACITY")
        for row in rows:
            try:
                value = loaded.get(row["input_id"]) or self._input(conn, row["input_id"])
            except DomainError:
                continue
            for proof in value.native:
                known.setdefault(proof.resource.resource_key, (value, proof))
        return known

    def _alias(
        self, conn: Connection, value: _Input, key: str, native: _Input, proof: ResourceEvidence
    ) -> tuple[ResourceEvidence, datetime, str] | None:
        rows = conn.execute(
            "SELECT l.*,v.payload_sha256,v.payload FROM app.sa_thread_link_inputs l "
            "JOIN app.sa_task_correlations h USING(correlation_id,project_id) JOIN "
            "app.sa_task_correlation_versions v ON v.correlation_id=l.correlation_id "
            "AND v.project_id=l.project_id AND v.revision=l.revision "
            "WHERE l.source_instance_id=%s AND l.project_id=%s AND l.conversation_sha256=%s "
            "AND l.resource_key=%s AND l.revision=h.revision "
            "ORDER BY l.correlation_id,l.input_id LIMIT 65",
            (
                value.ref.source_instance_id,
                self.project,
                value.item.event.source_thread.conversation_key,
                key,
            ),
        ).fetchall()
        if len(rows) > 64:
            raise _error("SOURCE_CORRELATION_CAPACITY")
        candidates = []
        for row in rows:
            if row["payload"]["state"] != "bound":
                continue
            try:
                anchor = self._input(conn, row["input_id"])
                target = self._target(conn, UUID(row["payload"]["target"]["ticket_id"]))
                self._context(value, target)
                self._context(native, target)
                self._context(anchor, target)
            except DomainError:
                continue
            token = _hash(
                [
                    row["correlation_id"],
                    row["revision"],
                    row["payload_sha256"],
                    anchor.ref.state_sha256,
                    native.ref.state_sha256,
                    proof.proof_sha256,
                ]
            )
            evidence = ResourceEvidence(
                input_id=value.ref.input_id,
                resource=proof.resource,
                proof_kind="signed_alias",
                proof_sha256=token,
                code_ref=proof.code_ref,
                alias_id=row["correlation_id"],
                alias_revision=row["revision"],
            )
            candidates.append((evidence, min(anchor.expires, native.expires), token))
        return candidates[0] if len({entry[0].alias_id for entry in candidates}) == 1 else None

    def _build(
        self, conn: Connection, input_ids: tuple[UUID, ...]
    ) -> tuple[CorrelationSnapshot, dict[UUID, _Input]]:
        if not 1 <= len(input_ids) <= 16 or len(set(input_ids)) != len(input_ids):
            raise _error("SOURCE_CORRELATION_INPUT_INVALID", 422)
        loaded = {input_id: self._input(conn, input_id) for input_id in sorted(input_ids, key=str)}
        known = self._known(conn, loaded)
        proofs, units, refs, deadlines = [], [], [], [value.expires for value in loaded.values()]
        for value in loaded.values():
            active = list(value.native)
            alias_tokens = []
            if not active:
                resources = exact_url_resources(
                    value.text, tuple(item[1].resource for item in known.values())
                )
                for resource in resources:
                    native, proof = known[resource.resource_key]
                    signed = self._alias(conn, value, resource.resource_key, native, proof)
                    if signed is not None:
                        evidence, deadline, token = signed
                        active.append(evidence)
                        deadlines.append(deadline)
                        alias_tokens.append(token)
                    else:
                        proofs.append(
                            ResourceEvidence(
                                input_id=value.ref.input_id,
                                resource=resource,
                                proof_kind="suggestion",
                                proof_sha256=proof.proof_sha256,
                                code_ref=proof.code_ref,
                            )
                        )
            ref = (
                value.ref.model_copy(
                    update={"state_sha256": _hash([value.ref.state_sha256, sorted(alias_tokens)])}
                )
                if alias_tokens
                else value.ref
            )
            refs.append(ref)
            if active:
                for proof in sorted(active, key=lambda item: item.resource.resource_key):
                    proofs.append(proof)
                    units.append(
                        InputUnit(
                            input_id=value.ref.input_id,
                            unit_key=input_unit_key(ref, proof.resource.resource_key),
                            resource_keys=(proof.resource.resource_key,),
                            origin="verified_resource"
                            if proof.proof_kind == "verified_resource"
                            else "signed_alias",
                        )
                    )
            else:
                units.append(InputUnit(input_id=value.ref.input_id, unit_key=input_unit_key(ref)))
        heads = tuple(
            self._head(conn, key)
            for key in sorted({proof.resource.resource_key for proof in proofs})
        )
        targets: dict[UUID, TargetRef] = {}
        for head in heads:
            if head.target is None:
                continue
            try:
                current = self._target(conn, head.target.ticket_id)
                for value in loaded.values():
                    self._context(value, current)
            except DomainError:
                continue
            targets[current.ticket_id] = current
        snapshot = CorrelationSnapshot(
            project_id=self.project,
            inputs=tuple(refs),
            units=tuple(units),
            resources=tuple(proofs),
            heads=heads,
            targets=tuple(targets[key] for key in sorted(targets, key=str)),
            expires_at=min(deadlines),
        )
        if len(snapshot.model_dump_json().encode()) > MAX_CORRELATION_BYTES:
            raise _error("SOURCE_CORRELATION_CAPACITY")
        return snapshot, loaded

    def _suggestion(
        self, conn: Connection, key: str | None, inputs: tuple[UUID, ...]
    ) -> SourceDecision | None:
        if key is None:
            return None
        row = conn.execute(
            "SELECT d.payload,d.payload_sha256,s.snapshot_sha256 FROM "
            "app.sa_source_eval_decisions d JOIN app.sa_source_eval_snapshots s USING(batch_id) "
            "WHERE d.decision_key=%s AND s.project_id=%s",
            (key, self.project),
        ).fetchone()
        if row is None:
            raise _error("SOURCE_CORRELATION_SUGGESTION_NOT_FOUND", 404)
        suggestion = _decode(SourceDecision, row["payload"])
        if (
            digest(row["payload"]) != row["payload_sha256"]
            or suggestion.snapshot_sha256 != row["snapshot_sha256"]
            or suggestion.expires_at <= self._now(conn)
            or not set(suggestion.input_ids) <= set(inputs)
        ):
            raise _error("SOURCE_CORRELATION_SUGGESTION_SCOPE", 403)
        versions = {
            input_id: fingerprint
            for input_id, fingerprint in zip(
                suggestion.input_ids, suggestion.input_version_fingerprints, strict=True
            )
        }
        if any(
            self._input(conn, input_id).ref.version_fingerprint != fingerprint
            for input_id, fingerprint in versions.items()
        ):
            raise _error("SOURCE_CORRELATION_SUGGESTION_SCOPE", 403)
        source_eval = self.engine.source_eval
        source_eval._checked(conn, source_eval._batch(conn, suggestion.batch_id))
        return suggestion

    def resolve(self, cookie: str, request: CorrelationResolveRequest) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            snapshot, _ = self._build(conn, request.input_ids)
            results = resolve_facts(
                snapshot,
                at=self._now(conn),
                suggestion=self._suggestion(
                    conn, request.suggestion_decision_key, request.input_ids
                ),
            )
            return {
                "snapshot": snapshot.model_dump(mode="json"),
                "snapshot_sha256": snapshot_hash(snapshot),
                "resolutions": [value.model_dump(mode="json") for value in results],
                "eligible_for_execution": False,
                "executes_tasks": False,
                "calls_model": False,
            }

    def _observations(self, conn: Connection, loaded: dict[UUID, _Input]) -> None:
        for value in loaded.values():
            for proof in value.native:
                resource = proof.resource.model_dump(mode="json")
                code = (
                    proof.code_ref.model_dump(mode="json") if proof.code_ref is not None else None
                )
                conn.execute(
                    "INSERT INTO app.sa_correlation_resources(input_id,source_instance_id,"
                    "project_id,version_fingerprint,resource_key,resource,code,"
                    "proof_sha256,code_version_key) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(input_id,resource_key) DO NOTHING",
                    (
                        value.ref.input_id,
                        value.ref.source_instance_id,
                        self.project,
                        value.ref.version_fingerprint,
                        proof.resource.resource_key,
                        Jsonb(resource),
                        Jsonb(code) if code is not None else None,
                        proof.proof_sha256,
                        proof.code_ref.code_version_key if proof.code_ref else None,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM app.sa_correlation_resources WHERE input_id=%s "
                    "AND resource_key=%s",
                    (value.ref.input_id, proof.resource.resource_key),
                ).fetchone()
                if (
                    row is None
                    or row["resource"] != resource
                    or row["code"] != code
                    or row["version_fingerprint"] != value.ref.version_fingerprint
                    or row["proof_sha256"] != proof.proof_sha256
                ):
                    raise _error("SOURCE_CORRELATION_RESOURCE_CONFLICT")

    def _claim(self, conn: Connection, loaded: dict[UUID, _Input], key: str) -> tuple[UUID, bool]:
        keys = self._resource_set(loaded, key)
        prior = self._group_head(conn, keys)
        correlation_id = prior.link_id or uuid4()
        created = prior.link_id is None
        for resource_key in keys:
            native = [
                (value, proof)
                for value in loaded.values()
                for proof in value.native
                if proof.resource.resource_key == resource_key
            ]
            if not native:
                raise _error("SOURCE_CORRELATION_NATIVE_PROOF_REQUIRED", 403)
            value, _ = native[0]
            row = conn.execute(
                "INSERT INTO app.sa_task_correlation_claims(project_id,resource_key,"
                "correlation_id,original_input_id,source_instance_id,version_fingerprint) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(project_id,resource_key) DO NOTHING "
                "RETURNING correlation_id",
                (
                    self.project,
                    resource_key,
                    correlation_id,
                    value.ref.input_id,
                    value.ref.source_instance_id,
                    value.ref.version_fingerprint,
                ),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT correlation_id FROM app.sa_task_correlation_claims "
                    "WHERE project_id=%s AND resource_key=%s",
                    (self.project, resource_key),
                ).fetchone()
            if row is None or row["correlation_id"] != correlation_id:
                raise _error("SOURCE_CORRELATION_RESOURCE_CONFLICT")
        if created:
            conn.execute(
                "INSERT INTO app.sa_task_correlations(correlation_id,project_id) VALUES(%s,%s)",
                (correlation_id, self.project),
            )
        return correlation_id, created

    @staticmethod
    def _resource_set(loaded: dict[UUID, _Input], key: str) -> tuple[str, ...]:
        keys = {key}
        present = False
        for value in loaded.values():
            if not any(proof.resource.resource_key == key for proof in value.native):
                continue
            present = True
            parents = {
                proof.resource.resource_key
                for proof in value.native
                if proof.resource.kind == "pull_request"
            }
            own = {
                proof.resource.resource_key
                for proof in value.native
                if proof.resource.kind in {"workflow_run", "check_run", "check_suite"}
            }
            if key in own and len(parents) > 1:
                raise _error("SOURCE_CORRELATION_RESOURCE_AMBIGUOUS")
            if len(parents) == 1 and own:
                keys.update(parents | own)
        if not present:
            raise _error("SOURCE_CORRELATION_NATIVE_PROOF_REQUIRED", 403)
        return tuple(sorted(keys))

    def _group_head(self, conn: Connection, keys: tuple[str, ...]) -> CorrelationHead:
        heads = [self._head(conn, key) for key in keys]
        bound = [head for head in heads if head.link_id is not None]
        if len({head.link_id for head in bound}) > 1:
            raise _error("SOURCE_CORRELATION_RESOURCE_CONFLICT")
        return bound[0] if bound else heads[0]

    def _selection(
        self, conn: Connection, loaded: dict[UUID, _Input], key: str, target: TargetRef
    ) -> ResourceRef:
        keys = self._resource_set(loaded, key)
        native = [
            proof.resource
            for value in loaded.values()
            for proof in value.native
            if proof.resource.resource_key in keys
        ]
        if not native:
            raise _error("SOURCE_CORRELATION_NATIVE_PROOF_REQUIRED", 403)
        resource = native[0]
        if (
            resource.repository != target.repository
            or target.project_id != self.project
            or not target.allowed
        ):
            raise _error("SOURCE_CORRELATION_TARGET_DENIED", 403)
        for value in loaded.values():
            self._context(value, target)
            if value.native:
                explicit = any(proof.resource.resource_key in keys for proof in value.native)
            else:
                explicit = bool(exact_url_resources(value.text, tuple(native)))
            if not explicit:
                raise _error("SOURCE_CORRELATION_REFERENCE_REQUIRED", 403)
        return resource

    def _append(
        self,
        conn: Connection,
        correlation_id: UUID,
        revision: int,
        loaded: dict[UUID, _Input],
        target: TargetRef,
        user: UUID,
        *,
        key: str,
        snapshot_sha256: str,
        review_ref: str,
        reason: str,
        state: str = "bound",
        sign_aliases: bool = True,
    ) -> Row:
        keys = sorted({row["resource_key"] for row in self._claims(conn, correlation_id)})
        references = {
            proof.resource.resource_key: proof.resource
            for value in loaded.values()
            for proof in value.native
        }
        anchors = []
        for value in loaded.values():
            explicit = (
                {proof.resource.resource_key for proof in value.native} & set(keys)
                if value.native
                else {
                    resource.resource_key
                    for resource in exact_url_resources(value.text, tuple(references.values()))
                }
                & set(keys)
            )
            if not explicit:
                raise _error("SOURCE_CORRELATION_REFERENCE_REQUIRED", 403)
            for resource_key in sorted(explicit):
                anchors.append(
                    {
                        **value.ref.model_dump(mode="json"),
                        "unit_key": input_unit_key(value.ref, resource_key),
                        "conversation_sha256": value.item.event.source_thread.conversation_key,
                        "topic_sha256": value.item.event.source_thread.topic_key,
                        "resource_key": resource_key if value.native or sign_aliases else None,
                    }
                )
        if not 1 <= len(keys) <= 64:
            raise _error("SOURCE_CORRELATION_CAPACITY")
        if len(anchors) > 64:
            raise _error("SOURCE_CORRELATION_CAPACITY")
        payload = {
            "schema_version": "source-correlation-v1",
            "state": state,
            "target": target.model_dump(mode="json"),
            "anchors": anchors,
            "resource_keys": keys,
            "snapshot_sha256": snapshot_sha256,
        }
        conn.execute(
            "INSERT INTO app.sa_task_correlation_versions(correlation_id,revision,project_id,"
            "previous_revision,thread_id,ticket_id,run_id,thread_revision,execution_epoch,mandate_id,"
            "mandate_sha256,repository,snapshot_sha256,payload,payload_sha256,reason_code,"
            "review_sha256,signed_by) "
            "VALUES(" + ",".join(["%s"] * 18) + ")",
            (
                correlation_id,
                revision,
                self.project,
                revision - 1 if revision > 1 else None,
                target.thread_id,
                target.ticket_id,
                target.run_id,
                target.thread_revision,
                target.execution_epoch,
                target.mandate_id,
                target.mandate_sha256,
                target.repository,
                snapshot_sha256,
                Jsonb(payload),
                digest(payload),
                reason,
                digest(review_ref),
                user,
            ),
        )
        for anchor in anchors:
            conn.execute(
                "INSERT INTO app.sa_thread_link_inputs(correlation_id,revision,input_id,"
                "source_instance_id,project_id,version_fingerprint,unit_key,conversation_sha256,topic_sha256,"
                "resource_key) VALUES(" + ",".join(["%s"] * 10) + ")",
                (
                    correlation_id,
                    revision,
                    UUID(anchor["input_id"]),
                    UUID(anchor["source_instance_id"]),
                    self.project,
                    anchor["version_fingerprint"],
                    anchor["unit_key"],
                    anchor["conversation_sha256"],
                    anchor["topic_sha256"],
                    anchor["resource_key"],
                ),
            )
        changed = conn.execute(
            "UPDATE app.sa_task_correlations SET revision=revision+1 "
            "WHERE correlation_id=%s AND project_id=%s AND revision=%s RETURNING revision",
            (correlation_id, self.project, revision - 1),
        ).fetchone()
        if changed is None:
            raise _error("SOURCE_CORRELATION_REVISION_CONFLICT")
        return self._record(conn, correlation_id)

    @staticmethod
    def _identity(target: TargetRef) -> tuple[Any, ...]:
        return (
            target.ticket_id,
            target.run_id,
            target.thread_id,
            target.mandate_id,
            target.mandate_sha256,
            target.repository,
            target.project_id,
        )

    def _same(self, row: Row, loaded: dict[UUID, _Input], target: TargetRef) -> bool:
        anchors = row["payload"]["anchors"]
        return (
            row["payload"]["state"] == "bound"
            and row["payload"]["target"] == target.model_dump(mode="json")
            and {anchor["input_id"] for anchor in anchors} == {str(key) for key in loaded}
            and all(
                anchor.get("state_sha256") == loaded[UUID(anchor["input_id"])].ref.state_sha256
                for anchor in anchors
            )
        )

    def _view(
        self, conn: Connection, row: Row, *, snapshot: CorrelationSnapshot | None = None
    ) -> Row:
        payload = row["payload"]
        result = {
            "link_id": row["correlation_id"],
            "correlation_id": row["correlation_id"],
            "revision": row["revision"],
            "state": payload["state"],
            "target": payload["target"],
            "input_ids": sorted({anchor["input_id"] for anchor in payload["anchors"]}),
            "resource_keys": payload["resource_keys"],
            "snapshot_sha256": row["snapshot_sha256"],
            "is_current": row["revision"] == row["current_revision"],
            "current_revision": row["current_revision"],
            "eligible_for_execution": False,
            "executes_tasks": False,
            "calls_model": False,
            "external_writes": 0,
        }
        if snapshot is not None:
            result.update(
                snapshot=snapshot.model_dump(mode="json"), snapshot_sha256=snapshot_hash(snapshot)
            )
        return result

    def link(self, cookie: str, request: CorrelationLinkRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            snapshot, loaded = self._build(conn, request.input_ids)
            current = self._target(conn, request.target.ticket_id)
            if current != request.target:
                raise _error("SOURCE_CORRELATION_TARGET_STALE")
            self._selection(conn, loaded, request.resource_key, current)
            selected_head = self._head(conn, request.resource_key)
            head = self._group_head(conn, self._resource_set(loaded, request.resource_key))
            prior = self._record(conn, head.link_id) if head.link_id else None
            same = prior is not None and self._same(prior, loaded, current)
            if request.snapshot_sha256 != snapshot_hash(snapshot) and not (
                prior is not None and same and request.snapshot_sha256 == prior["snapshot_sha256"]
            ):
                raise _error("SOURCE_CORRELATION_SNAPSHOT_STALE")
            if (
                prior is not None
                and same
                and request.expected_revision in {0, selected_head.revision}
            ):
                return {**self._view(conn, prior, snapshot=snapshot), "duplicate": True}
            if request.expected_revision != selected_head.revision:
                raise _error("SOURCE_CORRELATION_REVISION_CONFLICT")
            if prior is not None and self._identity(
                _decode(TargetRef, prior["payload"]["target"])
            ) != self._identity(current):
                raise _error("SOURCE_CORRELATION_RESOURCE_CONFLICT")
            if prior is not None and prior["payload"]["state"] != "bound":
                raise _error("SOURCE_CORRELATION_RESOURCE_REMOVED")
            self._observations(conn, loaded)
            correlation_id, created = self._claim(conn, loaded, request.resource_key)
            if created != (prior is None):
                raise _error("SOURCE_CORRELATION_REVISION_CONFLICT")
            row = self._append(
                conn,
                correlation_id,
                head.revision + 1,
                loaded,
                current,
                user,
                key=request.resource_key,
                snapshot_sha256=snapshot_hash(snapshot),
                review_ref=request.review_ref,
                reason="EXACT_RESOURCE",
            )
            return {**self._view(conn, row), "duplicate": False}

    def _history_inputs(self, conn: Connection, row: Row) -> tuple[UUID, ...]:
        rows = conn.execute(
            "SELECT DISTINCT input_id FROM app.sa_thread_link_inputs "
            "WHERE correlation_id=%s AND project_id=%s AND revision=%s ORDER BY input_id LIMIT 17",
            (row["correlation_id"], self.project, row["revision"]),
        ).fetchall()
        if not 1 <= len(rows) <= 16:
            raise _error("SOURCE_CORRELATION_CAPACITY")
        return tuple(value["input_id"] for value in rows)

    def _primary_resource(self, row: Row, loaded: dict[UUID, _Input]) -> str:
        """Choose an explicit shared unit, with a single PR parent taking priority.

        The claim array is hash-sorted storage, not an authority or tie breaker.
        An IM URL to a PR must not be tested against the related CI run's own ID.
        """
        resources = {
            proof.resource.resource_key: proof.resource
            for value in loaded.values()
            for proof in value.native
        }
        common: set[str] = set(row["payload"]["resource_keys"])
        for value in loaded.values():
            explicit = (
                {proof.resource.resource_key for proof in value.native}
                if value.native
                else {
                    resource.resource_key
                    for resource in exact_url_resources(value.text, tuple(resources.values()))
                }
            )
            common &= explicit
        parents = {key for key in common if resources[key].kind == "pull_request"}
        if len(parents) == 1:
            return next(iter(parents))
        if len(common) == 1:
            return next(iter(common))
        raise _error("SOURCE_CORRELATION_RESOURCE_AMBIGUOUS")

    def get(self, cookie: str, correlation_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            row = self._record(conn, correlation_id)
            snapshot, _ = self._build(conn, self._history_inputs(conn, row))
            return self._view(conn, row, snapshot=snapshot)

    def links(self, cookie: str, input_id: UUID, limit: int = 100) -> Row:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise _error("SOURCE_CORRELATION_LIMIT_INVALID", 422)
        with self.engine.transaction() as conn:
            self._console(conn, cookie)
            self._input(conn, input_id)
            rows = conn.execute(
                "SELECT DISTINCT correlation_id,revision FROM app.sa_thread_link_inputs "
                "WHERE input_id=%s AND project_id=%s ORDER BY correlation_id,revision LIMIT %s",
                (input_id, self.project, limit),
            ).fetchall()
            results = []
            for value in rows:
                row = self._record(conn, value["correlation_id"], value["revision"])
                for member in self._history_inputs(conn, row):
                    self._input(conn, member)
                results.append(self._view(conn, row))
            return {"input_id": input_id, "links": results, "eligible_for_execution": False}

    def _unstarted(self, conn: Connection, ticket_id: UUID) -> None:
        rows = conn.execute(
            "SELECT run_id,status,execution_epoch FROM app.runs WHERE ticket_id=%s", (ticket_id,)
        ).fetchall()
        for row in rows:
            if row["status"] != "queued" or row["execution_epoch"] != 0:
                raise _error("SOURCE_CORRELATION_EXECUTION_STARTED")
            run_id = row["run_id"]
            unsafe = conn.execute(
                "SELECT 1 FROM app.usage_calls WHERE run_id=%s UNION ALL "
                "SELECT 1 FROM app.read_jobs WHERE run_id=%s UNION ALL "
                "SELECT 1 FROM app.evidence WHERE run_id=%s UNION ALL "
                "SELECT 1 FROM app.actions WHERE run_id=%s UNION ALL "
                "SELECT 1 FROM app.target_write_grants WHERE run_id=%s UNION ALL "
                "SELECT 1 FROM app.repair_requests WHERE run_id=%s UNION ALL "
                "SELECT 1 FROM app.repair_steps WHERE run_id=%s UNION ALL "
                "SELECT 1 FROM app.repair_candidates WHERE run_id=%s UNION ALL "
                "SELECT 1 FROM app.repair_manual_inputs WHERE run_id=%s UNION ALL "
                "SELECT 1 FROM app.repo_tool_calls WHERE run_id=%s UNION ALL "
                "SELECT 1 FROM app.repo_analysis_runs WHERE run_id=%s",
                (run_id,) * 11,
            ).fetchone()
            if unsafe:
                raise _error("SOURCE_CORRELATION_EXECUTION_STARTED")

    def correct(self, cookie: str, correlation_id: UUID, request: CorrelationCorrectRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            prior = self._record(conn, correlation_id)
            if request.expected_revision != prior["revision"]:
                raise _error("SOURCE_CORRELATION_REVISION_CONFLICT")
            snapshot, loaded = self._build(conn, self._history_inputs(conn, prior))
            if snapshot_hash(snapshot) != request.snapshot_sha256:
                raise _error("SOURCE_CORRELATION_SNAPSHOT_STALE")
            target = self._target(conn, request.target.ticket_id)
            if target != request.target:
                raise _error("SOURCE_CORRELATION_TARGET_STALE")
            old = _decode(TargetRef, prior["payload"]["target"])
            if self._identity(old) != self._identity(target):
                self._unstarted(conn, old.ticket_id)
            key = self._primary_resource(prior, loaded)
            self._selection(conn, loaded, key, target)
            self._observations(conn, loaded)
            row = self._append(
                conn,
                correlation_id,
                prior["revision"] + 1,
                loaded,
                target,
                user,
                key=key,
                snapshot_sha256=snapshot_hash(snapshot),
                review_ref=request.review_ref,
                reason=request.reason_code,
                sign_aliases=False,
            )
            return self._view(conn, row)

    def remove(self, cookie: str, correlation_id: UUID, request: CorrelationRemoveRequest) -> Row:
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            prior = self._record(conn, correlation_id)
            if request.expected_revision != prior["revision"]:
                raise _error("SOURCE_CORRELATION_REVISION_CONFLICT")
            snapshot, loaded = self._build(conn, self._history_inputs(conn, prior))
            if snapshot_hash(snapshot) != request.snapshot_sha256:
                raise _error("SOURCE_CORRELATION_SNAPSHOT_STALE")
            original = _decode(TargetRef, prior["payload"]["target"])
            target = self._target(conn, original.ticket_id)
            row = self._append(
                conn,
                correlation_id,
                prior["revision"] + 1,
                loaded,
                target,
                user,
                key=self._primary_resource(prior, loaded),
                snapshot_sha256=snapshot_hash(snapshot),
                review_ref=request.review_ref,
                reason=request.reason_code,
                state="removed",
                sign_aliases=False,
            )
            return self._view(conn, row)

    def _creation_authority(self, conn: Connection, loaded: dict[UUID, _Input], key: str) -> None:
        keys = self._resource_set(loaded, key)
        for value in loaded.values():
            event, definition = value.item.event, value.definition
            roles, controlled = self.engine.source_policies._roles(
                conn, event.sender, event.source_thread, None
            )
            kind = parse_intent(value.item, value.text).request_kind
            if kind is None and "team_rule" in definition.admission.request_kinds:
                kind = "team_rule"
            if (
                definition.mode in {"shadow", "disabled"}
                or value.row["context_only"]
                or event.sender.actor_kind != "human"
                or "requester" not in roles
                or kind is None
                or not any(proof.resource.resource_key in keys for proof in value.native)
            ):
                raise _error("SOURCE_CORRELATION_CREATION_DENIED", 403)
            decision = self.engine.source_policies._evaluate(
                definition,
                "admission",
                event.sender,
                event.source_thread,
                PolicyFacts(
                    event_kind=event.event_kind,
                    request_kind=kind,
                    historical=event.historical_backfill,
                ),
                roles,
                self._now(conn),
                controlled,
            )
            if not decision.allowed:
                raise _error("SOURCE_CORRELATION_CREATION_DENIED", 403)
            resource = next(
                proof.resource for proof in value.native if proof.resource.resource_key in keys
            )
            if not any(
                boundary.repository.lower() == resource.repository
                for boundary in definition.execution.repositories
            ):
                raise _error("SOURCE_CORRELATION_CREATION_DENIED", 403)

    def claim_or_create(
        self,
        cookie: str,
        input_ids: tuple[UUID, ...],
        resource_key: str,
        create_target: Callable[[Connection], TargetRef] | None = None,
    ) -> Row:
        request = CorrelationResolveRequest(input_ids=tuple(input_ids))
        with self.engine.transaction() as conn:
            user = self._console(conn, cookie)
            snapshot, loaded = self._build(conn, request.input_ids)
            keys = self._resource_set(loaded, resource_key)
            head = self._group_head(conn, keys)
            if head.link_id is not None:
                row = self._record(conn, head.link_id)
                if row["payload"]["state"] != "bound":
                    raise _error("SOURCE_CORRELATION_RESOURCE_REMOVED")
                target = self._target(conn, UUID(row["payload"]["target"]["ticket_id"]))
                if target != _decode(TargetRef, row["payload"]["target"]):
                    raise _error("SOURCE_CORRELATION_TARGET_STALE")
                self._selection(conn, loaded, resource_key, target)
                all_claimed = all(self._head(conn, key).link_id == head.link_id for key in keys)
                if self._same(row, loaded, target) and all_claimed:
                    return {**self._view(conn, row), "created": False}
                self._observations(conn, loaded)
                correlation_id, created = self._claim(conn, loaded, resource_key)
                if created:
                    raise _error("SOURCE_CORRELATION_REVISION_CONFLICT")
                updated = self._append(
                    conn,
                    correlation_id,
                    row["revision"] + 1,
                    loaded,
                    target,
                    user,
                    key=resource_key,
                    snapshot_sha256=snapshot_hash(snapshot),
                    review_ref="trusted-source-correlation-existing-context-v1",
                    reason="EXACT_RESOURCE",
                )
                return {**self._view(conn, updated), "created": False}
            if create_target is None:
                raise _error("SOURCE_CORRELATION_FACTORY_REQUIRED")
            self._creation_authority(conn, loaded, resource_key)
            self._observations(conn, loaded)
            correlation_id, created = self._claim(conn, loaded, resource_key)
            if not created:
                raise _error("SOURCE_CORRELATION_REVISION_CONFLICT")
            made = create_target(conn)
            self._console(conn, cookie)
            after, current_inputs = self._build(conn, request.input_ids)
            if snapshot_hash(after) != snapshot_hash(snapshot):
                raise _error("SOURCE_CORRELATION_SNAPSHOT_STALE")
            if not isinstance(made, TargetRef):
                raise _error("SOURCE_CORRELATION_FACTORY_TARGET_INVALID", 422)
            target = self._target(conn, made.ticket_id)
            if target != made:
                raise _error("SOURCE_CORRELATION_FACTORY_TARGET_INVALID")
            mode = conn.execute(
                "SELECT r.execution_mode AS run_mode,m.execution_mode AS mandate_mode "
                "FROM app.runs r JOIN app.tickets t USING(ticket_id) "
                "JOIN app.mandates m USING(mandate_id) "
                "WHERE r.run_id=%s",
                (target.run_id,),
            ).fetchone()
            if mode is None or mode["run_mode"] != "fixture" or mode["mandate_mode"] != "fixture":
                raise _error("SOURCE_CORRELATION_FACTORY_LIVE_DENIED", 403)
            self._unstarted(conn, target.ticket_id)
            self._creation_authority(conn, current_inputs, resource_key)
            self._selection(conn, current_inputs, resource_key, target)
            row = self._append(
                conn,
                correlation_id,
                1,
                current_inputs,
                target,
                user,
                key=resource_key,
                snapshot_sha256=snapshot_hash(snapshot),
                review_ref="trusted-source-correlation-factory-v1",
                reason="EXACT_RESOURCE",
            )
            return {**self._view(conn, row), "created": True}
