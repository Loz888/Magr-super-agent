"""Owner-signed, bodyless delegations on the single original Engine connection."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.super_agent.source_autonomy_domain import AutonomyRule, AutonomyRuleSave
from mgar_v3.super_agent.source_autonomy_profiles import AutonomyProfile

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.super_agent.source_coding_domain import CodingMandate

Row = dict[str, Any]


def error(code: str, status: int = 403) -> DomainError:
    return DomainError("AUTONOMY_" + code, "Current reviewed Source delegation required", status)


def decode_rule(row: Row) -> AutonomyRule:
    rule = AutonomyRule.model_validate_json(json.dumps(row["definition"]))
    if rule.definition_sha256 != row["definition_sha256"]:
        raise error("RULE_INVALID")
    return rule


class AutonomyStore:
    engine: Engine
    _profiles: dict[str, AutonomyProfile]

    @property
    def project(self) -> str:
        return self.engine.settings.project_id

    def _conn(self, conn: Connection) -> None:
        if conn is not self.engine.connection:
            raise error("ORIGINAL_CONNECTION_REQUIRED")
        if conn.info.transaction_status != TransactionStatus.INTRANS:
            raise error("ORIGINAL_TRANSACTION_REQUIRED")

    def _ready(self, conn: Connection) -> bool:
        self._conn(conn)
        row = conn.execute("SELECT to_regclass('app.sa_autonomy_rules') AS t").fetchone()
        return bool(row and row["t"])

    def _console(self, conn: Connection, cookie: str) -> UUID:
        self._conn(conn)
        user = self.engine.source_correlations.identities._console(conn, cookie)
        if not self._ready(conn):
            raise error("NOT_MIGRATED", 409)
        return user

    def now(self, conn: Connection) -> datetime:
        self._conn(conn)
        return self.engine.source_ingress.now(conn)

    def _rule(self, conn: Connection, rule_id: UUID, revision: int | None = None) -> Row:
        self._conn(conn)
        row = conn.execute(
            "SELECT v.*,h.last_local_date,h.created_at FROM app.sa_autonomy_rules h "
            "JOIN app.sa_autonomy_rule_versions v ON v.rule_id=h.rule_id "
            "AND v.revision=coalesce(%s,h.revision) WHERE h.rule_id=%s AND h.project_id=%s "
            "FOR UPDATE OF h",
            (revision, rule_id, self.project),
        ).fetchone()
        if row is None:
            raise error("RULE_NOT_FOUND", 404)
        decode_rule(row)
        return row

    def _rule_policy(self, conn: Connection, rule: AutonomyRule) -> Row:
        p = self.engine.source_policies._current(conn, rule.policy_id)
        d, at = p["definition"], self.now(conn)
        if (
            p["source_instance_id"] != rule.source_instance_id
            or p["current_revision"] != rule.policy_revision
            or p["definition_sha256"] != rule.policy_sha256
            or p["revoked"]
            or d["admission"]["rule_ref"] != rule.rule_ref
            or d["admission"]["rule_sha256"] != rule.rule_sha256
            or "team_rule" not in d["admission"]["request_kinds"]
            or not d["admission"]["enabled"]
            or not d["execution"]["enabled"]
            or not d["receive"]["enabled"]
            or not d["receive"]["model_input"]
            or (rule.mode != "disabled" and d["mode"] != rule.mode)
            or datetime.fromisoformat(d["valid_from"]) > rule.valid_from
            or datetime.fromisoformat(d["valid_until"]) < rule.valid_until
            or not rule.valid_from <= at < rule.valid_until
        ):
            raise error("RULE_POLICY_CHANGED")
        return p

    def _profile(self, rule: AutonomyRule) -> AutonomyProfile:
        profile = self._profiles.get(rule.profile_sha256)
        if profile is None:
            raise error("EXECUTOR_NOT_CONFIGURED", 409)
        if (
            profile.objective_sha256 != rule.objective_sha256
            or profile.repository != rule.repository
            or (rule.mode == "auto_investigate" and profile.mode != "readonly")
            or (rule.mode == "auto_repair" and profile.mode != "coding")
        ):
            raise error("PROFILE_CHANGED")
        return profile

    def register_profile(self, profile: AutonomyProfile) -> None:
        if not isinstance(profile, AutonomyProfile):
            raise error("TRUSTED_PROFILE_REQUIRED", 422)
        profile.validate()
        if profile.repository != self.engine.settings.repository.lower():
            raise error("PROFILE_SCOPE_DENIED")
        old = self._profiles.get(profile.profile_sha256)
        if old is not None and old != profile:
            raise error("PROFILE_IMMUTABLE", 409)
        if profile.coding is not None:
            self.engine.source_coding.register_profile(profile.coding)
        self._profiles[profile.profile_sha256] = profile

    def _event(
        self,
        conn: Connection,
        rule: Row,
        kind: str,
        reason: str,
        *,
        claim_id: UUID | None = None,
        attempt_id: UUID | None = None,
        tick_id: UUID | None = None,
    ) -> None:
        self._conn(conn)
        # Events have no free-text payload, model report, plaintext or success field.
        conn.execute(
            "INSERT INTO app.sa_autonomy_events(project_id,rule_id,kind,reason_code,"
            "claim_id,attempt_id,tick_id) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (self.project, rule["rule_id"], kind, reason, claim_id, attempt_id, tick_id),
        )

    def _save(
        self,
        conn: Connection,
        user: UUID,
        rule_id: UUID,
        request: AutonomyRuleSave,
        *,
        disabled: bool = False,
    ) -> Row:
        self._conn(conn)
        old = conn.execute(
            "SELECT * FROM app.sa_autonomy_rules WHERE rule_id=%s AND project_id=%s FOR UPDATE",
            (rule_id, self.project),
        ).fetchone()
        definition = request.definition
        if (old["revision"] if old else 0) != request.expected_revision:
            raise error("REVISION_CONFLICT", 409)
        if old and old["source_instance_id"] != definition.source_instance_id:
            raise error("RULE_IDENTITY_IMMUTABLE", 409)
        if not disabled:
            policy = self._rule_policy(conn, definition)
            if policy["signed_by"] != user:
                raise error("SIGNER_CHANGED")
            if definition.mode in {"auto_investigate", "auto_repair"}:
                self._profile(definition)
        if old is None:
            conn.execute(
                "INSERT INTO app.sa_autonomy_rules(rule_id,project_id,source_instance_id) "
                "VALUES(%s,%s,%s)",
                (rule_id, self.project, definition.source_instance_id),
            )
        revision = request.expected_revision + 1
        d = definition.model_dump(mode="json")
        conn.execute(
            "INSERT INTO app.sa_autonomy_rule_versions(rule_id,revision,project_id,"
            "source_instance_id,policy_id,policy_revision,policy_sha256,rule_sha256,profile_sha256,"
            "definition,definition_sha256,mode,valid_from,valid_until,signed_by,review_sha256) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                rule_id,
                revision,
                self.project,
                definition.source_instance_id,
                definition.policy_id,
                definition.policy_revision,
                definition.policy_sha256,
                definition.rule_sha256,
                definition.profile_sha256,
                Jsonb(d),
                definition.definition_sha256,
                definition.mode,
                definition.valid_from,
                definition.valid_until,
                user,
                digest(request.review_ref),
            ),
        )
        if old:
            conn.execute(
                "UPDATE app.sa_autonomy_rules SET revision=%s,updated_at=clock_timestamp() "
                "WHERE rule_id=%s",
                (revision, rule_id),
            )
        saved = self._rule(conn, rule_id)
        self._event(
            conn,
            saved,
            "rule_disabled" if disabled else "rule_saved",
            "AUTONOMY_RULE_DISABLED" if disabled else "AUTONOMY_RULE_REVIEWED",
        )
        return saved

    def _delegation(self, conn: Connection, attempt: Row) -> tuple[Row, AutonomyRule]:
        self._conn(conn)
        row = self._rule(conn, attempt["rule_id"])
        definition = decode_rule(row)
        if (
            row["revision"] != attempt["rule_revision"]
            or attempt["project_id"] != self.project
            or not conn.execute(
                "SELECT app.sa_autonomy_run_current(%s) AS ok", (attempt["run_id"],)
            ).fetchone()["ok"]
        ):
            raise error("AUTHORITY_REVOKED")
        self._rule_policy(conn, definition)
        source = self.engine.source_correlations._input(conn, attempt["input_id"])
        if source.ref.state_sha256 != attempt["source_state_sha256"]:
            raise error("SOURCE_CHANGED")
        return row, definition

    def coding_current(self, conn: Connection, mandate: CodingMandate) -> bool:
        if not self._ready(conn):
            return False
        attempt = conn.execute(
            "SELECT * FROM app.sa_autonomy_attempts WHERE run_id=%s AND project_id=%s",
            (mandate.target.run_id, self.project),
        ).fetchone()
        if attempt is None:
            return False
        row, rule = self._delegation(conn, attempt)
        if (
            attempt["original_kind"] != "coding"
            or mandate.mandate_sha256 != attempt["mandate_sha256"]
            or mandate.target.mandate_id != attempt["mandate_id"]
            or mandate.signed_by != row["signed_by"]
            or (mandate.review_sha256 != row["review_sha256"] and attempt["reopened_from"] is None)
            or mandate.worker_profile_sha256 != rule.profile_sha256
        ):
            raise error("CODING_DELEGATION_CHANGED")
        if (
            attempt["reopened_from"] is not None
            and not conn.execute(
                "SELECT 1 FROM app.run_events e JOIN app.sa_autonomy_attempts a ON a.run_id=e.run_id "
                "WHERE a.attempt_id=%s AND e.kind='source_autonomy_reopen_reviewed' "
                "AND e.safe_payload->>'review_sha256'=%s AND e.safe_payload->>'signed_by'=%s",
                (attempt["reopened_from"], mandate.review_sha256, str(mandate.signed_by)),
            ).fetchone()
        ):
            raise error("REOPEN_REVIEW_MISSING")
        return True

    def check_run(
        self,
        conn: Connection,
        run: Row,
        capability: str | None = None,
        *,
        resume: bool = False,
    ) -> None:
        if not self._ready(conn):
            return
        attempt = conn.execute(
            "SELECT * FROM app.sa_autonomy_attempts WHERE run_id=%s AND project_id=%s",
            (run["run_id"], self.project),
        ).fetchone()
        if attempt is None:
            return
        if resume:
            raise error("EXPLICIT_REOPEN_REQUIRED")
        _, rule = self._delegation(conn, attempt)
        if run["mandate_id"] != attempt["mandate_id"]:
            raise error("MANDATE_CHANGED")
        if attempt["original_kind"] == "readonly" and capability not in {None, "read_repository"}:
            raise error("READ_ONLY")
        self._profile(rule)
