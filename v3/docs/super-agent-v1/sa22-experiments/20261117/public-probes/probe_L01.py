"""Public specification: reject nonfinite cosine inputs, retain ordinary behaviour.

Run only inside the pinned, isolated candidate workspace; cwd is v3/.
This is a model-visible public check, not an independent private judge.
"""

import math
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path.cwd() / "src"))

from mgar_v3.domain import DomainError  # noqa: E402
from mgar_v3.memory.retrieval import _cosine  # noqa: E402


class PublicCosineContract(unittest.TestCase):
    def test_collinear_finite(self):
        self.assertAlmostEqual(_cosine([1, 2], [2, 4]), 1.0)

    def test_orthogonal_finite(self):
        self.assertAlmostEqual(_cosine([1, 0], [0, 1]), 0.0)

    def test_zero_vector(self):
        self.assertEqual(_cosine([0, 0], [3, 4]), 0.0)

    def test_empty_vector(self):
        self.assertEqual(_cosine([], []), 0.0)

    def test_dimension_guard_retained(self):
        with self.assertRaises(DomainError) as error:
            _cosine([1], [1, 2])
        self.assertEqual(error.exception.code, "MEMORY_EMBEDDING_INVALID")

    def test_nan_left_rejected(self):
        with self.assertRaises(DomainError) as error:
            _cosine([math.nan, 1], [1, 1])
        self.assertEqual(error.exception.code, "MEMORY_EMBEDDING_INVALID")

    def test_infinity_left_rejected(self):
        with self.assertRaises(DomainError) as error:
            _cosine([math.inf], [1])
        self.assertEqual(error.exception.code, "MEMORY_EMBEDDING_INVALID")

    def test_negative_infinity_right_rejected(self):
        with self.assertRaises(DomainError) as error:
            _cosine([1], [-math.inf])
        self.assertEqual(error.exception.code, "MEMORY_EMBEDDING_INVALID")


if __name__ == "__main__":
    unittest.main(verbosity=2)
