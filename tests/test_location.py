"""Déduction province/pays depuis la ville (villes ambiguës). Lancer : python -m unittest discover -s tests"""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import score_prospects as sp  # noqa: E402


def rec(**kw):
    base = {k: "" for k in sp.COLUMNS}
    base.update(company="Acme", website="acme.com", domain="acme.com")
    base.update(kw)
    return base


def located(**kw):
    r = rec(**kw)
    sp.fill_location(r)
    return r["state"], r["country"]


class TestFillLocation(unittest.TestCase):
    def test_unambiguous_city_without_country(self):
        self.assertEqual(located(city="Toronto"), ("Ontario", "Canada"))

    def test_ambiguous_city_without_evidence(self):
        self.assertEqual(located(city="London"), ("", ""))

    def test_ambiguous_city_with_ca_domain(self):
        self.assertEqual(located(city="London", website="acme.ca", domain="acme.ca"), ("Ontario", "Canada"))

    def test_ambiguous_city_with_province_suffix(self):
        self.assertEqual(located(city="London, ON"), ("Ontario", "Canada"))

    def test_ambiguous_city_with_foreign_suffix(self):
        self.assertEqual(located(city="Cambridge, MA"), ("", ""))

    def test_foreign_state_does_not_force_canada(self):
        self.assertEqual(located(city="London", state="England"), ("England", ""))
        self.assertEqual(located(city="Toronto", state="CA"), ("CA", ""))

    def test_ambiguous_city_with_province_state(self):
        self.assertEqual(located(city="Kingston", state="ON"), ("ON", "Canada"))

    def test_foreign_country_is_kept(self):
        self.assertEqual(located(city="London", country="United Kingdom"), ("", "United Kingdom"))

    def test_canadian_source_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "apollo.csv"
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["Company", "Location (Canada)", "Domain"])
                writer.writeheader()
                writer.writerow({"Company": "A", "Location (Canada)": "London", "Domain": "a.com"})
            records, _, _ = sp.read_export(path)
        self.assertEqual((records[0]["state"], records[0]["country"]), ("Ontario", "Canada"))


if __name__ == "__main__":
    unittest.main()
