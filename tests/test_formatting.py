"""Grade weight display (shared by the brief, receipts, and syllabus import)."""

from __future__ import annotations

import unittest

from bot.formatting import pct


class PctTestCase(unittest.TestCase):
    def test_divided_weights_are_readable(self) -> None:
        """5% across 11 check-ins rendered as 0.454545% in a real brief."""
        self.assertEqual(pct(5 / 11), "0.45")
        self.assertEqual(pct(9 / 9), "1")

    def test_trailing_zeros_are_dropped(self) -> None:
        self.assertEqual(pct(17.0), "17")
        self.assertEqual(pct(100), "100")

    def test_genuine_decimals_survive(self) -> None:
        self.assertEqual(pct(12.5), "12.5")
        self.assertEqual(pct(7.25), "7.25")

    def test_zero_and_none(self) -> None:
        self.assertEqual(pct(0), "0")
        self.assertIsNone(pct(None))


if __name__ == "__main__":
    unittest.main()
