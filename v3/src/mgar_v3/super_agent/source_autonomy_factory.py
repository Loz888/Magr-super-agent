"""SQL-only original-record factories; caller has verified a frozen delegation."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import digest
from mgar_v3.super_agent.policy_domain import PolicyFacts
from mgar_v3.super_agent.source_autonomy_store import decode_rule, error
from mgar_v3.super_agent.source_coding_domain import (
    CODING_VERSION,
    CodingAdmitRequest,
    CodingMandate,
    CodingOrigin,
    CodingTarget,
)
from mgar_v3.super_agent.source_scheduler_domain import SchedulerEnqueueRequest

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection
    from mgar_v3.super_agent.source_autonomy_domain import AutonomySignal
    from mgar_v3.super_agent.source_autonomy_profiles import AutonomyProfile
    from mgar_v3.super_agent.source_autonomy_store import AutonomyStore, Row
    from mgar_v3.super_agent.source_correlation import _Input


def create_original(
    core: AutonomyStore,
    conn: Connection,
    rule_row: Row,
    claim: Row,
    signal: AutonomySignal,
    source: _Input,
    profile: AutonomyProfile,
    *,
    previous: Row | None = None,
    review_sha256: str | None = None,
) -> Row:
    core._conn(conn)
    review_sha256 = review_sha256 or rule_row["review_sha256"]
    engine, definition, now = core.engine, decode_rule(rule_row), core.now(conn)
    if source.native:
        raise error("RESOURCE_WINNER_FACTORY_REQUIRED", 409)
    until = min(
        definition.valid_until, source.expires, now + timedelta(seconds=definition.task_ttl_seconds)
    )
    if until <= now + timedelta(seconds=1):
        raise error("SOURCE_EXPIRED")
    thread_id = previous["thread_id"] if previous else uuid4()
    ticket_id = previous["ticket_id"] if previous else uuid4()
    run_id, mandate_id, attempt_id = uuid4(), uuid4(), uuid4()
    target = CodingTarget(
        project_id=core.project,
        thread_id=thread_id,
        ticket_id=ticket_id,
        run_id=run_id,
        mandate_id=mandate_id,
    )
    identity = conn.execute(
        "SELECT i.*,p.engine_actor FROM app.source_identities i JOIN app.source_principals p "
        "USING(principal_id,project_id) WHERE i.identity_id=%s AND i.project_id=%s",
        (source.row["identity_id"], core.project),
    ).fetchone()
    roles = conn.execute(
        "SELECT * FROM app.source_role_bindings WHERE identity_id=%s AND project_id=%s "
        "AND conversation_key=%s AND active",
        (source.row["identity_id"], core.project, source.item.event.source_thread.conversation_key),
    ).fetchone()
    if identity is None or roles is None:
        raise error("IDENTITY_CHANGED")
    coding: CodingMandate | None = None
    if profile.coding is not None:
        p = profile.coding
        coding = CodingMandate(
            coding_id=uuid4(),
            revision=1,
            target=target,
            origin=CodingOrigin(
                input=source.ref,
                identity_id=identity["identity_id"],
                identity_revision=identity["revision"],
                principal_id=identity["principal_id"],
                engine_actor=identity["engine_actor"],
                policy_id=definition.policy_id,
                policy_revision=definition.policy_revision,
                policy_sha256=definition.policy_sha256,
                expires_at=source.expires,
            ),
            repository=p.repository,
            grant=p.grant,
            budget=p.budget,
            harness=p.harness,
            context_refs=(source.ref,),
            context_sha256=engine.source_coding.context_sha256(source),
            acceptance_sha256=p.acceptance_sha256,
            worker_profile_sha256=p.profile_sha256,
            review_sha256=review_sha256,
            signed_by=rule_row["signed_by"],
            valid_from=now,
            valid_until=until,
        )
        scope = coding.model_dump(mode="json")
        mandate_sha = coding.mandate_sha256
    else:
        scope = {
            "scope_kind": "source-autonomy-readonly-v1",
            "read_only": True,
            "actions": [],
            "repository": profile.repository,
            "base_sha": profile.base_sha,
            "read_paths": [profile.read_path],
            "basis": "owner_reviewed_team_rule",
            "rule_id": str(rule_row["rule_id"]),
            "rule_revision": rule_row["revision"],
            "definition_sha256": definition.definition_sha256,
            "review_sha256": review_sha256,
            "signed_by": str(rule_row["signed_by"]),
            "source_input": source.ref.model_dump(mode="json"),
            "objective_sha256": profile.objective_sha256,
            "profile_sha256": profile.profile_sha256,
            "budget_profile_sha256": profile.budget_profile_sha256,
            "valid_until": until.isoformat(),
        }
        mandate_sha = digest(scope)
    values = {
        "attempt_id": attempt_id,
        "claim_id": claim["claim_id"],
        "rule_id": rule_row["rule_id"],
        "rule_revision": rule_row["revision"],
        "project_id": core.project,
        "source_instance_id": definition.source_instance_id,
        "thread_id": thread_id,
        "ticket_id": ticket_id,
        "run_id": run_id,
        "mandate_id": mandate_id,
        "mandate_sha256": mandate_sha,
        "original_kind": profile.mode,
        "profile_sha256": profile.profile_sha256,
        "budget_profile_sha256": profile.budget_profile_sha256,
        "input_id": source.ref.input_id,
        "source_state_sha256": source.ref.state_sha256,
        "identity_id": identity["identity_id"],
        "identity_revision": identity["revision"],
        "role_binding_id": roles["binding_id"],
        "role_revision": roles["revision"],
        "conversation_key": source.item.event.source_thread.conversation_key,
        "reason_code": "AUTONOMY_CURRENT_RULE_ACCEPTED",
        "authority_until": until,
        "reopened_from": previous["attempt_id"] if previous else None,
    }
    # Deferred original-record FKs allow the immutable delegation fence to exist
    # before the original Coding factory checks it, all in this same transaction.
    row = conn.execute(
        "INSERT INTO app.sa_autonomy_attempts("
        + ",".join(values)
        + ") VALUES("
        + ",".join("%s" for _ in values)
        + ") RETURNING *",
        tuple(values.values()),
    ).fetchone()
    assert row is not None
    if coding is not None and previous is None:
        engine.source_coding._create_original(conn, coding)
    else:
        if previous is None:
            conn.execute(
                "INSERT INTO app.threads(thread_id,project_id,repository,external_key,revision) "
                "VALUES(%s,%s,%s,%s,1)",
                (
                    thread_id,
                    core.project,
                    profile.repository,
                    "source-autonomy:" + signal.subject_key,
                ),
            )
        else:
            old = engine._run(conn, previous["run_id"])
            if old["status"] not in {
                "succeeded",
                "failed",
                "cancelled",
                "paused",
                "waiting_input",
                "waiting_approval",
            }:
                raise error("REOPEN_NOT_QUIESCENT")
        conn.execute(
            "INSERT INTO app.mandates(mandate_id,project_id,actor,scope,policy_version,execution_mode) "
            "VALUES(%s,%s,%s,%s,%s,'fixture')",
            (
                mandate_id,
                core.project,
                coding.origin.engine_actor if coding else engine.settings.local_actor,
                Jsonb(scope),
                CODING_VERSION if coding else "source-autonomy-readonly-v1",
            ),
        )
        if previous:
            conn.execute(
                "UPDATE app.tickets SET mandate_id=%s,status='open',outcome=NULL "
                "WHERE ticket_id=%s",
                (mandate_id, ticket_id),
            )
        else:
            conn.execute(
                "INSERT INTO app.tickets(ticket_id,mandate_id,thread_id,status) "
                "VALUES(%s,%s,%s,'open')",
                (ticket_id, mandate_id, thread_id),
            )
        seq = conn.execute(
            "SELECT coalesce(max(attempt_no),0)+1 AS n FROM app.runs WHERE ticket_id=%s",
            (ticket_id,),
        ).fetchone()
        assert seq is not None
        conn.execute(
            "INSERT INTO app.runs(run_id,ticket_id,attempt_no,graph_version,execution_mode,status,"
            "question,input_revision) VALUES(%s,%s,%s,%s,'fixture','queued',%s,"
            "(SELECT revision FROM app.threads WHERE thread_id=%s))",
            (
                run_id,
                ticket_id,
                seq["n"],
                CODING_VERSION if coding else "r1-quotes-v1",
                profile.objective,
                thread_id,
            ),
        )
        conn.execute(
            "INSERT INTO app.budgets(mandate_id,limit_micros) VALUES(%s,%s)",
            (mandate_id, profile.coding.limit_micros if profile.coding else 0),
        )
        if coding is not None:
            # Never update, refund or recreate an existing original authorization budget.
            old_budget = conn.execute(
                "SELECT * FROM app.authorization_budgets WHERE profile_hash=%s",
                (profile.budget_profile_sha256,),
            ).fetchone()
            if old_budget is None:
                raise error("ORIGINAL_BUDGET_MISSING")
        else:
            proof = conn.execute(
                "SELECT evidence_id FROM app.source_identity_evidence WHERE identity_id=%s "
                "AND project_id=%s AND conversation_key=%s ORDER BY verified_at DESC LIMIT 1",
                (identity["identity_id"], core.project, roles["conversation_key"]),
            ).fetchone()
            if proof is None:
                raise error("SOURCE_PROOF_MISSING")
            facts = PolicyFacts(
                event_kind=source.item.event.event_kind,
                request_kind="team_rule",
                capability="read_repository",
                repository=profile.repository,
                base_sha=profile.base_sha,
                path=profile.read_path,
            )
            conn.execute(
                "INSERT INTO app.source_run_policies(run_id,ticket_id,thread_id,policy_id,"
                "policy_revision,project_id,identity_id,identity_revision,binding_id,evidence_id,facts) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    run_id,
                    ticket_id,
                    thread_id,
                    definition.policy_id,
                    definition.policy_revision,
                    core.project,
                    identity["identity_id"],
                    identity["revision"],
                    roles["binding_id"],
                    proof["evidence_id"],
                    Jsonb(facts.model_dump(mode="json")),
                ),
            )
    original = engine._run(conn, run_id)
    target_hash = engine.source_scheduler._target_hash(conn, original)
    if coding is not None:
        # The helper is introduced only by the precisely agreed five-file patch.
        getattr(engine.source_coding, "_admit_authorized")(
            conn,
            rule_row["signed_by"],
            CodingAdmitRequest(
                request_id=uuid4(),
                run_id=run_id,
                expected_epoch=0,
                source_input=source.ref,
                expected_policy_revision=definition.policy_revision,
                expected_revision=0,
                target_sha256=target_hash,
                profile_sha256=profile.profile_sha256,
                review_ref="SA16-owner-reviewed-team-rule",
            ),
        )
    else:
        getattr(engine.source_scheduler, "_enqueue_authorized")(
            conn,
            rule_row["signed_by"],
            SchedulerEnqueueRequest(
                request_id=uuid4(),
                run_id=run_id,
                expected_epoch=0,
                target_sha256=target_hash,
                resources=profile.resources,
                source_input=source.ref,
                review_ref="SA16-owner-reviewed-team-rule",
            ),
        )
    engine._event(
        conn,
        run_id,
        "source_autonomy_reviewed",
        review_sha256=review_sha256,
        signed_by=str(rule_row["signed_by"]),
    )
    engine._event(
        conn,
        run_id,
        "source_autonomy_admitted",
        rule_id=str(rule_row["rule_id"]),
        rule_revision=rule_row["revision"],
        claim_id=str(claim["claim_id"]),
    )
    core._event(
        conn,
        rule_row,
        "reopen" if previous else "admitted",
        "AUTONOMY_CURRENT_RULE_ACCEPTED",
        claim_id=claim["claim_id"],
        attempt_id=attempt_id,
    )
    return cast(dict[str, Any], row)
