"""Public bounded context-pressure arguments and existing threshold behaviour."""

import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path.cwd() / "src"))

from mgar_v3.context.policy import ContextPolicy  # noqa: E402

from mgar_v3.domain import DomainError  # noqa: E402


class PublicContextPressure(unittest.TestCase):
    def test_existing_thresholds(self):
        policy = ContextPolicy()
        self.assertEqual(policy.pressure(64, 100), ("normal", 100))
        self.assertEqual(policy.pressure(65, 100), ("prepare", 50))
        self.assertEqual(policy.pressure(85, 100), ("forced", 50))

    def test_zero_observed_is_valid(self):
        self.assertEqual(ContextPolicy().pressure(0, 100), ("normal", 100))

    def test_negative_observed_rejected(self):
        with self.assertRaises(DomainError) as error:
            ContextPolicy().pressure(-1, 100)
        self.assertEqual(error.exception.code, "CONTEXT_BOUND")

    def test_nonpositive_limit_rejected(self):
        for limit in (0, -1):
            with self.assertRaises(DomainError) as error:
                ContextPolicy().pressure(0, limit)
            self.assertEqual(error.exception.code, "CONTEXT_BOUND")

    def test_noninteger_arguments_rejected(self):
        for observed, limit in ((True, 100), (1.5, 100), (0, True), (0, 10.5)):
            with self.assertRaises(DomainError) as error:
                ContextPolicy().pressure(observed, limit)
            self.assertEqual(error.exception.code, "CONTEXT_BOUND")


if __name__ == "__main__":
    unittest.main(verbosity=2)
