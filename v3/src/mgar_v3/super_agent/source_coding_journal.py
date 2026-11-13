"""Private original-Engine Coding intent, encrypted payload and receipt journal."""

from __future__ import annotations

import json
from datetime import timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4, uuid5

from mgar_v3.domain import DomainError, digest

if TYPE_CHECKING:
    from psycopg import Connection

    from mgar_v3.super_agent.source_coding import SourceCoding
    from mgar_v3.super_agent.source_coding_domain import CodingRunRequest


def _deny(code: str) -> DomainError:
    return DomainError(code, "Original Coding journal could not establish the requested fact", 409)


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class SourceCodingJournal:
    def __init__(
        self, core: SourceCoding, bearer: str, request: CodingRunRequest, attempt_id: UUID
    ) -> None:
        self.core = core
        self.bearer = bearer
        self.request = request
        self.attempt_id = attempt_id
        self.task_id = str(request.coding_id)

    def _event(
        self, conn: Connection[Any], event: str, operation_id: UUID | None = None, **details: Any
    ) -> None:
        binding = self.core.binding(conn, self.request.coding_id)
        self.core.event(
            conn, binding, event, attempt_id=self.attempt_id, operation_id=operation_id, **details
        )

    def find(self, step_key: str, kind: str) -> dict[str, Any] | None:
        with self.core.engine.transaction() as conn:
            return conn.execute(
                "SELECT * FROM app.sa_coding_operations "
                "WHERE attempt_id=%s AND kind=%s AND step_key=%s",
                (self.attempt_id, kind, step_key),
            ).fetchone()

    def operation(
        self,
        *,
        step_key: str,
        kind: str,
        capability: str,
        request_sha256: str,
        tree_sha256: str,
        usage_call_id: UUID | None = None,
        resource_operation_id: UUID | None = None,
    ) -> dict[str, Any]:
        with self.core.engine.transaction() as conn:
            mandate, _ = self.core.current(conn, self.bearer, self.request, tree_sha256=tree_sha256)
            previous = conn.execute(
                "SELECT * FROM app.sa_coding_operations "
                "WHERE attempt_id=%s AND kind=%s AND step_key=%s",
                (self.attempt_id, kind, step_key),
            ).fetchone()
            identity = (
                capability,
                request_sha256,
                tree_sha256,
                usage_call_id,
                resource_operation_id,
            )
            if previous is not None:
                if (
                    tuple(
                        previous[key]
                        for key in (
                            "capability",
                            "request_sha256",
                            "tree_sha256",
                            "usage_call_id",
                            "resource_operation_id",
                        )
                    )
                    != identity
                ):
                    raise _deny("SOURCE_CODING_OPERATION_CONFLICT")
                return previous
            operation_id = uuid5(self.attempt_id, kind + ":" + step_key)
            row = conn.execute(
                "INSERT INTO app.sa_coding_operations(operation_id,attempt_id,coding_id,"
                "project_id,run_id,assignment_id,"
                "session_id,execution_epoch,kind,capability,step_key,request_sha256,context_sha256,tree_sha256,"
                "environment_sha256,usage_call_id,resource_operation_id) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    operation_id,
                    self.attempt_id,
                    mandate.coding_id,
                    mandate.target.project_id,
                    self.request.run_id,
                    self.request.assignment_id,
                    self.request.session_id,
                    self.request.epoch,
                    kind,
                    capability,
                    step_key,
                    request_sha256,
                    mandate.context_sha256,
                    tree_sha256,
                    mandate.grant.environment_sha256,
                    usage_call_id,
                    resource_operation_id,
                ),
            ).fetchone()
            assert row is not None
            self._event(
                conn,
                "OPERATION_QUEUED",
                operation_id,
                state="queued",
                request_sha256=request_sha256,
                tree_sha256=tree_sha256,
                context_sha256=mandate.context_sha256,
            )
            return row

    def dispatch(self, operation: dict[str, Any]) -> bool:
        """Return true only for the single persisted queued→running dispatch."""
        with self.core.engine.transaction() as conn:
            self.core.current(conn, self.bearer, self.request, tree_sha256=operation["tree_sha256"])
            row = conn.execute(
                "SELECT * FROM app.sa_coding_operations WHERE operation_id=%s FOR UPDATE",
                (operation["operation_id"],),
            ).fetchone()
            if row is None or row["attempt_id"] != self.attempt_id:
                raise _deny("SOURCE_CODING_OPERATION_SCOPE")
            if row["state"] != "queued":
                return False
            conn.execute(
                "UPDATE app.sa_coding_operations SET state='running',revision=revision+1,"
                "updated_at=clock_timestamp() WHERE operation_id=%s",
                (row["operation_id"],),
            )
            self._event(
                conn,
                "OPERATION_RUNNING",
                row["operation_id"],
                state="running",
                previous_state="queued",
            )
            return True

    def _known(
        self, conn: Connection[Any], operation: dict[str, Any], result_code: str, result_sha256: str
    ) -> None:
        row = conn.execute(
            "SELECT * FROM app.sa_coding_operations WHERE operation_id=%s FOR UPDATE",
            (operation["operation_id"],),
        ).fetchone()
        if row is None or row["attempt_id"] != self.attempt_id:
            raise _deny("SOURCE_CODING_OPERATION_SCOPE")
        if row["state"] == "known":
            if (row["result_code"], row["result_sha256"]) != (result_code, result_sha256):
                raise _deny("SOURCE_CODING_RECEIPT_CONFLICT")
            return
        conn.execute(
            "UPDATE app.sa_coding_operations SET state='known',result_code=%s,"
            "result_sha256=%s,observed_at=clock_timestamp(),"
            "revision=revision+1,updated_at=clock_timestamp() WHERE operation_id=%s",
            (result_code, result_sha256, row["operation_id"]),
        )
        self._event(
            conn,
            "OPERATION_KNOWN",
            row["operation_id"],
            state="known",
            previous_state=row["state"],
            result_sha256=result_sha256,
        )

    def known(self, operation: dict[str, Any], result_code: str, result_sha256: str) -> None:
        # This is actual observation accounting; authority loss cannot erase it.
        with self.core.engine.transaction() as conn:
            self._known(conn, operation, result_code, result_sha256)

    def unknown(self, operation: dict[str, Any]) -> None:
        with self.core.engine.transaction() as conn:
            row = conn.execute(
                "UPDATE app.sa_coding_operations SET state='unknown',revision=revision+1,"
                "updated_at=clock_timestamp() WHERE operation_id=%s AND attempt_id=%s "
                "AND state IN ('queued','running') RETURNING *",
                (operation["operation_id"], self.attempt_id),
            ).fetchone()
            if row is not None:
                self._event(conn, "OPERATION_UNKNOWN", row["operation_id"], state="unknown")

    @staticmethod
    def _payload_binding(operation: dict[str, Any], kind: str) -> str:
        return digest(
            {
                "version": "source-coding-payload-v1",
                "operation": str(operation["operation_id"]),
                "attempt": str(operation["attempt_id"]),
                "coding": str(operation["coding_id"]),
                "project": operation["project_id"],
                "context": operation["context_sha256"],
                "kind": kind,
            }
        )

    def _put_payload(
        self, conn: Connection[Any], operation: dict[str, Any], kind: str, clear: bytes
    ) -> None:
        if self.core.cipher is None or not 0 < len(clear) <= 1048576:
            raise _deny("SOURCE_CODING_PAYLOAD_UNAVAILABLE")
        mandate, _ = self.core.current(
            conn, self.bearer, self.request, tree_sha256=operation["tree_sha256"]
        )
        content_hash = sha256(clear).hexdigest()
        previous = conn.execute(
            "SELECT * FROM app.sa_coding_payloads WHERE operation_id=%s AND payload_kind=%s",
            (operation["operation_id"], kind),
        ).fetchone()
        if previous is not None:
            if previous["content_sha256"] != content_hash or previous["unavailable_at"] is not None:
                raise _deny("SOURCE_CODING_PAYLOAD_CONFLICT")
            return
        now = conn.execute("SELECT clock_timestamp() AS at").fetchone()
        assert now is not None
        expiry = min(
            now["at"] + timedelta(seconds=self.core.payload_retention_seconds),
            mandate.valid_until,
            mandate.origin.expires_at,
        )
        ciphertext = self.core.cipher.encrypt(clear, self._payload_binding(operation, kind))
        conn.execute(
            "INSERT INTO app.sa_coding_payloads(payload_id,operation_id,attempt_id,coding_id,"
            "project_id,payload_kind,ciphertext,ciphertext_sha256,content_sha256,"
            "plaintext_bytes,context_sha256,expires_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                uuid4(),
                operation["operation_id"],
                self.attempt_id,
                self.request.coding_id,
                operation["project_id"],
                kind,
                ciphertext,
                sha256(ciphertext).hexdigest(),
                content_hash,
                len(clear),
                mandate.context_sha256,
                expiry,
            ),
        )
        self._event(
            conn,
            "PAYLOAD_STORED",
            operation["operation_id"],
            payload_kind=kind,
            context_sha256=mandate.context_sha256,
        )

    def put_payload(self, operation: dict[str, Any], kind: str, clear: bytes) -> None:
        with self.core.engine.transaction() as conn:
            self._put_payload(conn, operation, kind, clear)

    def get_payload(
        self, operation: dict[str, Any], kind: str, *, current_tree: str | None = None
    ) -> bytes:
        # This transaction must commit even if current() below denies access.
        self.core.maintain(coding_id=self.request.coding_id)
        unavailable = False
        clear: bytes | None = None
        try:
            with self.core.engine.transaction() as conn:
                self.core.current(
                    conn,
                    self.bearer,
                    self.request,
                    tree_sha256=current_tree or operation["tree_sha256"],
                )
                row = conn.execute(
                    "SELECT *,clock_timestamp() AS current_at FROM app.sa_coding_payloads "
                    "WHERE operation_id=%s AND payload_kind=%s AND attempt_id=%s "
                    "AND coding_id=%s AND project_id=%s FOR UPDATE",
                    (
                        operation["operation_id"],
                        kind,
                        self.attempt_id,
                        self.request.coding_id,
                        self.core.project,
                    ),
                ).fetchone()
                if row is None or self.core.cipher is None or row["unavailable_at"] is not None:
                    unavailable = True
                elif row["expires_at"] <= row["current_at"]:
                    conn.execute(
                        "UPDATE app.sa_coding_payloads SET ciphertext=NULL,"
                        "unavailable_at=clock_timestamp(),reason_code='TTL_EXPIRED',"
                        "revision=revision+1,updated_at=clock_timestamp() WHERE payload_id=%s",
                        (row["payload_id"],),
                    )
                    self._event(
                        conn, "PAYLOAD_ERASED", operation["operation_id"], payload_kind=kind
                    )
                    unavailable = True
                else:
                    ciphertext = bytes(row["ciphertext"])
                    if sha256(ciphertext).hexdigest() != row["ciphertext_sha256"]:
                        raise _deny("SOURCE_CODING_PAYLOAD_INTEGRITY")
                    clear = self.core.cipher.decrypt(ciphertext, self._payload_binding(row, kind))
                    if (
                        len(clear) != row["plaintext_bytes"]
                        or sha256(clear).hexdigest() != row["content_sha256"]
                    ):
                        raise _deny("SOURCE_CODING_PAYLOAD_INTEGRITY")
        except DomainError:
            # A revocation committed between maintenance and the read still
            # gets its own durable erasure after the rejected read rolls back.
            self.core.maintain(coding_id=self.request.coding_id)
            raise
        if unavailable or clear is None:
            raise _deny("SOURCE_CODING_PAYLOAD_UNAVAILABLE")
        return clear

    def record_verification(
        self,
        operation: dict[str, Any],
        *,
        result: str,
        receipt_sha256: str,
        execution_operation_id: UUID | None,
        provenance_sha256: str,
        tests_run: int,
        tests_failed: int,
    ) -> None:
        with self.core.engine.transaction() as conn:
            mandate, _ = self.core.current(
                conn, self.bearer, self.request, tree_sha256=operation["tree_sha256"]
            )
            self._known(conn, operation, "ok" if result == "passed" else "failed", receipt_sha256)
            previous = conn.execute(
                "SELECT * FROM app.sa_coding_verifications WHERE operation_id=%s",
                (operation["operation_id"],),
            ).fetchone()
            if previous is not None:
                if previous["receipt_sha256"] != receipt_sha256:
                    raise _deny("SOURCE_CODING_VERIFICATION_CONFLICT")
                return
            conn.execute(
                "INSERT INTO app.sa_coding_verifications(verification_id,operation_id,"
                "execution_operation_id,attempt_id,coding_id,project_id,"
                "operation_sha256,mandate_sha256,receipt_sha256,tree_sha256,environment_sha256,profile_sha256,acceptance_sha256,"
                "provenance_sha256,result,tests_run,tests_failed,observed_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp())",
                (
                    uuid4(),
                    operation["operation_id"],
                    execution_operation_id,
                    self.attempt_id,
                    self.request.coding_id,
                    mandate.target.project_id,
                    operation["request_sha256"],
                    mandate.mandate_sha256,
                    receipt_sha256,
                    operation["tree_sha256"],
                    mandate.grant.environment_sha256,
                    mandate.worker_profile_sha256,
                    mandate.acceptance_sha256,
                    provenance_sha256,
                    result,
                    tests_run,
                    tests_failed,
                ),
            )
            self._event(
                conn,
                "VERIFICATION_RECORDED",
                operation["operation_id"],
                verification_result=result,
                receipt_sha256=receipt_sha256,
                tree_sha256=operation["tree_sha256"],
                tests_run=tests_run,
                tests_failed=tests_failed,
            )

    def finish(self, tree_sha256: str) -> None:
        with self.core.engine.transaction() as conn:
            self.core.current(conn, self.bearer, self.request, tree_sha256=tree_sha256)
            row = conn.execute(
                "UPDATE app.sa_coding_attempts SET state='result_recorded',revision=revision+1,"
                "updated_at=clock_timestamp() "
                "WHERE attempt_id=%s AND state='running' RETURNING *",
                (self.attempt_id,),
            ).fetchone()
            if row is not None:
                self._event(
                    conn,
                    "ATTEMPT_RESULT_RECORDED",
                    state="result_recorded",
                    tree_sha256=tree_sha256,
                )
