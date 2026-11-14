"""SA14 synthetic signed Source and real original PG/H03/H06 test support."""

import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from pydantic import SecretStr
from sa04_support import SignedIngress, envelope
from sa13_runtime_support import RepairModel, runtime_env
from sa13_support import setup as coding_setup
from test_super_agent_sa02 import ControlledPlatform, bind
from test_super_agent_sa03 import definition

from mgar_v3.sources.domain import (
    ActorRef,
    ChannelCapabilities,
    SourceCapabilities,
    SourceInstance,
    SourceScope,
)
from mgar_v3.sources.gateway import SourceGateway
from mgar_v3.sources.identity import SourceIdentities
from mgar_v3.sources.identity_domain import ControllerGrant
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.sources.ingress_domain import IngressConfigure, IngressSettings, VerifiedDelivery
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_async import AsyncControlProfile
from mgar_v3.super_agent.source_async_domain import (
    AsyncAdmitRequest,
    AsyncRestriction,
    AsyncRestrictRequest,
    AsyncWorkerRequest,
)
from mgar_v3.super_agent.source_async_runtime import (
    ControlledAsyncCodingModel,
    ControlledAsyncModelCall,
    SourceAsyncRuntime,
)
from mgar_v3.web_access import LoginInput, WebAccounts


class SignedReplyIngress(SignedIngress):
    def verify_ingress(self, instance, transport):
        with self.engine._mutex:
            verified = super().verify_ingress(instance, transport)
        # Only enrich the fact from the same authenticated synthetic envelope.
        raw = json.loads(transport.payload)
        items = tuple(
            item.model_copy(
                update={
                    "event": item.event.model_copy(
                        update={"reply_to_entity_id": original.get("reply_to_entity_id")}
                    )
                }
            )
            for item, original in zip(verified.metadata.items, raw["items"], strict=True)
        )
        return VerifiedDelivery(
            verified.metadata.model_copy(update={"items": items}), verified.bodies
        )


def new_source(engine, *, cookie=None):
    accounts = WebAccounts(engine)
    if cookie is None:
        owner = accounts.create_owner(
            LoginInput(username="sa14-owner", password=SecretStr("synthetic-sa14-owner-password"))
        )
        cookie = accounts.issue_session(owner)
    else:
        assert accounts.session(cookie) is not None
    installation = SourceInstance(
        scope=SourceScope(
            source_kind="feishu",
            source_instance_id=uuid4(),
            tenant_id="Tenant-A",
            installation_id="Install-A",
        ),
        project_id=engine.settings.project_id,
        adapter_version="controlled-v1",
        capabilities=SourceCapabilities.model_validate(
            {key: "supported" for key in SourceCapabilities.model_fields}
        ),
        channel_capabilities=ChannelCapabilities.model_validate(
            {key: "unsupported" for key in ChannelCapabilities.model_fields}
        ),
    )
    SourceRegistry(engine).register_instance(installation)
    identities = SourceIdentities(engine, {("feishu", "controlled-v1"): ControlledPlatform(engine)})
    alice, credential = bind(
        identities, cookie, installation, roles=("context_provider", "requester")
    )
    raw = definition(
        installation,
        mode="auto_repair",
        receive={
            "enabled": True,
            "retain_body": True,
            "model_input": True,
            "body_ttl_seconds": 3600,
            "allow_history_context": True,
        },
    ).model_dump(mode="json")
    raw["selector"]["event_kinds"] = ["message.created", "message.edited", "message.deleted"]
    raw["selector"]["actors"].extend(
        ActorRef(
            scope=installation.scope, id_type="open_id", actor_id=actor, actor_kind=kind
        ).model_dump(mode="json")
        for actor, kind in [("Bob-Control", "human"), ("CI-Only", "bot")]
    )
    policy = PolicyDefinition.model_validate(raw)
    policy_id = uuid4()
    engine.source_policies.save(
        cookie,
        policy_id,
        PolicySave(
            expected_revision=0, definition=policy, review_ref="SA14-synthetic-frozen-input-scope"
        ),
    )
    settings = IngressSettings(owner="gateway", policy_id=policy_id, policy_revision=1)
    key = Fernet.generate_key()
    engine.source_ingress.cipher = BodyCipher(key)
    engine.source_ingress.configure(
        cookie,
        installation.scope.source_instance_id,
        IngressConfigure(
            expected_generation=0, settings=settings, review_ref="SA14-explicit-fixture-input"
        ),
    )
    env = SimpleNamespace(
        engine=engine,
        installation=installation,
        identities=identities,
        cookie=cookie,
        definition=policy,
        settings=settings,
        policy_id=policy_id,
        key=key,
        occurred_at=datetime.now(UTC).isoformat(),
        binding=alice,
        controller=None,
        robot=None,
        credential=credential,
    )
    env.gateway = SourceGateway(
        engine.source_ingress,
        installation.scope.source_instance_id,
        1,
        {("feishu", "controlled-v1"): SignedReplyIngress(engine)},
    )
    return env


def attach(env, *, database_paths=("src/calc.py",)):
    service = env.engine.source_async
    env.source.controller, _ = bind(
        env.source.identities,
        env.cookie,
        env.source.installation,
        actor="Bob-Control",
        roles=("context_provider", "requester", "task_controller"),
    )
    env.source.robot, _ = bind(
        env.source.identities,
        env.cookie,
        env.source.installation,
        actor="CI-Only",
        kind="bot",
        roles=("context_provider",),
    )
    service.register_profile(
        env.profile.profile_sha256,
        AsyncControlProfile(
            database_restriction=AsyncRestriction(deny_write_paths=tuple(sorted(database_paths)))
        ),
    )
    env.source.controller = env.source.identities.grant_controller(
        env.cookie,
        env.source.controller["binding_id"],
        ControllerGrant(
            expected_revision=env.source.controller["revision"],
            ticket_id=env.target.ticket_id,
            review_ref="SA14-exact-human-Ticket-control",
        ),
    )
    env.source.gateway = SourceGateway(
        env.engine.source_ingress,
        env.source.installation.scope.source_instance_id,
        2,
        {("feishu", "controlled-v1"): SignedReplyIngress(env.engine)},
    )
    env.async_service = service
    env.async_admit = AsyncAdmitRequest(
        request_id=uuid4(),
        coding_id=env.mandate.coding_id,
        run_id=env.target.run_id,
        expected_coding_revision=1,
        expected_epoch=env.run_request.epoch,
        review_ref="SA14-reviewed-fixture-async-binding",
    )
    env.async_binding = service.admit(env.cookie, env.async_admit)
    return env


def setup(engine, tmp_path, *, cookie=None):
    return attach(coding_setup(engine, tmp_path, source_env=new_source(engine, cookie=cookie)))


def worker_request(env):
    row = env.async_service.get(env.cookie, env.mandate.coding_id)
    return AsyncWorkerRequest(
        request_id=uuid4(),
        coding_id=env.mandate.coding_id,
        run_id=env.target.run_id,
        assignment_id=env.run_request.assignment_id,
        session_id=env.run_request.session_id,
        epoch=env.run_request.epoch,
        expected_revision=row["revision"],
    )


def restrict(env, *, paths=("src/calc.py",), request_id=None):
    row = env.async_service.get(env.cookie, env.mandate.coding_id)
    request = AsyncRestrictRequest(
        request_id=request_id or uuid4(),
        coding_id=env.mandate.coding_id,
        run_id=env.target.run_id,
        expected_epoch=env.run_request.epoch,
        expected_revision=row["revision"],
        restriction=AsyncRestriction(deny_write_paths=tuple(sorted(paths))),
        review_ref="SA14-owner-narrowing",
    )
    return request, env.async_service.restrict(env.cookie, request)


def followup(
    env,
    *,
    body="synthetic follow-up",
    sender="Bob-Control",
    actor_kind="human",
    entity=None,
    reply="m1",
):
    entity = entity or "sa14-" + uuid4().hex
    transport = envelope(
        env.source,
        delivery="delivery-" + entity,
        entity=entity,
        body=body,
        sender=sender,
        actor_kind=actor_kind,
        reply_to_entity_id=reply,
    )
    receipt = env.source.gateway.receive(transport)
    return transport, receipt


def rows(env, table):
    allowed = {
        "sa_async_bindings",
        "sa_async_inputs",
        "sa_async_checkpoints",
        "sa_async_jobs",
        "sa_async_operation_views",
        "sa_async_events",
        "sa_coding_operations",
        "sa_coding_payloads",
        "sa_coding_attempts",
        "sa_scheduler_operations",
        "usage_calls",
        "sa_budget_calls",
        "runs",
        "actions",
        "outbox",
    }
    assert table in allowed
    with env.engine.transaction() as conn:
        return conn.execute("SELECT * FROM app." + table + " ORDER BY 1").fetchall()


def point(env, stage="before_model"):
    return env.async_service.safe_point(
        env.worker["bearer"],
        env.run_request,
        tree=env.mandate.repository.materialized_tree_sha256,
        stage=stage,
    )


class ImmediateCall(ControlledAsyncModelCall):
    def __init__(self, response, release=None):
        self.response, self.release = response, release
        self.cancel_requested = False

    def poll(self):
        return self.response if self.release is None or self.release.is_set() else None

    def request_cancel(self):
        self.cancel_requested = True


class AsyncRepair(ControlledAsyncCodingModel):
    def __init__(self, *, gate=False, wrong_first=False):
        self.repair = RepairModel(wrong_first=wrong_first)
        self.requests = self.repair.requests
        self.started = threading.Event()
        self.release = threading.Event() if gate else None
        self.handles = []
        self.before_send = None

    def prepare(self, request, cancellation):
        if self.before_send:
            self.before_send(request)
        return None

    def dispatch(self, request, cancellation, prepared):
        response = self.repair.complete(request, cancellation)
        handle = ImmediateCall(response, self.release if len(self.requests) == 1 else None)
        self.handles.append(handle)
        self.started.set()
        return handle


@contextmanager
def actual(engine, tmp_path, monkeypatch, *, model=None, slow_tests=False):
    import sa13_runtime_support as fixtures

    monkeypatch.setattr(fixtures, "new_source", new_source)
    if slow_tests:
        files = dict(fixtures.FILES)
        files["tests/test_calc.py"] = (
            files["tests/test_calc.py"]
            .replace("import sys\n", "import time\nimport sys\n")
            .replace(
                "self.assertEqual(total([2, 3]), 5)",
                "time.sleep(1)\n        self.assertEqual(total([2, 3]), 5)",
            )
        )
        monkeypatch.setattr(fixtures, "FILES", files)
    model = model or AsyncRepair()
    with runtime_env(engine, tmp_path, model=model) as env:
        attach(env)
        runtime = SourceAsyncRuntime(env.async_service, env.service.runtime)
        env.async_service.runtime = runtime
        env.async_runtime = runtime
        try:
            yield env
        finally:
            if model.release:
                model.release.set()
            assert runtime.join(env.mandate.coding_id, timeout=30)


def start(env):
    return env.async_service.start(env.worker["bearer"], worker_request(env))


def wait_for(predicate, timeout=15):
    import time

    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("SA14 controlled boundary was not reached")
