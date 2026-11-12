"""Bounded owner configuration and canonical input references for source observation."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mgar_v3.domain import Model
from mgar_v3.sources.domain import ActorRef, OpaqueId

FAST_PATH_VERSION: Literal["source-fast-path-v1"] = "source-fast-path-v1"


class FastPathSettings(Model):
    enabled: bool = Field(default=False, strict=True)
    cooldown_seconds: Annotated[int, Field(strict=True, ge=0, le=3600)] = 30
    merge_window_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 10
    max_batch_items: Annotated[int, Field(strict=True, ge=1, le=16)] = 16
    max_decisions: Annotated[int, Field(strict=True, ge=1, le=100000)] = 10000
    max_batches: Annotated[int, Field(strict=True, ge=1, le=10000)] = 1000
    own_actors: Annotated[tuple[ActorRef, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def distinct_automation(self) -> Self:
        if any(actor.actor_kind not in {"bot", "service"} for actor in self.own_actors):
            raise ValueError("Own-result actors must be verified bots or services")
        if len({actor.key for actor in self.own_actors}) != len(self.own_actors):
            raise ValueError("Duplicate own-result actor")
        return self


class FastPathConfigure(Model):
    expected_revision: Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]
    settings: FastPathSettings
    review_ref: OpaqueId


class ObserveRequest(Model):
    input_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def distinct_inputs(self) -> Self:
        if len(set(self.input_ids)) != len(self.input_ids):
            raise ValueError("Duplicate canonical input reference")
        return self


class PreviewRequest(Model):
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 100
