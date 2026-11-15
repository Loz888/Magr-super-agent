"""Bounded adapters to original Scheduler, Autonomy, Dispatcher and async Worker.

The service process triggers these ports over HTTP. Only the original Engine
writes business state; project/tool processes never receive an Owner session.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from mgar_v3.super_agent.source_operations import OperationsPort, TickOutcome, denied

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine
    from mgar_v3.super_agent.source_async_runtime import SourceAsyncRuntime


@contextmanager
def delegation(engine: Engine, guard: Callable[[], None]) -> Iterator[str]:
    """Short in-process bridge to original Owner-only deterministic methods.

    It is bound to the current operations signer and expires in <=60 seconds;
    the bearer is never returned through a service API or written to a file.
    Original policies/Mandates/budgets remain mandatory for resulting work.
    """
    guard()
    token = secrets.token_urlsafe(48)
    hashed = hashlib.sha256(token.encode()).hexdigest()
    with engine.transaction() as conn:
        row, definition = engine.source_operations._current(conn)
        if row is None or definition is None:
            raise denied("INACTIVE", 403)
        now = engine.source_operations.now(conn)
        conn.execute(
            "INSERT INTO app.web_sessions(token_hash,user_id,csrf_token,expire"
            "s_at) VALUES(%s,%s,%s,%s)",
            (
                hashed,
                row["signed_by"],
                secrets.token_urlsafe(32),
                min(now + timedelta(seconds=60), definition.valid_until),
            ),
        )
    try:
        yield token
    finally:
        with engine.transaction() as conn:
            conn.execute("DELETE FROM app.web_sessions WHERE token_hash=%s", (hashed,))


class OriginalSchedulerPort(OperationsPort):
    role = "scheduler"

    def __init__(self, engine: Engine):
        self.engine = engine

    def invoke(self, guard: Callable[[], None]) -> TickOutcome:
        from mgar_v3.super_agent.source_autonomy_domain import AutonomyCycleRequest

        guard()
        maintenance = self.engine.source_scheduler.maintain()
        with delegation(self.engine, guard) as cookie:
            command = AutonomyCycleRequest(limit=16)
            guard()
            timed = self.engine.source_autonomy.tick(cookie, command)
            guard()
            admitted = self.engine.source_autonomy.cycle(cookie, command)
        return TickOutcome(
            processed=len(timed.get("outcomes", [])) + len(admitted.get("outcomes", [])),
            unknown=maintenance["unknown"],
            blocked=maintenance["blocked"],
        )


class OriginalDispatcherPort(OperationsPort):
    role = "dispatcher"

    def __init__(self, engine: Engine):
        self.engine = engine

    def invoke(self, guard: Callable[[], None]) -> TickOutcome:
        from mgar_v3.effects.dispatcher import Dispatcher
        from mgar_v3.super_agent.source_notification_transport import NotificationTransport

        engine = self.engine

        class GuardedNotificationTransport(NotificationTransport):
            def send(self, action: dict[str, Any]) -> str:
                guard()
                return super().send(action)

        guard()
        action_id = Dispatcher(
            engine, GuardedNotificationTransport(engine.source_notifications)
        ).tick()
        # Dispatcher itself persists accepted/rejected/unknown outcomes. A known
        # tick receipt says the bounded attempt finished, not "message sent".
        return TickOutcome(processed=int(action_id is not None))


class OriginalAsyncWorkerPort(OperationsPort):
    role = "worker"

    def __init__(self, engine: Engine, runtime: SourceAsyncRuntime, session_id: UUID, bearer: str):
        from mgar_v3.super_agent.source_async_runtime import SourceAsyncRuntime

        if not isinstance(runtime, SourceAsyncRuntime) or runtime.service.engine is not engine:
            raise denied("ORIGINAL_WORKER_REQUIRED", 422)
        self.engine, self.runtime = engine, runtime
        self.session_id, self._bearer = session_id, bearer

    def invoke(self, guard: Callable[[], None]) -> TickOutcome:
        from mgar_v3.super_agent.source_coding_domain import CodingRunRequest

        guard()
        with self.engine.transaction() as conn:
            # Start only an already-authorized ready attempt from the original
            # Scheduler/SA14, never manufacture a session, lease or task here.
            row = conn.execute(
                "SELECT b.async_id,b.coding_id,b.run_id,a.assignment_id,a.execution_epoch,"
                "a.revision FROM app.sa_async_bindings b JOIN app.sa_coding_attempts a "
                "USING(coding_id,project_id,run_id,assignment_id,session_id,execution_epoch) "
                "WHERE b.project_id=%s AND b.session_id=%s AND b.state='active' "
                "AND a.state='ready' ORDER BY a.created_at LIMIT 1",
                (self.engine.settings.project_id, self.session_id),
            ).fetchone()
        if row is None:
            return TickOutcome()
        guard()
        self.runtime.start(
            self._bearer,
            CodingRunRequest(
                request_id=uuid4(),
                coding_id=row["coding_id"],
                run_id=row["run_id"],
                assignment_id=row["assignment_id"],
                session_id=self.session_id,
                epoch=row["execution_epoch"],
                expected_revision=row["revision"],
            ),
            row["async_id"],
        )
        return TickOutcome(processed=1)


def create_application() -> Any:
    """Production entrypoint: sole controller; Worker/runtime activation is explicit.

    Install a reviewed composition calling create_app(run_background=False) and
    binding original scoped Worker/source profiles to activate those capabilities.
    Secret/config presence or this factory never creates platform/model authority.
    """
    from mgar_v3.api import create_app
    from mgar_v3.settings import Settings

    return create_app(Settings(), run_background=False)
