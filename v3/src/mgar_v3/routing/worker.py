from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.routing.domain import Proposal, Proposals, RouterDecision
from mgar_v3.routing.store import CONTROL, addressed, question_for

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine, Row
    from mgar_v3.live import LivePermit


class RouterProvider(Protocol):
    mode: str

    def propose(self, batch: "Row") -> Proposals: ...


class PipelineRouterProvider(Protocol):
    """Output is schema checked by its matching injected pipeline, never trusted."""

    mode: str

    def propose(self, batch: "Row") -> Any: ...


class RouterPipeline(Protocol):
    """Structural composition only; authority remains on the original Engine."""

    def prepare(self, mode: str = "fixture") -> "Row | None": ...

    def wrap(self, batch: "Row", proposals: Any) -> list[Any]: ...

    def save(self, batch_id: UUID, decisions: list[Any]) -> None: ...

    def commit(
        self, batch_id: UUID, decisions: list[Any], *, permit: "LivePermit | None" = None
    ) -> Any: ...

    def failure(self, batch_id: UUID, reason: str) -> None: ...


class FixtureRouter:
    mode = "fixture"

    def propose(self, batch: "Row") -> Proposals:
        proposals = []
        for group in batch["snapshot"]["threads"]:
            body = question_for(group["events"])
            target = None
            action = "ignore"
            message = None
            related = []
            for candidate in group["candidates"]:
                for kind, verb in CONTROL.items():
                    if body == f"/mgar {verb}-ticket {candidate['ticket_id']}":
                        action = kind
                        target = candidate["ticket_id"]
            if target is None and any(addressed(e["payload"]["body"]) for e in group["events"]):
                if (
                    body.strip() in {"help", "?", "帮忙", "帮我处理一下", "请帮忙"}
                    or len(body) > 8000
                ):
                    action = "request_clarification"
                    message = "clarification"
                else:
                    action = "create_ticket"
                    related = [
                        c["ticket_id"] for c in group["candidates"] if c["ticket_id"] in body
                    ][:3]
            proposals.append(
                Proposal(
                    thread_id=group["thread_id"],
                    action=action,
                    target_id=target,
                    related_ticket_ids=related,
                    message_code=message,
                )
            )
        return Proposals(proposals=proposals)


class RouterWorker:
    def __init__(
        self,
        engine: "Engine",
        provider: PipelineRouterProvider,
        permit: "LivePermit | None" = None,
        *,
        pipeline: RouterPipeline | None = None,
        decision_decoder: Callable[[Any], Any] | None = None,
    ):
        if (pipeline is None) != (decision_decoder is None):
            raise DomainError(
                "ROUTER_PIPELINE_CONFIGURATION", "Supply a matching pipeline and decoder", 422
            )
        self.engine, self.provider, self.permit = engine, provider, permit
        self.pipeline: RouterPipeline = pipeline if pipeline is not None else engine.routing
        self.decision_decoder: Callable[[Any], Any] = (
            decision_decoder if decision_decoder is not None else RouterDecision.model_validate
        )

    def tick(self) -> UUID | None:
        batch = self.pipeline.prepare(self.provider.mode)
        if batch is None:
            return None
        try:
            if batch["status"] == "ready":
                decisions = [self.decision_decoder(d) for d in batch["decisions"]]
            else:
                proposals = self.provider.propose(batch)
                decisions = self.pipeline.wrap(batch, proposals)
                self.pipeline.save(batch["batch_id"], decisions)
            self.pipeline.commit(batch["batch_id"], decisions, permit=self.permit)
        except DomainError as exc:
            self.pipeline.failure(batch["batch_id"], exc.code)
        except Exception:
            self.pipeline.failure(batch["batch_id"], "ROUTER_FAILED")
        return UUID(str(batch["batch_id"]))
