"""Lancer : python -m unittest tests.test_csv_safe -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import csv_safe  # noqa: E402


class CsvSafeTest(unittest.TestCase):
    def clean(self, value):
        return next(csv_safe([{"c": value}]))["c"]

    def test_formulas_neutralised(self):
        for v in ('=HYPERLINK("http://x","clic")', "+cmd|' /C calc'!A0", "-2+3", "@SUM(A1)", "\t=1"):
            self.assertEqual(self.clean(v), "'" + v)

    def test_plain_values_untouched(self):
        for v in ("Acme Inc", "-3", "+5 %".replace(" ", ""), "-1,5", "", 12, None, "acme.com"):
            self.assertEqual(self.clean(v), v)

    def test_idempotent(self):
        once = self.clean("=1+1")
        self.assertEqual(self.clean(once), once)


if __name__ == "__main__":
    unittest.main()
