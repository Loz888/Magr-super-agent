"""Frozen synthetic retrieval set and private candidate boundaries, not semantic live."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sa04_support import envelope
from sa14_support import new_source
from sa15_support import TEXT, path, ref, setup
from test_super_agent_sa02 import bind

from mgar_v3.domain import DomainError
from mgar_v3.sources.ingress_domain import GapDeclaration
from mgar_v3.super_agent.policy_domain import PolicyDefinition
from mgar_v3.super_agent.source_recall_domain import (
    RecallResolveRequest,
    RecallSearchRequest,
    RecallWalkRequest,
)

CASES = json.loads(Path("fixtures/super-agent-v1/sa15/retrieval-quality.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_frozen_synthetic_query_returns_authorized_task_and_real_status(engine, tmp_path, case):
    env = setup(engine, tmp_path)
    now = datetime.now(UTC)
    after, before = now - timedelta(minutes=5), now + timedelta(minutes=1)
    if case["mode"] == "outside_window":
        after, before = now - timedelta(days=2), now - timedelta(days=1)
    if case["mode"] == "ambiguous":
        env.source.gateway.receive(
            envelope(
                env.source,
                delivery="sa15-other-login",
                entity="sa15-other-login",
                body="登录的另一个问题",
            )
        )
    result = env.recall.search(
        env.owner, RecallSearchRequest(query=case["query"], after=after, before=before)
    )
    task_ids = [task["node"]["ref"]["id"] for row in result["results"] for task in row["tasks"]]
    assert (str(env.target.run_id) in task_ids) is case["expected_task"], result
    if not case["expected_task"] or case["mode"] == "ambiguous":
        assert result["clarification_required"] is True and result["clarification"]
    else:
        assert result["clarification_required"] is False
        assert result["results"][0]["tasks"][0]["node"]["state"] == "running"
    assert TEXT not in json.dumps(result, ensure_ascii=False)
    assert result["model_calls"] == 0 and result["grants_authority"] is False


def test_same_installation_other_authorized_conversation_is_not_worker_scope(engine, tmp_path):
    # Prepare both receive scopes before minting the immutable original Coding target.
    source = new_source(engine)
    bind(
        source.identities,
        source.cookie,
        source.installation,
        roles=("context_provider", "requester"),
        conversation="Chat-B",
    )
    raw = source.definition.model_dump(mode="json")
    raw["selector"]["conversations"] = ["Chat-A", "Chat-B"]
    source.definition = PolicyDefinition.model_validate(raw)
    env = setup_from_source(engine, tmp_path, source)
    source.gateway.receive(
        envelope(
            source,
            delivery="private-other-chat",
            entity="private-other-chat",
            conversation="Chat-B",
            body="PRIVATE_OTHER_CHAT 登录 secret",
        )
    )
    with engine.transaction() as conn:
        private_id = conn.execute(
            "SELECT input_id FROM app.source_ingress_inputs ORDER BY received_at DESC LIMIT 1"
        ).fetchone()["input_id"]
    # The console owner can read both explicitly received groups; a Worker cannot.
    assert engine.source_recall.resolve(
        env.owner, RecallResolveRequest(path=path(ref("source", private_id)))
    )
    with pytest.raises(DomainError):
        engine.source_recall.resolve(
            env.worker_caller, RecallResolveRequest(path=path(ref("source", private_id)))
        )
    result = engine.source_recall.search(
        env.worker_caller, RecallSearchRequest(query="登录", limit=1)
    )
    assert str(private_id) not in json.dumps(result)
    assert "PRIVATE_OTHER_CHAT" not in json.dumps(result)


def setup_from_source(engine, tmp_path, source):
    from sa13_support import setup as coding_setup

    from mgar_v3.super_agent.source_recall_access import RecallCaller

    env = coding_setup(engine, tmp_path, source_env=source)
    env.owner = RecallCaller("owner", env.cookie)
    env.worker_caller = RecallCaller("worker", env.worker["bearer"], env.run_request)
    return env


def test_different_installation_same_names_and_private_candidate_ids_hidden(engine, tmp_path):
    from unittest.mock import patch

    from mgar_v3.sources.domain import SourceScope

    env = setup(engine, tmp_path)
    cipher = engine.source_ingress.cipher

    def other_scope(**kwargs):
        kwargs["installation_id"] = "Install-B"
        return SourceScope(**kwargs)

    with patch("sa14_support.SourceScope", side_effect=other_scope):
        other = new_source(engine, cookie=env.cookie)
    engine.source_ingress.cipher = cipher
    other.gateway.receive(
        envelope(other, delivery="other-installation", body="登录 PRIVATE_INSTALLATION")
    )
    with engine.transaction() as conn:
        private_id = conn.execute(
            "SELECT input_id FROM app.source_ingress_inputs WHERE source_instance_id=%s",
            (other.installation.scope.source_instance_id,),
        ).fetchone()["input_id"]
    with pytest.raises(DomainError):
        env.recall.resolve(
            env.worker_caller, RecallResolveRequest(path=path(ref("source", private_id)))
        )
    with pytest.raises(DomainError):
        env.recall.search(
            env.worker_caller,
            RecallSearchRequest(
                query="登录", source_instance_id=other.installation.scope.source_instance_id
            ),
        )
    result = env.recall.search(env.worker_caller, RecallSearchRequest(query="登录", limit=1))
    assert str(private_id) not in json.dumps(result)
    assert "PRIVATE_INSTALLATION" not in json.dumps(result)


def test_gap_is_explicit_bodyless_uncertainty_and_not_false_recovery(engine, tmp_path):
    env = setup(engine, tmp_path)
    now = datetime.now(UTC)
    engine.source_ingress.gap(
        env.source.installation.scope.source_instance_id,
        GapDeclaration(
            stream_id="synthetic-source",
            epoch="epoch1",
            reason="no_replay",
            started_at=now - timedelta(minutes=2),
            ended_at=now,
        ),
    )
    result = env.recall.walk(env.owner, RecallWalkRequest(path=path(env.source_ref), max_hops=1))
    gaps = [node for node in result["nodes"] if node["ref"]["kind"] == "gap"]
    assert gaps and gaps[0]["state"] == "gap"
    assert {"name": "uncertainty", "value": "history_not_proven_complete"} in gaps[0]["facts"]
    assert not gaps[0]["content_available"]


def test_new_task_dependency_must_be_readable_before_derived_content(engine, tmp_path):
    env = setup(engine, tmp_path)
    # A context edge to an inaccessible Task never conveys that Task's identity.
    private = uuid4()
    with engine.transaction() as conn:
        conn.execute(
            "INSERT INTO knowledge.context_edges(edge_id,project_id,repository,actor,"
            "source_kind,source_id,target_kind,target_id,relation,run_id) "
            "VALUES(%s,%s,%s,%s,'run',%s,'run',%s,'related_to',%s)",
            (
                uuid4(),
                engine.settings.project_id,
                engine.settings.repository.lower(),
                env.mandate.origin.engine_actor,
                env.target.run_id,
                private,
                env.target.run_id,
            ),
        )
    result = env.recall.walk(
        env.worker_caller, RecallWalkRequest(path=path(ref("run", env.target.run_id)))
    )
    assert str(private) not in json.dumps(result)
