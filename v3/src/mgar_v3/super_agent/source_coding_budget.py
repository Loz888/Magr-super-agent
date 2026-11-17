"""Controlled fixture accounting in the original usage and SA07 ledgers.

The injected fixture adapter is free of network/paid credentials. These rows
are still real reservations: an interrupted call cannot obtain another send.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid5

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import (
    BudgetLimits,
    BudgetReservation,
    BudgetSnapshot,
    ModelUsage,
)
from mgar_v3.super_agent.budget_domain import PriceQuote

if TYPE_CHECKING:
    from psycopg import Connection

    from mgar_v3.super_agent.source_coding_journal import SourceCodingJournal


def _deny(code: str) -> DomainError:
    return DomainError(code, "Controlled Coding accounting could not be established", 409)


class CodingBudgetLedger:
    """Kernel ledger port; model truth remains in app.usage_calls."""

    def __init__(
        self,
        journal: SourceCodingJournal,
        *,
        price: PriceQuote,
        limits: BudgetLimits,
        reserved_tokens: int,
    ) -> None:
        self.journal = journal
        self.price = price
        self.limits = limits
        self.reserved_tokens = reserved_tokens
        self._tools: dict[str, BudgetReservation] = {}
        self._issued: set[UUID] = set()
        self._prepared_tokens: dict[str, int] = {}

    def prepare_model(self, operation_id: str, tokens: int) -> None:
        """Trusted wire builder supplies a bounded reservation, never the model."""
        if type(tokens) is not int or not 0 < tokens <= self.reserved_tokens:
            raise _deny("SOURCE_CODING_TOKEN_RESERVATION_INVALID")
        previous = self._prepared_tokens.get(operation_id)
        if previous is not None and previous != tokens:
            raise _deny("SOURCE_CODING_TOKEN_RESERVATION_CONFLICT")
        self._prepared_tokens[operation_id] = tokens

    def freshly_reserved(self, call_id: UUID) -> bool:
        return call_id in self._issued

    def call_id(self, operation_id: str) -> UUID:
        return uuid5(self.journal.attempt_id, "model:" + operation_id)

    def _reservation(self, row: dict[str, Any], operation_id: str) -> BudgetReservation:
        return BudgetReservation(
            reservation_id=str(row["call_id"]),
            task_id=self.journal.task_id,
            operation_id=operation_id,
            kind="model",
            reserved_micros=row["reserved_micros"],
            reserved_units=1,
            status=cast(
                Literal["reserved", "settled", "released", "unknown"],
                {"known": "settled"}.get(row["status"], row["status"]),
            ),
            actual_micros=row["settled_micros"] or 0,
            actual_units=1 if row["status"] == "known" else 0,
        )

    def reserve(
        self,
        task_id: str,
        operation_id: str,
        kind: str,
        *,
        reserved_micros: int = 0,
        reserved_units: int = 1,
    ) -> BudgetReservation:
        if task_id != self.journal.task_id or reserved_units != 1:
            raise _deny("SOURCE_CODING_BUDGET_SCOPE")
        if kind != "model":
            if kind != "tool":
                raise _deny("BUDGET_EXCEEDED")
            key = str(uuid5(self.journal.attempt_id, "tool-budget:" + operation_id))
            value = self._tools.get(key)
            if value is None:
                with self.journal.core.engine.transaction() as conn:
                    self.journal.core.current(conn, self.journal.bearer, self.journal.request)
                    operations = conn.execute(
                        "SELECT step_key FROM app.sa_coding_operations WHERE attempt_id=%s "
                        "AND kind='tool' AND step_key NOT LIKE 'continuation:%%'",
                        (self.journal.attempt_id,),
                    ).fetchall()
                recorded = {row["step_key"] for row in operations}
                pending = {row.operation_id for row in self._tools.values()}
                if (
                    operation_id not in recorded | pending
                    and len(recorded | pending) >= self.limits.max_tool_calls
                ):
                    raise _deny("BUDGET_EXCEEDED")
                value = BudgetReservation(
                    reservation_id=key,
                    task_id=task_id,
                    operation_id=operation_id,
                    kind="tool",
                    reserved_micros=0,
                    reserved_units=1,
                )
                self._tools[key] = value
            return value
        if not 0 < reserved_micros <= self.limits.max_cost_micros:
            raise _deny("BUDGET_EXCEEDED")
        with self.journal.core.engine.transaction() as conn:
            mandate, _ = self.journal.core.current(conn, self.journal.bearer, self.journal.request)
            call_id = self.call_id(operation_id)
            previous = conn.execute(
                "SELECT * FROM app.usage_calls WHERE call_id=%s", (call_id,)
            ).fetchone()
            if previous is not None:
                if previous["reserved_micros"] != reserved_micros:
                    raise _deny("SOURCE_CODING_BUDGET_CONFLICT")
                # The adapter may replay a *known encrypted* response. Reserved
                # and unknown calls are never another permission to send.
                return self._reservation(previous, operation_id)
            counts = conn.execute(
                "SELECT count(*) AS n,coalesce(sum(CASE WHEN status='known' THEN settled_micros "
                "ELSE reserved_micros END),0) AS cost FROM app.usage_calls WHERE run_id=%s",
                (mandate.target.run_id,),
            ).fetchone()
            assert counts is not None
            if (
                counts["n"] >= self.limits.max_model_calls
                or counts["cost"] + reserved_micros > self.limits.max_cost_micros
            ):
                raise _deny("BUDGET_EXCEEDED")
            for table, key, ledger_key in (
                ("budgets", "mandate_id", mandate.target.mandate_id),
                ("authorization_budgets", "profile_hash", mandate.budget.profile_sha256),
            ):
                extra = ",used_calls=used_calls+1" if table == "authorization_budgets" else ""
                result = conn.execute(
                    f"UPDATE app.{table} SET reserved_micros=reserved_micros+%s{extra} "
                    f"WHERE {key}=%s AND reserved_micros+settled_micros+%s<=limit_micros "
                    + ("AND used_calls<call_limit " if table == "authorization_budgets" else "")
                    + f"RETURNING {key}",
                    (reserved_micros, ledger_key, reserved_micros),
                ).fetchone()
                if result is None:
                    raise _deny("BUDGET_EXCEEDED")
            conn.execute(
                "INSERT INTO app.usage_calls(call_id,run_id,step_key,status,"
                "reserved_micros,profile_hash) "
                "VALUES(%s,%s,%s,'reserved',%s,%s)",
                (
                    call_id,
                    mandate.target.run_id,
                    "sa13:" + operation_id,
                    reserved_micros,
                    mandate.budget.profile_sha256,
                ),
            )
            if not self.journal.core.engine.project_budgets.configured(conn):
                raise _deny("SOURCE_CODING_SHARED_BUDGET_REQUIRED")
            self.journal.core.engine.project_budgets.reserve(
                conn,
                "usage",
                call_id,
                price=self.price,
                tokens=self._prepared_tokens.get(operation_id, self.reserved_tokens),
                component="worker",
            )
            self._issued.add(call_id)
            return BudgetReservation(
                reservation_id=str(call_id),
                task_id=task_id,
                operation_id=operation_id,
                kind="model",
                reserved_micros=reserved_micros,
                reserved_units=1,
            )

    def settle_model(self, conn: Connection[Any], call_id: UUID, usage: ModelUsage) -> bool:
        row = conn.execute(
            "SELECT * FROM app.usage_calls WHERE call_id=%s FOR UPDATE", (call_id,)
        ).fetchone()
        if row is None:
            raise _deny("SOURCE_CODING_RESERVATION_MISSING")
        if row["status"] == "known":
            if row["settled_micros"] != usage.cost_micros:
                raise _deny("SOURCE_CODING_SETTLEMENT_CONFLICT")
            return True
        # Engine.transaction nests on its original held connection. Reuse the
        # original settlement algorithm and every original budget side ledger.
        # The raw model result stays exclusively in the encrypted payload table.
        token_receipt = usage.model_dump(mode="json")
        if usage.known and {"input_tokens", "output_tokens"} <= usage.model_fields_set:
            # SA07 consumes the original Engine usage vocabulary. Keep the
            # Harness fields as aliases; never infer tokens from monetary cost.
            token_receipt.update(
                prompt_tokens=usage.input_tokens, completion_tokens=usage.output_tokens
            )
        self.journal.core.engine.settle_call(
            call_id,
            usage.cost_micros if usage.known else None,
            usage=token_receipt,
            result=None,
        )
        current = conn.execute(
            "SELECT status FROM app.usage_calls WHERE call_id=%s", (call_id,)
        ).fetchone()
        return current is not None and current["status"] == "known"

    def settle(
        self, reservation_id: str, *, actual_micros: int, actual_units: int = 1
    ) -> BudgetReservation:
        if reservation_id in self._tools:
            tool_reservation = self._tools[reservation_id].model_copy(
                update={"status": "settled", "actual_units": actual_units}
            )
            self._tools[reservation_id] = tool_reservation
            return tool_reservation
        with self.journal.core.engine.transaction() as conn:
            known = self.settle_model(
                conn, UUID(reservation_id), ModelUsage(cost_micros=actual_micros)
            )
            row = conn.execute(
                "SELECT * FROM app.usage_calls WHERE call_id=%s", (UUID(reservation_id),)
            ).fetchone()
            assert row is not None
            value = self._reservation(row, row["step_key"][5:])
        if not known:
            raise _deny("HARNESS_MODEL_UNKNOWN")
        return value

    def mark_unknown(self, reservation_id: str, reason: str) -> BudgetReservation:
        if reservation_id in self._tools:
            tool_reservation = self._tools[reservation_id].model_copy(
                update={"status": "unknown", "reason": reason[:200]}
            )
            self._tools[reservation_id] = tool_reservation
            return tool_reservation
        with self.journal.core.engine.transaction() as conn:
            previous = conn.execute(
                "SELECT status FROM app.usage_calls WHERE call_id=%s", (UUID(reservation_id),)
            ).fetchone()
            if previous is not None and previous["status"] != "known":
                self.journal.core.engine.settle_call(UUID(reservation_id), None)
            row = conn.execute(
                "SELECT * FROM app.usage_calls WHERE call_id=%s", (UUID(reservation_id),)
            ).fetchone()
            if row is None:
                raise _deny("SOURCE_CODING_RESERVATION_MISSING")
            return self._reservation(row, row["step_key"][5:])

    def release(self, reservation_id: str) -> BudgetReservation:
        # A provider exception cannot prove that no request escaped. Retain it.
        return self.mark_unknown(reservation_id, "OUTCOME_NOT_ESTABLISHED")

    def snapshot(self, task_id: str) -> BudgetSnapshot:
        if task_id != self.journal.task_id:
            raise _deny("SOURCE_CODING_BUDGET_SCOPE")
        with self.journal.core.engine.transaction() as conn:
            row = conn.execute(
                "SELECT count(*) AS n,coalesce(sum(CASE WHEN status='known' "
                "THEN settled_micros ELSE reserved_micros END),0) AS cost "
                "FROM app.usage_calls WHERE run_id=%s",
                (self.journal.request.run_id,),
            ).fetchone()
            assert row is not None
            tools = conn.execute(
                "SELECT count(*) AS n FROM app.sa_coding_operations WHERE attempt_id=%s "
                "AND kind='tool' AND step_key NOT LIKE 'continuation:%%'",
                (self.journal.attempt_id,),
            ).fetchone()
            assert tools is not None
            return BudgetSnapshot(
                task_id=task_id,
                model_calls=row["n"],
                tool_calls=tools["n"],
                cost_micros=row["cost"],
            )
