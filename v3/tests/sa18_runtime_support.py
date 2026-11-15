"""Actual Git/H03/H06/PG source result and explicitly controlled Draft remote."""

import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sa13_runtime_support import RepairModel, run, runtime_env

from mgar_v3.repair.pr_delivery import DraftPullRequest
from mgar_v3.super_agent.source_review_capture import H03ReviewCapture
from mgar_v3.super_agent.source_review_domain import ReviewApprove, ReviewPrepare
from mgar_v3.super_agent.source_review_ports import ReviewProfile


def git(root, *args, data=None, extra=None):
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=SA18 Fixture",
            "-c",
            "user.email=sa18@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        input=data,
        capture_output=True,
        check=True,
        timeout=10,
        env={**os.environ, **(extra or {})},
    )
    return result.stdout.decode().strip()


class GitRemote:
    """Local Git objects plus a persistent fixture PR, never a GitHub connection."""

    def __init__(self, root, *, engine=None, source=None, lost=None):
        self.root, self.engine, self.lost = Path(root), engine, lost
        self.reads = 0
        if source is not None:
            subprocess.run(
                ["git", "clone", "--quiet", "--bare", str(source), str(root)],
                check=True,
                capture_output=True,
                timeout=10,
            )
            git(root, "update-ref", "refs/heads/main", git(source, "rev-parse", "HEAD"))
        self.calls = self.root / "fixture-writes.json"
        self.pull = self.root / "fixture-pr.json"

    def idle(self):
        if self.engine:
            assert self.engine.connection.info.transaction_status.name == "IDLE"

    def record(self, kind):
        self.idle()
        values = json.loads(self.calls.read_text()) if self.calls.exists() else []
        values.append(kind)
        self.calls.write_text(json.dumps(values))

    @property
    def writes(self):
        return json.loads(self.calls.read_text()) if self.calls.exists() else []

    def accepted(self, kind):
        if self.lost == kind:
            self.lost = None
            raise OSError("Explicit fixture accepted write then lost response")


class Reader:
    def __init__(self, remote):
        self.remote = remote

    def branch_sha(self, repository, branch):
        self.remote.idle()
        self.remote.reads += 1
        try:
            return git(self.remote.root, "rev-parse", "--verify", "refs/heads/" + branch)
        except subprocess.CalledProcessError:
            return None

    def commit_identity(self, repository, sha):
        self.remote.idle()
        parts = git(self.remote.root, "show", "-s", "--format=%T %P", sha).split()
        return parts[0], tuple(parts[1:])

    def commit_blobs(self, repository, sha):
        self.remote.idle()
        result = {}
        for line in git(self.remote.root, "ls-tree", "-r", sha).splitlines():
            left, path = line.split("\t", 1)
            mode, kind, blob = left.split()
            if mode != "100644" or kind != "blob":
                return None
            result[path] = blob
        return result

    def find_draft(self, repository, head, base):
        self.remote.idle()
        if not self.remote.pull.exists():
            return None
        pull = DraftPullRequest.model_validate_json(self.remote.pull.read_text())
        return (
            pull
            if (pull.repository, pull.head_ref, pull.base_ref) == (repository, head, base)
            else None
        )


class Writer:
    scope = "write"

    def __init__(self, remote):
        self.remote = remote

    def create_branch(self, repository, branch, from_branch):
        self.remote.record("branch")
        git(self.remote.root, "update-ref", "refs/heads/" + branch, from_branch, "0" * 40)
        self.remote.accepted("branch")

    def push_files(self, repository, branch, files, message):
        self.remote.record("commit")
        parent = git(self.remote.root, "rev-parse", "refs/heads/" + branch)
        with tempfile.TemporaryDirectory() as temp:
            env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
            git(self.remote.root, "read-tree", parent, extra=env)
            for path, content in files.items():
                blob = git(self.remote.root, "hash-object", "-w", "--stdin", data=content.encode())
                git(
                    self.remote.root,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "100644",
                    blob,
                    path,
                    extra=env,
                )
            tree = git(self.remote.root, "write-tree", extra=env)
        commit = git(self.remote.root, "commit-tree", tree, "-p", parent, "-m", message)
        git(self.remote.root, "update-ref", "refs/heads/" + branch, commit, parent)
        self.remote.accepted("commit")

    def create_draft_pull_request(self, repository, base, head, title, body):
        self.remote.record("pull_request")
        assert not self.remote.pull.exists(), "a second create is a duplicate remote write"
        p = DraftPullRequest(
            number=1,
            draft=True,
            title=title,
            body=body,
            repository=repository,
            head_ref=head,
            head_sha=git(self.remote.root, "rev-parse", "refs/heads/" + head),
            base_ref=base,
            base_sha=git(self.remote.root, "rev-parse", "refs/heads/" + base),
        )
        self.remote.pull.write_text(p.model_dump_json())
        self.remote.accepted("pull_request")


@contextmanager
def reviewed(engine, tmp_path, *, write=True, lost=None, prepare=True):
    with runtime_env(engine, tmp_path, model=RepairModel(wrong_first=False)) as worker:
        outcome = run(worker)
        assert outcome["verification"] == "verified"
        service = engine.source_reviews
        remote = GitRemote(
            tmp_path / "controlled.git", source=worker.source_root, engine=engine, lost=lost
        )
        with engine.transaction() as conn:
            v = conn.execute(
                "SELECT verification_id FROM app.sa_coding_verifications WHERE coding_id=%s "
                "ORDER BY created_at DESC LIMIT 1",
                (worker.mandate.coding_id,),
            ).fetchone()
            now = service.now(conn)
        profile = ReviewProfile(
            "explicit-fixture-SA18",
            worker.mandate.worker_profile_sha256,
            worker.mandate.repository.repository,
            worker.mandate.repository.repository_id,
            "main",
            engine.settings.local_actor,
            now + timedelta(hours=1),
            H03ReviewCapture(engine, worker.runtime_profile.driver),
            Reader(remote),
            Writer(remote),
            write_authorized=write,
        )
        service.register_profile(profile)
        request = ReviewPrepare(
            request_id=uuid4(),
            coding_id=worker.mandate.coding_id,
            verification_id=v["verification_id"],
            expected_epoch=worker.run_request.epoch,
            expected_input_revision=1,
            profile_sha256=profile.sha256,
        )
        env = SimpleNamespace(
            worker=worker,
            service=service,
            profile=profile,
            remote=remote,
            cookie=worker.cookie,
            request=request,
            engine=engine,
        )
        env.review = service.prepare(env.cookie, request) if prepare else None
        yield env


def approve(env):
    view = env.service.inspect(env.cookie, env.review["review_id"])
    request = ReviewApprove.model_validate_json(
        json.dumps(
            {
                "presentation_id": view["presentation"]["presentation_id"],
                "bundle_sha256": view["review"]["bundle_sha256"],
                "action_sha256": view["review"]["action_sha256"],
            }
        )
    )
    return env.service.approve(env.cookie, env.review["review_id"], request)
