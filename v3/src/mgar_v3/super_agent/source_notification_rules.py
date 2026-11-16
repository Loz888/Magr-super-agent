"""Deterministic minimal disclosures. No source text, logs, URLs or model output."""

import hashlib
from datetime import datetime, timedelta
from typing import Literal

from mgar_v3.domain import DomainError, digest
from mgar_v3.super_agent.policy_domain import NotificationDestination, NotificationGrant
from mgar_v3.super_agent.source_notification_domain import NotificationFacts, NotificationTemplate

_TEMPLATES = {
    "verified_result": "核验结果已记录，请在已认证控制台查看。",
    "necessary_question": "任务需要补充信息，请在已认证控制台查看待答问题。",
    "review_available": "已有核验产物可供审查，请在已认证控制台查看。",
    "critical_fault": "任务遇到重要故障，请在已认证控制台查看真实状态。",
}


def render(facts: NotificationFacts) -> NotificationTemplate:
    supported = {
        "verified_result": facts.verified,
        "necessary_question": facts.needs_input,
        "review_available": facts.reviewable,
        "critical_fault": facts.critical,
    }
    if (
        facts.input_revision != facts.thread_revision
        or not supported[facts.content_class]
        or facts.state == "cancelled"
    ):
        raise DomainError("NOTIFICATION_CAUSE_UNAVAILABLE", "Current verified cause required", 409)
    data_class: Literal["public_summary", "private_summary"] = "public_summary"
    if facts.conversation_status is not None:
        if facts.content_class != "necessary_question" or facts.state != "waiting_input":
            raise DomainError(
                "NOTIFICATION_CAUSE_UNAVAILABLE", "Current conversation status only", 409
            )
        body = {
            "model_unknown": "模型请求结果尚未确认，已保留费用预占，不会自动重复调用。",
            "budget": "当前共享预算不足，本次未继续调用模型或编码。",
            "invalid": "模型返回内容未通过检查，本次没有执行编码。",
            "error": "本次处理未完成，请查看任务记录；系统没有将它标为成功。",
        }[facts.conversation_status]
    elif facts.answer_ref is not None:
        if (
            facts.content_class != "necessary_question"
            or facts.state != "waiting_input"
            or not facts.answer_ref.startswith("source-chat-output-v1:")
        ):
            raise DomainError("NOTIFICATION_CAUSE_UNAVAILABLE", "Model answer reference only", 409)
        body = facts.answer_ref
        data_class = "private_summary"
    elif facts.source_receipt:
        if facts.content_class != "verified_result" or facts.state != "waiting_input":
            raise DomainError("NOTIFICATION_CAUSE_UNAVAILABLE", "Receipt reply only", 409)
        body = "已收到你的 @ 消息，消息接收与回复服务已连通。当前为受限试用，尚未开启自动编码。"
    else:
        body = _TEMPLATES[facts.content_class] + "\n任务：" + str(facts.ticket_id)
    values = facts.model_dump(mode="json")
    if facts.answer_ref is None:
        values.pop("answer_ref")
    if facts.conversation_status is None:
        values.pop("conversation_status")
    if not facts.source_receipt:
        values.pop("source_receipt")  # Preserve pre-SA22 task-template fact hashes.
    return NotificationTemplate(
        content_class=facts.content_class,
        data_class=data_class,
        body=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        facts_sha256=digest(values),
    )


def quiet_until(grant: NotificationGrant, at: datetime) -> datetime | None:
    """UTC traversal handles repeated/missing local minutes without guessing DST."""
    if not any(q.contains(at) for q in grant.quiet_hours):
        return None
    probe = at.replace(second=0, microsecond=0)
    for _ in range(7 * 24 * 60 + 1):
        probe += timedelta(minutes=1)
        if not any(q.contains(probe) for q in grant.quiet_hours):
            return probe
    return at + timedelta(days=7)  # Permanently quiet: expiry cancels before any send.


def delivery_scope(destination: object) -> dict[str, object]:
    """The exact installed native address carried by ChannelMessage.destination."""
    return NotificationDestination.model_validate(destination).thread.model_dump(mode="json")


def delivery_key(project: str, run_id: str, cause: str, destination: object, kind: str) -> str:
    # Display aliases, disclosure metadata and configuration revisions cannot
    # give the same native recipient a second delivery identity.
    return digest(
        ["source-notification-v1", project, run_id, cause, delivery_scope(destination), kind]
    )
