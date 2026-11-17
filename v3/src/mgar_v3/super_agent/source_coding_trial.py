"""Explicit owner exception for the SA22 experiment; never a quality-gate PASS."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import AwareDatetime

from mgar_v3.domain import DomainError, Model
from mgar_v3.super_agent.source_trial_limits import (
    OWNER_TRIAL_PROJECT_LIMITS,
    OwnerTrialProjectLimit,
)

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine
    from mgar_v3.super_agent.source_coding import CodingProfile

R05_SOURCE = "08cb1e28fac1c5e2e27117f7de62fbd3c20d0ac2"
R05_FINGERPRINT = "8bee95a622e4fd6c504654e31dbce8ebf6880efec7277bd7ab87c629839cb56b"


class CodingTrial(Model):
    trial_id: UUID
    project_id: str
    source_instance_id: UUID
    source_revision: int
    policy_id: UUID
    policy_revision: int
    profile_sha256: str
    repository: str
    base_commit: str
    registration_sha256: str
    h12_commit: str
    h12_source_sha256: str
    h12_evidence_sha256: str
    manifest_sha256: str
    signed_by: UUID
    review_sha256: str
    maximum_project_micros: OwnerTrialProjectLimit = 2000000
    valid_from: AwareDatetime
    valid_until: AwareDatetime


def _deny() -> DomainError:
    return DomainError(
        "SOURCE_CODING_TRIAL_AUTHORIZATION", "Exact current owner trial required", 403
    )


def current_trial(
    engine: Engine,
    conn: Connection,
    profile: CodingProfile,
) -> dict[str, Any]:
    ready = conn.execute("SELECT to_regclass('app.sa_coding_trial_authorizations') AS t").fetchone()
    if profile.trial_id is None or not ready or not ready["t"]:
        raise _deny()
    row = conn.execute(
        "SELECT * FROM app.sa_coding_trial_authorizations WHERE trial_id=%s AND project_id=%s",
        (profile.trial_id, engine.settings.project_id),
    ).fetchone()
    if row is None:
        raise _deny()
    p = engine.source_policies._current(conn, row["policy_id"])
    source = engine.source_correlations.identities.registry._instance(
        conn, row["source_instance_id"]
    )
    now: datetime = engine.source_ingress.now(conn)
    if (
        not row["valid_from"] <= now < row["valid_until"]
        or row["maximum_project_micros"] not in OWNER_TRIAL_PROJECT_LIMITS
        or row["source_revision"] != source.revision
        or row["policy_revision"] != p["current_revision"]
        or p["revoked"]
        or p["source_instance_id"] != row["source_instance_id"]
        or row["profile_sha256"] != profile.profile_sha256
        or row["repository"] != profile.repository.repository
        or row["base_commit"] != profile.repository.base_commit
        or row["registration_sha256"] != profile.repository.registration_sha256
        or row["h12_commit"] != profile.harness.h12.commit
        or row["h12_source_sha256"] != profile.harness.h12.source_sha256
        or row["h12_evidence_sha256"] != profile.harness.h12.evidence_sha256
        or row["h12_commit"] != R05_SOURCE
        or row["h12_source_sha256"] != R05_FINGERPRINT
        or not engine.settings.allow_paid
    ):
        raise _deny()
    configs = engine.project_budgets._configs(conn)
    if len(configs) != 2:
        raise _deny()
    for _, definition in configs:
        engine.project_budgets._active(definition, now)
        if not 0 < definition.caps.total_micros <= row["maximum_project_micros"]:
            raise _deny()
    return row


def authorize_trial(engine: Engine, cookie: str, value: CodingTrial) -> None:
    with engine.transaction() as conn:
        owner = engine.source_correlations.identities._console(conn, cookie)
        if value.signed_by != owner or value.project_id != engine.settings.project_id:
            raise _deny()
        columns = list(CodingTrial.model_fields)
        data = value.model_dump()
        conn.execute(
            "INSERT INTO app.sa_coding_trial_authorizations(" + ",".join(columns) + ") "
            "VALUES(" + ",".join(["%s"] * len(columns)) + ")",
            tuple(data[k] for k in columns),
        )
