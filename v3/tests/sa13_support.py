"""Real PG Source/Coding fixtures, explicitly synthetic and never live."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from sa04_support import envelope, rows
from sa04_support import setup as source_setup
from sa07_support import PRICE, definition, save
from sa12_support import claim, session
from sa12_support import ready as scheduler_ready
from sa12_support import setup as scheduler_setup

from mgar_v3.domain import digest
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.sources.ingress_domain import IngressConfigure
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_coding import (
    H11_COMMIT,
    H12_COMMIT,
    CodingProfile,
)
from mgar_v3.super_agent.source_coding_domain import (
    CodingAdmitRequest,
    CodingGrant,
    CodingHarnessGate,
    CodingHarnessGates,
    CodingMandate,
    CodingOrigin,
    CodingRepository,
    CodingRunRequest,
    CodingTarget,
)
from mgar_v3.super_agent.source_scheduler_domain import ResourceRequest


def new_source(engine):
    return source_setup(
        engine,
        mode="auto_repair",
        receive={
            "enabled": True,
            "retain_body": True,
            "body_ttl_seconds": 3600,
            "allow_history_context": True,
            "model_input": True,
        },
    )


def setup(
    engine,
    tmp_path,
    *,
    admitted=True,
    ready=True,
    driver=None,
    profile=None,
    source_env=None,
    repository=None,
    grant=None,
    acceptance_sha256=None,
):
    """No Broker/model activation. Supply a real trusted driver/profile explicitly.

    admitted=False deliberately prepares an Owner-reviewed typed original target
    for the HTTP admission test. Production create_target binds and queues before
    committing, so its factory cannot leave an unmanaged queued run behind.
    """
    source = source_env or new_source(engine)
    scheduler = scheduler_setup(engine, tmp_path, driver=driver, cookie=source.cookie)
    budget_env = SimpleNamespace(
        engine=engine, service=engine.project_budgets, cookie=source.cookie
    )
    save(budget_env, "organization", "sa07-controlled-organization", definition(), revision=1)
    save(
        budget_env,
        "project",
        engine.settings.project_id,
        definition(organization_id="sa07-controlled-organization"),
        revision=1,
    )
    service = engine.source_coding
    service.cipher = BodyCipher(Fernet.generate_key())
    repository = (
        profile.repository
        if profile
        else repository
        or CodingRepository(
            repository=engine.settings.repository.lower(),
            repository_id=1,
            source_kind="fixture_git",
            base_commit="a" * 40,
            upstream_tree="b" * 40,
            materialized_commit="c" * 40,
            materialized_tree_sha256=digest("sa13-controlled-tree"),
            registration_sha256=digest("sa13-controlled-registration"),
            snapshot_sha256=digest("sa13-controlled-snapshot"),
            model_disclosure="controlled_fixture",
        )
    )
    grant = (
        profile.grant
        if profile
        else grant
        or CodingGrant(
            read_paths=("src",),
            write_paths=("src",),
            allowed_commands=("python",),
            resources=ResourceRequest(cpu_millis=1000, memory_mib=256),
            environment_sha256=digest("sa13-controlled-environment"),
        )
    )
    raw = source.definition.model_dump(mode="json")
    raw["execution"] = {
        "enabled": True,
        "repositories": [
            {
                "repository": repository.repository,
                "base_sha": repository.base_commit,
                "paths": list(grant.read_paths),
            }
        ],
        "capabilities": ["read_repository", "edit_files", "run_tests"],
    }
    source.definition = PolicyDefinition.model_validate(raw)
    engine.source_policies.save(
        source.cookie,
        source.policy_id,
        PolicySave(
            expected_revision=1,
            definition=source.definition,
            review_ref="SA13-controlled-auto-repair",
        ),
    )
    source.settings = source.settings.model_copy(update={"policy_revision": 2})
    engine.source_ingress.configure(
        source.cookie,
        source.installation.scope.source_instance_id,
        IngressConfigure(
            expected_generation=1,
            settings=source.settings,
            review_ref="SA13-current-policy-cutover",
        ),
    )
    from sa04_support import gateway

    source.gateway = gateway(source, generation=2)
    source.gateway.receive(envelope(source, body="SA13 synthetic private repair request"))
    input_id = rows(source, "inputs")[0]["input_id"]
    with engine.transaction() as conn:
        loaded = engine.source_correlations._input(conn, input_id)
        identity = conn.execute(
            "SELECT i.*,p.engine_actor FROM app.source_identities i "
            "JOIN app.source_principals p USING(principal_id,project_id) WHERE identity_id=%s",
            (loaded.row["identity_id"],),
        ).fetchone()
        policy = engine.source_policies._current(conn, source.policy_id)
        user = service._console(conn, source.cookie)
        budget = service.budget_ref(conn, PRICE, digest("sa13-original-budget-profile"))
        now = service.now(conn)
        origin = CodingOrigin(
            input=loaded.ref,
            identity_id=identity["identity_id"],
            identity_revision=identity["revision"],
            principal_id=identity["principal_id"],
            engine_actor=identity["engine_actor"],
            policy_id=source.policy_id,
            policy_revision=2,
            policy_sha256=policy["definition_sha256"],
            expires_at=loaded.expires,
        )
        harness = CodingHarnessGates(
            h11=CodingHarnessGate(
                card="HV4-H11",
                commit=H11_COMMIT,
                source_sha256=digest("fixed-H11-source"),
                evidence_sha256=digest("fixed-H11-proof"),
                engineering="PASS",
                recovery="PASS",
            ),
            h12=CodingHarnessGate(
                card="HV4-H12",
                commit=H12_COMMIT,
                source_sha256=digest("fixed-H12-source"),
                evidence_sha256=digest("fixed-H12-proof"),
                engineering="PASS",
                live_agent="FAIL",
                mvp="FAIL",
            ),
        )
        if profile is None:
            profile = CodingProfile(
                profile_sha256=scheduler.driver.profile_sha256,
                repository=repository,
                grant=grant,
                budget=budget,
                harness=harness,
                acceptance_sha256=acceptance_sha256 or digest("sa13-actual-controlled-acceptance"),
                limit_micros=1000000,
            )
        else:
            # The profile's immutable budget must describe these exact real PG configs.
            assert profile.budget == budget
            if acceptance_sha256 is not None:
                assert profile.acceptance_sha256 == acceptance_sha256
        service.register_profile(profile)
        target = CodingTarget(
            project_id=engine.settings.project_id,
            thread_id=uuid4(),
            ticket_id=uuid4(),
            run_id=uuid4(),
            mandate_id=uuid4(),
        )
        mandate = CodingMandate(
            coding_id=uuid4(),
            revision=1,
            target=target,
            origin=origin,
            repository=profile.repository,
            grant=profile.grant,
            budget=profile.budget,
            harness=profile.harness,
            context_refs=(loaded.ref,),
            context_sha256=service.context_sha256(loaded),
            acceptance_sha256=profile.acceptance_sha256,
            worker_profile_sha256=profile.profile_sha256,
            review_sha256=digest("SA13-trusted-review"),
            signed_by=user,
            valid_from=now - timedelta(seconds=1),
            valid_until=min(loaded.expires, now + timedelta(minutes=30)),
        )
        service._create_original(conn, mandate)
        run = engine._run(conn, target.run_id)
        request = CodingAdmitRequest(
            request_id=uuid4(),
            run_id=target.run_id,
            expected_epoch=0,
            source_input=loaded.ref,
            expected_policy_revision=2,
            expected_revision=0,
            target_sha256=engine.source_scheduler._target_hash(conn, run),
            profile_sha256=profile.profile_sha256,
            review_ref="SA13-owner-exact-target-review",
        )
        binding = service.admit(source.cookie, request) if admitted else None
    env = SimpleNamespace(
        engine=engine,
        service=service,
        cookie=source.cookie,
        source=source,
        input=loaded.ref,
        input_id=input_id,
        mandate=mandate,
        target=target,
        request=request,
        binding=binding,
        scheduler=scheduler,
        driver=scheduler.driver,
        profile=profile,
        worker=None,
        claimed=None,
        ready=None,
        run_request=None,
    )
    if ready:
        assert admitted
        worker = session(scheduler)
        claimed = claim(scheduler, worker)
        prepared = scheduler_ready(scheduler, worker, claimed)
        env.worker, env.claimed, env.ready = worker, claimed, prepared
        with engine.transaction() as conn:
            assignment = engine.source_scheduler._assignment(conn, claimed["assignment_id"])
        env.run_request = CodingRunRequest(
            request_id=uuid4(),
            coding_id=mandate.coding_id,
            run_id=target.run_id,
            assignment_id=assignment["assignment_id"],
            session_id=worker["session_id"],
            epoch=assignment["execution_epoch"],
            expected_revision=1,
        )
    return env


def snapshot(env):
    with env.engine.transaction() as conn:
        return {
            name: conn.execute("SELECT * FROM app." + name + " ORDER BY 1").fetchall()
            for name in (
                "threads",
                "tickets",
                "runs",
                "mandates",
                "usage_calls",
                "sa_coding_bindings",
                "sa_coding_attempts",
                "sa_coding_operations",
                "sa_coding_payloads",
                "sa_coding_verifications",
                "sa_coding_events",
            )
        }
