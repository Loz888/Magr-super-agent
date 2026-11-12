"""Shared budget constraints; original usage/router ledgers remain monetary truth."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import TypeAdapter, ValidationError

from mgar_v3.domain import DomainError, digest
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.super_agent.budget_domain import (
    BudgetDefinition,
    BudgetId,
    BudgetSave,
    Period,
    PriceQuote,
    window_bounds,
)

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine

Row = dict[str, Any]
COMPONENTS = {"router", "worker", "verification", "recall", "subtask"}
IDENTIFIER = TypeAdapter(BudgetId)
QUOTAS: dict[str, tuple[str, Period | None]] = {
    "candidate": ("candidates_hour", "hour"),
    "source": ("source_minute", "minute"),
    "notification": ("notifications_hour", "hour"),
    "queue": ("queue", None),
    "slot": ("slots", None),
}


class SharedBudgets:
    def __init__(self, engine: Engine):
        self.engine = engine

    @staticmethod
    def now(conn: Connection) -> datetime:
        row = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        assert row is not None and isinstance(row["now"], datetime)
        return row["now"]

    @staticmethod
    def quote(profile: Any) -> PriceQuote:
        keys = (
            "model_id",
            "price_version",
            "input_micros_per_million",
            "output_micros_per_million",
        )
        return PriceQuote.model_validate(
            {k: profile[k] if isinstance(profile, dict) else getattr(profile, k) for k in keys}
        )

    @staticmethod
    def _ready(conn: Connection) -> bool:
        row = conn.execute(
            "SELECT to_regclass('app.sa_budget_policies') IS NOT NULL AS ready"
        ).fetchone()
        assert row is not None
        return bool(row["ready"])

    @staticmethod
    def _key(kind: str, key: str) -> None:
        if kind not in {"organization", "project"}:
            raise DomainError("BUDGET_SCOPE_INVALID", "Invalid budget scope", 422)
        try:
            IDENTIFIER.validate_python(key, strict=True)
        except ValidationError:
            raise DomainError("BUDGET_SCOPE_INVALID", "Invalid budget scope", 422) from None

    def _project(self, conn: Connection) -> Row | None:
        if not self._ready(conn):
            return None
        return conn.execute(
            "SELECT * FROM app.sa_budget_policies WHERE scope_type='project' AND scope_id=%s",
            (self.engine.settings.project_id,),
        ).fetchone()

    def configured(self, conn: Connection) -> bool:
        return self._project(conn) is not None

    def _scope(self, conn: Connection, kind: str, key: str, *, create: bool = False) -> None:
        self._key(kind, key)
        project = self._project(conn)
        if kind == "project":
            allowed = key == self.engine.settings.project_id
        else:
            allowed = (project is None and create) or (
                project is not None and project["organization_id"] == key
            )
        if not allowed:
            raise DomainError("BUDGET_SCOPE_DENIED", "Budget is outside this server project", 403)

    def _configs(self, conn: Connection) -> list[tuple[Row, BudgetDefinition]]:
        project = self._project(conn)
        if project is None:
            return []
        organization = conn.execute(
            "SELECT * FROM app.sa_budget_policies WHERE scope_type='organization' "
            "AND scope_id=%s FOR UPDATE",
            (project["organization_id"],),
        ).fetchone()
        if organization is None:
            raise DomainError("BUDGET_ORGANIZATION_MISSING", "Project budget has no organization")
        project = conn.execute(
            "SELECT * FROM app.sa_budget_policies WHERE scope_type='project' AND scope_id=%s "
            "FOR UPDATE",
            (self.engine.settings.project_id,),
        ).fetchone()
        assert project is not None
        return [
            (r, BudgetDefinition.model_validate(r["definition"])) for r in (organization, project)
        ]

    def _active(self, definition: BudgetDefinition, at: datetime) -> None:
        if not definition.enabled or not definition.valid_from <= at < definition.valid_until:
            raise DomainError(
                "BUDGET_DISABLED_OR_EXPIRED", "Shared budget is disabled or expired", 403
            )

    def _audit(self, conn: Connection, event: str, **details: Any) -> None:
        conn.execute(
            "INSERT INTO app.sa_budget_audit(project_id,event,details) VALUES(%s,%s,%s)",
            (self.engine.settings.project_id, event, Jsonb(details)),
        )

    def save(self, cookie: str, scope_type: str, scope_id: str, body: BudgetSave) -> Row:
        with self.engine.transaction() as conn:
            user = SourceIdentities(self.engine)._console(conn, cookie)
            self._scope(conn, scope_type, scope_id, create=True)
            definition = body.definition
            if scope_type == "project":
                if (
                    definition.organization_id is None
                    or not conn.execute(
                        "SELECT 1 FROM app.sa_budget_policies WHERE scope_type='organization' "
                        "AND scope_id=%s",
                        (definition.organization_id,),
                    ).fetchone()
                ):
                    raise DomainError(
                        "BUDGET_ORGANIZATION_MISSING", "Configure the organization first"
                    )
            elif definition.organization_id is not None:
                raise DomainError("BUDGET_SCOPE_INVALID", "Organization cannot have a parent", 422)
            old = conn.execute(
                "SELECT * FROM app.sa_budget_policies WHERE scope_type=%s "
                "AND scope_id=%s FOR UPDATE",
                (scope_type, scope_id),
            ).fetchone()
            if (old["revision"] if old else 0) != body.expected_revision:
                raise DomainError("BUDGET_REVISION_CONFLICT", "Reload budget before saving", 409)
            if old and old["organization_id"] != definition.organization_id:
                raise DomainError(
                    "BUDGET_ORGANIZATION_IMMUTABLE", "Project organization is immutable"
                )
            revision = body.expected_revision + 1
            document = definition.model_dump(mode="json")
            values = (
                scope_type,
                scope_id,
                definition.organization_id,
                revision,
                Jsonb(document),
                digest(document),
                user,
                body.review_ref,
            )
            conn.execute(
                "INSERT INTO app.sa_budget_versions(scope_type,scope_id,organization_id,revision,"
                "definition,definition_sha256,signed_by,review_ref) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                values,
            )
            if old is None:
                conn.execute(
                    "INSERT INTO app.sa_budget_policies(scope_type,scope_id,organization_id,"
                    "revision,"
                    "definition,definition_sha256,signed_by,review_ref) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    values,
                )
            else:
                conn.execute(
                    "UPDATE app.sa_budget_policies SET revision=%s,definition=%s,"
                    "definition_sha256=%s,signed_by=%s,review_ref=%s,signed_at=now() "
                    "WHERE scope_type=%s AND scope_id=%s",
                    (
                        revision,
                        Jsonb(document),
                        digest(document),
                        user,
                        body.review_ref,
                        scope_type,
                        scope_id,
                    ),
                )
            self._audit(
                conn, "budget_saved", scope_type=scope_type, scope_id=scope_id, revision=revision
            )
            return self._inspect(conn, scope_type, scope_id)

    def _projects(self, conn: Connection, row: Row) -> list[str]:
        if row["scope_type"] == "project":
            return [row["scope_id"]]
        return [
            r["scope_id"]
            for r in conn.execute(
                "SELECT scope_id FROM app.sa_budget_policies WHERE scope_type='project' "
                "AND organization_id=%s",
                (row["scope_id"],),
            ).fetchall()
        ]

    def _calls(self, conn: Connection, projects: list[str]) -> list[Row]:
        if not projects:
            return []
        return conn.execute(
            "SELECT 'usage' AS ledger_kind,u.call_id,u.run_id,h.project_id,u.status,"
            "u.reserved_micros,u.settled_micros,u.usage,b.component,"
            "coalesce(b.reserved_tokens,nullif(u.token_reservation,0)) AS reserved_tokens,"
            "b.observed_at FROM app.usage_calls u JOIN app.runs r USING(run_id) "
            "JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id) "
            "LEFT JOIN app.sa_budget_calls b ON b.ledger_kind='usage' AND b.call_id=u.call_id "
            "WHERE h.project_id=ANY(%s) UNION ALL "
            "SELECT 'router',u.call_id,NULL,q.project_id,u.status,u.reserved_micros,"
            "u.settled_micros,u.usage,b.component,b.reserved_tokens,b.observed_at "
            "FROM app.router_calls u JOIN app.router_batches q USING(batch_id) "
            "LEFT JOIN app.sa_budget_calls b ON b.ledger_kind='router' AND b.call_id=u.call_id "
            "WHERE q.project_id=ANY(%s)",
            (projects, projects),
        ).fetchall()

    def _totals(
        self,
        definition: BudgetDefinition,
        calls: list[Row],
        at: datetime,
        run_id: UUID | None = None,
    ) -> dict[str, int]:
        result = {
            k: 0
            for k in (
                "total_micros",
                "hour_micros",
                "day_micros",
                "router_day_micros",
                "total_tokens",
                "hour_tokens",
                "day_tokens",
                "run_micros",
                "run_tokens",
                "unknown_calls",
                "unknown_token_calls",
            )
        }
        hour = window_bounds(at, definition.timezone, "hour")
        day = window_bounds(at, definition.timezone, "day")
        for call in calls:
            unknown = call["status"] != "known"
            cost = call["reserved_micros"] if unknown else call["settled_micros"]
            # Historical settlement accepts JSON of any shape. A non-object is
            # not a token receipt and must retain its reliable reservation.
            usage = call["usage"] if isinstance(call["usage"], dict) else {}
            prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
            tokens_known = not unknown and all(
                type(n) is int and n >= 0 for n in (prompt, completion)
            )
            if tokens_known:
                assert isinstance(prompt, int) and isinstance(completion, int)
                tokens = prompt + completion
            else:
                tokens = call["reserved_tokens"] or definition.caps.total_tokens + 1
            observed = call["observed_at"]
            in_hour = observed is None or hour[0] <= observed < hour[1]
            in_day = observed is None or day[0] <= observed < day[1]
            result["total_micros"] += cost
            result["total_tokens"] += tokens
            result["hour_micros"] += cost if unknown or in_hour else 0
            result["day_micros"] += cost if unknown or in_day else 0
            result["hour_tokens"] += tokens if not tokens_known or in_hour else 0
            result["day_tokens"] += tokens if not tokens_known or in_day else 0
            if call["ledger_kind"] == "router":
                result["router_day_micros"] += cost if unknown or in_day else 0
            if run_id is not None and call["run_id"] == run_id:
                result["run_micros"] += cost
                result["run_tokens"] += tokens
            result["unknown_calls"] += int(unknown)
            result["unknown_token_calls"] += int(not tokens_known)
        return result

    def _inspect(self, conn: Connection, kind: str, key: str) -> Row:
        row = conn.execute(
            "SELECT * FROM app.sa_budget_policies WHERE scope_type=%s AND scope_id=%s", (kind, key)
        ).fetchone()
        if row is None:
            return {
                "configured": False,
                "current": {"revision": 0, "definition": None},
                "usage": {},
                "quota_usage": {},
                "router_semantics": "NOT_RUN_NO_SHARED_BUDGET",
            }
        definition = BudgetDefinition.model_validate(row["definition"])
        at = self.now(conn)
        usage = self._totals(definition, self._calls(conn, self._projects(conn, row)), at)
        return {
            "configured": True,
            "current": row,
            "usage": usage,
            "quota_usage": {
                kind: self._quota_used(conn, row, definition, kind, at) for kind in QUOTAS
            },
            "router_semantics": "NOT_RUN; budget does not invoke a model",
        }

    def inspect(self, cookie: str, scope_type: str, scope_id: str) -> Row:
        with self.engine.transaction() as conn:
            SourceIdentities(self.engine)._console(conn, cookie)
            self._scope(conn, scope_type, scope_id)
            return self._inspect(conn, scope_type, scope_id)

    def reserve(
        self,
        conn: Connection,
        ledger_kind: str,
        call_id: UUID,
        *,
        price: PriceQuote,
        tokens: int,
        component: str = "worker",
    ) -> None:
        configs = self._configs(conn)
        if not configs:
            return  # No new project policy: legacy grants/ledgers remain unchanged.
        if (
            ledger_kind not in {"usage", "router"}
            or component not in COMPONENTS
            or type(tokens) is not int
            or not 0 < tokens <= 2**63 - 1
        ):
            raise DomainError("BUDGET_CALL_INVALID", "Invalid internal budget reservation", 422)
        price = PriceQuote.model_validate(price)
        previous = conn.execute(
            "SELECT * FROM app.sa_budget_calls WHERE ledger_kind=%s AND call_id=%s",
            (ledger_kind, call_id),
        ).fetchone()
        if previous:
            if previous["price"] != price.model_dump(mode="json") or (
                previous["reserved_tokens"],
                previous["component"],
                previous["project_id"],
            ) != (tokens, component, self.engine.settings.project_id):
                raise DomainError("BUDGET_CALL_CONFLICT", "Call attribution already differs", 409)
            return
        at = self.now(conn)
        run_id = None
        for row, definition in configs:
            self._active(definition, at)
            if price not in definition.prices:
                raise DomainError("BUDGET_PRICE_DENIED", "Price differs from signed budget", 403)
            calls = self._calls(conn, self._projects(conn, row))
            target = next(
                (c for c in calls if c["ledger_kind"] == ledger_kind and c["call_id"] == call_id),
                None,
            )
            if target is None or target["project_id"] != self.engine.settings.project_id:
                raise DomainError("BUDGET_CALL_SCOPE", "Call is outside this project ledger", 403)
            if target["status"] != "reserved":
                raise DomainError(
                    "BUDGET_CALL_STATE", "Only a newly reserved call may be attributed"
                )
            target.update(observed_at=at, reserved_tokens=tokens, component=component)
            run_id = target["run_id"]
            usage = self._totals(definition, calls, at, run_id)
            for name, limit in definition.caps.model_dump().items():
                if name.startswith("run_") and run_id is None:
                    continue
                if usage[name] > limit:
                    raise DomainError(
                        "PROJECT_BUDGET_EXHAUSTED", "Shared budget limit exhausted", 403
                    )
        conn.execute(
            "INSERT INTO app.sa_budget_calls(ledger_kind,call_id,project_id,organization_id,"
            "component,reserved_tokens,price,observed_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                ledger_kind,
                call_id,
                self.engine.settings.project_id,
                configs[0][0]["scope_id"],
                component,
                tokens,
                Jsonb(price.model_dump(mode="json")),
                at,
            ),
        )

    def model_available(self, conn: Connection) -> bool:
        configs = self._configs(conn)
        if not configs:
            return False
        at = self.now(conn)
        try:
            for row, definition in configs:
                self._active(definition, at)
                usage = self._totals(definition, self._calls(conn, self._projects(conn, row)), at)
                if any(
                    usage[k] >= getattr(definition.caps, k)
                    for k in (
                        "total_micros",
                        "hour_micros",
                        "day_micros",
                        "total_tokens",
                        "hour_tokens",
                        "day_tokens",
                    )
                ):
                    return False
        except DomainError:
            return False
        return True

    def _quota_used(
        self,
        conn: Connection,
        row: Row,
        definition: BudgetDefinition,
        kind: str,
        at: datetime,
        source_hash: str | None = None,
    ) -> int:
        period = QUOTAS[kind][1]
        start, end = (
            window_bounds(at, definition.timezone, period)
            if period
            else (at - timedelta(days=365000), at + timedelta(days=365000))
        )
        projects = self._projects(conn, row)
        result = conn.execute(
            "SELECT coalesce(sum(units),0) AS used FROM app.sa_budget_quota "
            "WHERE project_id=ANY(%s) AND kind=%s AND "
            "((%s AND released_at IS NULL) OR (NOT %s AND observed_at>=%s AND observed_at<%s)) "
            "AND (%s::text IS NULL OR source_hash=%s)",
            (projects, kind, period is None, period is None, start, end, source_hash, source_hash),
        ).fetchone()
        assert result is not None
        return int(result["used"])

    def quota(
        self,
        conn: Connection,
        kind: str,
        operation_key: str,
        *,
        units: int = 1,
        source_id: UUID | None = None,
    ) -> bool:
        configs = self._configs(conn)
        if not configs:
            return False
        if (
            kind not in QUOTAS
            or type(units) is not int
            or not 0 < units <= 2**63 - 1
            or (not isinstance(operation_key, str) or not 1 <= len(operation_key) <= 256)
        ):
            raise DomainError("PROJECT_QUOTA_INVALID", "Invalid internal quota request", 422)
        if kind == "source" and source_id is None:
            raise DomainError("PROJECT_QUOTA_SCOPE", "Source quota requires a verified source", 403)
        if (
            source_id is not None
            and not conn.execute(
                "SELECT 1 FROM app.source_instances WHERE source_instance_id=%s AND project_id=%s",
                (source_id, self.engine.settings.project_id),
            ).fetchone()
        ):
            raise DomainError("PROJECT_QUOTA_SCOPE", "Source is outside this project", 403)
        operation_hash = digest(operation_key)
        source_hash = digest(str(source_id)) if source_id is not None else None
        fingerprint = digest([kind, operation_key, units, source_hash])
        previous = conn.execute(
            "SELECT fingerprint FROM app.sa_budget_quota WHERE project_id=%s AND kind=%s "
            "AND operation_hash=%s",
            (self.engine.settings.project_id, kind, operation_hash),
        ).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise DomainError("PROJECT_QUOTA_CONFLICT", "Quota operation already differs", 409)
            return False
        at = self.now(conn)
        for row, definition in configs:
            self._active(definition, at)
            used = self._quota_used(conn, row, definition, kind, at)
            if used + units > getattr(definition.quotas, QUOTAS[kind][0]):
                raise DomainError("PROJECT_QUOTA_EXHAUSTED", "Shared project quota exhausted", 429)
        conn.execute(
            "INSERT INTO app.sa_budget_quota(project_id,kind,operation_hash,organization_id,"
            "source_hash,units,fingerprint,observed_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                self.engine.settings.project_id,
                kind,
                operation_hash,
                configs[0][0]["scope_id"],
                source_hash,
                units,
                fingerprint,
                at,
            ),
        )
        return True

    def release_quota(self, conn: Connection, kind: str, operation_key: str) -> None:
        if kind not in {"queue", "slot"}:
            raise DomainError(
                "PROJECT_QUOTA_RELEASE_DENIED", "Window quotas cannot be refunded", 403
            )
        if self._ready(conn):
            conn.execute(
                "UPDATE app.sa_budget_quota SET released_at=coalesce(released_at,%s) "
                "WHERE project_id=%s AND kind=%s AND operation_hash=%s AND released_at IS NULL",
                (self.now(conn), self.engine.settings.project_id, kind, digest(operation_key)),
            )

    def notification_quota(self, conn: Connection, action_id: str) -> None:
        configs = self._configs(conn)
        if configs:
            at = self.now(conn)
            windows = [
                (
                    r["scope_type"],
                    r["scope_id"],
                    window_bounds(at, d.timezone, "hour")[0].isoformat(),
                )
                for r, d in configs
            ]
            self.quota(conn, "notification", action_id + ":" + digest(windows))
