"""Public feature: configurable RRF k across the ranker and memory retrieval.

Controlled rank inputs isolate fusion behaviour; the independent judge must
also exercise unmodified corpus scoring and current permission-scoped storage.
"""

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path.cwd() / "src"))

import mgar_v3.memory.retrieval as memory  # noqa: E402
from mgar_v3.domain import DomainError  # noqa: E402
from mgar_v3.memory.model import MemoryRecord, SemanticDetail  # noqa: E402
from mgar_v3.rag.rank import rrf  # noqa: E402
from mgar_v3.settings import Settings  # noqa: E402


def retriever(**kwargs):
    now = datetime(2026, 11, 17, tzinfo=UTC)
    rows = [
        MemoryRecord(
            memory_id=UUID(int=i + 1),
            memory_kind="semantic",
            project_id="sa22-public",
            repository="example/mgar-test",
            commit=None,
            actor="owner",
            scope="repository",
            scope_key="example/mgar-test",
            subject_key="record-" + str(i),
            summary_hash="a" * 64,
            evidence_ids=[],
            artifact_hashes=[],
            confidence=0.5,
            verification_status="supported",
            detail=SemanticDetail(subject="alpha", predicate="is", object_value="record " + str(i)),
            created_at=now,
            updated_at=now,
        )
        for i in range(6)
    ]
    store = SimpleNamespace(recall=lambda **_: rows)
    settings = Settings(
        _env_file=None,
        database_url="postgresql://unused@127.0.0.1:1/unused",
        local_token="sa22-public-probe-not-a-secret",
        project_id="sa22-public",
        repository="example/mgar-test",
        local_actor="owner",
    )
    return memory.MemoryRetriever(
        settings,
        store=store,
        now=lambda: now,
        embedder=SimpleNamespace(model_id="controlled-no-network"),
        **kwargs,
    )


class PublicRRFFeature(unittest.TestCase):
    def test_default_and_explicit_sixty_compatible(self):
        a, b = retriever(), retriever(rrf_k=60)
        self.assertIsInstance(a, memory.MemoryRetriever)
        self.assertIsInstance(b, memory.MemoryRetriever)
        self.assertEqual(rrf([("a", 2), ("b", 1)]), rrf([("a", 2), ("b", 1)], k=60))

    def test_zero_and_integer_parameter_supported(self):
        retriever(rrf_k=0)
        retriever(rrf_k=7)
        self.assertEqual(rrf([("a", 1), ("b", 0.5)], k=0), [("a", 1.0), ("b", 0.5)])

    def test_negative_parameter_rejected_by_both_entry_points(self):
        for operation in (lambda: retriever(rrf_k=-1), lambda: rrf([("a", 1)], k=-1)):
            with self.assertRaises(DomainError) as error:
                operation()
            self.assertEqual(error.exception.code, "RRF_K_INVALID")

    def test_boolean_and_noninteger_parameter_rejected(self):
        for value in (True, 1.5, "7", None, 10001):
            for operation in (
                lambda value=value: retriever(rrf_k=value),
                lambda value=value: rrf([], k=value),
            ):
                with self.assertRaises(DomainError) as error:
                    operation()
                self.assertEqual(error.exception.code, "RRF_K_INVALID")

    def test_actual_memory_order_changes_with_k(self):
        ids = [str(UUID(int=i + 1)) for i in range(6)]
        lexical = [(key, float(6 - i)) for i, key in enumerate(ids)]
        dense = [(key, float(5 - i)) for i, key in enumerate(ids[1:])]
        with (
            patch.object(memory, "_lexical", return_value=lexical),
            patch.object(memory, "_dense", return_value=dense),
        ):
            small = retriever(rrf_k=1).search_memory("alpha", top_k=6)
            large = retriever(rrf_k=60).search_memory("alpha", top_k=6)
        left = {str(hit.memory_id): hit.score for hit in small.hits}
        right = {str(hit.memory_id): hit.score for hit in large.hits}
        self.assertGreater(left[ids[0]], left[ids[5]])
        self.assertLess(right[ids[0]], right[ids[5]])
        self.assertEqual(small.retrieval_mode, "hybrid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
