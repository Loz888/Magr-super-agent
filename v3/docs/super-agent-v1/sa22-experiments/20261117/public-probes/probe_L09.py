"""Public linear-ranker guard, used as the process-recovery task's objective."""

import math
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path.cwd() / "src"))

from mgar_v3.domain import DomainError  # noqa: E402
from mgar_v3.rag.rank import PairwiseRanker  # noqa: E402


class PublicRankerInputs(unittest.TestCase):
    def test_finite_score_retained(self):
        self.assertEqual(PairwiseRanker([1, 2], "public-probe", "fixture").score([3, 4]), 11)

    def test_dimension_guard_retained(self):
        with self.assertRaises(DomainError) as error:
            PairwiseRanker([1, 2], "public-probe", "fixture").score([3])
        self.assertEqual(error.exception.code, "RERANKER_DIMENSION")

    def test_nan_input_rejected(self):
        with self.assertRaises(DomainError) as error:
            PairwiseRanker([1], "public-probe", "fixture").score([math.nan])
        self.assertEqual(error.exception.code, "RERANKER_INPUT_INVALID")

    def test_infinite_input_rejected(self):
        with self.assertRaises(DomainError) as error:
            PairwiseRanker([1], "public-probe", "fixture").score([-math.inf])
        self.assertEqual(error.exception.code, "RERANKER_INPUT_INVALID")

    def test_overflow_result_rejected(self):
        with self.assertRaises(DomainError) as error:
            PairwiseRanker([2], "public-probe", "fixture").score([1e308])
        self.assertEqual(error.exception.code, "RERANKER_RESULT_INVALID")


if __name__ == "__main__":
    unittest.main(verbosity=2)
