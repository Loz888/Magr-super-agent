from __future__ import annotations

import hashlib
import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copyfile
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, TriggerInput
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import ROOT
from mgar_v3.sources.domain import (
    ActorRef,
    BusinessReference,
    ChannelCapabilities,
    EntityRevision,
    SourceCapabilities,
    SourceEvent,
    SourceGap,
    SourceHealth,
    SourceInstance,
    SourceMetadata,
    SourceScope,
    SourceThreadRef,
)
from mgar_v3.sources.legacy import LegacyTriggerAdapter
from mgar_v3.sources.ports import TransportEnvelope, VerifiedEnvelope
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.store.migrate import migrate

NOW = datetime(2026, 11, 10, 10, tzinfo=UTC)


def scope(**changes):
    return SourceScope.model_validate(
        {
            "source_kind": "feishu",
            "source_instance_id": uuid4(),
            "tenant_id": "Tenant-A",
            "installation_id": "Install-A",
            **changes,
        }
    )


def event(identity=None, **changes):
    identity = identity or scope()
    return SourceEvent.model_validate(
        {
            "scope": identity,
            "project_id": "orders-demo",
            "source_revision": 1,
            "delivery_id": "delivery-1",
            "remote_event_id": "event-1",
            "entity_id": "message-1",
            "entity_revision": EntityRevision(origin="native", value="Version-A"),
            "event_kind": "message.created",
            "source_thread": SourceThreadRef(scope=identity, conversation_id="Chat-A"),
            "sender": ActorRef(
                scope=identity, id_type="open_id", actor_id="User-AbC", actor_kind="human"
            ),
            "addressed": "mention",
            "occurred_at": NOW - timedelta(days=1),
            "received_at": NOW,
            "body_ref": "a" * 64,
            "verification_ref": "server-receipt-1",
            "provenance": "verified_source",
            **changes,
        }
    )


def capabilities(**changes):
    return SourceCapabilities.model_validate(
        {key: changes.get(key, "unknown") for key in SourceCapabilities.model_fields}
    )


def channel_capabilities(**changes):
    return ChannelCapabilities.model_validate(
        {key: changes.get(key, "unknown") for key in ChannelCapabilities.model_fields}
    )


def instance(identity=None, **changes):
    return SourceInstance.model_validate(
        {
            "scope": identity or scope(),
            "project_id": "orders-demo",
            "display_name": "研发群",
            "adapter_version": "controlled-v1",
            "capabilities": capabilities(),
            "channel_capabilities": channel_capabilities(),
            **changes,
        }
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "Tenant-B"),
        ("installation_id", "Install-B"),
        ("source_instance_id", UUID("00000000-0000-0000-0000-000000000002")),
        ("source_kind", "github"),
    ],
)
def test_actor_identity_is_installation_scoped(field, value):
    original = scope(source_instance_id=UUID(int=1))
    other = SourceScope.model_validate({**original.model_dump(), field: value})
    actor = ActorRef(scope=original, id_type="open_id", actor_id="Same-ID", actor_kind="human")
    assert (
        actor.key
        != ActorRef(scope=other, id_type="open_id", actor_id="Same-ID", actor_kind="human").key
    )


def test_actor_case_and_id_type_are_preserved_without_display_name_authority():
    identity = scope()
    actor = ActorRef(scope=identity, id_type="open_id", actor_id="AbC", actor_kind="human")
    assert actor.actor_id == "AbC"
    assert actor.key != actor.model_copy(update={"actor_id": "abc"}).key
    assert actor.key != actor.model_copy(update={"id_type": "union_id"}).key
    assert actor.key == actor.model_copy(update={"actor_kind": "bot"}).key


def test_same_named_chats_and_missing_native_topics_do_not_become_one_thread():
    one, two = instance(), instance()
    assert one.display_name == two.display_name
    first = event(one.scope)
    second = event(two.scope)
    assert first.source_thread.conversation_key != second.source_thread.conversation_key
    assert first.source_thread.topic_key is None
    assert second.source_thread.topic_key is None
    assert event(one.scope, entity_id="another-problem").entity_key != first.entity_key


def test_transport_event_entity_and_edit_revisions_have_separate_keys():
    original = event()
    redelivery = event(original.scope, delivery_id="delivery-2")
    edited = event(
        original.scope, entity_revision=EntityRevision(origin="native", value="Version-B")
    )
    assert original.delivery_key != redelivery.delivery_key
    assert original.version_key == redelivery.version_key
    assert original.event_key == edited.event_key
    assert original.version_key != edited.version_key
    assert original.entity_key == edited.entity_key


def test_late_and_clock_skewed_events_are_representable_with_aware_timestamps():
    late = event(occurred_at=NOW - timedelta(days=30))
    assert late.received_at - late.occurred_at == timedelta(days=30)
    assert event(occurred_at=NOW + timedelta(seconds=5)).occurred_at > NOW
    with pytest.raises(ValidationError):
        event(received_at=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "changes",
    [
        {"source_revision": True},
        {"historical_backfill": "true"},
        {"entity_revision": {"origin": "native", "value": None}},
        {"schema_version": "source-event-v2"},
        {"execution_grant": {"write": True}},
        {"body": "private conversation"},
    ],
)
def test_invalid_versions_authority_and_plaintext_are_rejected(changes):
    with pytest.raises(ValidationError):
        event(**changes)


def test_sender_and_conversation_cannot_escape_the_event_installation():
    identity, other = scope(), scope()
    with pytest.raises(ValidationError):
        event(
            identity,
            sender=ActorRef(scope=other, id_type="open_id", actor_id="Same", actor_kind="human"),
        )
    with pytest.raises(ValidationError):
        event(identity, source_thread=SourceThreadRef(scope=other, conversation_id="Chat-A"))


def test_capability_absence_is_explicit_and_online_is_not_permission():
    with pytest.raises(ValidationError):
        SourceCapabilities.model_validate({})
    assert capabilities(history_read="unsupported").history_read == "unsupported"
    assert channel_capabilities().query_delivery == "unknown"
    unavailable = EntityRevision(origin="unavailable", value=None)
    assert event(entity_revision=unavailable).entity_revision.value is None
    health = SourceHealth(
        scope=scope(),
        observed_at=NOW,
        connection="online",
        permissions="revoked",
        gaps=(SourceGap(started_at=NOW - timedelta(hours=1), reason="unreplayable"),),
    )
    assert health.connection == "online" and health.permissions == "revoked" and health.gaps


def test_metadata_drops_content_artifact_identity_and_repository_hints():
    original = event(repository_hint={"full_name": "private/repo", "resource_kind": "repository"})
    metadata = SourceMetadata.from_event(original)
    body = metadata.model_dump(mode="json")
    assert (
        not {"body_ref", "sender", "repository_hint", "verification_ref", "legacy_context"}
        & body.keys()
    )
    assert "private/repo" not in metadata.model_dump_json()
    assert "User-AbC" not in metadata.model_dump_json()
    assert metadata.version_key == original.version_key


@pytest.mark.parametrize("source", ["github", "cli", "web"])
@pytest.mark.parametrize("mode", ["fixture", "live"])
def test_legacy_trigger_roundtrip_preserves_existing_contract(source, mode):
    trigger = TriggerInput(
        source=source,
        external_event_id="legacy-1",
        project_id="orders-demo",
        repository="Example/Repo",
        issue_number=7,
        actor="Owner",
        body="/mgar investigate a bug",
        execution_mode=mode,
    )
    wrapped = LegacyTriggerAdapter.wrap(trigger, received_at=NOW)
    assert wrapped.sender.actor_id == "owner" and trigger.repository == "example/repo"
    assert LegacyTriggerAdapter.restore(wrapped, body=trigger.body) == trigger
    assert wrapped.provenance == "legacy_trigger"


def test_legacy_identifiers_are_encoded_without_tightening_the_old_input_schema():
    trigger = TriggerInput(
        source="web",
        external_event_id="old\nidentifier",
        project_id="orders-demo",
        repository="x" * 500 + "/repo",
        issue_number=10**300,
        actor="Owner",
        body="old body",
    )
    wrapped = LegacyTriggerAdapter.wrap(trigger, received_at=NOW)
    assert LegacyTriggerAdapter.restore(wrapped, body=trigger.body) == trigger
    assert "\n" not in wrapped.remote_event_id and len(wrapped.entity_id) < 256


def test_new_source_and_tampered_legacy_wrappers_cannot_fabricate_old_triggers(trigger):
    with pytest.raises(DomainError, match="Only legacy"):
        LegacyTriggerAdapter.restore(event(), body="text")
    original = LegacyTriggerAdapter.wrap(trigger, received_at=NOW)
    with pytest.raises(DomainError) as error:
        LegacyTriggerAdapter.restore(original, body=trigger.body + "changed")
    assert error.value.code == "SOURCE_BODY_MISMATCH"
    altered = SourceEvent.model_validate({**original.model_dump(), "delivery_id": "other"})
    with pytest.raises(DomainError) as error:
        LegacyTriggerAdapter.restore(altered, body=trigger.body)
    assert error.value.code == "LEGACY_WRAPPER_MISMATCH"
    with pytest.raises(ValidationError):
        event(legacy_context=original.legacy_context)


def test_sample_events_and_capabilities_validate_as_the_published_contract():
    samples = json.loads((ROOT / "fixtures/super-agent-v1/source-v1/samples.json").read_text())
    events = [SourceEvent.model_validate(row) for row in samples["events"]]
    assert {item.scope.source_kind for item in events} == {"github", "feishu", "cli", "web"}
    assert any(
        item.repository_hint is None and item.source_thread.topic_key is None for item in events
    )
    for definition in samples["source_instances"]:
        SourceInstance.model_validate(definition)


@pytest.mark.parametrize(
    "relative",
    [
        "fixtures/super-agent-v1/source-v1/samples.json",
        "schemas/super-agent-v1/source-event-v1.schema.json",
    ],
)
def test_verification_fingerprint_covers_source_samples_and_published_schema(tmp_path, relative):
    fingerprint = runpy.run_path(str(ROOT / "scripts/verify.py"))["source_fingerprint"]
    artifact = Path(tmp_path) / relative
    artifact.parent.mkdir(parents=True)
    artifact.write_text("first frozen artifact")
    before = fingerprint(tmp_path)
    artifact.write_text("changed artifact")
    assert fingerprint(tmp_path) != before


def test_registry_versions_and_all_writes_use_the_same_engine_transaction(engine):
    registry = SourceRegistry(engine)
    definition = instance()
    with engine.transaction() as conn:
        pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
    assert registry.register_instance(definition) == definition
    assert registry.register_instance(definition) == definition
    second = definition.model_copy(update={"revision": 2, "display_name": "renamed chat"})
    registry.register_instance(second)
    assert registry.get_instance(definition.scope.source_instance_id) == second
    assert registry.get_instance(definition.scope.source_instance_id, 1) == definition
    with engine.transaction() as conn:
        assert conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"] == pid
        assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 0
    with pytest.raises(DomainError) as error:
        registry.register_instance(
            second.model_copy(update={"display_name": "changed same revision"})
        )
    assert error.value.code == "SOURCE_REVISION_CONFLICT"
    with pytest.raises(DomainError):
        registry.register_instance(second.model_copy(update={"revision": 4}))
    changed_scope = definition.scope.model_copy(update={"tenant_id": "Other-Tenant"})
    with pytest.raises(DomainError) as error:
        registry.register_instance(
            second.model_copy(update={"scope": changed_scope, "revision": 3})
        )
    assert error.value.code == "SOURCE_IDENTITY_IMMUTABLE"


def test_installations_with_same_ids_remain_separate_in_postgres(engine):
    registry = SourceRegistry(engine)
    first = instance(scope(installation_id="Installation"))
    second = instance(scope(installation_id="installation"))
    for definition in [first, second]:
        registry.register_instance(definition)
    ids = [
        registry.stage_metadata(SourceMetadata.from_event(event(definition.scope)))
        for definition in [first, second]
    ]
    assert ids[0] != ids[1]
    alias = instance(first.scope.model_copy(update={"source_instance_id": uuid4()}))
    with pytest.raises(DomainError) as error:
        registry.register_instance(alias)
    assert error.value.code == "SOURCE_INSTALLATION_EXISTS"


def test_unassociated_inbox_retains_no_body_and_creates_no_fake_issue_or_task(engine):
    registry = SourceRegistry(engine)
    definition = instance()
    registry.register_instance(definition)
    metadata = SourceMetadata.from_event(event(definition.scope))
    inbox_id = registry.stage_metadata(metadata)
    assert registry.get_metadata(inbox_id) == metadata
    with engine.transaction() as conn:
        for table in ["threads", "tickets", "runs", "trigger_events", "actions", "outbox"]:
            assert (
                conn.execute(
                    sql.SQL("SELECT count(*) AS n FROM app.{}").format(sql.Identifier(table))
                ).fetchone()["n"]
                == 0
            )
        row = conn.execute(
            "SELECT metadata FROM app.source_inbox WHERE inbox_id=%s", (inbox_id,)
        ).fetchone()
        assert not {"body", "body_ref", "sender", "repository_hint"} & row["metadata"].keys()


def test_edits_and_out_of_order_versions_are_append_only_metadata(engine):
    registry = SourceRegistry(engine)
    definition = instance()
    registry.register_instance(definition)
    newer = event(definition.scope, entity_revision=EntityRevision(origin="native", value="v2"))
    older = event(
        definition.scope,
        entity_revision=EntityRevision(origin="native", value="v1"),
        occurred_at=NOW - timedelta(days=7),
    )
    first = registry.stage_metadata(SourceMetadata.from_event(newer))
    second = registry.stage_metadata(SourceMetadata.from_event(older))
    assert registry.get_metadata(first).entity_revision.value == "v2"
    assert registry.get_metadata(second).entity_revision.value == "v1"
    with pytest.raises(DomainError) as error:
        registry.stage_metadata(SourceMetadata.from_event(older))
    assert error.value.code == "SOURCE_METADATA_EXISTS"
    with pytest.raises(psycopg.errors.RaiseException), engine.transaction() as conn:
        conn.execute(
            "UPDATE app.source_inbox SET remote_event_id='overwrite' WHERE inbox_id=%s", (first,)
        )


def test_registration_and_staging_roll_back_with_the_outer_engine_transaction(engine):
    registry = SourceRegistry(engine)
    definition = instance()
    with pytest.raises(RuntimeError, match="abort"):
        with engine.transaction():
            registry.register_instance(definition)
            registry.stage_metadata(SourceMetadata.from_event(event(definition.scope)))
            raise RuntimeError("abort")
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.source_instances").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.source_inbox").fetchone()["n"] == 0


def test_metadata_project_installation_and_version_are_checked(engine):
    registry = SourceRegistry(engine)
    definition = instance()
    registry.register_instance(definition)
    with pytest.raises(DomainError) as error:
        registry.stage_metadata(
            SourceMetadata.from_event(event(definition.scope, project_id="other-project"))
        )
    assert error.value.code == "PROJECT_DENIED"
    other_scope = definition.scope.model_copy(update={"tenant_id": "Other-Tenant"})
    with pytest.raises(DomainError) as error:
        registry.stage_metadata(SourceMetadata.from_event(event(other_scope)))
    assert error.value.code == "SOURCE_SCOPE_MISMATCH"
    with pytest.raises(DomainError) as error:
        registry.stage_metadata(
            SourceMetadata.from_event(event(definition.scope, source_revision=2))
        )
    assert error.value.code == "SOURCE_NOT_FOUND"
    with pytest.raises(DomainError):
        registry.register_instance(instance(project_id="other-project"))


def test_legacy_receipt_maps_to_existing_thread_ticket_run_without_copying_state(engine, trigger):
    receipt = engine.ingest(trigger)
    wrapped = LegacyTriggerAdapter.wrap(trigger, received_at=NOW)
    registry = SourceRegistry(engine)
    registry.register_instance(instance(wrapped.scope))
    inbox_id = registry.stage_metadata(SourceMetadata.from_event(wrapped))
    reference = registry.link_legacy_receipt(inbox_id, receipt)
    assert reference == BusinessReference(
        inbox_id=inbox_id,
        thread_id=receipt.thread_id,
        trigger_event_id=receipt.event_id,
        ticket_id=receipt.ticket_id,
        run_id=receipt.run_id,
    )
    assert registry.link_legacy_receipt(inbox_id, receipt) == reference
    with engine.transaction() as conn:
        for table in ["threads", "tickets", "runs", "source_business_links"]:
            assert (
                conn.execute(
                    sql.SQL("SELECT count(*) AS n FROM app.{}").format(sql.Identifier(table))
                ).fetchone()["n"]
                == 1
            )
    with pytest.raises(DomainError):
        registry.link_legacy_receipt(inbox_id, receipt.model_copy(update={"run_id": uuid4()}))


def test_new_source_metadata_cannot_claim_an_existing_legacy_receipt(engine, trigger):
    receipt = engine.ingest(trigger)
    registry = SourceRegistry(engine)
    definition = instance()
    registry.register_instance(definition)
    inbox_id = registry.stage_metadata(SourceMetadata.from_event(event(definition.scope)))
    with pytest.raises(DomainError) as error:
        registry.link_legacy_receipt(inbox_id, receipt)
    assert error.value.code == "SOURCE_RECEIPT_MISMATCH"


def test_database_link_constraints_reject_wrong_run_and_cross_project(engine, trigger):
    first = engine.ingest(trigger)
    second = engine.ingest(
        trigger.model_copy(update={"external_event_id": "other-trigger", "issue_number": 2})
    )
    registry = SourceRegistry(engine)
    wrapped = LegacyTriggerAdapter.wrap(trigger, received_at=NOW)
    registry.register_instance(instance(wrapped.scope))
    inbox_id = registry.stage_metadata(SourceMetadata.from_event(wrapped))
    statement = (
        "INSERT INTO app.source_business_links"
        "(inbox_id,project_id,revision,thread_id,ticket_id,run_id) "
        "VALUES (%s,%s,1,%s,%s,%s)"
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), engine.transaction() as conn:
        conn.execute(
            statement, (inbox_id, "orders-demo", first.thread_id, first.ticket_id, second.run_id)
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), engine.transaction() as conn:
        conn.execute(
            statement, (inbox_id, "other-project", first.thread_id, first.ticket_id, first.run_id)
        )
    with pytest.raises(ValidationError):
        BusinessReference(inbox_id=inbox_id, thread_id=first.thread_id, run_id=first.run_id)


def test_metadata_schema_rejects_content_columns_even_through_sql(engine):
    registry = SourceRegistry(engine)
    definition = instance()
    registry.register_instance(definition)
    inbox_id = registry.stage_metadata(SourceMetadata.from_event(event(definition.scope)))
    with engine.transaction() as conn:
        row = conn.execute(
            "SELECT metadata FROM app.source_inbox WHERE inbox_id=%s", (inbox_id,)
        ).fetchone()
    replacement = {**row["metadata"], "remote_event_id": "other-event", "body_ref": "b" * 64}
    with pytest.raises(psycopg.errors.CheckViolation), engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.source_inbox SELECT %s,source_instance_id,source_revision,project_id,"
            "event_kind,'other-event',entity_id,revision_origin,revision_value,%s,%s,"
            "occurred_at,received_at "
            "FROM app.source_inbox WHERE inbox_id=%s",
            (uuid4(), "b" * 64, Jsonb(replacement), inbox_id),
        )


def test_missing_history_or_delivery_query_never_becomes_available(engine):
    class Adapter:
        source_kind = "feishu"
        adapter_version = "controlled-v1"
        capabilities = capabilities(history_read="unknown")

        def configuration_schema(self):
            return {"type": "object"}

        def verify(self, *args):
            raise AssertionError("Registry must not call transports")

        def normalize(self, *args):
            raise AssertionError("Registry must not call transports")

        def fetch_context(self, *args):
            raise AssertionError("Registry must not call transports")

        def health(self, *args):
            raise AssertionError("Registry must not call transports")

    class Channel:
        source_kind = "feishu"
        capabilities = channel_capabilities(send="supported", query_delivery="unsupported")

        def send(self, *args):
            raise AssertionError("No authorized Action was supplied")

        def query(self, *args):
            raise AssertionError("Query is unavailable")

    registry = SourceRegistry(engine)
    definition = instance(
        capabilities=capabilities(history_read="supported"),
        channel_capabilities=channel_capabilities(send="supported", query_delivery="unsupported"),
    )
    registry.register_instance(definition)
    adapter, channel = Adapter(), Channel()
    registry.add_adapter(adapter)
    registry.add_channel(channel)
    assert registry.adapter_for(definition.scope.source_instance_id) is adapter
    assert registry.channel_for(definition.scope.source_instance_id, require="send") is channel
    with pytest.raises(DomainError) as error:
        registry.adapter_for(definition.scope.source_instance_id, require="history_read")
    assert error.value.code == "SOURCE_CAPABILITY_UNAVAILABLE"
    with pytest.raises(DomainError):
        registry.channel_for(definition.scope.source_instance_id, require="query_delivery")


@pytest.mark.parametrize(
    "failure",
    [None, "signature", "verified_scope", "project", "revision", "event_scope", "receipt"],
)
def test_adapter_verification_and_normalization_are_scoped_and_outside_transactions(
    engine, failure
):
    registry = SourceRegistry(engine)
    definition = instance()
    registry.register_instance(definition)
    calls = []

    class Adapter:
        source_kind = "feishu"
        adapter_version = "controlled-v1"
        capabilities = capabilities()

        def configuration_schema(self):
            return {"type": "object"}

        def verify(self, registered, envelope):
            assert engine.connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            calls.append("verify")
            if failure == "signature":
                raise DomainError("SOURCE_BAD_SIGNATURE", "controlled rejection", 401)
            return VerifiedEnvelope(
                scope=scope() if failure == "verified_scope" else registered.scope,
                verification_ref="server-receipt-1",
                payload=envelope.payload,
            )

        def normalize(self, registered, envelope):
            assert engine.connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            calls.append("normalize")
            changes = {}
            if failure == "project":
                changes["project_id"] = "other-project"
            if failure == "revision":
                changes["source_revision"] = 2
            if failure == "receipt":
                changes["verification_ref"] = "self-asserted"
            return (event(scope() if failure == "event_scope" else registered.scope, **changes),)

        def fetch_context(self, *args):
            raise AssertionError("not requested")

        def health(self, *args):
            raise AssertionError("not requested")

    registry.add_adapter(Adapter())
    envelope = TransportEnvelope(headers={}, payload=b"controlled transport sample")
    if failure is None:
        normalized = registry.verify_and_normalize(definition.scope.source_instance_id, envelope)
        assert normalized[0].scope == definition.scope
    else:
        with pytest.raises(DomainError):
            registry.verify_and_normalize(definition.scope.source_instance_id, envelope)
    assert calls == (
        ["verify"] if failure in {"signature", "verified_scope"} else ["verify", "normalize"]
    )
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.source_inbox").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 0


def test_forward_upgrade_from_29_migrations_preserves_old_business_state(
    settings, trigger, tmp_path
):
    base = settings.database_url.get_secret_value()
    name = "mgar_sa01_upgrade_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        dsn = make_conninfo(base, dbname=name)
        older, upgrade = tmp_path / "old-29", tmp_path / "up-to-0030"
        older.mkdir()
        upgrade.mkdir()
        paths = [
            p
            for p in sorted((ROOT / "migrations").glob("*.sql"))
            if p.name <= "0030_source_contracts.sql"
        ]
        for path in paths:
            copyfile(path, upgrade / path.name)
            if path.name < "0030_source_contracts.sql":
                copyfile(path, older / path.name)
        assert len(migrate(dsn, older)) == 29
        from pydantic import SecretStr

        config = settings.model_copy(update={"database_url": SecretStr(dsn)})
        before = Engine(config, ArtifactStore(tmp_path / "before-artifacts"))
        try:
            receipt = before.ingest(trigger)
            with before.transaction() as conn:
                original = conn.execute(
                    "SELECT * FROM app.trigger_events WHERE event_id=%s", (receipt.event_id,)
                ).fetchone()
        finally:
            before.close()
        assert migrate(dsn, upgrade) == ["0030_source_contracts.sql"]
        assert migrate(dsn, upgrade) == []
        after = Engine(config, ArtifactStore(tmp_path / "after-artifacts"))
        try:
            with after.transaction() as conn:
                assert (
                    conn.execute(
                        "SELECT * FROM app.trigger_events WHERE event_id=%s", (receipt.event_id,)
                    ).fetchone()
                    == original
                )
                assert conn.execute("SELECT count(*) AS n FROM app.runs").fetchone()["n"] == 1
                recorded = conn.execute(
                    "SELECT name,sha256 FROM app.schema_migrations ORDER BY name"
                ).fetchall()
                assert [(row["name"], row["sha256"]) for row in recorded] == [
                    (p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths
                ]
        finally:
            after.close()
    finally:
        assert name.startswith("mgar_sa01_upgrade_") and len(name) == 50
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
