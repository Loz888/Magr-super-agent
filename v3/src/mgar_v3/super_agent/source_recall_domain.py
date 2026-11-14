"""SA15 metadata-only contracts. References, summaries and cursors grant nothing."""

from datetime import timedelta
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from mgar_v3.domain import Model, digest
from mgar_v3.super_agent.source_coding_domain import CodingRunRequest

RECALL_VERSION: Literal["source-recall-v1"] = "source-recall-v1"
SHA256 = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
NodeKind = Literal[
    "source",
    "thread",
    "ticket",
    "run",
    "job",
    "evidence",
    "artifact",
    "memory",
    "review",
    "delivery",
    "gap",
]


class RecallModel(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RecallRef(RecallModel):
    kind: NodeKind
    id: Annotated[str, Field(strict=True, min_length=1, max_length=64)]

    @model_validator(mode="after")
    def canonical_identity(self) -> Self:
        if self.kind == "artifact":
            if len(self.id) != 64 or any(c not in "0123456789abcdef" for c in self.id):
                raise ValueError("Artifact identity is a lowercase SHA256, never a path or URL")
        elif str(UUID(self.id)) != self.id:
            raise ValueError("Object identity must be an exact canonical UUID")
        return self

    @property
    def key(self) -> str:
        return self.kind + ":" + self.id


class RecallPath(RecallModel):
    refs: Annotated[tuple[RecallRef, ...], Field(min_length=1, max_length=5)]

    @model_validator(mode="after")
    def no_cycle(self) -> Self:
        if len({ref.key for ref in self.refs}) != len(self.refs):
            raise ValueError("A recalled path cannot contain a cycle")
        if self.refs[0].kind in {"artifact", "memory", "review", "delivery"}:
            raise ValueError("Derived content needs an original Source/Task/Evidence path")
        return self

    @property
    def path_sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


class RecallResolveRequest(RecallModel):
    path: RecallPath


class RecallWalkRequest(RecallResolveRequest):
    max_hops: Annotated[int, Field(strict=True, ge=0, le=4)] = 2
    max_nodes: Annotated[int, Field(strict=True, ge=1, le=32)] = 16
    max_edges: Annotated[int, Field(strict=True, ge=1, le=128)] = 64
    max_bytes: Annotated[int, Field(strict=True, ge=1024, le=16384)] = 8192


class RecallPageRequest(RecallResolveRequest):
    max_bytes: Annotated[int, Field(strict=True, ge=128, le=4096)] = 2048
    cursor: Annotated[str | None, Field(strict=True, max_length=4096)] = None


class RecallSearchRequest(RecallModel):
    query: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    source_instance_id: UUID | None = None
    conversation_key: SHA256 | None = None
    after: AwareDatetime | None = None
    before: AwareDatetime | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=16)] = 8
    cursor: Annotated[str | None, Field(strict=True, max_length=4096)] = None

    @model_validator(mode="after")
    def bounded_query(self) -> Self:
        if not self.query.strip() or "\x00" in self.query:
            raise ValueError("A nonempty bounded query is required")
        if (self.after is None) != (self.before is None):
            raise ValueError("A time range needs both endpoints")
        if self.after is not None and self.before is not None:
            if not timedelta(0) < self.before - self.after <= timedelta(days=31):
                raise ValueError("Search time range must be positive and at most31days")
        if self.conversation_key is not None and self.source_instance_id is None:
            raise ValueError("Conversation identity must be scoped to its installation")
        return self


class WorkerRecallResolve(RecallResolveRequest):
    worker: CodingRunRequest


class WorkerRecallWalk(RecallWalkRequest):
    worker: CodingRunRequest


class WorkerRecallPage(RecallPageRequest):
    worker: CodingRunRequest


class WorkerRecallSearch(RecallSearchRequest):
    worker: CodingRunRequest


class RecallFact(RecallModel):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")]
    value: Annotated[str, Field(strict=True, max_length=256)]


class RecallNode(RecallModel):
    ref: RecallRef
    version_sha256: SHA256
    state: Annotated[str, Field(strict=True, max_length=64)]
    availability: Literal["available", "unavailable", "historical", "unknown"]
    content_available: bool = False
    facts: Annotated[tuple[RecallFact, ...], Field(max_length=24)] = ()
    read_only: Literal[True] = True
    grants_authority: Literal[False] = False


class RecallEdge(RecallModel):
    source: RecallRef
    target: RecallRef
    relation: Literal[
        "origin",
        "contains",
        "attempt",
        "resource",
        "evidence",
        "artifact",
        "review",
        "delivery",
        "memory",
        "context",
        "gap",
    ]
    grants_authority: Literal[False] = False


class RecallWalkResult(RecallModel):
    version: Literal["source-recall-v1"] = RECALL_VERSION
    nodes: tuple[RecallNode, ...]
    edges: tuple[RecallEdge, ...]
    truncated: bool
    clarification_required: bool = False
    read_only: Literal[True] = True


class RecallCursor(RecallModel):
    version: Literal["source-recall-v1"] = RECALL_VERSION
    purpose: Literal["page", "search"]
    caller_sha256: SHA256
    request_sha256: SHA256
    version_sha256: SHA256
    offset: Annotated[int, Field(strict=True, ge=0, le=2_000_000)]
    after_id: UUID | None = None
    expires_at: AwareDatetime
