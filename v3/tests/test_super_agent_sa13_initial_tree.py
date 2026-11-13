"""First actual-PG check for materialized baseline drift before Coding starts."""

import pytest
from sa13_runtime_support import BAD, GOOD, facts, run, runtime_env

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import WorkspaceEdit, sha256_text


def test_valid_h03_owner_edit_cannot_replace_the_reviewed_initial_coding_tree(engine, tmp_path):
    with runtime_env(engine, tmp_path) as env:
        before = env.raw.snapshot()
        env.raw.apply(
            [
                WorkspaceEdit(path="src/calc.py", expected_sha256=sha256_text(BAD), content=GOOD),
            ],
            expected_tree=before.tree_hash,
        )
        assert env.raw.tree_hash() != env.mandate.repository.materialized_tree_sha256
        with pytest.raises(DomainError) as rejected:
            run(env)
        assert rejected.value.code == "SOURCE_CODING_INITIAL_TREE_MISMATCH"
        saved = facts(env)
        assert not env.model.requests and not saved["usage_calls"]
        assert not saved["sa_coding_verifications"]
        assert not any(row["kind"] in {"start", "job"} for row in saved["sa_scheduler_operations"])
