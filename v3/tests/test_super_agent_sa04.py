from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sa04_support import SignedIngress, envelope, gateway, rows, setup, signed

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError
from mgar_v3.settings import ROOT
from mgar_v3.sources.gateway import SourceGateway
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.sources.ingress_crypto import BodyCipher, EncryptedSpool
from mgar_v3.sources.ingress_domain import (
    CursorReset,
    HistoryPage,
    HistoryRequest,
    IngressConfigure,
    IngressSettings,
    ReplayRequest,
)
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.policy_domain import PolicyRevoke
from mgar_v3.web_access import SESSION_COOKIE, WebAccounts


@pytest.fixture
def env(engine):
    return setup(engine)


def test_commit_before_ack_and_one_canonical_inbox_without_fake_issue(env):
    acknowledgements = []

    def ack(receipt):
        assert env.engine.connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        with psycopg.connect(env.engine.settings.database_url.get_secret_value()) as conn:
            assert conn.execute(
                "SELECT sealed FROM app.source_ingress_deliveries WHERE receipt_id=%s",
                (receipt.receipt_id,),
            ).fetchone() == (True,)
            assert conn.execute("SELECT count(*) FROM app.source_inbox").fetchone() == (1,)
            for table in [
                "threads",
                "tickets",
                "runs",
                "usage_calls",
                "outbox",
                "source_business_links",
            ]:
                assert conn.execute("SELECT count(*) FROM app." + table).fetchone() == (0,)
        acknowledgements.append(receipt.receipt_id)

    result = env.gateway.receive(envelope(env), ack)
    assert result.disposition == "accepted" and len(result.input_ids) == 1
    assert acknowledgements == [result.receipt_id]
    assert rows(env, "inputs")[0]["inbox_id"] == result.input_ids[0]


def test_concurrent_deliveries_commit_one_input_and_one_receipt(env):
    message = envelope(env)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: env.gateway.receive(message), range(36)))
    assert sum(r.disposition == "accepted" for r in results) == 1
    assert len({r.receipt_id for r in results}) == 1
    assert len(rows(env, "inputs")) == len(rows(env, "deliveries")) == 1


def test_delivery_event_and_version_are_distinct_dedupe_layers(env):
    first = env.gateway.receive(envelope(env))
    redelivery = env.gateway.receive(envelope(env, delivery="d2"))
    alias = env.gateway.receive(envelope(env, delivery="d3", event="new-platform-event"))
    assert first.input_ids == redelivery.input_ids == alias.input_ids
    assert len(rows(env, "deliveries")) == 3 and len(rows(env, "inputs")) == 1
    assert len(rows(env, "aliases")) == 2


@pytest.mark.parametrize(
    "change,code",
    [
        ({"body": "changed"}, "INGRESS_DELIVERY_CONFLICT"),
        ({"delivery": "d2", "body": "changed"}, "INGRESS_EVENT_CONFLICT"),
        ({"delivery": "d2", "event": "other", "body": "changed"}, "INGRESS_VERSION_CONFLICT"),
        ({"delivery": "d2", "version": 2, "order": 1}, "INGRESS_ORDER_CONFLICT"),
    ],
)
def test_changed_delivery_event_version_or_native_slot_is_explicit_conflict(env, change, code):
    first = env.gateway.receive(envelope(env))
    acks = []
    with pytest.raises(DomainError) as error:
        env.gateway.receive(envelope(env, **change), acks.append)
    assert error.value.code == code and not acks
    assert len(rows(env, "inputs")) == 1 and len(rows(env, "conflicts")) == 1
    assert (
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
        == b"synthetic private content"
    )


def test_edit_reordering_delete_and_late_create_cannot_revive(env):
    first = env.gateway.receive(envelope(env))
    newer = env.gateway.receive(
        envelope(env, delivery="d3", version=3, operation="edit", body="current version")
    )
    older = env.gateway.receive(
        envelope(env, delivery="d2", version=2, operation="edit", body="old version")
    )
    assert env.engine.source_ingress.body(env.cookie, newer.input_ids[0]) == b"current version"
    assert {r["reason_code"] for r in rows(env, "inputs")} >= {
        "INGRESS_ENTITY_EDITED",
        "INGRESS_OLDER_VERSION",
    }
    for identifier in first.input_ids + older.input_ids:
        with pytest.raises(DomainError):
            env.engine.source_ingress.body(env.cookie, identifier)
    env.gateway.receive(envelope(env, delivery="d4", version=4, operation="delete"))
    env.gateway.receive(envelope(env, delivery="d5", version=5, body="must stay deleted"))
    assert not rows(env, "bodies") and rows(env, "entities")[0]["deleted"]
    assert all(r["unavailable_at"] is not None for r in rows(env, "inputs"))


def test_unordered_edit_is_ambiguous_and_does_not_guess_latest(env):
    env.gateway.receive(envelope(env, order=None))
    env.gateway.receive(envelope(env, delivery="d2", version=2, order=None, operation="edit"))
    assert rows(env, "entities")[0]["ambiguous"] and not rows(env, "bodies")
    assert all(r["context_only"] for r in rows(env, "inputs"))


def test_untrusted_editor_does_not_destroy_current_body(env):
    first = env.gateway.receive(envelope(env))
    # A valid signed but unbound author is outside Receive. It cannot delete Alice's entity.
    denied = env.gateway.receive(
        envelope(env, delivery="other", version=2, operation="delete", sender="Other")
    )
    assert denied.disposition == "rejected"
    assert (
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
        == b"synthetic private content"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"conversation": "Private"},
        {"sender": "alice-abc"},
        {"sender": " Alice-AbC"},
        {"actor_kind": "bot"},
        {"kind": "private.message"},
    ],
)
def test_outside_receive_body_never_persists_or_reaches_model_recall_log(env, change, caplog):
    secret = "OUTSIDE-RECEIVE-" + uuid4().hex
    result = env.gateway.receive(envelope(env, body=secret, **change))
    assert result.disposition == "rejected" and result.denied_count == 1
    assert not rows(env, "inputs") and not rows(env, "bodies")
    with env.engine.transaction() as conn:
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='app'"
        ).fetchall():
            payload = conn.execute(
                "SELECT coalesce(jsonb_agg(to_jsonb(t)),'[]'::jsonb)::text AS data FROM app."
                + row["tablename"]
                + " t"
            ).fetchone()["data"]
            assert secret not in payload
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0
    assert secret not in caplog.text
    assert not list(env.engine.settings.artifact_root.rglob("*"))


def test_ciphertext_is_bound_to_input_and_api_views_hide_content(env):
    receipt = env.gateway.receive(envelope(env))
    encrypted = bytes(rows(env, "bodies")[0]["ciphertext"])
    assert b"synthetic private content" not in encrypted
    with pytest.raises(DomainError):
        env.engine.source_ingress.cipher.decrypt(encrypted, str(uuid4()))
    view = env.engine.source_ingress.inspect(
        env.cookie, env.installation.scope.source_instance_id, "inputs"
    )
    assert all("metadata" not in row and "sender_ref" not in row for row in view)
    assert (
        env.engine.source_ingress.body(env.cookie, receipt.input_ids[0])
        == b"synthetic private content"
    )


@pytest.mark.parametrize("change", ["policy", "roles", "source", "owner"])
def test_current_revocation_purges_ciphertext_and_replay_never_restores_it(env, change):
    receipt = env.gateway.receive(envelope(env))
    if change == "policy":
        env.engine.source_policies.revoke(
            env.cookie, env.policy_id, PolicyRevoke(expected_revision=1, review_ref="SA04-revoke")
        )
    elif change == "roles":
        env.identities.roles(
            env.cookie,
            env.binding["binding_id"],
            RoleChange(
                expected_revision=env.binding["revision"],
                roles=("requester",),
                review_ref="SA04-revoke-role",
            ),
        )
    elif change == "source":
        SourceRegistry(env.engine).register_instance(
            env.installation.model_copy(update={"revision": 2})
        )
    else:
        env.engine.source_ingress.configure(
            env.cookie,
            env.installation.scope.source_instance_id,
            IngressConfigure(
                expected_generation=1,
                settings=env.settings.model_copy(update={"owner": "disabled"}),
                review_ref="SA04-disable",
            ),
        )
    assert not rows(env, "bodies")
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, receipt.input_ids[0])
    if change != "owner":
        result = env.engine.source_ingress.replay(
            env.cookie,
            env.installation.scope.source_instance_id,
            ReplayRequest(
                expected_generation=1, input_ids=receipt.input_ids, review_ref="SA04-replay"
            ),
        )
        assert result["new_business_inputs"] == 0 and not result["executes_tasks"]
        assert result["items"][0]["status"] == "blocked_current_authority_or_retention"


@pytest.mark.parametrize("age", [-3600, 120])
def test_old_or_future_messages_are_context_only(env, age):
    env.gateway.receive(
        envelope(env, occurred_at=(datetime.now(UTC) + timedelta(seconds=age)).isoformat())
    )
    assert rows(env, "inputs")[0]["context_only"]


def test_cursor_jump_uses_contiguous_watermark_and_only_actual_receipts_close_gap(env):
    for sequence in [1, 3]:
        env.gateway.receive(
            envelope(
                env,
                delivery=f"d{sequence}",
                entity=f"m{sequence}",
                cursor={
                    "stream_id": "messages",
                    "epoch": "E1",
                    "sequence": sequence,
                    "value": f"cursor-{sequence}",
                },
            )
        )
    row = rows(env, "cursors")[0]
    assert row["committed_sequence"] == 1 and row["highest_sequence"] == 3
    gap = rows(env, "gaps")[0]
    assert (gap["sequence_start"], gap["sequence_end"], gap["resolved_at"]) == (2, 2, None)
    env.gateway.receive(
        envelope(
            env,
            delivery="d2",
            entity="m2",
            historical=True,
            cursor={"stream_id": "messages", "epoch": "E1", "sequence": 2, "value": "cursor-2"},
        )
    )
    assert rows(env, "cursors")[0]["committed_sequence"] == 3
    assert rows(env, "gaps")[0]["resolution_code"] == "COVERED_BY_COMMITTED_RECEIPTS"
    assert all(
        r["context_only"]
        for r in rows(env, "inputs")
        if r["input_id"] != rows(env, "inputs")[0]["input_id"]
    )


def test_cursor_epoch_reset_is_explicit_cas_and_keeps_unknown_gap(env):
    env.gateway.receive(
        envelope(env, cursor={"stream_id": "messages", "epoch": "E1", "sequence": 1})
    )
    reset = env.engine.source_ingress.reset_cursor(
        env.cookie,
        env.installation.scope.source_instance_id,
        "messages",
        CursorReset(
            expected_revision=rows(env, "cursors")[0]["revision"],
            epoch="E2",
            review_ref="SA04-reviewed-reset",
        ),
    )
    assert reset["gap_recovered"] is False
    assert rows(env, "cursors")[0]["committed_sequence"] == 0
    assert rows(env, "gaps")[0]["resolved_at"] is None


def test_history_rechecks_scope_before_network_and_remains_context_only(env):
    class Provider:
        calls = 0

        def fetch(self, instance, request):
            self.calls += 1
            assert (
                env.engine.connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            )
            return HistoryPage(
                (SignedIngress(env.engine).verify_ingress(instance, envelope(env)),), None, True
            )

    provider = Provider()
    request = HistoryRequest(
        source_thread={"scope": env.installation.scope, "conversation_id": "Chat-A"}, limit=2
    )
    env.gateway.history(provider, request)
    assert provider.calls == 1 and rows(env, "inputs")[0]["context_only"]
    env.engine.source_policies.revoke(
        env.cookie,
        env.policy_id,
        PolicyRevoke(expected_revision=1, review_ref="SA04-revoke-before-fetch"),
    )
    with pytest.raises(DomainError):
        env.gateway.history(provider, request)
    assert provider.calls == 1


def test_missing_history_records_gap_without_fabricating_recovery(engine):
    env = setup(
        engine,
        capabilities={
            key: "unsupported"
            for key in [
                "history_read",
                "incremental_cursor",
                "reconnect_replay",
                "edit_events",
                "delete_events",
                "native_entity_revision",
            ]
        },
    )

    class Provider:
        def fetch(self, *args):
            raise AssertionError("Must not read unsupported history")

    with pytest.raises(DomainError):
        env.gateway.history(
            Provider(),
            HistoryRequest(
                source_thread={"scope": env.installation.scope, "conversation_id": "Chat-A"}
            ),
        )
    assert rows(env, "gaps")[0]["declaration"]["reason"] == "no_replay"
    assert not rows(env, "inputs")


@pytest.mark.parametrize("bound", ["pending", "receipts", "bytes"])
def test_bounded_backlog_receipts_and_ciphertext_never_ack_overflow(engine, bound):
    limits = {
        "pending": {"max_pending": 1},
        "receipts": {"max_receipts": 1},
        "bytes": {"max_retained_bytes": 1024},
    }[bound]
    env = setup(engine, limits=limits)
    first = envelope(env, body="A" * 350 if bound == "bytes" else "A")
    env.gateway.receive(first)
    acks = []
    with pytest.raises(DomainError):
        env.gateway.receive(
            envelope(env, delivery="d2", entity="m2", body="B" * 350 if bound == "bytes" else "B"),
            acks.append,
        )
    assert not acks and len(rows(env, "inputs")) == 1


def test_retention_false_stores_no_body_and_expiry_cannot_revive(engine):
    env = setup(engine, receive={"enabled": True})
    receipt = env.gateway.receive(envelope(env))
    assert not rows(env, "bodies")
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, receipt.input_ids[0])
    assert (
        env.engine.source_ingress.buffer_permit(
            env.installation,
            1,
            SignedIngress(env.engine).verify_ingress(env.installation, envelope(env)),
        )
        is None
    )


def test_body_expiry_and_monotonic_input_and_receipt_facts(engine):
    env = setup(engine, receive={"enabled": True, "retain_body": True, "body_ttl_seconds": 1})
    receipt = env.gateway.receive(envelope(env))
    time.sleep(1.05)
    with pytest.raises(DomainError):
        env.engine.source_ingress.body(env.cookie, receipt.input_ids[0])
    assert (
        env.engine.source_ingress.purge(env.cookie, env.installation.scope.source_instance_id)[
            "expired_bodies"
        ]
        == 1
    )
    assert not rows(env, "bodies")
    for statement, parameters in [
        (
            "UPDATE app.source_ingress_inputs SET unavailable_at=NULL WHERE input_id=%s",
            (receipt.input_ids[0],),
        ),
        ("DELETE FROM app.source_ingress_inputs WHERE input_id=%s", (receipt.input_ids[0],)),
        (
            "UPDATE app.source_ingress_deliveries SET reason_code='forged' WHERE receipt_id=%s",
            (receipt.receipt_id,),
        ),
    ]:
        with pytest.raises(psycopg.Error), env.engine.transaction() as conn:
            conn.execute(statement, parameters)
    assert env.gateway.receive(envelope(env)).disposition == "duplicate"
    assert not rows(env, "bodies")


def test_owner_cas_and_legacy_cutover_prevent_both_business_writers(engine, trigger):
    old = engine.ingest(trigger)
    env = setup(engine, kind="github")
    with pytest.raises(DomainError) as error:
        engine.ingest(trigger.model_copy(update={"external_event_id": "new-old-path"}))
    assert error.value.code == "INGRESS_NEW_OWNER"
    adopted = env.gateway.receive(
        envelope(env, delivery=trigger.external_event_id, body=trigger.body)
    )
    assert (
        adopted.disposition == "legacy_owned"
        and adopted.original_trigger_id == old.event_id
        and not adopted.input_ids
    )
    fresh = env.gateway.receive(envelope(env, delivery="new-receipt"))
    assert fresh.input_ids
    env.engine.source_ingress.configure(
        env.cookie,
        env.installation.scope.source_instance_id,
        IngressConfigure(
            expected_generation=1,
            settings=env.settings.model_copy(update={"owner": "legacy"}),
            review_ref="SA04-cut-back",
        ),
    )
    with pytest.raises(DomainError) as error:
        engine.ingest(trigger.model_copy(update={"external_event_id": "new-receipt"}))
    assert error.value.code == "INGRESS_DELIVERY_ALREADY_OWNED"
    with pytest.raises(DomainError):
        env.gateway.receive(envelope(env, delivery="stale-generation"))
    with pytest.raises(DomainError):
        env.engine.source_ingress.configure(
            env.cookie,
            env.installation.scope.source_instance_id,
            IngressConfigure(
                expected_generation=1, settings=env.settings, review_ref="stale-owner"
            ),
        )


def test_outer_savepoint_is_not_a_durable_ack_boundary(env):
    with env.engine.transaction(), pytest.raises(DomainError) as error:
        env.gateway.receive(envelope(env))
    assert error.value.code == "INGRESS_OUTER_TRANSACTION"
    assert not rows(env, "inputs")


def test_buffer_is_encrypted_private_bounded_and_only_replayed_into_engine(env, tmp_path):
    directory = tmp_path / "buffer"
    directory.mkdir(mode=0o700)
    spool = EncryptedSpool(directory, BodyCipher(env.key), max_records=1)
    delivery = SignedIngress(env.engine).verify_ingress(env.installation, envelope(env))
    permit = env.engine.source_ingress.buffer_permit(env.installation, 1, delivery)
    assert permit is not None
    identifier = spool.put(permit)
    file = directory / (identifier + ".bin")
    assert (
        b"synthetic private content" not in file.read_bytes()
        and file.stat().st_mode & 0o777 == 0o600
    )
    assert spool.read(identifier).delivery == permit.delivery
    with pytest.raises(DomainError):
        spool.put(permit)
    result = gateway(env, spool=spool).drain_buffer()
    assert result[0]["status"] == "engine_committed" and not spool.ids()
    assert len(rows(env, "inputs")) == 1 and rows(env, "inputs")[0]["context_only"]


@pytest.mark.parametrize("change", ["expiry", "revocation"])
def test_buffer_replay_rechecks_authority_and_discards_stale_body(env, tmp_path, change):
    directory = tmp_path / "buffer"
    directory.mkdir(mode=0o700)
    spool = EncryptedSpool(directory, BodyCipher(env.key))
    permit = env.engine.source_ingress.buffer_permit(
        env.installation,
        1,
        SignedIngress(env.engine).verify_ingress(env.installation, envelope(env)),
    )
    if change == "expiry":
        permit = replace(permit, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    spool.put(permit)
    if change == "revocation":
        env.engine.source_policies.revoke(
            env.cookie,
            env.policy_id,
            PolicyRevoke(expected_revision=1, review_ref="SA04-buffer-revoke"),
        )
    result = gateway(env, spool=spool).drain_buffer()
    assert (
        result[0]["status"] == "discarded_stale_authority"
        and not spool.ids()
        and not rows(env, "inputs")
    )
    assert rows(env, "gaps")[0]["resolved_at"] is None


@pytest.mark.parametrize(
    "attack", ["outside", "directory_symlink", "file_symlink", "hardlink", "corrupt"]
)
def test_private_buffer_cannot_escape_or_accept_corrupt_records(env, tmp_path, attack):
    directory = tmp_path / "buffer"
    directory.mkdir(mode=0o700)
    spool = EncryptedSpool(directory, BodyCipher(env.key))
    permit = env.engine.source_ingress.buffer_permit(
        env.installation,
        1,
        SignedIngress(env.engine).verify_ingress(env.installation, envelope(env)),
    )
    identifier = spool.put(permit)
    file = directory / (identifier + ".bin")
    if attack == "outside":
        spool = EncryptedSpool(Path("/tmp/sa04-outside"), BodyCipher(env.key))

        def method():
            return spool.put(permit)
    elif attack == "directory_symlink":
        link = tmp_path / "link"
        link.symlink_to(directory, target_is_directory=True)
        spool = EncryptedSpool(link, BodyCipher(env.key))
        method = spool.ids
    elif attack == "file_symlink":
        file.unlink()
        file.symlink_to(tmp_path / "unreadable-target")
        method = spool.ids
    elif attack == "hardlink":
        os.link(file, tmp_path / "other-link")
        method = spool.ids
    else:
        file.write_bytes(b"not valid ciphertext")

        def method():
            return spool.read(identifier)

    with pytest.raises((DomainError, OSError)):
        method()


def test_gateway_requires_trusted_verifier_and_actual_signed_scope(env):
    no_verifier = SourceGateway(
        env.engine.source_ingress, env.installation.scope.source_instance_id, 1
    )
    with pytest.raises(DomainError):
        no_verifier.receive(envelope(env))
    original = envelope(env)
    forged = TransportEnvelope(headers=original.headers, payload=original.payload + b" ")
    with pytest.raises(DomainError):
        env.gateway.receive(forged)
    raw = json.loads(original.payload)
    raw["scope"]["tenant_id"] = "Tenant-B"
    with pytest.raises(DomainError):
        env.gateway.receive(signed(raw))
    assert not rows(env, "inputs")


def test_management_api_has_cookie_origin_csrf_no_store_and_no_raw_ingress(env):
    app = create_app(env.engine.settings, env.engine, run_background=False)
    prefix = f"/api/v3/source-ingress/{env.installation.scope.source_instance_id}"
    with TestClient(app, base_url="http://127.0.0.1:18080", client=("127.0.0.1", 12345)) as client:
        assert client.get(prefix + "/view/receipts").status_code in {401, 403}
        client.cookies.set(SESSION_COOKIE, env.cookie)
        assert client.get(prefix + "/view/receipts").headers["cache-control"] == "no-store"
        assert client.post(prefix + "/purge").status_code == 403
        user = WebAccounts(env.engine).session(env.cookie)
        headers = {"Origin": "http://127.0.0.1:18080", "X-CSRF-Token": user.csrf_token}
        assert client.post(prefix + "/purge", headers=headers).status_code == 200
        assert client.post(prefix + "/receive", json={}).status_code in {404, 405}
        assert client.get(prefix + "/view/bodies").status_code == 422
        assert client.get(f"/api/v3/source-ingress/{uuid4()}/view/inputs").status_code == 404


def test_published_ingress_schemas_match_runtime_exactly():
    from mgar_v3.sources.ingress_domain import (
        CursorReset,
        DeliveryMetadata,
        GapDeclaration,
        HistoryRequest,
        IngressConfigure,
        IngressReceipt,
        IngressSettings,
        ReplayRequest,
    )

    for name, model in [
        ("settings", IngressSettings),
        ("configure", IngressConfigure),
        ("delivery", DeliveryMetadata),
        ("receipt", IngressReceipt),
        ("gap", GapDeclaration),
        ("replay", ReplayRequest),
        ("cursor-reset", CursorReset),
        ("history", HistoryRequest),
    ]:
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        assert (
            json.loads(
                (
                    ROOT / "schemas/super-agent-v1" / ("source-ingress-" + name + "-v1.schema.json")
                ).read_text()
            )
            == schema
        )


@pytest.mark.parametrize(
    "values",
    [
        {"max_pending": 0},
        {"max_receipts": 100001},
        {"max_body_bytes": 65537},
        {"max_retained_bytes": 1023},
        {"owner": "gateway"},
        {"buffer_ttl_seconds": 301},
    ],
)
def test_invalid_bounds_and_implicit_policy_are_rejected(values):
    with pytest.raises(ValidationError):
        IngressSettings(**values)


@pytest.mark.parametrize("changed", ["tenant", "installation", "platform"])
def test_same_external_ids_in_distinct_trusted_installations_are_independent(env, changed):
    from types import SimpleNamespace

    from test_super_agent_sa02 import ControlledPlatform, bind

    from mgar_v3.sources.domain import SourceScope
    from mgar_v3.sources.identity import SourceIdentities
    from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave

    first = env.gateway.receive(envelope(env))
    scope = env.installation.scope.model_dump()
    scope["source_instance_id"] = uuid4()
    if changed == "platform":
        scope["source_kind"] = "github"
    else:
        scope[changed + "_id"] = "Different-Case-ID"
    installation = env.installation.model_copy(update={"scope": SourceScope.model_validate(scope)})
    SourceRegistry(env.engine).register_instance(installation)
    identities = SourceIdentities(
        env.engine,
        {(installation.scope.source_kind, "controlled-v1"): ControlledPlatform(env.engine)},
    )
    bind(identities, env.cookie, installation)
    raw = env.definition.model_dump(mode="json")
    raw["selector"]["scope"] = scope
    raw["selector"]["actors"][0]["scope"] = scope
    policy_id = uuid4()
    env.engine.source_policies.save(
        env.cookie,
        policy_id,
        PolicySave(
            expected_revision=0,
            definition=PolicyDefinition.model_validate(raw),
            review_ref="SA04-other-scope",
        ),
    )
    settings = env.settings.model_copy(
        update={
            "policy_id": policy_id,
            "legacy_repository": env.engine.settings.repository if changed == "platform" else None,
        }
    )
    env.engine.source_ingress.configure(
        env.cookie,
        installation.scope.source_instance_id,
        IngressConfigure(expected_generation=0, settings=settings, review_ref="SA04-other-source"),
    )
    other = SimpleNamespace(
        engine=env.engine, installation=installation, occurred_at=env.occurred_at
    )
    second = gateway(other).receive(envelope(other))
    assert first.input_ids != second.input_ids and first.delivery_key != second.delivery_key
    assert len(rows(env, "inputs")) == 2


def test_event_type_is_part_of_key_even_with_same_remote_id(env):
    first = env.gateway.receive(envelope(env, event="shared-platform-id"))
    second = env.gateway.receive(
        envelope(env, delivery="edit", version=2, operation="edit", event="shared-platform-id")
    )
    assert first.input_ids != second.input_ids and len(rows(env, "aliases")) == 2


def test_snapshot_generation_change_during_verification_fences_submission(env):
    base = SignedIngress(env.engine)

    class CutoverVerifier:
        def verify_ingress(self, instance, incoming):
            delivery = base.verify_ingress(instance, incoming)
            env.engine.source_ingress.configure(
                env.cookie,
                instance.scope.source_instance_id,
                IngressConfigure(
                    expected_generation=1,
                    settings=env.settings.model_copy(update={"owner": "disabled"}),
                    review_ref="SA04-cutover-race",
                ),
            )
            return delivery

    candidate = SourceGateway(
        env.engine.source_ingress,
        env.installation.scope.source_instance_id,
        1,
        {("feishu", "controlled-v1"): CutoverVerifier()},
    )
    ack = []
    with pytest.raises(DomainError):
        candidate.receive(envelope(env), ack.append)
    assert not ack and not rows(env, "inputs")


def test_database_unavailable_before_receive_authority_never_buffers_body(env, tmp_path):
    directory = tmp_path / "buffer"
    directory.mkdir(mode=0o700)
    spool = EncryptedSpool(directory, BodyCipher(env.key))
    candidate = gateway(env, spool=spool)
    env.engine.close()
    with pytest.raises(DomainError) as error:
        candidate.receive(envelope(env))
    assert error.value.code == "INGRESS_DATABASE_UNAVAILABLE" and not spool.ids()


def test_history_wrong_conversation_is_rejected_before_any_page_commits(env):
    class Provider:
        def fetch(self, instance, request):
            adapter = SignedIngress(env.engine)
            return HistoryPage(
                (
                    adapter.verify_ingress(instance, envelope(env)),
                    adapter.verify_ingress(
                        instance,
                        envelope(env, delivery="private", entity="m2", conversation="Private"),
                    ),
                ),
                None,
                True,
            )

    with pytest.raises(DomainError):
        env.gateway.history(
            Provider(),
            HistoryRequest(
                source_thread={"scope": env.installation.scope, "conversation_id": "Chat-A"},
                limit=2,
            ),
        )
    assert not rows(env, "inputs") and not rows(env, "deliveries")


def test_metadata_retention_clears_source_inbox_and_original_delivery_id(env):
    # Explicit time fixture; physical failure tests use processes/containers, not status rewrites.
    from mgar_v3.domain import digest
    from mgar_v3.sources.domain import SourceMetadata

    env.gateway.receive(envelope(env))
    event = (
        SignedIngress(env.engine)
        .verify_ingress(
            env.installation, envelope(env, delivery="old-metadata", entity="old-entity")
        )
        .metadata.items[0]
        .event
    )
    inbox = SourceRegistry(env.engine).stage_metadata(SourceMetadata.from_event(event))
    receipt = uuid4()
    with env.engine.transaction() as conn:
        conn.execute(
            "INSERT INTO app.source_ingress_deliveries(receipt_id,source_instance_id,"
            "project_id,delivery_key,delivery_id,payload_sha256,normalized_sha256,"
            "generation,disposition,reason_code,sealed,metadata_expires_at) VALUES (%s,%s,"
            "%s,%s,%s,%s,%s,1,'rejected','TIME_FIXTURE',true,now()-interval '1 second')",
            (
                receipt,
                env.installation.scope.source_instance_id,
                env.engine.settings.project_id,
                digest(str(receipt)),
                "expired-raw-delivery",
                "f" * 64,
                "f" * 64,
            ),
        )
        conn.execute(
            "INSERT INTO app.source_ingress_inputs(input_id,inbox_id,receipt_id,"
            "source_instance_id,project_id,entity_key,version_key,version_fingerprint,"
            "operation,metadata,sender_ref,policy_id,policy_revision,identity_id,"
            "identity_revision,reason_code,metadata_expires_at) SELECT %s,%s,%s,"
            "source_instance_id,project_id,entity_key,%s,version_fingerprint,operation,"
            "metadata,sender_ref,policy_id,policy_revision,identity_id,identity_revision,"
            "'TIME_FIXTURE',now()-interval '1 second' FROM app.source_ingress_inputs LIMIT 1",
            (inbox, inbox, receipt, digest(str(inbox))),
        )
    result = env.engine.source_ingress.purge(env.cookie, env.installation.scope.source_instance_id)
    assert result["expired_metadata"] == 1
    with env.engine.transaction() as conn:
        item = conn.execute(
            "SELECT metadata,sender_ref,inbox_id,unavailable_at FROM "
            "app.source_ingress_inputs WHERE input_id=%s",
            (inbox,),
        ).fetchone()
        assert (
            item["metadata"] is None
            and item["sender_ref"] is None
            and item["inbox_id"] is None
            and item["unavailable_at"] is not None
        )
        assert (
            conn.execute("SELECT 1 FROM app.source_inbox WHERE inbox_id=%s", (inbox,)).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT delivery_id FROM app.source_ingress_deliveries WHERE receipt_id=%s",
                (receipt,),
            ).fetchone()["delivery_id"]
            is None
        )


def test_replay_revalidates_current_input_but_does_not_duplicate_or_reset_consumption(env):
    receipt = env.gateway.receive(envelope(env))
    with env.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.source_ingress_inputs SET consumed_at=now() WHERE input_id=%s",
            (receipt.input_ids[0],),
        )
    result = env.engine.source_ingress.replay(
        env.cookie,
        env.installation.scope.source_instance_id,
        ReplayRequest(
            expected_generation=1, input_ids=receipt.input_ids, review_ref="SA04-consumed-replay"
        ),
    )
    assert result["items"][0]["status"] == "already_consumed" and result["new_business_inputs"] == 0
    assert len(rows(env, "inputs")) == 1
    with pytest.raises(psycopg.Error), env.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.source_ingress_inputs SET consumed_at=NULL WHERE input_id=%s",
            (receipt.input_ids[0],),
        )


def test_stale_cookie_cannot_configure_or_replay_even_after_http_precheck(env):
    receipt = env.gateway.receive(envelope(env))
    WebAccounts(env.engine).session(env.cookie)
    WebAccounts(env.engine).revoke(env.cookie)
    with pytest.raises(DomainError):
        env.engine.source_ingress.replay(
            env.cookie,
            env.installation.scope.source_instance_id,
            ReplayRequest(
                expected_generation=1, input_ids=receipt.input_ids, review_ref="SA04-stale-cookie"
            ),
        )


def test_explicit_disabled_repository_blocks_both_old_and_new_ingress(engine, trigger):
    env = setup(engine, kind="github")
    env.engine.source_ingress.configure(
        env.cookie,
        env.installation.scope.source_instance_id,
        IngressConfigure(
            expected_generation=1,
            settings=env.settings.model_copy(update={"owner": "disabled"}),
            review_ref="SA04-explicit-disabled",
        ),
    )
    with pytest.raises(DomainError) as error:
        engine.ingest(trigger)
    assert error.value.code == "INGRESS_DISABLED"
    with pytest.raises(DomainError):
        env.gateway.receive(envelope(env))
    assert not rows(env, "inputs")


def test_cursor_position_conflict_is_durable_without_changing_original_input(env):
    cursor = {"stream_id": "messages", "epoch": "E1", "sequence": 1}
    first = env.gateway.receive(envelope(env, cursor=cursor))
    with pytest.raises(DomainError) as error:
        env.gateway.receive(
            envelope(
                env, delivery="cursor-conflict", entity="other", body="different", cursor=cursor
            )
        )
    assert error.value.code == "INGRESS_CURSOR_CONFLICT"
    assert len(rows(env, "inputs")) == 1 and len(rows(env, "conflicts")) == 1
    assert (
        env.engine.source_ingress.body(env.cookie, first.input_ids[0])
        == b"synthetic private content"
    )
