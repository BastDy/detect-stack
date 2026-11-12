"""Grille de score réglable via config.json. Lancer : python -m unittest discover -s tests"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import score_prospects as sp  # noqa: E402

TODAY = date(2026, 11, 11)


def rec(**kw):
    base = {k: "" for k in sp.COLUMNS}
    base.update(company="Acme", website="acme.com", domain="acme.com")
    base.update(kw)
    return base


def grid_from(config):
    """Grille obtenue avec ce contenu de config.json."""
    with patch.object(sp, "load_config", return_value=config):
        return sp.scoring_config()


FULL = (
    rec(industry="Computer Software", employees="80", state="Ontario", last_raised="2026-06-01"),
    {"hubspot": "Oui", "autres_outils": "Pipedrive, Calendly"},
    {"titles": ["Revenue Operations Manager", "Account Executive", "SDR", "Growth Marketer"]},
)


class TestScoringConfig(unittest.TestCase):
    def test_defaults_match_current_behaviour(self):
        grid = grid_from({})
        self.assertEqual(grid, sp.DEFAULT_SCORING)
        out = sp.score(*FULL, TODAY, grid)
        self.assertEqual((out["score"], out["tier"]), (22, "A"))
        self.assertIn("20-200 salariés +2", out["detail_score"])
        self.assertIn("Levée < 12 mois +3", out["detail_score"])
        self.assertIn("3+ offres sales/marketing +2", out["detail_score"])

    def test_partial_override_keeps_other_defaults(self):
        grid = grid_from({"scoring": {"points": {"funding": 4}}})
        self.assertEqual(grid["points"]["funding"], 4)
        self.assertEqual(grid["points"]["revops_job"], 5)
        self.assertEqual(grid["tiers"], sp.DEFAULT_SCORING["tiers"])
        self.assertEqual(sp.DEFAULT_SCORING["points"]["funding"], 3)  # défauts non modifiés

    def test_weight_and_threshold_change_tier(self):
        # Ontario 2 + RevOps 5 + HubSpot 2 = 9 -> B par défaut
        r, det, jobs = rec(state="ON"), {"hubspot": "Oui"}, {"titles": ["Sales Operations Analyst"]}
        self.assertEqual(sp.score(r, det, jobs, TODAY)["tier"], "B")
        # RevOps à 3 -> 7, sous le seuil B
        lighter = grid_from({"scoring": {"points": {"revops_job": 3}}})
        self.assertEqual(sp.score(r, det, jobs, TODAY, lighter)["tier"], "C")
        # Seuil A abaissé à 9 -> A
        easier = grid_from({"scoring": {"tiers": {"A": 9}}})
        self.assertEqual(sp.score(r, det, jobs, TODAY, easier)["tier"], "A")

    def test_labels_follow_config(self):
        grid = grid_from({"scoring": {"size": {"min": 50, "max": 500},
                                      "signals": {"funding_days": 180, "sales_jobs_min": 2}}})
        r = rec(employees="300", last_raised="2026-08-01")
        out = sp.score(r, {}, {"titles": ["Account Executive", "SDR"]}, TODAY, grid)
        self.assertIn("50-500 salariés +2", out["detail_score"])
        self.assertIn("Levée < 180 jours +3", out["detail_score"])
        self.assertIn("2+ offres sales/marketing +2", out["detail_score"])
        # 30 salariés : hors de la nouvelle fourchette
        self.assertNotIn("salariés", sp.score(rec(employees="30"), {}, {}, TODAY, grid)["detail_score"])

    def test_unknown_key_is_rejected(self):
        for config, typo in (({"scoring": {"points": {"fundng": 4}}}, "scoring.points.fundng"),
                             ({"scoring": {"tier": {"A": 10}}}, "tier")):
            with self.assertRaises(ValueError) as ctx:
                grid_from(config)
            self.assertIn(typo, str(ctx.exception))
            self.assertIn("inconnue", str(ctx.exception))

    def test_non_numeric_value_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            grid_from({"scoring": {"tiers": {"A": "12"}}})
        self.assertIn("doit être un nombre", str(ctx.exception))

    def test_exclusions_use_configured_min_size(self):
        records = [rec(company="Tiny", domain="tiny.io", employees="4"),
                   rec(company="Small", domain="small.io", employees="15"),
                   rec(company="Mid", domain="mid.io", employees="60")]
        _, excluded = sp.exclusions(records, set())
        self.assertEqual(set(excluded), {"tiny.io"})
        grid = grid_from({"scoring": {"size": {"exclude_below": 20}}})
        candidates, excluded = sp.exclusions(records, set(), grid)
        self.assertEqual(set(excluded), {"tiny.io", "small.io"})
        self.assertEqual(excluded["small.io"], "Exclu : moins de 20 salariés")
        self.assertEqual([r["domain"] for r in candidates], ["mid.io"])


if __name__ == "__main__":
    unittest.main()
