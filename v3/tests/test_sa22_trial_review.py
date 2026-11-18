"""The opted-in trial view serves only current, exact artifacts to a signed-in Owner."""

import hashlib
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sa22_coding_support import coding_environment

from mgar_v3.super_agent.source_trial_review import TrialReview, register_trial_review
from mgar_v3.web_access import SESSION_COOKIE


def test_review_opens_exact_trial_artifact_and_fences_expired_source(engine, tmp_path):
    env = coding_environment(engine, tmp_path)
    try:
        env.experiment(env.input_id)
        with engine.transaction() as conn:
            binding = conn.execute("SELECT run_id FROM app.sa_coding_bindings").fetchone()
            run = binding["run_id"]
            verification = conn.execute(
                "SELECT v.result,v.tree_sha256,v.tests_run,v.tests_failed "
                "FROM app.sa_coding_verifications v JOIN app.sa_coding_bindings b USING(coding_id) "
                "WHERE b.run_id=%s ORDER BY v.observed_at DESC LIMIT 1", (run,),
            ).fetchone()
        directory = env.experiment.artifacts / str(run)
        diff = (directory / "patch.diff").read_bytes()
        assert diff
        metadata = json.dumps({
            "run_id": str(run), "final_tree_sha256": verification["tree_sha256"],
            "diff_sha256": hashlib.sha256(diff).hexdigest(), "formal_verifier": "incomplete",
            "original_git_publication": False, "public_verification": verification,
        }).encode()
        (directory / "verification.json").write_bytes(metadata)
        artifact = TrialReview.load(directory, hashlib.sha256(metadata).hexdigest())
        app = FastAPI()
        register_trial_review(app, engine, artifact)
        path = f"/source-trial-review/{run}"
        with TestClient(app) as browser:
            assert browser.get(path).status_code == 401
            browser.cookies.set(SESSION_COOKIE, env.source.cookie)
            assert browser.get(path, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 401
            response = browser.get(path)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert response.content.endswith(diff)
            with engine.transaction() as conn:
                receipt = conn.execute(
                    "SELECT safe_payload FROM app.run_events "
                    "WHERE run_id=%s AND kind='source_trial_review_viewed'", (run,),
                ).fetchone()
                assert receipt["safe_payload"]["diff_sha256"] == artifact.diff_sha256
                conn.execute(
                    "UPDATE app.source_ingress_bodies SET expires_at=clock_timestamp() "
                    "WHERE input_id=%s", (env.input_id,),
                )
            response = browser.get(path)
            assert response.status_code in (403, 404) and diff not in response.content
            with engine.transaction() as conn:
                assert conn.execute("SELECT count(*) AS n FROM app.actions").fetchone()["n"] == 0
                n = conn.execute(
                    "SELECT count(*) AS n FROM app.run_events "
                    "WHERE kind='source_trial_review_viewed'"
                ).fetchone()["n"]
                assert n == 1
    finally:
        env.close()
