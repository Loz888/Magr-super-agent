"""DB-backed V3.2 repository analysis tests.

These run only when a real PostgreSQL DSN is configured (for example through
``v3/.env`` + ``export MGAR_V3_DATABASE_URL``).  They are kept separate from the
network-free unit suite so engineering and live evidence stay distinguishable.
"""

from __future__ import annotations

import os
from threading import Event, Thread
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

pytestmark = pytest.mark.skipif(
    not os.environ.get("MGAR_V3_DATABASE_URL"),
    reason="Set MGAR_V3_DATABASE_URL (v3/.env) to run PostgreSQL integration tests",
)


def _input(task: str, request_id: str):
    from mgar_v3.repository_analysis.domain import CreateAnalysisInput

    return CreateAnalysisInput(
        repository_url="https://github.com/octo/example",
        task=task,
        execution_mode="fixture",
        client_request_id=request_id,
    )


def test_fixture_run_persists_evidence_claims_and_report(engine, settings):
    from mgar_v3.repository_analysis.service import RepositoryAnalysisService

    service = RepositoryAnalysisService(engine, settings)
    spec = service.create(_input("trace the startup path", "req-1"))
    assert spec.execution_mode == "fixture"
    assert len(spec.commit) == 40
    service.execute(spec.run_id)
    detail = service.detail(spec.run_id)
    assert detail["run"]["run_status"] == "succeeded"
    assert detail["run"]["outcome"] == "internal_report"
    assert detail["claims"], "verified claims must be persisted"
    for claim in detail["claims"]:
        assert claim["evidence_id"]
        assert claim["quote"]
        assert claim["start_line"] >= 1
    assert any(call["result_version"] for call in detail["tool_calls"])
    report = service.report_bytes(spec.run_id).decode()
    assert f"github://octo/example@{spec.commit}/src/main.py" in report
    assert "L1-L3" in report
    assert "机械" in report


def test_finish_success_commits_stop_reason_with_run_status(engine, settings):
    """The run terminal state and repo stop_reason must share one commit."""

    from mgar_v3.repository_analysis.report import ReportVerification
    from mgar_v3.repository_analysis.service import RepositoryAnalysisService
    from mgar_v3.repository_analysis.store import RepositoryAnalysisStore

    service = RepositoryAnalysisService(engine, settings)
    spec = service.create(_input("atomic completion", "req-atomic"))
    store = RepositoryAnalysisStore(engine)
    run = store.begin_run(spec.run_id)
    epoch = int(run["execution_epoch"])
    entered = Event()
    release = Event()
    original = store.spec

    def blocking_spec(run_id):
        entered.set()
        assert release.wait(10), "test did not release the post-commit reader"
        return original(run_id)

    store.spec = blocking_spec  # type: ignore[method-assign]
    outcome: dict[str, object] = {}

    def worker() -> None:
        try:
            outcome["hash"] = store.finish_success(
                spec.run_id,
                epoch,
                b"# atomic report\n",
                ReportVerification(passed=True, checks=["controlled"]),
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            outcome["error"] = exc

    thread = Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(10), "finish_success did not reach the post-commit read"
        with psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            row_factory=dict_row,
        ) as conn:
            row = conn.execute(
                "SELECT r.status,s.stop_reason,s.report_hash FROM app.runs r "
                "JOIN app.repo_analysis_runs s USING(run_id) WHERE r.run_id=%s",
                (spec.run_id,),
            ).fetchone()
        assert row is not None
        assert row["status"] == "succeeded"
        assert row["stop_reason"] == "complete"
        assert row["report_hash"]
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert "error" not in outcome


def test_idempotent_create_and_thread_follow_up_reuses_evidence(engine, settings):
    from mgar_v3.repository_analysis.domain import FollowUpInput
    from mgar_v3.repository_analysis.service import RepositoryAnalysisService

    service = RepositoryAnalysisService(engine, settings)
    first = service.create(_input("trace the startup path", "req-2"))
    duplicate = service.create(_input("trace the startup path", "req-2"))
    assert first.run_id == duplicate.run_id
    service.execute(first.run_id)

    second = service.follow_up(
        first.run_id, FollowUpInput(client_request_id="req-3", body="继续说明分发入口")
    )
    assert second.thread_id == first.thread_id
    assert second.parent_run_id == first.run_id
    assert second.prior_evidence_map, "parent evidence must be copied into the new run"
    service.execute(second.run_id)
    detail = service.detail(second.run_id)
    assert detail["run"]["run_status"] == "succeeded"
    assert detail["run"]["parent_run_id"] == str(first.run_id)


def test_pause_and_resume_checkpoint(engine, settings):
    from mgar_v3.repository_analysis.service import RepositoryAnalysisService

    service = RepositoryAnalysisService(engine, settings)
    spec = service.create(_input("trace the startup path", "req-4"))
    service.pause(spec.run_id)
    assert service.store.spec(spec.run_id).run_status == "paused"
    service.resume(spec.run_id)
    assert service.store.spec(spec.run_id).run_status == "queued"
    service.execute(spec.run_id)
    assert service.store.spec(spec.run_id).run_status == "succeeded"


def test_unknown_repository_identity_is_rejected_before_database(engine, settings):
    from mgar_v3.domain import DomainError
    from mgar_v3.repository_analysis.domain import CreateAnalysisInput
    from mgar_v3.repository_analysis.service import RepositoryAnalysisService

    service = RepositoryAnalysisService(engine, settings)
    with pytest.raises(ValueError):
        CreateAnalysisInput(
            repository_url="https://github.com.do/octo/example",
            task="x",
            client_request_id=str(uuid4()),
        )
    with pytest.raises(DomainError):
        service.detail(uuid4())
