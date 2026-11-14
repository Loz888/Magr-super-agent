"""First checks for actual atomic edits under newly committed SA14 constraints."""

import pytest
from sa13_runtime_support import BAD, WRONG
from sa14_support import AsyncRepair, actual, restrict, rows, start

from mgar_v3.domain import DomainError


def test_committed_mid_batch_restriction_preserves_all_actual_postimages_and_receipt(
    engine, tmp_path, monkeypatch
):
    model = AsyncRepair(wrong_first=True)
    model.repair.two_files = True
    with actual(engine, tmp_path, monkeypatch, model=model) as env:
        boundaries = []
        receipt = []

        def restrict_after_first_replace(operation_id, path):
            assert engine.connection.info.transaction_status.name == "IDLE"
            boundaries.append((operation_id, path))
            if len(boundaries) == 1:
                _, received = restrict(env, paths=("src/calc.py", "src/other.py"))
                receipt.append(received)
                assert received["state"] == "received"

        env.service.runtime.write_boundary = restrict_after_first_replace
        start(env)
        assert env.async_runtime.join(env.mandate.coding_id, 60)
        assert len(boundaries) == 2 and len({item[0] for item in boundaries}) == 1
        assert {item[1] for item in boundaries} == {"src/calc.py", "src/other.py"}
        assert (env.raw.root / "src/calc.py").read_text() == WRONG
        assert (env.raw.root / "src/other.py").read_text() == "value = 1\n"
        assert (env.source_root / "src/calc.py").read_text() == BAD
        writes = [op for op in rows(env, "sa_coding_operations") if op["capability"] == "write"]
        assert len(writes) == 1 and writes[0]["state"] == "known"
        assert writes[0]["operation_id"] == boundaries[0][0]
        assert all(call["status"] == "known" for call in rows(env, "usage_calls"))
        tree = env.raw.tree_hash()
        cp = env.async_service.safe_point(
            env.worker["bearer"], env.run_request, tree=tree, stage="before_tool"
        )
        assert cp.restriction.deny_write_paths == ("src/calc.py", "src/other.py")
        with pytest.raises(DomainError) as denied:
            with env.async_service.operation_guard(
                env.worker["bearer"],
                env.run_request,
                tree=tree,
                capability="write",
                paths=("src/calc.py",),
            ):
                pytest.fail("Newly denied write reached its physical boundary")
        assert denied.value.code == "SOURCE_ASYNC_PATH_DENIED"
        control = next(
            r for r in rows(env, "sa_async_inputs") if r["input_id"] == receipt[0]["input_id"]
        )
        assert control["state"] == "applied"
        assert len(rows(env, "runs")) == 1 and rows(env, "runs")[0]["status"] != "succeeded"
        assert rows(env, "actions") == rows(env, "outbox") == []
