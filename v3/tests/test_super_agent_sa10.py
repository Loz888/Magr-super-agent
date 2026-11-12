"""Real PostgreSQL correlation/claim tests over authenticated synthetic sources.

Run only after the private service and disposable 0041 test schema are ready.
No real model, external message, worker or publication is used by this suite.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from sa05_support import BASE, HEAD, payload, signed
from sa10_support import (
    TABLES,
    add_im_source,
    controller,
    counts,
    fresh_target,
    ingest_im,
    ingest_native,
    link,
    link_id,
    other_repository_input,
    resolved,
    setup,
    snapshot,
)

from mgar_v3.domain import DomainError, TriggerInput
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.super_agent.policy_domain import PolicyRevoke
from mgar_v3.super_agent.source_correlation_domain import (
    CorrelationCorrectRequest,
    CorrelationLinkRequest,
    CorrelationRemoveRequest,
)


def row(engine, resource_key):
    with engine.transaction() as conn:
        return conn.execute(
            "SELECT h.correlation_id,h.revision,v.ticket_id,v.run_id,v.payload "
            "FROM app.sa_task_correlation_claims c JOIN app.sa_task_correlations h "
            "USING(correlation_id,project_id) JOIN app.sa_task_correlation_versions v "
            "USING(correlation_id,project_id,revision) WHERE c.resource_key=%s "
            "AND c.project_id=%s", (resource_key, engine.settings.project_id),
        ).fetchone()


def test_native_resource_links_existing_original_task_without_business_side_effects(engine):
    env = setup(engine)
    before = counts(engine)
    pending = resolved(env)
    frozen = snapshot(pending)
    assert frozen.inputs[0].input_id == env.native_input_id
    assert frozen.resources[0].resource.repository_id == 900
    assert frozen.resources[0].resource.resource_id == "7"
    assert frozen.resources[0].resource.resource_key == env.resource_key
    assert frozen.resources[0].proof_kind == "verified_resource"
    result = link(env)
    saved = row(engine, env.resource_key)
    assert saved["ticket_id"] == env.target.ticket_id and saved["revision"] == 1
    current = resolved(env)
    assert snapshot(current).heads[0].target.ticket_id == env.target.ticket_id
    assert env.service.get(env.cookie, link_id(result))["revision"] == 1
    assert env.service.links(env.cookie, env.native_input_id)
    assert counts(engine) == before


def test_link_facts_are_bodyless_and_do_not_persist_raw_source_or_url(engine):
    env = setup(engine)
    link(env)
    with engine.transaction() as conn:
        rows = {table: conn.execute("SELECT row_to_json(t) AS value FROM app." + table +
                                   " t").fetchall() for table in TABLES}
    persisted = json.dumps(rows, default=str)
    assert "SA10_SYNTHETIC_PRIVATE_NATIVE_7" not in persisted
    assert "/mgar investigate" not in persisted
    assert "ciphertext" not in persisted


def test_same_ticket_and_inputs_are_idempotent_with_one_resource_claim(engine):
    env = setup(engine)
    first = link(env)
    current = row(engine, env.resource_key)
    second = link(env, expected_revision=current["revision"])
    assert link_id(first) == link_id(second)
    assert row(engine, env.resource_key)["revision"] == 1
    with engine.transaction() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM app.sa_task_correlation_claims WHERE resource_key=%s",
            (env.resource_key,),
        ).fetchone()["n"] == 1


def test_resource_cannot_be_claimed_by_a_second_target_through_plain_link(engine):
    env = setup(engine)
    link(env)
    other = fresh_target(env, second=True)
    before = counts(engine)
    with pytest.raises(DomainError) as error:
        link(env, target=other, expected_revision=1)
    assert error.value.status == 409
    assert row(engine, env.resource_key)["ticket_id"] == env.target.ticket_id
    assert row(engine, env.resource_key)["revision"] == 1
    assert counts(engine) == before


def test_parallel_claim_factory_creates_only_one_actual_original_ticket(engine):
    env = setup(engine, create_target=False)
    assert counts(engine)["tickets"] == 0
    start, mutex, calls = Barrier(2), Lock(), []

    def factory(conn):
        assert conn is engine.connection
        with mutex:
            calls.append("original-ticket-factory")
        return fresh_target(env)

    def claim(_index):
        start.wait(timeout=5)
        return env.service.claim_or_create(
            env.cookie, (env.native_input_id,), env.resource_key, create_target=factory,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert len(calls) == 1
    assert link_id(results[0]) == link_id(results[1])
    actual = counts(engine)
    assert actual["threads"] == actual["mandates"] == actual["tickets"] == actual["runs"] == 1
    assert row(engine, env.resource_key)["ticket_id"] == env.target.ticket_id
    assert actual["actions"] == actual["outbox"] == actual["usage_calls"] == 0


def test_claim_factory_exception_rolls_back_actual_ticket_and_claim_together(engine):
    env = setup(engine, create_target=False)
    before = counts(engine)

    def factory(conn):
        assert conn is engine.connection
        fresh_target(env)
        raise RuntimeError("controlled-SA10-before-commit")

    with pytest.raises(RuntimeError, match="controlled-SA10-before-commit"):
        env.service.claim_or_create(
            env.cookie, (env.native_input_id,), env.resource_key, create_target=factory,
        )
    assert counts(engine) == before
    with engine.transaction() as conn:
        for table in TABLES[1:]:
            assert conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"] == 0


def test_claim_without_trusted_factory_never_creates_an_original_task(engine):
    env = setup(engine, create_target=False)
    before = counts(engine)
    with pytest.raises(DomainError):
        env.service.claim_or_create(env.cookie, (env.native_input_id,), env.resource_key)
    assert counts(engine) == before
    assert row(engine, env.resource_key) is None


def test_stale_target_and_snapshot_cannot_be_written_after_original_thread_changes(engine):
    env = setup(engine)
    old = resolved(env)
    request = CorrelationLinkRequest(
        input_ids=(env.native_input_id,), resource_key=env.resource_key,
        snapshot_sha256=old["snapshot_sha256"], expected_revision=0,
        target=env.target, review_ref="SA10-freeze-before-real-thread-change",
    )
    engine.ingest(TriggerInput(
        source="web", external_event_id="sa10-current-thread-revision",
        project_id=engine.settings.project_id, repository=engine.settings.repository,
        issue_number=7, actor=engine.settings.local_actor,
        body="additional current native issue context", execution_mode="fixture",
    ))
    with pytest.raises(DomainError) as error:
        env.service.link(env.cookie, request)
    assert error.value.status == 409
    assert row(engine, env.resource_key) is None


def test_unexecuted_correction_appends_revision_and_preserves_old_target(engine):
    env = setup(engine)
    first = link(env)
    other = fresh_target(env, second=True)
    frozen = resolved(env)
    corrected = env.service.correct(env.cookie, link_id(first), CorrelationCorrectRequest(
        snapshot_sha256=frozen["snapshot_sha256"], expected_revision=1,
        target=other, review_ref="SA10-owner-corrects-unexecuted-link",
    ))
    assert corrected["revision"] == 2
    saved = row(engine, env.resource_key)
    assert saved["ticket_id"] == other.ticket_id
    with engine.transaction() as conn:
        history = conn.execute(
            "SELECT revision,ticket_id FROM app.sa_task_correlation_versions "
            "WHERE correlation_id=%s ORDER BY revision", (saved["correlation_id"],),
        ).fetchall()
    assert history == [{"revision": 1, "ticket_id": env.target.ticket_id},
                       {"revision": 2, "ticket_id": other.ticket_id}]


def test_correction_rechecks_all_attempts_even_when_latest_run_is_fresh_and_queued(engine):
    env = setup(engine)
    original = env.target.run_id
    engine.begin_run(original)
    engine.pause(original)
    replacement = engine.resume(original)
    assert replacement != original
    current_target = env.service.target(env.cookie, env.target.ticket_id)
    assert current_target.run_id == replacement and current_target.execution_epoch == 0
    first = link(env, target=current_target)
    frozen = resolved(env)
    with pytest.raises(DomainError) as error:
        env.service.correct(env.cookie, link_id(first), CorrelationCorrectRequest(
            snapshot_sha256=frozen["snapshot_sha256"], expected_revision=1,
            target=fresh_target(env, second=True), review_ref="SA10-cannot-retarget-old-execution",
        ))
    assert error.value.status == 409
    saved = row(engine, env.resource_key)
    assert saved["revision"] == 1 and saved["ticket_id"] == env.target.ticket_id
    with engine.transaction() as conn:
        runs = conn.execute(
            "SELECT run_id,status,execution_epoch FROM app.runs WHERE ticket_id=%s "
            "ORDER BY attempt_no", (env.target.ticket_id,),
        ).fetchall()
    assert runs[0]["execution_epoch"] > 0 and runs[0]["status"] == "failed"
    assert runs[1]["execution_epoch"] == 0 and runs[1]["status"] == "queued"


def test_removed_relation_keeps_claim_and_does_not_allow_factory_recreation(engine):
    env = setup(engine)
    first = link(env)
    current = resolved(env)
    env.service.remove(env.cookie, link_id(first), CorrelationRemoveRequest(
        snapshot_sha256=current["snapshot_sha256"], expected_revision=1,
        review_ref="SA10-owner-removes-relation",
    ))
    before, calls = counts(engine), []

    def factory(_conn):
        calls.append("must-not-be-called")
        return env.target

    with pytest.raises(DomainError):
        env.service.claim_or_create(
            env.cookie, (env.native_input_id,), env.resource_key, create_target=factory,
        )
    assert not calls and counts(engine) == before
    assert row(engine, env.resource_key)["revision"] == 2
    assert row(engine, env.resource_key)["payload"]["state"] == "removed"


def test_signed_cross_source_alias_requires_current_read_on_both_edges(engine):
    env = setup(engine)
    first = link(env)
    private = add_im_source(env)
    received = ingest_im(private, body=(
        "SA10_PRIVATE_EDGE https://github.com/example/mgar-test/issues/7"
    ))
    preview = resolved(env, received.input_ids)
    assert all((value.outcome if hasattr(value, "outcome") else value["outcome"]) != "matched"
               for value in preview["resolutions"])
    alias = link(env, input_ids=(env.native_input_id, received.input_ids[0]), expected_revision=1)
    assert link_id(alias) == link_id(first) and alias["revision"] == 2
    current = env.service.get(env.cookie, link_id(alias))
    assert current["revision"] == 2
    engine.source_policies.revoke(env.cookie, private.policy_id, PolicyRevoke(
        expected_revision=1, review_ref="SA10-revoke-private-alias-edge",
    ))
    for read in [lambda: env.service.get(env.cookie, link_id(alias)),
                 lambda: env.service.links(env.cookie, received.input_ids[0])]:
        with pytest.raises(DomainError) as error:
            read()
        assert error.value.status in {403, 404, 409}
        assert "SA10_PRIVATE_EDGE" not in str(error.value)


def test_same_chat_and_native_topic_keep_two_explicit_problems_separate(engine):
    env = setup(engine)
    second_target = fresh_target(env, second=True)
    second, second_resource = ingest_native(env, number=8)
    link(env)
    second_link = link(
        env, target=second_target, input_ids=second.input_ids,
        resource_key=second_resource.resource_key,
    )
    private = add_im_source(env, topic="same-platform-topic")
    controller(env, second_target, sourceenv=private)
    one = ingest_im(private, body="https://github.com/example/mgar-test/issues/7")
    two = ingest_im(private, body="https://github.com/example/mgar-test/issues/8")
    link(env, input_ids=(env.native_input_id, one.input_ids[0]), expected_revision=1)
    alias = link(
        env, target=second_target, input_ids=(second.input_ids[0], two.input_ids[0]),
        resource_key=second_resource.resource_key, expected_revision=1,
    )
    assert link_id(alias) == link_id(second_link)
    with engine.transaction() as conn:
        edges = conn.execute(
            "SELECT DISTINCT correlation_id,conversation_sha256,topic_sha256 "
            "FROM app.sa_thread_link_inputs WHERE input_id=ANY(%s)",
            (list(one.input_ids + two.input_ids),),
        ).fetchall()
    assert len({value["correlation_id"] for value in edges}) == 2
    assert len({value["conversation_sha256"] for value in edges}) == 1
    assert len({value["topic_sha256"] for value in edges}) == 1


def test_same_title_and_issue_number_in_other_verified_repository_do_not_match(engine):
    env = setup(engine)
    link(env)
    other_input, other_resource = other_repository_input(env)
    assert engine.source_ingress.body(env.cookie, other_input)
    try:
        current = resolved(env, (other_input,))
    except DomainError as error:
        # The single-repository product may conservatively reject that boundary.
        assert error.status == 403
    else:
        frozen = snapshot(current)
        assert all(head.target is None for head in frozen.heads)
        assert all(target.ticket_id != env.target.ticket_id for target in frozen.targets)
    assert other_resource.resource_key != env.resource_key
    assert row(engine, other_resource.resource_key) is None


@pytest.mark.parametrize("withdrawal", ["policy", "identity-role"])
def test_native_revocation_never_releases_old_relation_or_body(engine, withdrawal):
    env = setup(engine)
    first = link(env)
    if withdrawal == "policy":
        engine.source_policies.revoke(env.cookie, env.sourceenv.policy_id, PolicyRevoke(
            expected_revision=2, review_ref="SA10-native-policy-revocation",
        ))
    else:
        env.sourceenv.identities.roles(
            env.cookie, env.sourceenv.binding["binding_id"], RoleChange(
                expected_revision=env.sourceenv.binding["revision"], roles=("requester",),
                review_ref="SA10-native-context-provider-removed",
            ),
        )
    for read in [lambda: resolved(env), lambda: env.service.get(env.cookie, link_id(first))]:
        with pytest.raises(DomainError) as error:
            read()
        assert error.value.status in {403, 404, 409}
        assert "SA10_SYNTHETIC_PRIVATE_NATIVE_7" not in str(error.value)
    assert row(engine, env.resource_key)["revision"] == 1


def test_actual_body_ttl_expiry_blocks_relation_read_without_restoring_old_body(engine):
    env = setup(engine, body_ttl_seconds=2)
    first = link(env)
    time.sleep(2.2)
    with pytest.raises(DomainError) as error:
        env.service.get(env.cookie, link_id(first))
    assert error.value.status in {403, 404, 409}
    assert "SA10_SYNTHETIC_PRIVATE_NATIVE_7" not in str(error.value)
    assert row(engine, env.resource_key)["revision"] == 1


def test_real_pr_force_push_invalidates_old_source_and_cannot_reuse_old_code(engine):
    env = setup(engine, family="pull_request")
    first = link(env)
    old = snapshot(resolved(env)).resources[0].code_ref
    assert old is not None and old.head_sha == HEAD and old.base_sha == BASE
    raw = payload("pull_request", action="synchronize")
    raw["pull_request"].update(id=307, number=7)
    raw["pull_request"]["head"]["sha"] = "c" * 40
    # GitHub's derived revisions may conservatively become ambiguous. Neither
    # ambiguity nor a new SHA may keep the old authorized tree current.
    receipt = env.sourceenv.runtime.receive(
        signed(raw, family="pull_request"), env.sourceenv.generation,
    )
    assert receipt.committed
    with pytest.raises(DomainError):
        env.service.get(env.cookie, link_id(first))
    with engine.transaction() as conn:
        recorded = conn.execute(
            "SELECT code FROM app.sa_correlation_resources WHERE input_id=%s "
            "AND resource_key=%s", (env.native_input_id, env.resource_key),
        ).fetchone()
    assert recorded["code"]["head_sha"] == HEAD
    assert counts(engine)["actions"] == counts(engine)["outbox"] == 0
