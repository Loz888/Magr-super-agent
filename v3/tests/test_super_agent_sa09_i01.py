"""Original SA09-I01 independent failures retained as permanent PG regressions."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, get_ident
from time import monotonic_ns

import httpx
import pytest
from sa09_support import ingest_case, prepared, setup
from test_super_agent_sa09 import frames, live

from mgar_v3.domain import DomainError
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.super_agent.policy_domain import PolicyRevoke
from mgar_v3.super_agent.source_eval import SourceEvalProvider
from mgar_v3.super_agent.source_eval_model import SourceEvalModel

PRIVATE = "INDEPENDENT_SA09_PREPOST_PRIVATE"


@pytest.mark.parametrize("withdrawal", ["policy", "identity-role"])
def test_independent_durable_reservation_then_withdrawal_must_not_post_old_source(
    engine, monkeypatch, withdrawal
):
    env = setup(engine)
    permit, profile = live(env)
    receipt = ingest_case(env, body=PRIVATE)
    batch = prepared(env, receipt, mode="live")
    posts = []
    observations = []
    body_checks = []

    def respond(request):
        posts.append(request)
        try:
            with engine.transaction() as c:
                env.service._checked(c, batch)
        except DomainError as e:
            stale = e.code
        else:
            stale = None
        observations.append(
            dict(snapshot_rejected=stale, source_text_in_post=PRIVATE in request.content.decode())
        )
        return httpx.Response(
            200, content=frames(str(batch["source_snapshot"]["groups"][0]["group_id"]))
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = SourceEvalModel(
            profile, permit, client=client, api_key="synthetic-private-review-key"
        )
        original = model.request_body

        def before_actual_model_request(request):
            body_checks.append(1)
            if len(body_checks) == 2:
                assert engine.connection.info.transaction_status.name == "IDLE"
                with engine.transaction() as c:
                    call = c.execute(
                        "SELECT status FROM app.router_calls WHERE batch_id=%s",
                        (batch["batch_id"],),
                    ).fetchone()
                    assert call is not None and call["status"] == "reserved"
                if withdrawal == "policy":
                    engine.source_policies.revoke(
                        env.cookie,
                        env.policy_id,
                        PolicyRevoke(
                            expected_revision=env.policy_revision,
                            review_ref="independent-before-post-revoke",
                        ),
                    )
                else:
                    env.identities.roles(
                        env.cookie,
                        env.binding["binding_id"],
                        RoleChange(
                            expected_revision=env.binding["revision"],
                            roles=("requester",),
                            review_ref="independent-before-post-context-withdrawn",
                        ),
                    )
            return original(request)

        monkeypatch.setattr(model, "request_body", before_actual_model_request)
        error = None
        try:
            SourceEvalProvider(env.service, model).propose(batch)
        except DomainError as e:
            error = e.code
    print(
        "PREPOST_OBSERVATIONS",
        json.dumps(
            dict(
                withdrawal=withdrawal,
                posts=len(posts),
                body_checks=len(body_checks),
                observations=observations,
                caught_error=error,
            )
        ),
        flush=True,
    )
    assert not posts, (
        "Source plaintext was posted after current authority had already been withdrawn"
    )


@pytest.mark.parametrize("withdrawal", ["policy", "identity-role"])
def test_committed_revocation_before_http_dispatch_must_not_release_source(
    engine, monkeypatch, withdrawal
):
    env = setup(engine)
    permit, profile = live(env)
    private = "INDEPENDENT_SA09_CONCURRENT_PRIVATE"
    receipt = ingest_case(env, body=private)
    batch = prepared(env, receipt, mode="live")
    withdrawn = Event()
    timeline = {}
    posts = []
    gates = []
    futures = []

    def revoke():
        if withdrawal == "policy":
            engine.source_policies.revoke(
                env.cookie,
                env.policy_id,
                PolicyRevoke(
                    expected_revision=env.policy_revision,
                    review_ref="independent-real-thread-before-post",
                ),
            )
        else:
            env.identities.roles(
                env.cookie,
                env.binding["binding_id"],
                RoleChange(
                    expected_revision=env.binding["revision"],
                    roles=("requester",),
                    review_ref="independent-real-thread-context-withdrawn",
                ),
            )
        timeline["revocation_committed_ns"] = monotonic_ns()
        withdrawn.set()

    def respond(request):
        sent_at = monotonic_ns()
        posts.append(
            dict(
                post_ns=sent_at,
                revocation_already_committed=withdrawn.is_set(),
                contains_prior_source=private in request.content.decode(),
            )
        )
        return httpx.Response(
            200, content=frames(str(batch["source_snapshot"]["groups"][0]["group_id"]))
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            model = SourceEvalModel(
                profile, permit, client=client, api_key="synthetic-no-real-network-key"
            )
            original = model.request_body

            def dispatch_gate(request):
                gates.append(1)
                if len(gates) == 2:
                    assert engine.connection.info.transaction_status.name == "IDLE"
                    with engine.transaction() as c:
                        assert (
                            c.execute(
                                "SELECT status FROM app.router_calls WHERE batch_id=%s",
                                (batch["batch_id"],),
                            ).fetchone()["status"]
                            == "reserved"
                        )
                    futures.append(pool.submit(revoke))
                    withdrawn.wait(timeout=1)
                return original(request)

            monkeypatch.setattr(model, "request_body", dispatch_gate)
            try:
                SourceEvalProvider(env.service, model).propose(batch)
            except DomainError as e:
                timeline["provider_error"] = e.code
        for f in futures:
            f.result(timeout=5)
    assert futures and withdrawn.is_set()
    print(
        "DISPATCH_REVOKE_ORDER",
        json.dumps(dict(kind=withdrawal, timeline=timeline, posts=posts)),
        flush=True,
    )
    assert not any(
        p["revocation_already_committed"] and p["contains_prior_source"] for p in posts
    ), (
        "A separate thread committed Source revocation before HTTP dispatch, "
        "but the model POST still contained the previously authorized plaintext"
    )


@pytest.mark.parametrize("withdrawal", ["policy", "identity-role"])
def test_revocation_after_reservation_commit_before_model_entry_blocks_source_send(
    engine, monkeypatch, withdrawal
):
    env = setup(engine)
    permit, profile = live(env)
    private = "INDEPENDENT_SA09_COMMIT_BOUNDARY_PRIVATE"
    receipt = ingest_case(env, body=private)
    batch = prepared(env, receipt, mode="live")
    original_transaction = engine.transaction
    owner = get_ident()
    armed = [True]
    withdrawn = Event()
    futures = []
    timeline = {}
    posts = []

    def revoke():
        if withdrawal == "policy":
            engine.source_policies.revoke(
                env.cookie,
                env.policy_id,
                PolicyRevoke(
                    expected_revision=env.policy_revision,
                    review_ref="independent-postcommit-policy-revoke",
                ),
            )
        else:
            env.identities.roles(
                env.cookie,
                env.binding["binding_id"],
                RoleChange(
                    expected_revision=env.binding["revision"],
                    roles=("requester",),
                    review_ref="independent-postcommit-context-revoke",
                ),
            )
        timeline["revocation_commit_ns"] = monotonic_ns()
        withdrawn.set()

    def respond(request):
        posts.append(
            dict(
                post_ns=monotonic_ns(),
                revocation_precedes_post=withdrawn.is_set(),
                prior_source_present=private in request.content.decode(),
            )
        )
        return httpx.Response(
            200, content=frames(str(batch["source_snapshot"]["groups"][0]["group_id"]))
        )

    with ThreadPoolExecutor(max_workers=1) as pool:

        @contextmanager
        def transaction_boundary():
            outer = engine.connection.info.transaction_status.name == "IDLE"
            with original_transaction() as c:
                yield c
            if get_ident() == owner and outer and armed[0]:
                with original_transaction() as c:
                    call = c.execute(
                        "SELECT status FROM app.router_calls WHERE batch_id=%s",
                        (batch["batch_id"],),
                    ).fetchone()
                if call and call["status"] == "reserved":
                    armed[0] = False
                    timeline["reservation_durable_ns"] = monotonic_ns()
                    futures.append(pool.submit(revoke))
                    withdrawn.wait(timeout=1)

        monkeypatch.setattr(engine, "transaction", transaction_boundary)
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            model = SourceEvalModel(
                profile, permit, client=client, api_key="synthetic-no-physical-egress-key"
            )
            original_propose = model.propose

            def entered(request):
                timeline["model_entry_ns"] = monotonic_ns()
                return original_propose(request)

            monkeypatch.setattr(model, "propose", entered)
            try:
                SourceEvalProvider(env.service, model).propose(batch)
            except DomainError as e:
                timeline["provider_error"] = e.code
        for f in futures:
            f.result(timeout=5)
    assert futures and withdrawn.is_set()
    print(
        "DURABLE_MODEL_BOUNDARY",
        json.dumps(dict(kind=withdrawal, timeline=timeline, posts=posts)),
        flush=True,
    )
    assert not any(p["revocation_precedes_post"] and p["prior_source_present"] for p in posts), (
        "Source revocation committed after the durable reservation and before model.propose entry, "
        "but the later POST still released cached prior plaintext"
    )
