"""First SA18 check that incoming native follow-ups resolve through original SA10."""

from uuid import uuid4

from sa05_support import payload, signed
from sa05_support import setup as github_setup
from sa18_runtime_support import approve, reviewed

from mgar_v3.super_agent.source_correlation_domain import CorrelationResolveRequest
from mgar_v3.super_agent.source_review_domain import ReviewNativeLink
from mgar_v3.web_access import WebAccounts


def test_native_pr_and_ci_use_original_correlation_index_without_creating_new_tasks(
    engine, tmp_path, monkeypatch
):
    import sa13_runtime_support

    original = sa13_runtime_support.CodingRepository

    def repository(**values):
        values["repository_id"] = 900
        return original(**values)

    monkeypatch.setattr(sa13_runtime_support, "CodingRepository", repository)
    with reviewed(engine, tmp_path) as e:
        approve(e)
        e.service.tick(e.cookie)
        pull = e.service.get(e.cookie, e.review["review_id"])["links"][0]["facts"]
        with engine.transaction() as conn:
            owner = e.service._console(conn, e.cookie)
            before = {
                t: conn.execute("SELECT count(*) AS n FROM app." + t).fetchone()["n"]
                for t in ("threads", "tickets", "runs", "usage_calls")
            }
        cipher = engine.source_ingress.cipher
        with monkeypatch.context() as patch:
            patch.setattr(WebAccounts, "create_owner", lambda self, body: owner)
            gh = github_setup(engine)
        engine.source_ingress.cipher = cipher
        for family in ("pull_request", "workflow_run"):
            raw = payload(family)
            if family == "pull_request":
                raw[family]["number"] = 1
                raw[family]["head"]["sha"] = pull["head_sha"]
                raw[family]["base"]["sha"] = pull["base_sha"]
            else:
                raw[family]["head_sha"] = pull["head_sha"]
                raw[family]["pull_requests"] = [{"number": 1}]
            receipt = gh.runtime.receive(
                signed(raw, family=family, delivery="sa18-index-" + uuid4().hex), 1
            )
            assert receipt.committed and len(receipt.input_ids) == 1
            with engine.transaction() as conn:
                ref = engine.source_correlations._input(conn, receipt.input_ids[0]).ref
            linked = e.service.link_native(
                e.cookie,
                e.review["review_id"],
                ReviewNativeLink(
                    bundle_sha256=e.review["bundle_sha256"],
                    action_sha256=e.review["action_sha256"],
                    source_input=ref,
                ),
            )
            assert linked["correlation_id"]
            resolved = engine.source_correlations.resolve(
                e.cookie, CorrelationResolveRequest(input_ids=receipt.input_ids)
            )
            assert resolved["resolutions"]
            for resolution in resolved["resolutions"]:
                assert resolution["outcome"] == "matched"
                assert resolution["candidates"][0]["ticket_id"] == str(e.review["ticket_id"])
                assert resolution["candidates"][0]["run_id"] == str(e.review["run_id"])
                assert not resolution["eligible_for_execution"]
        with engine.transaction() as conn:
            after = {
                t: conn.execute("SELECT count(*) AS n FROM app." + t).fetchone()["n"]
                for t in before
            }
            assert after == before
