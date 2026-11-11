"""SA05-RR01/RR02. Exact independent assertions plus history authority regressions."""

import httpx
import pytest
from sa05_support import payload, setup, signed
from test_super_agent_sa05_repair import two_actors

from mgar_v3.domain import DomainError
from mgar_v3.sources.domain import ActorRef
from mgar_v3.sources.gateway import SourceGateway
from mgar_v3.sources.github_broker import GitHubHistory
from mgar_v3.sources.ingress_domain import HistoryRequest, IngressConfigure
from mgar_v3.super_agent.policy_domain import PolicySave


def history(env, raw, row):
    env.response = lambda request: (
        httpx.Response(200, json=[row])
        if request.method == "GET" and request.url.path.endswith("/comments")
        else None
    )
    subject = env.adapter.verify_identity(env.instance, signed(raw))
    return SourceGateway(env.engine.source_ingress, env.source_id, 1).history(
        GitHubHistory(env.broker), HistoryRequest(source_thread=subject.source_thread, limit=1)
    )


@pytest.mark.parametrize("history_first", [False, True])
def test_equivalent_live_create_and_history_are_one_canonical_input(engine, history_first):
    env = setup(engine)
    raw = payload()
    row = dict(raw["comment"])
    error = None
    try:
        if history_first:
            history(env, raw, row)
            result = env.runtime.receive(signed(raw, delivery="equivalent-live"), 1)  # noqa: F841
        else:
            result = env.runtime.receive(signed(raw, delivery="equivalent-live"), 1)  # noqa: F841
            history(env, raw, row)
    except DomainError as e:
        error = e.code
    with env.engine.transaction() as conn:
        count = conn.execute("SELECT count(*) AS n FROM app.source_ingress_inputs").fetchone()["n"]
        conflicts = conn.execute(
            "SELECT count(*) AS n FROM app.source_ingress_conflicts"
        ).fetchone()["n"]
    print(
        {
            "history_first": history_first,
            "error": error,
            "input_count": count,
            "conflicts": conflicts,
        }
    )
    assert error is None, (
        "Equivalent current history and live create must deduplicate, not conflict: " + str(error)
    )
    assert count == 1 and conflicts == 0


def test_changed_current_history_invalidates_body_of_unselected_original_author(engine):
    env = setup(engine)
    raw = payload(action="edited")
    raw["comment"]["user"] = {"id": 43, "type": "User", "login": "unselected-original-author"}
    raw["comment"]["body"] = "first observed selected-moderator edit"
    first = env.runtime.receive(signed(raw, delivery="moderator-first"), 1)
    assert first.disposition == "accepted"
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    row = dict(raw["comment"], body="changed current content from original author")
    page = history(env, raw, row)
    print({"history_deliveries": len(page.deliveries)})
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])


def current_history(env, raw, row, generation=1):
    env.response = lambda request: (
        httpx.Response(200, json=[row])
        if request.method == "GET" and request.url.path.endswith("/comments")
        else None
    )
    subject = env.adapter.verify_identity(env.instance, signed(raw))
    return SourceGateway(env.engine.source_ingress, env.source_id, generation).history(
        GitHubHistory(env.broker), HistoryRequest(source_thread=subject.source_thread, limit=1)
    )


@pytest.mark.parametrize("pr", [False, True])
@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("oversized", [False, True])
def test_current_snapshot_observes_unselected_or_selected_author_without_inventing_editor(
    engine, pr, selected, oversized
):
    if selected:
        env, author = two_actors(engine)
        generation = 2
    else:
        env = setup(engine)
        author = {"id": 43, "type": "User", "login": "unselected-original"}
        generation = 1
    raw = payload(action="edited")
    if pr:
        raw["issue"]["pull_request"] = {}
    raw["comment"]["user"] = author
    raw["comment"]["body"] = "moderator 42 first observed trusted content"
    first = env.runtime.receive(signed(raw, delivery="history-moderator-first"), generation)
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    with env.engine.transaction() as conn:
        before = conn.execute("SELECT count(*) AS n FROM app.source_identity_evidence").fetchone()[
            "n"
        ]
    changed = dict(
        raw["comment"], body="界" * 30000 if oversized else "denied current changed body"
    )
    page = current_history(env, raw, changed, generation)
    assert len(page.deliveries) == 1
    delivery = page.deliveries[0]
    item = delivery.metadata.items[0]
    assert item.operation == "create"  # An authored snapshot, never a claimed editor event.
    assert item.event.sender.actor_id == "43"
    assert item.event.historical_backfill
    assert delivery.metadata.verification_method == "installation_proof"
    assert item.platform_moderation
    if not selected or oversized:
        assert delivery.bodies == (None,) and item.event.body_ref is None
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_bodies").fetchone()["n"] == 0
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_identity_evidence").fetchone()["n"]
            == before
        )
        assert conn.execute("SELECT ambiguous FROM app.source_ingress_entities").fetchone()[
            "ambiguous"
        ]
        if not selected:
            assert (
                conn.execute(
                    "SELECT 1 FROM app.source_identities WHERE actor_ref->>'actor_id'='43'"
                ).fetchone()
                is None
            )
            assert (
                conn.execute(
                    "SELECT 1 FROM app.source_ingress_inputs WHERE sender_ref->>'actor_id'='43'"
                ).fetchone()
                is None
            )
            receipt = conn.execute(
                "SELECT * FROM app.source_ingress_deliveries WHERE delivery_id=%s",
                (delivery.metadata.delivery_id,),
            ).fetchone()
            assert receipt["disposition"] == "rejected" and receipt["denied_count"] == 1


@pytest.mark.parametrize("author_id", [88, 99])
def test_unselected_and_own_bot_authored_snapshot_only_invalidates(engine, author_id):
    env = setup(engine)
    raw = payload(action="edited")
    raw["comment"]["user"] = {"id": author_id, "type": "Bot", "login": "unselected-bot"}
    first = env.runtime.receive(signed(raw, delivery="moderated-bot-content"), 1)
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    page = current_history(env, raw, dict(raw["comment"], body="bot content changed"))
    assert len(page.deliveries) == 1 and page.deliveries[0].bodies == (None,)
    assert page.deliveries[0].metadata.items[0].event.sender.actor_kind == "bot"
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    with env.engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_inputs").fetchone()["n"] == 1
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM app.source_ingress_bodies").fetchone()["n"] == 0
        )
        assert conn.execute("SELECT count(*) AS n FROM app.source_identities").fetchone()["n"] == 1


@pytest.mark.parametrize("pr", [False, True])
@pytest.mark.parametrize("history_first", [False, True])
def test_equivalent_live_history_keeps_body_authority_and_original_context_classification(
    engine, pr, history_first
):
    env = setup(engine)
    raw = payload()
    if pr:
        raw["issue"]["pull_request"] = {}
    row = dict(raw["comment"])
    if history_first:
        page = current_history(env, raw, row)
        with env.engine.transaction() as conn:
            identifier = conn.execute("SELECT input_id FROM app.source_ingress_inputs").fetchone()[
                "input_id"
            ]
    else:
        result = env.runtime.receive(signed(raw, delivery="canonical-live"), 1)
        identifier = result.input_ids[0]
    original_body = env.engine.source_ingress.body(env.cookie, identifier)
    if history_first:
        result = env.runtime.receive(signed(raw, delivery="canonical-live"), 1)
        assert result.input_ids == (identifier,)
    else:
        page = current_history(env, raw, row)
    assert len(page.deliveries) == 1
    assert env.engine.source_ingress.body(env.cookie, identifier) == original_body
    current_history(env, raw, row)
    with env.engine.transaction() as conn:
        rows = conn.execute(
            "SELECT input_id,context_only,unavailable_at FROM app.source_ingress_inputs"
        ).fetchall()
        assert len(rows) == 1 and rows[0]["input_id"] == identifier
        assert (
            rows[0]["context_only"] == history_first
        )  # Redelivery never upgrades historical authority.
        assert rows[0]["unavailable_at"] is None
        assert not conn.execute("SELECT 1 FROM app.source_ingress_conflicts").fetchone()
        assert not conn.execute("SELECT ambiguous FROM app.source_ingress_entities").fetchone()[
            "ambiguous"
        ]


def test_invalid_author_type_is_not_a_trusted_snapshot_mutation(engine):
    env = setup(engine)
    raw = payload()
    first = env.runtime.receive(signed(raw, delivery="before-invalid-history"), 1)
    original_body = env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    row = dict(raw["comment"], user={"id": 43, "type": "Organization"}, body="untrusted change")
    with pytest.raises(DomainError) as error:
        current_history(env, raw, row)
    assert error.value.code == "GITHUB_HISTORY_INVALID"
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0]) == original_body


@pytest.mark.parametrize(
    "changes", [{"body": "conflicting content"}, {"sender": "Other-Actor"}, {"moderation": True}]
)
def test_real_content_identity_or_moderation_difference_still_conflicts(engine, changes):
    from sa04_support import envelope
    from sa04_support import setup as generic_setup

    env = generic_setup(engine)
    first = env.gateway.receive(envelope(env))
    original_body = env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    with pytest.raises(DomainError) as error:
        env.gateway.receive(envelope(env, delivery="conflicting-fact", **changes))
    assert error.value.code == "INGRESS_EVENT_CONFLICT"
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0]) == original_body


def test_history_policy_actor_cannot_exceed_app_ceiling(engine):
    env = setup(engine)
    data = env.policy.model_dump(mode="json")
    actor = ActorRef(
        scope=env.instance.scope, id_type="github_id", actor_id="43", actor_kind="human"
    )
    data["selector"]["actors"].append(actor.model_dump(mode="json"))
    env.engine.source_policies.save(
        env.cookie,
        env.policy_id,
        PolicySave(expected_revision=1, definition=data, review_ref="history-ceiling-negative"),
    )
    env.engine.source_ingress.configure(
        env.cookie,
        env.source_id,
        IngressConfigure(
            expected_generation=1,
            settings=env.control.model_copy(update={"policy_revision": 2}),
            review_ref="history-ceiling-negative",
        ),
    )
    with pytest.raises(DomainError) as error:
        current_history(env, payload(), payload()["comment"], 2)
    assert error.value.code == "GITHUB_POLICY_SCOPE"
    assert not env.calls


def test_history_still_requires_current_explicit_receive_permission(engine):
    env = setup(
        engine,
        receive={
            "enabled": True,
            "retain_body": True,
            "body_ttl_seconds": 300,
            "allow_history_context": False,
        },
    )
    raw = payload()
    first = env.runtime.receive(signed(raw, delivery="live-only-authority"), 1)
    original_body = env.engine.source_ingress.body(env.cookie, first.input_ids[0])
    with pytest.raises(DomainError) as error:
        current_history(env, raw, dict(raw["comment"], body="change outside history grant"))
    assert error.value.code == "INGRESS_HISTORY_RECEIVE_DENIED"
    assert not env.calls
    assert env.engine.source_ingress.body(env.cookie, first.input_ids[0]) == original_body
