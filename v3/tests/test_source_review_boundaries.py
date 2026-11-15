"""First SA18 current remote, session, exact approval and native proof boundaries."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sa05_support import payload, signed
from sa05_support import setup as github_setup
from sa18_runtime_support import approve, git, reviewed

from mgar_v3.domain import DomainError
from mgar_v3.super_agent.source_review_domain import ReviewApprove, ReviewNativeLink
from mgar_v3.super_agent.source_review_transport import ReviewLedger
from mgar_v3.web_access import WebAccounts


def test_original_approval_requires_current_session_and_unexpired_complete_presentation(
    engine, tmp_path, monkeypatch
):
    with reviewed(engine, tmp_path) as e:
        view = e.service.inspect(e.cookie, e.review["review_id"])
        request = ReviewApprove.model_validate_json(
            json.dumps(
                {
                    "presentation_id": view["presentation"]["presentation_id"],
                    "bundle_sha256": e.review["bundle_sha256"],
                    "action_sha256": e.review["action_sha256"],
                }
            )
        )
        with engine.transaction() as conn:
            user = e.service._console(conn, e.cookie)
        another = WebAccounts(engine).issue_session(user)
        with pytest.raises(DomainError):
            e.service.approve(another, e.review["review_id"], request)
        now = e.service.now
        monkeypatch.setattr(e.service, "now", lambda conn: now(conn) + timedelta(seconds=121))
        with pytest.raises(DomainError) as exc:
            e.service.approve(e.cookie, e.review["review_id"], request)
        assert exc.value.code == "SOURCE_REVIEW_PRESENTATION_EXPIRED"
        assert not e.remote.writes
        with engine.transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM app.outbox").fetchone()["n"] == 0


def test_workspace_or_remote_target_changes_after_reservation_block_physical_write(
    engine, tmp_path, monkeypatch
):
    with reviewed(engine, tmp_path) as e:
        approve(e)
        original = ReviewLedger.begin_step

        def reserve_then_change(self, *args):
            result = original(self, *args)
            if args[2] == 1:  # branch was accepted, commit has not been sent
                base = git(e.remote.root, "rev-parse", "refs/heads/main")
                tree = git(e.remote.root, "show", "-s", "--format=%T", base)
                other = git(e.remote.root, "commit-tree", tree, "-p", base, "-m", "unapproved head")
                git(
                    e.remote.root, "update-ref", "refs/heads/" + e.review["target"]["branch"], other
                )
            return result

        monkeypatch.setattr(ReviewLedger, "begin_step", reserve_then_change)
        e.service.tick(e.cookie)
        assert e.remote.writes == ["branch"]
        assert e.service.get(e.cookie, e.review["review_id"])["status"] == "failed"


def test_read_only_unknown_lookup_checks_full_tree_parent_and_complete_pr(engine, tmp_path):
    with reviewed(engine, tmp_path, lost="pull_request") as e:
        approve(e)
        e.service.tick(e.cookie)
        original = json.loads(e.remote.pull.read_text())
        before = list(e.remote.writes)
        for key, value in (
            ("draft", False),
            ("title", "different"),
            ("body", "different"),
            ("base_sha", "0" * 40),
            ("head_sha", "0" * 40),
        ):
            e.remote.pull.write_text(json.dumps({**original, key: value}))
            assert not e.service.reconcile(e.cookie, e.review["review_id"])["reconciled"]
            assert e.remote.writes == before
        e.remote.pull.write_text(json.dumps(original))
        reader = e.profile.reader
        original_identity, original_blobs = reader.commit_identity, reader.commit_blobs
        try:
            reader.commit_identity = lambda repo, sha: (
                original_identity(repo, sha)[0],
                ("0" * 40,),
            )
            assert not e.service.reconcile(e.cookie, e.review["review_id"])["reconciled"]
            reader.commit_identity = original_identity
            reader.commit_blobs = lambda repo, sha: {
                **original_blobs(repo, sha),
                "unexpected.py": "0" * 40,
            }
            assert not e.service.reconcile(e.cookie, e.review["review_id"])["reconciled"]
        finally:
            reader.commit_identity, reader.commit_blobs = original_identity, original_blobs
        assert e.service.reconcile(e.cookie, e.review["review_id"])["reconciled"]
        assert e.remote.writes == before


def test_original_normalized_pr_and_ci_head_attempt_link_to_exact_ticket(
    engine, tmp_path, monkeypatch
):
    import sa13_runtime_support

    repository = sa13_runtime_support.CodingRepository

    def controlled_repository(**kw):
        kw["repository_id"] = 900  # same explicit synthetic GitHub repository
        return repository(**kw)

    monkeypatch.setattr(sa13_runtime_support, "CodingRepository", controlled_repository)
    with reviewed(engine, tmp_path) as e:
        approve(e)
        e.service.tick(e.cookie)
        row = e.service.get(e.cookie, e.review["review_id"])["links"][0]["facts"]
        with engine.transaction() as conn:
            owner = e.service._console(conn, e.cookie)
        cipher = engine.source_ingress.cipher
        # This only reuses the already existing singleton Owner for fixture
        # setup. Verification, ingress, roles, policies and normalization run.
        with monkeypatch.context() as patch:
            patch.setattr(WebAccounts, "create_owner", lambda self, request: owner)
            gh = github_setup(engine)
        engine.source_ingress.cipher = cipher

        def ingest(family, *, head=None, attempt=2):
            raw = payload(family)
            if family == "pull_request":
                raw["pull_request"]["number"] = 1
                raw["pull_request"]["head"]["sha"] = head or row["head_sha"]
                raw["pull_request"]["base"]["sha"] = row["base_sha"]
            else:
                raw[family]["head_sha"] = head or row["head_sha"]
                raw[family]["run_attempt"] = attempt
                raw[family]["pull_requests"] = [{"number": 1}]
            receipt = gh.runtime.receive(
                signed(raw, family=family, delivery="sa18-" + uuid4().hex), 1
            )
            assert receipt.committed and len(receipt.input_ids) == 1
            with engine.transaction() as conn:
                return engine.source_correlations._input(conn, receipt.input_ids[0]).ref

        def request(ref):
            return ReviewNativeLink(
                bundle_sha256=e.review["bundle_sha256"],
                action_sha256=e.review["action_sha256"],
                source_input=ref,
            )

        ci = ingest("workflow_run")
        with pytest.raises(DomainError) as exc:
            e.service.link_native(e.cookie, e.review["review_id"], request(ci))
        assert exc.value.code == "SOURCE_REVIEW_CURRENT_NATIVE_PR_ANCHOR_REQUIRED"
        pr = ingest("pull_request")
        e.service.link_native(e.cookie, e.review["review_id"], request(pr))
        result = e.service.link_native(e.cookie, e.review["review_id"], request(ci))
        assert result["ticket_id"] == e.review["ticket_id"] and result["kind"] == "workflow_run"
        again = e.service.link_native(e.cookie, e.review["review_id"], request(ci))
        assert again["receipt_sha256"] == result["receipt_sha256"]
        newer = ingest("workflow_run", attempt=3)
        assert (
            e.service.link_native(e.cookie, e.review["review_id"], request(newer))["receipt_sha256"]
            != result["receipt_sha256"]
        )
        wrong = ingest("workflow_run", head="0" * 40, attempt=4)
        with pytest.raises(DomainError):
            e.service.link_native(e.cookie, e.review["review_id"], request(wrong))
