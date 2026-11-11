"""SA05-I01/I02 regression. Original independent assertions retained; real PG, no live API."""

import json
from unittest.mock import patch

import pytest
from sa05_support import payload, setup, signed

from mgar_v3.domain import DomainError
from mgar_v3.sources.github_domain import GitHubSettings
from mgar_v3.sources.identity_domain import BindIdentity
from mgar_v3.sources.ingress_domain import IngressConfigure
from mgar_v3.super_agent.policy_domain import PolicySave


@pytest.mark.parametrize("action", ["edited", "deleted"])
@pytest.mark.parametrize("moderator_first", [False, True])
def test_resource_author_mutation_invalidates_moderator_first_body(engine, action, moderator_first):
    env = setup(engine)
    raw = payload()
    author = (
        {"id": 43, "type": "User", "login": "original-author"}
        if moderator_first
        else raw["sender"].copy()
    )
    raw["action"] = "edited"
    raw["comment"]["user"] = author
    raw["comment"]["body"] = "first observed moderator-edited body"
    first = env.runtime.receive(signed(raw, delivery="first-observed-edit"), 1)
    assert first.disposition == "accepted"
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    raw["action"] = action
    raw["sender"] = author
    raw["comment"]["body"] = "new original author content must not be retained if not selected"
    try:
        receipt = env.runtime.receive(signed(raw, delivery="author-mutation"), 1)
        print(
            {
                "action": action,
                "moderator_first": moderator_first,
                "disposition": receipt.disposition,
            }
        )
    except DomainError as error:
        print({"action": action, "moderator_first": moderator_first, "mutation_error": error.code})
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])


@pytest.mark.parametrize("action", ["edited", "deleted"])
@pytest.mark.parametrize("oversized", [False, True])
def test_authenticated_mutation_invalidates_previous_body(engine, action, oversized):
    env = setup(engine)
    raw = payload()
    raw["comment"]["body"] = "previous body must become unreadable"
    first = env.runtime.receive(signed(raw, delivery="original"), 1)
    assert first.disposition == "accepted"
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    raw["action"] = action
    raw["comment"]["body"] = "界" * 30000 if oversized else "replacement body"
    error_code = None
    try:
        receipt = env.runtime.receive(signed(raw, delivery="mutation"), 1)
        print({"action": action, "oversized": oversized, "disposition": receipt.disposition})
    except DomainError as error:
        error_code = error.code
        print({"action": action, "oversized": oversized, "mutation_error": error_code})
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])


@pytest.mark.parametrize("action", ["edited", "deleted"])
def test_selected_original_author_after_selected_moderator(engine, action):
    def two_actor_settings(**kw):
        return GitHubSettings(**{**kw, "sender_ids": (42, 43)})

    with patch("sa05_support.GitHubSettings", two_actor_settings):
        env = setup(engine)
    author = {"id": 43, "type": "User", "login": "original-author"}
    identity_payload = payload(sender=author)
    identity_payload["comment"]["user"] = author
    proof = env.identities.authenticate(
        env.source_id, signed(identity_payload, delivery="author-proof")
    )
    record = next(
        r for r in env.identities.evidence(env.cookie) if r["evidence_id"] == proof.evidence_id
    )
    env.identities.bind(
        env.cookie,
        BindIdentity(
            evidence_id=proof.evidence_id,
            expected_identity_revision=record["identity_revision"],
            roles=("context_provider", "requester"),
            review_ref="independent-selected-author",
        ),
    )
    actor = env.adapter.verify_identity(env.instance, signed(identity_payload)).actor
    raw_policy = env.policy.model_dump(mode="json")
    raw_policy["selector"]["actors"].append(actor.model_dump(mode="json"))
    env.engine.source_policies.save(
        env.cookie,
        env.policy_id,
        PolicySave(
            expected_revision=1, definition=raw_policy, review_ref="independent-both-actors"
        ),
    )
    env.engine.source_ingress.configure(
        env.cookie,
        env.source_id,
        IngressConfigure(
            expected_generation=1,
            settings=env.control.model_copy(update={"policy_revision": 2}),
            review_ref="independent-both-actors",
        ),
    )
    raw = payload(action="edited")
    raw["comment"]["user"] = author
    raw["comment"]["body"] = "first observed moderator edit"
    first = env.runtime.receive(signed(raw, delivery="moderator-first"), 2)
    assert first.disposition == "accepted"
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    raw["action"] = action
    raw["sender"] = author
    raw["comment"]["body"] = "original author later content"
    try:
        result = env.runtime.receive(signed(raw, delivery="selected-author-mutation"), 2)
        print({"action": action, "disposition": result.disposition})
    except DomainError as error:
        print({"action": action, "mutation_error": error.code})
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])


def two_actors(engine):
    def settings(**kw):
        return GitHubSettings(**{**kw, "sender_ids": (42, 43)})

    with patch("sa05_support.GitHubSettings", settings):
        env = setup(engine)
    author = {"id": 43, "type": "User", "login": "original-author"}
    data = payload(sender=author)
    data["comment"]["user"] = author
    credential = env.identities.authenticate(env.source_id, signed(data, delivery="author-proof"))
    record = next(
        r for r in env.identities.evidence(env.cookie) if r["evidence_id"] == credential.evidence_id
    )
    env.identities.bind(
        env.cookie,
        BindIdentity(
            evidence_id=credential.evidence_id,
            expected_identity_revision=record["identity_revision"],
            roles=("context_provider", "requester"),
            review_ref="repair-explicit-original-author",
        ),
    )
    actor = env.adapter.verify_identity(env.instance, signed(data)).actor
    policy = env.policy.model_dump(mode="json")
    policy["selector"]["actors"].append(actor.model_dump(mode="json"))
    env.engine.source_policies.save(
        env.cookie,
        env.policy_id,
        PolicySave(expected_revision=1, definition=policy, review_ref="repair-both-actors"),
    )
    env.engine.source_ingress.configure(
        env.cookie,
        env.source_id,
        IngressConfigure(
            expected_generation=1,
            settings=env.control.model_copy(update={"policy_revision": 2}),
            review_ref="repair-both-actors",
        ),
    )
    return env, author


@pytest.mark.parametrize(
    "family,field,action",
    [
        ("issues", "issue", "edited"),
        ("issues", "issue", "deleted"),
        ("issue_comment", "comment", "edited"),
        ("issue_comment", "comment", "deleted"),
        ("pull_request", "pull_request", "edited"),
        ("pull_request_review", "review", "edited"),
        ("pull_request_review", "review", "dismissed"),
        ("pull_request_review_comment", "comment", "edited"),
        ("pull_request_review_comment", "comment", "deleted"),
    ],
)
@pytest.mark.parametrize("moderator_first", [False, True])
def test_unicode_mutation_matrix_never_retains_large_content_or_revives_old(
    engine, family, field, action, moderator_first
):
    env, author = two_actors(engine)
    raw = payload(family, action="edited")
    raw[field]["user"] = author if moderator_first else raw["sender"].copy()
    raw[field]["body"] = "small trusted content"
    first = env.runtime.receive(signed(raw, family, delivery="before-large"), 2)
    before = json.loads(env.engine.source_ingress.body(env.cookie, first.input_ids[0]))
    assert before["resource"]["author_id"] == (43 if moderator_first else 42)
    raw["action"] = action
    raw["sender"] = author if moderator_first else raw["sender"].copy()
    raw[field]["body"] = "界" * 30000
    candidate = signed(raw, family, delivery="large-change")
    assert len(candidate.payload) < 262144
    receipt = env.runtime.receive(candidate, 2)
    assert receipt.committed
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    for identifier in receipt.input_ids:
        with pytest.raises(DomainError):
            env.engine.source_ingress.body(env.cookie, identifier)
    again = env.runtime.receive(candidate, 2)
    assert again.input_ids == receipt.input_ids
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_bodies").fetchone()["n"] == 0
        )
        inputs = conn.execute(
            "SELECT metadata,sender_ref FROM app.source_ingress_inputs WHERE receipt_id=%s",
            (receipt.receipt_id,),
        ).fetchall()
        for row in inputs:
            assert row["sender_ref"]["actor_id"] == str(raw["sender"]["id"])
            assert "界" * 100 not in json.dumps(row, ensure_ascii=False)


@pytest.mark.parametrize("action", ["edited", "deleted"])
def test_configured_body_bound_commits_invalidation_instead_of_rolling_back(engine, action):
    env = setup(engine)
    controls = env.control.model_copy(update={"max_body_bytes": 1024})
    env.engine.source_ingress.configure(
        env.cookie,
        env.source_id,
        IngressConfigure(
            expected_generation=1, settings=controls, review_ref="small-content-bound"
        ),
    )
    raw = payload()
    raw["comment"]["body"] = "short"
    first = env.runtime.receive(signed(raw, delivery="within-configured-limit"), 2)
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    raw["action"] = action
    raw["comment"]["body"] = "界" * 600
    result = env.runtime.receive(signed(raw, delivery="above-configured-limit"), 2)
    assert result.committed
    if action == "edited":
        assert result.disposition == "rejected" and result.reason_code == "INGRESS_BODY_BOUND"
        assert not result.input_ids
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_bodies").fetchone()["n"] == 0
        )


def test_resource_author_is_not_the_receive_principal(engine):
    env = setup(engine)
    raw = payload(action="edited")
    raw["comment"]["user"] = {"id": 43, "type": "User", "login": "original"}
    first = env.runtime.receive(signed(raw, delivery="moderator-writes"), 1)
    stored = json.loads(env.engine.source_ingress.body(env.cookie, first.input_ids[0]))
    assert stored["resource"]["author_id"] == 43
    with env.engine.transaction() as conn:
        row = conn.execute(
            "SELECT sender_ref FROM app.source_ingress_inputs WHERE input_id=%s",
            (first.input_ids[0],),
        ).fetchone()
        assert row["sender_ref"]["actor_id"] == "42"
    raw["sender"] = raw["comment"]["user"]
    raw["comment"]["body"] = "ORIGINAL_UNSELECTED_BODY_NEVER_STORED"
    last = env.runtime.receive(signed(raw, delivery="unselected-original-writes"), 1)
    assert last.disposition == "rejected" and not last.input_ids
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    with env.engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM app.source_identities WHERE actor_ref->>'actor_id'='43'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("fault", ["signature", "installation", "repository"])
def test_large_unverified_mutation_cannot_invalidate_current_body(engine, fault):
    env = setup(engine)
    raw = payload()
    first = env.runtime.receive(signed(raw, delivery="valid-original"), 1)
    original = env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    raw["action"] = "edited"
    raw["comment"]["body"] = "界" * 30000
    headers = {}
    if fault == "signature":
        headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    elif fault == "installation":
        raw["installation"]["id"] = 201
    else:
        raw["repository"]["id"] = 901
    with pytest.raises(DomainError):
        env.runtime.receive(signed(raw, delivery="unverified-large", headers=headers), 1)
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0]) == original


def test_generic_nonmoderator_does_not_borrow_github_resource_authority(engine):
    from sa04_support import envelope
    from sa04_support import setup as generic_setup

    env = generic_setup(engine)
    first = env.gateway.receive(envelope(env))
    original = env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    env.gateway.receive(
        envelope(
            env,
            delivery="other-actor",
            operation="edit",
            sender="Other-Actor",
            version=2,
            body='{"author_id":"Alice-AbC","platform_moderation":true}',
        )
    )
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0]) == original


def test_first_oversized_edit_is_bodyless_and_later_small_edit_stays_ambiguous(engine):
    env = setup(engine)
    raw = payload(action="edited")
    raw["comment"]["body"] = "界" * 30000
    first = env.runtime.receive(signed(raw, delivery="first-large"), 1)
    assert first.committed
    for identifier in first.input_ids:
        with pytest.raises(DomainError):
            env.engine.source_ingress.body(env.cookie, identifier)
    raw["comment"]["body"] = "smaller later content with no native order"
    later = env.runtime.receive(signed(raw, delivery="later-small"), 1)
    for identifier in later.input_ids:
        with pytest.raises(DomainError):
            env.engine.source_ingress.body(env.cookie, identifier)


def test_large_new_create_remains_rejected_without_privacy_authority(engine):
    env = setup(engine)
    raw = payload()
    raw["comment"]["body"] = "界" * 30000
    with pytest.raises(DomainError) as exc:
        env.runtime.receive(signed(raw), 1)
    assert exc.value.code == "INGRESS_BODY_BOUND"
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_inputs").fetchone()["n"] == 0
        )


@pytest.mark.parametrize("selected", [False, True])
def test_operator_transition_with_identical_content_and_timestamp_is_a_new_observation(
    engine, selected
):
    if selected:
        env, author = two_actors(engine)
        generation = 2
    else:
        env = setup(engine)
        author = {"id": 43, "type": "User", "login": "original"}
        generation = 1
    raw = payload(action="edited")
    raw["comment"]["user"] = author
    raw["comment"]["body"] = "identical text, different verified operator"
    first = env.runtime.receive(signed(raw, delivery="same-content-moderator"), generation)
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    raw["sender"] = author
    result = env.runtime.receive(signed(raw, delivery="same-content-original-author"), generation)
    assert result.committed
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])


@pytest.mark.parametrize("oversized", [False, True])
def test_current_history_after_moderator_first_observation_invalidates_old_body(engine, oversized):
    import httpx

    from mgar_v3.sources.gateway import SourceGateway
    from mgar_v3.sources.github_broker import GitHubHistory
    from mgar_v3.sources.ingress_domain import HistoryRequest

    env, author = two_actors(engine)
    raw = payload(action="edited")
    raw["comment"]["user"] = author
    first = env.runtime.receive(signed(raw, delivery="moderator-before-history"), 2)
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    row = dict(
        raw["comment"],
        user=author,
        body="界" * 30000 if oversized else "current snapshot by author",
    )
    env.response = lambda request: (
        httpx.Response(200, json=[row])
        if request.method == "GET" and request.url.path.endswith("/comments")
        else None
    )
    subject = env.adapter.verify_identity(env.instance, signed(raw))
    page = SourceGateway(env.engine.source_ingress, env.source_id, 2).history(
        GitHubHistory(env.broker), HistoryRequest(source_thread=subject.source_thread, limit=1)
    )
    assert len(page.deliveries) == 1
    assert page.deliveries[0].metadata.items[0].event.sender.actor_id == "43"
    if oversized:
        assert page.deliveries[0].bodies == (None,)
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    with env.engine.transaction() as conn:
        assert all(
            r["context_only"]
            for r in conn.execute("SELECT context_only FROM app.source_ingress_inputs").fetchall()
        )


def test_configured_oversized_content_is_not_eligible_for_transport_spool(engine):
    env = setup(engine)
    env.engine.source_ingress.configure(
        env.cookie,
        env.source_id,
        IngressConfigure(
            expected_generation=1,
            settings=env.control.model_copy(update={"max_body_bytes": 1024}),
            review_ref="spool-content-bound",
        ),
    )
    raw = payload(action="edited")
    raw["comment"]["body"] = "界" * 600
    delivery = env.adapter.verify_ingress(env.instance, signed(raw))
    assert len(delivery.bodies[0]) > 1024
    assert env.engine.source_ingress.buffer_permit(env.instance, 2, delivery) is None


def test_configured_bound_privacy_fact_and_receipt_roll_back_together(engine):
    from mgar_v3.sources.gateway import SourceGateway

    env = setup(engine)
    env.engine.source_ingress.configure(
        env.cookie,
        env.source_id,
        IngressConfigure(
            expected_generation=1,
            settings=env.control.model_copy(update={"max_body_bytes": 1024}),
            review_ref="transaction-bound",
        ),
    )
    raw = payload()
    raw["comment"]["body"] = "short original"
    first = env.runtime.receive(signed(raw, delivery="before-forced-rollback"), 2)
    content = env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    raw["action"] = "edited"
    raw["comment"]["body"] = "界" * 600

    def fail(point):
        if point == "after_input_write":
            raise RuntimeError("forced transaction rollback")

    gateway = SourceGateway(
        env.engine.source_ingress,
        env.source_id,
        2,
        {("github", "github-app-v1"): env.adapter},
        checkpoint=fail,
    )
    with pytest.raises(RuntimeError, match="forced transaction rollback"):
        gateway.receive(signed(raw, delivery="rolled-back-oversized-edit"))
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0]) == content
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_deliveries").fetchone()["n"]
            == 1
        )


def test_equivalent_moderated_edit_redelivery_preserves_canonical_body(engine):
    env = setup(engine)
    raw = payload(action="edited")
    raw["comment"]["user"] = {"id": 43, "type": "User", "login": "original"}
    a = env.runtime.receive(signed(raw, delivery="canonical-moderator"), 1)
    body = env.engine.source_ingress.body(env.cookie, a.input_ids[0])
    b = env.runtime.receive(signed(raw, delivery="same-moderator-version"), 1)
    assert a.input_ids == b.input_ids
    assert env.engine.source_ingress.body(env.cookie, a.input_ids[0]) == body
