"""SA19 uses original Engine, Owner, platform settings and genuine temporary keys."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from test_source_console_preparation import secret_payload, source_payload

from mgar_v3.sources.domain import ActorRef, SourceThreadRef
from mgar_v3.sources.ingress_domain import IngressConfigure, IngressSettings
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_console_domain import ConsoleSourceSave
from mgar_v3.web_access import LoginInput, WebAccounts


def owner(engine):
    accounts = WebAccounts(engine)
    identifier = accounts.create_owner(
        LoginInput(username="console-owner", password="sa19-fixture-password")
    )
    cookie = accounts.issue_session(identifier)
    return SimpleNamespace(
        engine=engine,
        service=engine.source_console,
        cookie=cookie,
        accounts=accounts,
        user_id=identifier,
    )


def create_source(env, kind="github", *, credentials=True, enabled=True):
    identifier = uuid4()
    payload = source_payload(kind)
    payload["configuration"]["settings"]["enabled"] = enabled
    if credentials:
        payload.update(
            credentials=secret_payload(kind),
            credential_valid_until=datetime.now(UTC) + timedelta(hours=1),
        )
    request = ConsoleSourceSave.model_validate(payload)
    value = env.service.save_source(env.cookie, identifier, request)
    return SimpleNamespace(env=env, source_id=identifier, request=request, value=value)


def policy(source, *, mode="shadow", **changes):
    scope = source.value["scope"]
    github = scope["source_kind"] == "github"
    actor = ActorRef.model_validate(
        {
            "scope": scope,
            "id_type": "github_id" if github else "open_id",
            "actor_id": "10" if github else "Human_SA19",
            "actor_kind": "human",
        }
    )
    conversation = "repository:4" if github else "Chat_SA19"
    now = datetime.now(UTC)
    raw = {
        "mode": mode,
        "selector": {
            "scope": scope,
            "source_revision": source.value["source_revision"],
            "conversations": [conversation],
            "actors": [actor.model_dump(mode="json")],
            "event_kinds": ["issue_comment.created" if github else "im.message.receive_v1"],
        },
        "valid_from": now - timedelta(seconds=1),
        "valid_until": now + timedelta(hours=1),
        "receive": {"enabled": True, "retain_body": True, "body_ttl_seconds": 300},
        "admission": {"enabled": True, "request_kinds": ["explicit_request"]},
        "execution": {"enabled": False},
        "notification": {"enabled": False},
        "budget": {},
    }
    raw.update(changes)
    return (
        PolicyDefinition.model_validate(raw),
        actor,
        SourceThreadRef(scope=actor.scope, conversation_id=conversation),
    )


def save_policy(source):
    definition, actor, thread = policy(source)
    identifier = uuid4()
    env = source.env
    value = env.service.save_policy(
        env.cookie,
        identifier,
        PolicySave(expected_revision=0, definition=definition, review_ref="fixture-policy"),
    )
    return SimpleNamespace(
        source=source,
        policy_id=identifier,
        definition=definition,
        actor=actor,
        thread=thread,
        value=value,
    )


def bind_ingress(p):
    source = p.source
    env = source.env
    value = env.service.configure_ingress(
        env.cookie,
        source.source_id,
        IngressConfigure(
            expected_generation=0,
            settings=IngressSettings(
                owner="gateway",
                policy_id=p.policy_id,
                policy_revision=p.value["current_revision"],
                legacy_repository=env.engine.settings.repository
                if source.value["source_kind"] == "github"
                else None,
            ),
            review_ref="fixture-ingress",
        ),
    )
    return value
