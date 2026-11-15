"""First formal SA18 original Engine, migrations, actual Worker and Draft protocol."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from sa18_runtime_support import approve, reviewed

from mgar_v3.domain import DomainError
from mgar_v3.sources.identity_domain import RoleChange
from mgar_v3.super_agent.source_review_domain import ReviewExact
from mgar_v3.super_agent.source_review_transport import ReviewLedger
from mgar_v3.super_agent.source_reviews import SourceReviews

A = Path(__file__).resolve().parents[1]


def test_formal_0050_is_exact_confirmed_forward_migration_and_not_draft(engine):
    assert type(engine.source_reviews) is SourceReviews
    ack = json.loads((A / "docs/super-agent-v1/sa18-proposal/ownership-confirmed.json").read_text())
    assert ack["commit"] == "09145fe0939b209c43addb0ae6bbbdcc6a6df9a2"
    with engine.transaction() as conn:
        rows = conn.execute(
            "SELECT name,sha256 FROM app.schema_migrations ORDER BY name"
        ).fetchall()
        assert len(rows) == 48
        for r in rows:
            assert (
                hashlib.sha256((A / "migrations" / r["name"]).read_bytes()).hexdigest()
                == r["sha256"]
            )
        installed = {row["name"]: row["sha256"] for row in rows}
        assert installed["0050_source_reviews.sql"] == ack["SQL_sha256"]
        assert conn.execute("SELECT pg_try_advisory_lock(31010002) AS owned").fetchone()["owned"]
        conn.execute("SELECT pg_advisory_unlock(31010002)")


def test_formal_sixteen_routes_exist_in_original_app_without_default_runtime(engine):
    from mgar_v3.api import create_app

    app = create_app(engine.settings, engine, run_background=False)
    actual = {
        method + " " + r.path for r in app.routes if hasattr(r, "methods") for method in r.methods
    }
    manifest = json.loads(
        (A / "docs/super-agent-v1/sa18-proposal/proposal-manifest.json").read_text()
    )
    assert set(manifest["interfaces"]) <= actual
    assert engine.source_reviews._profiles == {}
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 0


def test_worker_actual_bundle_owner_approval_and_original_three_step_draft(engine, tmp_path):
    with reviewed(engine, tmp_path) as e:
        review = e.review
        assert review["status"] == "proposed" and review["mode"] == "fixture"
        assert engine.get_run(review["run_id"])["status"] == "waiting_approval"
        view = e.service.inspect(e.cookie, review["review_id"])
        assert view["complete"] and "+    return sum(values)\n" in view["bundle"]["diff"]
        assert len(view["bundle"]["files"]) == 1
        assert view["review"]["verification"]["tests_run"] >= 2
        with engine.transaction() as conn:
            row = conn.execute("SELECT ciphertext FROM app.sa_source_review_payloads").fetchone()
            assert b"return sum(values)" not in bytes(row["ciphertext"])
            assert conn.execute("SELECT count(*) AS n FROM app.approvals").fetchone()["n"] == 0
        approved = approve(e)
        assert approved["status"] == "ready" and not e.remote.writes
        result = e.service.tick(e.cookie)
        assert result["actions"] == [review["action_id"]]
        assert e.remote.writes == ["branch", "commit", "pull_request"]
        after = e.service.get(e.cookie, review["review_id"])
        assert after["status"] == "succeeded" and len(after["links"]) == 1
        assert after["links"][0]["facts"]["ticket_id"] == str(review["ticket_id"])
        assert after["links"][0]["facts"]["mode"] == "fixture"
        assert e.service.tick(e.cookie)["actions"] == []
        with engine.transaction() as conn:
            assert [
                r["status"]
                for r in conn.execute(
                    "SELECT status FROM app.repair_publication_steps ORDER BY ordinal"
                ).fetchall()
            ] == ["succeeded"] * 3
            assert (
                conn.execute("SELECT count(*) AS n FROM app.workbench_publications").fetchone()["n"]
                == 0
            )


def test_duplicate_prepare_and_generic_approve_cannot_bypass_owner_presentation(engine, tmp_path):
    with reviewed(engine, tmp_path) as e:
        duplicate = e.service.prepare(e.cookie, e.request)
        assert not duplicate["created"] and duplicate["action_id"] == e.review["action_id"]
        with pytest.raises(DomainError):
            engine.approve(
                e.review["action_id"], e.review["action_sha256"], engine.settings.local_actor
            )
        with engine.transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 1
            assert conn.execute("SELECT count(*) AS n FROM app.outbox").fetchone()["n"] == 0
        assert not e.remote.writes


def test_read_only_profile_exports_complete_patch_and_stays_pending(engine, tmp_path):
    with reviewed(engine, tmp_path, write=False) as e:
        assert "return sum(values)" in e.service.patch(e.cookie, e.review["review_id"])
        with pytest.raises(DomainError) as exc:
            approve(e)
        assert exc.value.code == "SOURCE_REVIEW_WRITE_CREDENTIAL_REQUIRED"
        assert e.service.get(e.cookie, e.review["review_id"])["status"] == "proposed"
        assert e.service.tick(e.cookie)["actions"] == [] and not e.remote.writes


def test_actual_tree_changed_after_verification_blocks_inspection_and_approval(engine, tmp_path):
    with reviewed(engine, tmp_path) as e:
        (e.worker.raw.root / "src/calc.py").write_text("UNVERIFIED = True\n")
        with pytest.raises(DomainError):
            e.service.inspect(e.cookie, e.review["review_id"])
        assert e.service.get(e.cookie, e.review["review_id"])["status"] == "proposed"
        assert not e.remote.writes


def test_owner_rejection_erases_body_and_cancels_only_unsent_action(engine, tmp_path):
    with reviewed(engine, tmp_path) as e:
        approve(e)
        exact = ReviewExact(
            bundle_sha256=e.review["bundle_sha256"], action_sha256=e.review["action_sha256"]
        )
        result = e.service.cancel(e.cookie, e.review["review_id"], exact)
        assert result["status"] == "cancelled" and result["invalidated_at"] is not None
        with engine.transaction() as conn:
            assert (
                conn.execute("SELECT ciphertext FROM app.sa_source_review_payloads").fetchone()[
                    "ciphertext"
                ]
                is None
            )
            assert conn.execute("SELECT status FROM app.outbox").fetchone()["status"] == "cancelled"
        assert e.service.tick(e.cookie)["actions"] == []


@pytest.mark.parametrize("cause", ["input", "role"])
def test_committed_source_loss_erases_body_and_cancels_pending_in_same_transaction(
    cause, engine, tmp_path
):
    with reviewed(engine, tmp_path) as e:
        approve(e)
        if cause == "input":
            with engine.transaction() as conn:
                conn.execute(
                    "SELECT app.expire_source_input(%s,'SA18_TEST_DELETE')", (e.worker.input_id,)
                )
        else:
            binding = e.worker.source.binding
            e.worker.source.identities.roles(
                e.cookie,
                binding["binding_id"],
                RoleChange(
                    expected_revision=binding["revision"],
                    roles=("requester",),
                    review_ref="withdraw",
                ),
            )
        with engine.transaction() as conn:
            r = conn.execute("SELECT * FROM app.sa_source_reviews").fetchone()
            assert r["invalidated_at"] is not None
            assert (
                conn.execute("SELECT ciphertext FROM app.sa_source_review_payloads").fetchone()[
                    "ciphertext"
                ]
                is None
            )
        assert e.service.get(e.cookie, e.review["review_id"])["status"] == "cancelled"
        assert not e.remote.writes


@pytest.mark.parametrize("loss", ["branch", "commit", "pull_request"])
def test_lost_receipt_is_unknown_and_read_only_reconcile_never_continues_writes(
    loss, engine, tmp_path
):
    with reviewed(engine, tmp_path, lost=loss) as e:
        approve(e)
        e.service.tick(e.cookie)
        assert e.service.get(e.cookie, e.review["review_id"])["status"] == "outcome_unknown"
        before = list(e.remote.writes)
        assert e.service.tick(e.cookie)["actions"] == []
        assert e.service.reconcile(e.cookie, e.review["review_id"])["reconciled"] == (
            loss == "pull_request"
        )
        assert e.remote.writes == before
        with pytest.raises(DomainError):
            e.service.cancel(
                e.cookie,
                e.review["review_id"],
                ReviewExact(
                    bundle_sha256=e.review["bundle_sha256"], action_sha256=e.review["action_sha256"]
                ),
            )


def test_final_guard_follows_committed_step_reservation(engine, tmp_path, monkeypatch):
    with reviewed(engine, tmp_path) as e:
        approve(e)
        original = ReviewLedger.begin_step

        def reserve_then_revoke(self, *args):
            state = original(self, *args)
            with engine.transaction() as conn:
                conn.execute(
                    "SELECT app.expire_source_input(%s,'SA18_FINAL_BOUNDARY')", (e.worker.input_id,)
                )
            return state

        monkeypatch.setattr(ReviewLedger, "begin_step", reserve_then_revoke)
        e.service.tick(e.cookie)
        assert not e.remote.writes
        assert e.service.get(e.cookie, e.review["review_id"])["status"] == "failed"


def test_reconciliation_uses_hashes_after_payload_erasure(engine, tmp_path):
    with reviewed(engine, tmp_path, lost="pull_request") as e:
        approve(e)
        e.service.tick(e.cookie)
        before = list(e.remote.writes)
        with engine.transaction() as conn:
            conn.execute(
                "SELECT app.sa_source_review_invalidate(%s,'SOURCE_REVIEW_TEST_ERASURE')",
                (e.review["review_id"],),
            )

        class NoCapture:
            def capture(self, context):
                raise AssertionError("Unknown reconciliation must not access Source content")

        e.service._profiles[e.profile.sha256] = replace(e.profile, capture=NoCapture())
        result = e.service.reconcile(e.cookie, e.review["review_id"])
        assert result["reconciled"] and e.remote.writes == before
