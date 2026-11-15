"""First SA18 publication handoff from the actual SA14 asynchronous Worker."""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from sa14_support import actual, restrict, start
from sa18_runtime_support import GitRemote, Reader, Writer

from mgar_v3.super_agent.source_review_capture import H03ReviewCapture
from mgar_v3.super_agent.source_review_domain import ReviewPrepare
from mgar_v3.super_agent.source_review_ports import ReviewProfile


@contextmanager
def async_review(engine, tmp_path, monkeypatch):
    with actual(engine, tmp_path, monkeypatch) as worker:
        start(worker)
        assert worker.async_runtime.join(worker.mandate.coding_id, timeout=90)
        service = engine.source_reviews
        with engine.transaction() as conn:
            v = conn.execute(
                "SELECT * FROM app.sa_coding_verifications WHERE coding_id=%s "
                "ORDER BY created_at DESC LIMIT 1",
                (worker.mandate.coding_id,),
            ).fetchone()
            assert v and v["result"] == "passed"
            now = service.now(conn)
        remote = GitRemote(
            tmp_path / "async-controlled.git", source=worker.source_root, engine=engine
        )
        profile = ReviewProfile(
            "explicit-fixture-SA18-async",
            worker.mandate.worker_profile_sha256,
            worker.mandate.repository.repository,
            worker.mandate.repository.repository_id,
            "main",
            engine.settings.local_actor,
            now + timedelta(hours=1),
            H03ReviewCapture(engine, worker.driver),
            Reader(remote),
            Writer(remote),
            write_authorized=True,
        )
        service.register_profile(profile)
        review = service.prepare(
            worker.cookie,
            ReviewPrepare(
                request_id=uuid4(),
                coding_id=worker.mandate.coding_id,
                verification_id=v["verification_id"],
                expected_epoch=worker.run_request.epoch,
                expected_input_revision=1,
                profile_sha256=profile.sha256,
            ),
        )
        yield SimpleNamespace(
            worker=worker,
            service=service,
            cookie=worker.cookie,
            review=review,
            remote=remote,
            profile=profile,
            verification=v,
        )


def test_async_review_and_ciphertext_do_not_outlive_the_verified_context(
    engine, tmp_path, monkeypatch
):
    with async_review(engine, tmp_path, monkeypatch) as e:
        with engine.transaction() as conn:
            view = conn.execute(
                "SELECT expires_at FROM app.sa_async_operation_views WHERE operation_id=%s",
                (e.verification["operation_id"],),
            ).fetchone()
            body = conn.execute(
                "SELECT expires_at FROM app.sa_source_review_payloads WHERE review_id=%s",
                (e.review["review_id"],),
            ).fetchone()
        assert e.review["expires_at"] <= view["expires_at"]
        assert body["expires_at"] <= view["expires_at"]
        assert not e.remote.writes


def test_owner_async_restriction_commits_while_review_waits_and_invalidates_old_bundle(
    engine, tmp_path, monkeypatch
):
    with async_review(engine, tmp_path, monkeypatch) as e:
        assert engine.get_run(e.review["run_id"])["status"] == "waiting_approval"
        _, receipt = restrict(e.worker)
        assert receipt["state"] == "received"
        with engine.transaction() as conn:
            review = conn.execute(
                "SELECT invalidated_at FROM app.sa_source_reviews WHERE review_id=%s",
                (e.review["review_id"],),
            ).fetchone()
            body = conn.execute(
                "SELECT ciphertext FROM app.sa_source_review_payloads WHERE review_id=%s",
                (e.review["review_id"],),
            ).fetchone()
        assert review["invalidated_at"] is not None and body["ciphertext"] is None
        assert e.service.get(e.cookie, e.review["review_id"])["status"] == "cancelled"
        assert not e.remote.writes
