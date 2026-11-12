"""SA10 real signed synthetic input and original-Engine fixture helpers.

Only disposable pytest databases may load the unapproved migration draft. No
production schema, real platform/model, network, worker or publication is enabled.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sa04_support import SignedIngress
from sa04_support import envelope as im_envelope
from sa05_support import HEAD, KEY, payload, signed
from sa05_support import setup as github_setup
from test_super_agent_sa02 import ControlledPlatform, bind, source
from test_super_agent_sa03 import definition

from mgar_v3.domain import TriggerInput
from mgar_v3.sources.gateway import SourceGateway
from mgar_v3.sources.github import GitHubAdapter
from mgar_v3.sources.github_domain import VERSION, GitHubConfigure
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.identity_domain import BindIdentity, ControllerGrant, RoleChange
from mgar_v3.sources.ingress_domain import IngressConfigure, IngressSettings
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_correlation_domain import (
    CorrelationLinkRequest,
    CorrelationResolveRequest,
    CorrelationSnapshot,
    ResourceRef,
)

TABLES = (
    "sa_correlation_resources", "sa_task_correlations", "sa_task_correlation_versions",
    "sa_task_correlation_claims", "sa_thread_link_inputs",
)
BUSINESS_TABLES = (
    "threads", "mandates", "tickets", "runs", "subscriptions", "mailbox",
    "actions", "approvals", "outbox", "usage_calls", "router_calls",
)


def install_test_schema(engine):
    """Explicitly load docs draft only into conftest's disposable database, if needed."""
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT current_database() AS database,"
            "to_regclass('app.sa_task_correlations') IS NOT NULL AS installed"
        ).fetchone()
        assert row is not None and row["database"].startswith("mgar_v3_test_")
        if row["installed"]:
            return False
        path = Path(__file__).resolve().parents[1] / (
            "docs/super-agent-v1/sa10-proposal/0041_source_correlations.sql"
        )
        conn.execute(path.read_text())
        return True


def counts(engine, tables=BUSINESS_TABLES):
    with engine.transaction() as conn:
        return {table: conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"]
                for table in tables}


def setup(engine, *, create_target=True, family="issues", body_ttl_seconds=300):
    from mgar_v3.super_agent.source_correlation import SourceCorrelations

    install_test_schema(engine)
    github = github_setup(engine, receive={
        "enabled": True, "retain_body": True, "body_ttl_seconds": body_ttl_seconds,
        "allow_history_context": True,
    })
    github.policy_revision = 1
    github.generation = 1
    github.binding = github.identities.roles(
        github.cookie, github.binding["binding_id"], RoleChange(
            expected_revision=github.binding["revision"],
            roles=("context_provider", "requester", "task_controller"),
            review_ref="SA10-native-controller-role",
        ),
    )
    raw = github.policy.model_dump(mode="json")
    raw["admission"]["request_kinds"] = ["explicit_request", "follow_up"]
    github.policy = PolicyDefinition.model_validate(raw)
    engine.source_policies.save(
        github.cookie, github.policy_id,
        PolicySave(expected_revision=1, definition=github.policy,
                   review_ref="SA10-native-follow-up-policy"),
    )
    github.policy_revision = 2
    github.control = github.control.model_copy(update={"policy_revision": 2})
    engine.source_ingress.configure(
        github.cookie, github.source_id,
        IngressConfigure(expected_generation=1, settings=github.control,
                         review_ref="SA10-native-current-ingress"),
    )
    github.generation = 2
    env = SimpleNamespace(
        engine=engine, service=SourceCorrelations(engine), cookie=github.cookie,
        sourceenv=github, github=github, native_input_id=None, native_receipt=None,
        target=None, fullTargetRef=None, target_receipt=None,
        resource_key=None, family=family, number=7, native_inputs={}, targets={},
    )
    # The target refers to the real number in this authenticated synthetic native
    # fixture. Web is the original operator channel, not the gateway's old owner.
    if create_target:
        fresh_target(env)
        # Grants change identity revision, so prepare both controller memberships
        # before the first canonical input, rather than expiring a test's probe.
        fresh_target(env, second=True)
    receipt, resource = ingest_native(env, family=family, number=7)
    env.native_input_id, env.native_receipt = receipt.input_ids[0], receipt
    env.resource_key, env.resource = resource.resource_key, resource
    return env


def ingest_native(env, *, family="issues", number=7, action=None, raw=None, delivery=None):
    value = raw or payload(family, action=action)
    if family == "issues":
        value["issue"]["number"] = number
        value["issue"]["id"] = 700 + number
        if raw is None:
            value["issue"]["body"] = (
                "/mgar investigate SA10_SYNTHETIC_PRIVATE_NATIVE_" + str(number)
            )
        kind, resource_id = "issue", str(number)
    elif family == "pull_request":
        value["pull_request"]["number"] = number
        value["pull_request"]["id"] = 300 + number
        if raw is None:
            value["pull_request"]["body"] = (
                "/mgar investigate SA10_SYNTHETIC_PRIVATE_NATIVE_" + str(number)
            )
        kind, resource_id = "pull_request", str(number)
    elif family == "issue_comment":
        value["issue"]["number"] = number
        value["issue"]["id"] = 700 + number
        value["comment"]["id"] = int(uuid4().hex[:12], 16)
        kind, resource_id = "issue", str(number)
    else:
        kind, resource_id = family, str(value[family]["id"])
    receipt = env.sourceenv.runtime.receive(
        signed(value, family=family, delivery=delivery or "sa10-" + uuid4().hex),
        env.sourceenv.generation,
    )
    assert receipt.committed and receipt.disposition in {"accepted", "duplicate"}
    assert len(receipt.input_ids) == 1
    resource = ResourceRef(
        project_id=env.engine.settings.project_id, scope=env.sourceenv.instance.scope,
        repository_id=900, repository=env.engine.settings.repository,
        kind=kind, resource_id=resource_id,
    )
    env.native_inputs[family, number] = receipt.input_ids[0]
    return receipt, resource


def controller(env, target, *, sourceenv=None):
    origin = sourceenv or env.sourceenv
    if target.ticket_id not in origin.binding["controlled_tickets"]:
        origin.binding = origin.identities.grant_controller(
            env.cookie, origin.binding["binding_id"], ControllerGrant(
                expected_revision=origin.binding["revision"], ticket_id=target.ticket_id,
                review_ref="SA10-exact-existing-ticket-controller",
            ),
        )
    return origin.binding


def fresh_target(env, second=False):
    """Return an actual original Engine fixture Ticket on native issue 7 or 8."""
    number = 8 if second else 7
    if number in env.targets:
        return env.service.target(env.cookie, env.targets[number].ticket_id)
    event = TriggerInput(
        source="web", external_event_id="sa10-owner-native-target-" + uuid4().hex,
        project_id=env.engine.settings.project_id, repository=env.engine.settings.repository,
        issue_number=number, actor=env.engine.settings.local_actor,
        body="/mgar investigate SA10 native issue " + str(number), execution_mode="fixture",
    )
    receipt = env.engine.ingest(event)
    assert receipt.route == "created" and receipt.ticket_id is not None
    target = env.service.target(env.cookie, receipt.ticket_id)
    if env.native_input_id is None:
        controller(env, target)
    target = env.service.target(env.cookie, receipt.ticket_id)
    env.targets[number] = target
    if not second:
        env.target, env.fullTargetRef, env.target_receipt = target, target, receipt
    return target


def resolved(env, input_ids=None):
    return env.service.resolve(
        env.cookie, CorrelationResolveRequest(
            input_ids=tuple(input_ids or (env.native_input_id,)),
        ),
    )


def snapshot(result):
    value = result["snapshot"]
    return value if isinstance(value, CorrelationSnapshot) else (
        CorrelationSnapshot.model_validate_json(json.dumps(value, default=str))
    )


def link(env, *, target=None, input_ids=None, resource_key=None, expected_revision=0):
    inputs = tuple(input_ids or (env.native_input_id,))
    current = resolved(env, inputs)
    return env.service.link(env.cookie, CorrelationLinkRequest(
        input_ids=inputs, resource_key=resource_key or env.resource_key,
        snapshot_sha256=current["snapshot_sha256"], expected_revision=expected_revision,
        target=target or env.service.target(env.cookie, env.target.ticket_id),
        review_ref="SA10-owner-reviewed-exact-link",
    ))


def link_id(result):
    return result["link_id"] if "link_id" in result else result["correlation_id"]


def add_im_source(env, *, conversation="Chat-A", topic=None):
    """A second signed Source, sharing the owner/cipher but no identity authority."""
    installation = source(env.engine)
    # This controlled HMAC adapter supplies its own exact integer order. Declare
    # that capability before binding actors or receiving native-versioned input.
    installation = installation.model_copy(update={
        "revision": installation.revision + 1,
        "capabilities": installation.capabilities.model_copy(update={
            "native_entity_revision": "supported",
        }),
    })
    SourceRegistry(env.engine).register_instance(installation)
    identities = SourceIdentities(
        env.engine, {("feishu", "controlled-v1"): ControlledPlatform(env.engine)},
    )
    binding, credential = bind(
        identities, env.cookie, installation,
        roles=("context_provider", "requester", "task_controller"), conversation=conversation,
    )
    raw = definition(installation).model_dump(mode="json")
    raw["selector"].update(
        conversations=[conversation], event_kinds=["message.created", "message.edited"],
    )
    raw["admission"]["request_kinds"] = ["explicit_request", "follow_up"]
    raw["receive"].update(enabled=True, retain_body=True, body_ttl_seconds=300)
    raw["execution"]["repositories"][0]["base_sha"] = HEAD
    policy = PolicyDefinition.model_validate(raw)
    policy_id = uuid4()
    env.engine.source_policies.save(
        env.cookie, policy_id,
        PolicySave(expected_revision=0, definition=policy, review_ref="SA10-private-source-policy"),
    )
    settings = IngressSettings(owner="gateway", policy_id=policy_id, policy_revision=1)
    env.engine.source_ingress.configure(
        env.cookie, installation.scope.source_instance_id,
        IngressConfigure(expected_generation=0, settings=settings,
                         review_ref="SA10-private-source-ingress"),
    )
    origin = SimpleNamespace(
        engine=env.engine, cookie=env.cookie, installation=installation,
        identities=identities, binding=binding, credential=credential,
        policy=policy, policy_id=policy_id, policy_revision=1, generation=1, settings=settings,
        occurred_at=datetime.now(UTC).isoformat(), conversation=conversation, topic=topic,
    )
    origin.gateway = SourceGateway(
        env.engine.source_ingress, installation.scope.source_instance_id, 1,
        {("feishu", "controlled-v1"): SignedIngress(env.engine)},
    )
    if env.target is not None:
        controller(env, env.target, sourceenv=origin)
    return origin


def ingest_im(origin, *, body=None, entity=None, topic=None):
    envelope = im_envelope(
        origin, delivery="sa10-im-" + uuid4().hex, entity=entity or "m-" + uuid4().hex,
        body=body or "https://github.com/example/mgar-test/issues/7",
        conversation=origin.conversation, topic=origin.topic if topic is None else topic,
    )
    receipt = origin.gateway.receive(envelope)
    assert receipt.committed and receipt.disposition == "accepted"
    return receipt


def other_repository_input(env):
    """A separately signed install/repository, with the same issue number and title."""
    github = env.sourceenv
    scope = github.instance.scope.model_copy(update={
        "source_instance_id": uuid4(), "installation_id": "201",
    })
    instance = github.instance.model_copy(update={"scope": scope, "revision": 1})
    registry = SourceRegistry(env.engine)
    registry.register_instance(instance)
    settings = github.settings.model_copy(update={
        "installation_id": 201,
        "repository": github.settings.repository.model_copy(update={
            "repository_id": 901, "full_name": "example/other",
        }),
    })
    # Original configuration validates the selected Engine repository. Prepare
    # this explicit second-repository fixture under that repository, then restore
    # the original product boundary before any correlation/target lookup.
    original_settings = env.engine.settings
    env.engine.settings = original_settings.model_copy(update={"repository": "example/other"})
    try:
        github.sources.configure(
            env.cookie, scope.source_instance_id,
            GitHubConfigure(expected_revision=0, settings=settings,
                            review_ref="SA10-second-verified-installation"),
        )
    finally:
        env.engine.settings = original_settings
    instance = registry.get_instance(scope.source_instance_id)
    adapter = GitHubAdapter(settings, KEY)
    raw = payload("issues")
    raw["installation"]["id"] = 201
    raw["repository"].update(id=901, full_name="example/other")
    identities = SourceIdentities(env.engine, {("github", VERSION): adapter})
    envelope = signed(raw, family="issues", delivery="sa10-other-identity-" + uuid4().hex)
    credential = identities.authenticate(scope.source_instance_id, envelope)
    evidence = next(value for value in identities.evidence(env.cookie)
                    if value["evidence_id"] == credential.evidence_id)
    identities.bind(env.cookie, BindIdentity(
        evidence_id=credential.evidence_id,
        expected_identity_revision=evidence["identity_revision"],
        roles=("context_provider", "requester"), review_ref="SA10-other-native-identity",
    ))
    actor = adapter.verify_identity(instance, envelope).actor
    policy = definition(instance).model_dump(mode="json")
    policy["selector"].update(
        actors=[actor.model_dump(mode="json")], conversations=["repository:901"],
        event_kinds=["issues.opened"],
    )
    policy["receive"].update(enabled=True, retain_body=True, body_ttl_seconds=300)
    policy["execution"]["repositories"] = [{
        "repository": "example/other", "base_sha": HEAD, "paths": ["src"],
    }]
    policy_id = uuid4()
    env.engine.source_policies.save(
        env.cookie, policy_id, PolicySave(
            expected_revision=0, definition=PolicyDefinition.model_validate(policy),
            review_ref="SA10-other-current-repository-boundary",
        ),
    )
    original_settings = env.engine.settings
    env.engine.settings = original_settings.model_copy(update={"repository": "example/other"})
    try:
        env.engine.source_ingress.configure(
            env.cookie, scope.source_instance_id, IngressConfigure(
                expected_generation=0, settings=IngressSettings(
                    owner="gateway", policy_id=policy_id, policy_revision=1,
                    legacy_repository="example/other",
                ), review_ref="SA10-other-current-source-owner",
            ),
        )
    finally:
        env.engine.settings = original_settings
    gateway = SourceGateway(
        env.engine.source_ingress, scope.source_instance_id, 1,
        {("github", VERSION): adapter},
    )
    receipt = gateway.receive(signed(
        raw, family="issues", delivery="sa10-other-input-" + uuid4().hex,
    ))
    assert receipt.committed and receipt.disposition == "accepted"
    resource = ResourceRef(
        project_id=env.engine.settings.project_id, scope=scope, repository_id=901,
        repository="example/other", kind="issue", resource_id="7",
    )
    return receipt.input_ids[0], resource
