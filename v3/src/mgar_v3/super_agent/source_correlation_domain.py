"""Versioned, bodyless relationship facts; none grants execution or data access.

Resource identity survives a PR head change. CodeRef identifies the exact code and
CI observation separately. Source/target authority is always re-read by Engine;
even a correctly hashed snapshot or an allowed TargetRef is only a frozen fact.
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from mgar_v3.domain import Model, digest
from mgar_v3.sources.domain import OpaqueId, ProjectId, SourceScope

CORRELATION_VERSION: Literal["source-correlation-v1"] = "source-correlation-v1"
CORRELATION_SNAPSHOT_VERSION: Literal["source-correlation-snapshot-v1"] = (
    "source-correlation-snapshot-v1"
)
MAX_CORRELATION_BYTES = 65536
SHA256 = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
Commit = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{40}$")]
Counter = Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]
Revision = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]
GitHubId = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]
Repository = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
ResourceKind = Literal["issue", "pull_request", "workflow_run", "check_run", "check_suite"]
ProofKind = Literal["verified_resource", "signed_alias", "suggestion"]
CorrelationReason = Literal[
    "EXACT_RESOURCE", "NO_EXPLICIT_RESOURCE", "RESOURCE_UNCLAIMED", "RESOURCE_AMBIGUOUS",
    "RESOURCE_CONFLICT", "RESOURCE_REMOVED", "TARGET_UNAVAILABLE", "TARGET_DENIED",
    "SUGGESTION_ONLY",
]


class CorrelationModel(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ResourceRef(CorrelationModel):
    """A verified owner's resource, never a resource guessed from a chat title."""

    project_id: ProjectId
    scope: SourceScope
    repository_id: GitHubId
    repository: Repository
    kind: ResourceKind
    resource_id: Annotated[str, Field(strict=True, pattern=r"^[1-9][0-9]{0,18}$")]

    @field_validator("repository")
    @classmethod
    def canonical_repository(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def supported_owner(self) -> Self:
        if self.scope.source_kind != "github" or int(self.resource_id) > 9223372036854775807:
            raise ValueError("Resource must have a supported, exact installation owner")
        return self

    @property
    def resource_key(self) -> str:
        # Repository names are display/matching metadata; immutable ID owns identity.
        return digest([
            CORRELATION_VERSION, self.project_id, self.scope.key,
            self.repository_id, self.kind, self.resource_id,
        ])


class CodeRef(CorrelationModel):
    resource_key: SHA256
    proof_sha256: SHA256
    head_repository_id: GitHubId | None = None
    head_sha: Commit | None = None
    base_sha: Commit | None = None
    tree_sha: Commit | None = None
    run_id: GitHubId | None = None
    run_attempt: GitHubId | None = None
    check_run_id: GitHubId | None = None
    check_suite_id: GitHubId | None = None

    @model_validator(mode="after")
    def attempt_has_run(self) -> Self:
        if self.run_attempt is not None and self.run_id is None:
            raise ValueError("A CI attempt must name its exact workflow run")
        return self

    @property
    def code_version_key(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"proof_sha256"}))


class TargetRef(CorrelationModel):
    ticket_id: UUID
    run_id: UUID
    thread_id: UUID
    thread_revision: Counter
    execution_epoch: Counter
    mandate_id: UUID
    mandate_sha256: SHA256
    project_id: ProjectId
    repository: Repository
    allowed: bool

    @field_validator("repository")
    @classmethod
    def canonical_repository(cls, value: str) -> str:
        return value.lower()


class CorrelationInputRef(CorrelationModel):
    input_id: UUID
    source_instance_id: UUID
    project_id: ProjectId
    version_fingerprint: SHA256
    # Engine hashes current Source/generation/identity/policy/head/TTL facts here.
    # This is a comparison token, never a bearer capability or caller assertion.
    state_sha256: SHA256


class InputUnit(CorrelationModel):
    input_id: UUID
    unit_key: SHA256
    resource_keys: Annotated[tuple[SHA256, ...], Field(max_length=16)] = ()
    origin: Literal["verified_resource", "signed_alias", "unresolved"] = "unresolved"

    @model_validator(mode="after")
    def explicit_units(self) -> Self:
        if len(set(self.resource_keys)) != len(self.resource_keys):
            raise ValueError("Unit resources must be distinct")
        if (self.origin == "unresolved") != (not self.resource_keys):
            raise ValueError("Only unresolved units may omit exact resources")
        return self


class ResourceEvidence(CorrelationModel):
    input_id: UUID
    resource: ResourceRef
    proof_kind: ProofKind
    proof_sha256: SHA256
    code_ref: CodeRef | None = None
    alias_id: UUID | None = None
    alias_revision: Revision | None = None

    @model_validator(mode="after")
    def exact_proof(self) -> Self:
        if self.proof_kind == "signed_alias":
            if self.alias_id is None or self.alias_revision is None:
                raise ValueError("A signed alias requires an exact signed revision")
        elif self.alias_id is not None or self.alias_revision is not None:
            raise ValueError("Only a signed alias may supply alias authority references")
        if self.code_ref is not None and self.code_ref.resource_key != self.resource.resource_key:
            raise ValueError("Code proof must name the same exact resource")
        return self


class CorrelationHead(CorrelationModel):
    resource_key: SHA256
    revision: Counter
    head_sha256: SHA256
    state: Literal["unclaimed", "bound", "removed"]
    target: TargetRef | None = None
    link_id: UUID | None = None

    @model_validator(mode="after")
    def complete_head(self) -> Self:
        if self.state == "unclaimed":
            if self.revision != 0 or self.target is not None or self.link_id is not None:
                raise ValueError("Unclaimed heads have no target or signed revision")
        elif self.revision == 0 or self.link_id is None:
            raise ValueError("Recorded heads require their existing link and revision")
        elif (self.state == "bound") != (self.target is not None):
            raise ValueError("Only a bound head has a current target")
        return self


class CorrelationSnapshot(CorrelationModel):
    schema_version: Literal["source-correlation-snapshot-v1"] = CORRELATION_SNAPSHOT_VERSION
    project_id: ProjectId
    inputs: Annotated[tuple[CorrelationInputRef, ...], Field(min_length=1, max_length=16)]
    units: Annotated[tuple[InputUnit, ...], Field(min_length=1, max_length=64)]
    resources: Annotated[tuple[ResourceEvidence, ...], Field(max_length=256)] = ()
    heads: Annotated[tuple[CorrelationHead, ...], Field(max_length=64)] = ()
    targets: Annotated[tuple[TargetRef, ...], Field(max_length=64)] = ()
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def scoped_membership(self) -> Self:
        inputs = {value.input_id: value for value in self.inputs}
        if len(inputs) != len(self.inputs):
            raise ValueError("Canonical inputs must be distinct")
        if any(value.project_id != self.project_id for value in self.inputs):
            raise ValueError("Inputs must belong to this project")
        if len({value.unit_key for value in self.units}) != len(self.units):
            raise ValueError("Input units must be distinct")
        if {unit.input_id for unit in self.units} != set(inputs):
            raise ValueError("Every input must have its own explicit or unresolved unit")
        proofs: set[tuple[UUID, str]] = set()
        for value in self.resources:
            source = inputs.get(value.input_id)
            resource = value.resource
            if source is None or resource.project_id != self.project_id:
                raise ValueError("Resource proof must belong to a current input and project")
            if value.proof_kind == "verified_resource" and (
                source.source_instance_id != resource.scope.source_instance_id
            ):
                raise ValueError("Native proof cannot cross installation ownership")
            key = value.input_id, resource.resource_key
            if key in proofs:
                raise ValueError("Each input has one unambiguous proof per resource")
            proofs.add(key)
        if any((unit.input_id, key) not in proofs
               for unit in self.units for key in unit.resource_keys):
            raise ValueError("Every unit resource must have a proof for that input")
        keys = {value.resource.resource_key for value in self.resources}
        if len({value.resource_key for value in self.heads}) != len(self.heads) or any(
            value.resource_key not in keys for value in self.heads
        ):
            raise ValueError("Heads must uniquely name resources in this snapshot")
        if len({value.ticket_id for value in self.targets}) != len(self.targets):
            raise ValueError("Current target tickets must be distinct")
        all_targets = [*self.targets, *(head.target for head in self.heads if head.target)]
        if any(value.project_id != self.project_id for value in all_targets):
            raise ValueError("Targets must belong to this project")
        return self


class CorrelationResolution(CorrelationModel):
    schema_version: Literal["source-correlation-v1"] = CORRELATION_VERSION
    input_id: UUID
    unit_key: SHA256
    snapshot_sha256: SHA256
    outcome: Literal["matched", "unmatched", "ambiguous", "blocked"]
    reason_code: CorrelationReason
    resource_keys: Annotated[tuple[SHA256, ...], Field(max_length=16)]
    candidates: Annotated[tuple[TargetRef, ...], Field(max_length=3)] = ()
    suggestion_decision_key: SHA256 | None = None
    eligible_for_execution: Literal[False] = False
    executes_tasks: Literal[False] = False
    calls_model: Literal[False] = False
    external_writes: Literal[0] = 0

    @field_validator("eligible_for_execution", "executes_tasks", "calls_model", mode="before")
    @classmethod
    def never_active(cls, value: object) -> object:
        if value is not False:
            raise ValueError("A correlation cannot grant execution or model authority")
        return value

    @field_validator("external_writes", mode="before")
    @classmethod
    def no_external_write(cls, value: object) -> object:
        if type(value) is not int or value != 0:
            raise ValueError("A correlation never authorizes an external write")
        return value

    @model_validator(mode="after")
    def unique_match(self) -> Self:
        if (self.outcome == "matched") != (len(self.candidates) == 1):
            raise ValueError("Only an exact match may expose one authorized target")
        if self.outcome != "matched" and self.candidates:
            raise ValueError("Uncertain or blocked results cannot expose target candidates")
        if any(not target.allowed for target in self.candidates):
            raise ValueError("Denied targets cannot become a match")
        return self


class CorrelationResolveRequest(CorrelationModel):
    input_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=16)]
    suggestion_decision_key: SHA256 | None = None

    @field_validator("input_ids")
    @classmethod
    def distinct_inputs(cls, values: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(values)) != len(values):
            raise ValueError("Canonical input IDs must be distinct")
        return values


class CorrelationLinkRequest(CorrelationResolveRequest):
    resource_key: SHA256
    snapshot_sha256: SHA256
    expected_revision: Counter
    target: TargetRef
    review_ref: OpaqueId


class CorrelationCorrectRequest(CorrelationModel):
    snapshot_sha256: SHA256
    expected_revision: Revision
    target: TargetRef
    review_ref: OpaqueId
    reason_code: Literal["OPERATOR_CORRECTION"] = "OPERATOR_CORRECTION"


class CorrelationRemoveRequest(CorrelationModel):
    snapshot_sha256: SHA256
    expected_revision: Revision
    review_ref: OpaqueId
    reason_code: Literal["OPERATOR_REMOVAL"] = "OPERATOR_REMOVAL"


def input_unit_key(value: CorrelationInputRef, resource_key: str | None = None) -> str:
    return digest([
        CORRELATION_VERSION, str(value.input_id), value.version_fingerprint, resource_key,
    ])


def snapshot_hash(value: CorrelationSnapshot) -> str:
    return digest(value.model_dump(mode="json"))


def verification_reusable(recorded: CodeRef | None, current: CodeRef | None) -> bool:
    """Relationship continuity cannot substitute for an exact, known code tree."""
    if recorded is None or current is None:
        return False
    required = ("head_repository_id", "head_sha", "base_sha", "tree_sha")
    if any(getattr(value, field) is None for value in (recorded, current) for field in required):
        return False
    return recorded.code_version_key == current.code_version_key
