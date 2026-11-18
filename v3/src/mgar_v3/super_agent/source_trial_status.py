"""Scoped task metadata for the explicit SA22 status query."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_recall_access import RecallAccess, RecallCaller
from mgar_v3.super_agent.source_recall_rules import denied
from mgar_v3.super_agent.source_recall_store import RecallStore

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.super_agent.source_correlation import _Input

ANSWER_REASON = "SOURCE_TASK_STATUS_ANSWER"
ANSWER_EVENT = "source_task_status_available"


def parse_query(text: str) -> str | None:
    """A missing command returns None. A malformed command asks for an ID."""
    from mgar_v3.super_agent.source_coding_experiment import normalize_coding_mention

    parts = normalize_coding_mention(text).split(maxsplit=1)
    if not parts or parts[0] != "任务状态":
        return None
    if len(parts) != 2 or len(parts[1]) != 36:
        return ""
    try:
        identifier = str(UUID(parts[1]))
    except ValueError:
        return ""
    return identifier if identifier == parts[1].lower() else ""


def reply(engine: Engine, conn: Connection, cookie: str, source: _Input, target: str) -> str:
    if not target:
        return "请发送“任务状态 <原任务 ID>”。"
    try:
        return _describe(engine, conn, cookie, source, UUID(target))
    except DomainError:
        return "当前会话中没有可查看的对应任务。请核对原任务 ID。"


def _describe(engine: Engine, conn: Connection, cookie: str, source: _Input, target: UUID) -> str:
    access = RecallAccess(engine, conn, RecallCaller("owner", cookie))
    store = RecallStore(access)
    binding = conn.execute(
        "SELECT b.coding_id,b.input_id,b.run_id FROM app.sa_coding_bindings b "
        "JOIN app.runs r USING(run_id) WHERE b.project_id=%s AND (b.ticket_id=%s OR b.run_id=%s) "
        "ORDER BY r.attempt_no DESC,r.created_at DESC,r.run_id LIMIT 1",
        (source.ref.project_id, target, target),
    ).fetchone()
    if binding is None:
        raise denied()

    # The console credential is an internal port. For a group reply, every
    # contributing source must also match the current sender and native topic.
    run, _ = store._run(binding["run_id"])
    origins = store._sources(run["run_id"])
    event = source.item.event
    for input_id in origins:
        previous, item, _ = access.metadata(input_id, task_status=True)
        if (
            previous["identity_id"] != source.row["identity_id"]
            or item.event.sender != event.sender
            or item.event.source_thread != event.source_thread
        ):
            raise denied()
    if binding["input_id"] not in origins:
        raise denied()

    verification = conn.execute(
        "SELECT v.result,v.tests_run,v.tests_failed FROM app.sa_coding_verifications v "
        "WHERE v.attempt_id=(SELECT a.attempt_id FROM app.sa_coding_attempts a "
        "WHERE a.coding_id=%s ORDER BY a.created_at DESC,a.attempt_id DESC LIMIT 1) "
        "ORDER BY v.observed_at DESC,v.verification_id DESC LIMIT 1",
        (binding["coding_id"],),
    ).fetchone()
    operations = conn.execute(
        "SELECT count(*) FILTER (WHERE state='unknown') unknown,"
        "bool_or(capability='write' AND state='known' AND result_code='ok') edited "
        "FROM app.sa_coding_operations WHERE run_id=%s",
        (run["run_id"],),
    ).fetchone()
    usage = conn.execute(
        "SELECT coalesce(sum(settled_micros),0) settled,"
        "coalesce(sum(reserved_micros) FILTER (WHERE status<>'known'),0) held "
        "FROM app.usage_calls WHERE run_id=%s",
        (run["run_id"],),
    ).fetchone()
    assert operations is not None and usage is not None
    delivery = conn.execute(
        "SELECT a.status FROM app.sa_notification_intents n JOIN app.actions a USING(action_id) "
        "WHERE n.project_id=%s AND n.source_instance_id=%s AND n.input_id=%s "
        "ORDER BY n.created_at DESC,n.action_id DESC LIMIT 1",
        (source.ref.project_id, source.ref.source_instance_id, binding["input_id"]),
    ).fetchone()
    state = {
        "queued": "排队中", "running": "运行中", "paused": "已暂停", "failed": "失败",
        "waiting_input": "等待输入", "cancelled": "已取消", "completed": "已完成",
    }.get(run["status"], run["status"])
    tests = (
        f"最近一次验证记录为 {verification['result']}，检查 {verification['tests_run']} 项，"
        f"失败 {verification['tests_failed']} 项。"
        if verification else "原运行尚无最终树验证记录。"
    )
    review = "有工作区编辑记录，仍待核验。" if operations["edited"] else "尚无编辑回执。"
    notice = {
        "succeeded": "原请求回复已送达。", "outcome_unknown": "原请求回复结果待核对。",
        "failed": "原请求回复发送失败。", "cancelled": "原请求回复已取消。",
    }.get(delivery["status"], "原请求回复待发送确认。") if delivery else "尚无原请求回复记录。"
    access.check()
    return (
        f"任务 {run['ticket_id']} · {state}\n{tests}{review}\n{notice}\n"
        f"编码已结算 USD{usage['settled'] / 1000000:.6f}，预占 USD{usage['held'] / 1000000:.6f}，"
        f"未确认操作 {operations['unknown']} 项。"
    )
