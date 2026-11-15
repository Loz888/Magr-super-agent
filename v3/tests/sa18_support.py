"""Explicitly synthetic SA18 metadata; actual runtime evidence is tested separately."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from mgar_v3.super_agent.source_coding_domain import CodingOrigin, CodingTarget
from mgar_v3.super_agent.source_correlation_domain import CorrelationInputRef
from mgar_v3.super_agent.source_review_bundle import tree_sha256
from mgar_v3.super_agent.source_review_domain import (
    ReviewContext,
    ReviewCurrent,
    ReviewPresentation,
    ReviewTarget,
    ReviewVerification,
)

AT = datetime(2026, 11, 15, 6, 0, tzinfo=UTC)
BEFORE = {
    "src/calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_calc.py": "assert 1 + 2 == 3\n",
}
AFTER = {**BEFORE, "src/calc.py": "def add(a, b):\n    return a + b\n"}


def context():
    coding, attempt = uuid4(), uuid4()
    return ReviewContext(
        review_id=uuid4(),
        coding_id=coding,
        target=CodingTarget(
            project_id="orders-demo",
            thread_id=uuid4(),
            ticket_id=uuid4(),
            run_id=uuid4(),
            mandate_id=uuid4(),
        ),
        origin=CodingOrigin(
            input=CorrelationInputRef(
                input_id=uuid4(),
                source_instance_id=uuid4(),
                project_id="orders-demo",
                version_fingerprint="1" * 64,
                state_sha256="2" * 64,
            ),
            identity_id=uuid4(),
            identity_revision=1,
            principal_id=uuid4(),
            engine_actor="sa_" + "a" * 32,
            policy_id=uuid4(),
            policy_revision=1,
            policy_sha256="3" * 64,
            expires_at=AT + timedelta(hours=1),
        ),
        attempt_id=attempt,
        assignment_id=uuid4(),
        session_id=uuid4(),
        workspace_id=uuid4(),
        epoch=1,
        input_revision=1,
        async_revision=0,
        async_input_revision=0,
        mandate_sha256="4" * 64,
        coding_revision=1,
        worker_profile_sha256="5" * 64,
        profile_sha256="6" * 64,
        environment_sha256="7" * 64,
        acceptance_sha256="8" * 64,
        baseline_tree_sha256=tree_sha256(BEFORE),
        registration_sha256="9" * 64,
        context_sha256="a" * 64,
        publication=ReviewTarget(
            repository="example/controlled",
            repository_id=1,
            base_branch="main",
            base_sha="b" * 40,
            branch="sa18/" + uuid4().hex,
        ),
        verification=ReviewVerification(
            verification_id=uuid4(),
            operation_id=uuid4(),
            execution_operation_id=uuid4(),
            attempt_id=attempt,
            coding_id=coding,
            mandate_sha256="4" * 64,
            receipt_sha256="c" * 64,
            tree_sha256=tree_sha256(AFTER),
            environment_sha256="7" * 64,
            profile_sha256="5" * 64,
            acceptance_sha256="8" * 64,
            provenance_sha256="d" * 64,
            result="passed",
            tests_run=3,
            observed_at=AT - timedelta(seconds=1),
        ),
        actor="owner",
        prepared_by=uuid4(),
        prepared_at=AT,
        expires_at=AT + timedelta(minutes=10),
    )


def current(c):
    return ReviewCurrent(
        context_sha256=c.context_sha256,
        epoch=c.epoch,
        input_revision=c.input_revision,
        async_revision=c.async_revision,
        async_input_revision=c.async_input_revision,
        tree_sha256=c.verification.tree_sha256,
        profile_sha256=c.profile_sha256,
        user_id=c.prepared_by,
        actor=c.actor,
        source_current=True,
        roles_current=True,
        policy_current=True,
        target_current=True,
        verification_current=True,
        primary_current=True,
        write_grant_current=True,
        no_running_jobs=True,
        owner_current=True,
        run_state="running",
    )


def presentation(c, bundle):
    return ReviewPresentation(
        presentation_id=uuid4(),
        review_id=c.review_id,
        user_id=c.prepared_by,
        session_sha256="e" * 64,
        context_sha256=c.sha256,
        bundle_sha256=bundle.payload_hash,
        action_sha256="f" * 64,
        presented_at=AT,
        expires_at=AT + timedelta(seconds=120),
    )
