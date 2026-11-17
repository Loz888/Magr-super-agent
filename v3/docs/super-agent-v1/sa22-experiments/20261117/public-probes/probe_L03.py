"""Public bugfix contract: duplicate rank entries must not add extra votes."""

import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path.cwd() / "src"))

from mgar_v3.rag.rank import rrf  # noqa: E402


class PublicRRFDeduplication(unittest.TestCase):
    def test_one_list_does_not_vote_twice(self):
        expected = rrf([("a", 1), ("b", 0.5)])
        self.assertEqual(rrf([("a", 1), ("a", 0.9), ("b", 0.5)]), expected)

    def test_unique_positions_are_renumbered(self):
        self.assertEqual(rrf([("a", 1), ("a", 0.9), ("b", 0.5)], k=0), [("a", 1.0), ("b", 0.5)])

    def test_independent_lists_still_contribute(self):
        score = dict(rrf([("a", 1), ("a", 0.9)], [("a", 0.8)]))["a"]
        self.assertAlmostEqual(score, 2 / 61)

    def test_distinct_existing_rankings_retained(self):
        result = dict(rrf([("a", 2), ("b", 1)], [("b", 2), ("c", 1)]))
        self.assertAlmostEqual(result["a"], 1 / 61)
        self.assertAlmostEqual(result["b"], 1 / 62 + 1 / 61)
        self.assertAlmostEqual(result["c"], 1 / 62)

    def test_empty_rankings(self):
        self.assertEqual(rrf([], []), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
