"""Tests de la partie Timing du score (timing.py + demi-vie dans score_prospects). Base en mémoire, pas de réseau.

Lancer : python -m unittest tests.test_timing -v
"""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import score_prospects as sp  # noqa: E402
import timing  # noqa: E402

TODAY = date(2026, 11, 13)
GRID = sp.DEFAULT_SCORING


def ago(n):
    return (TODAY - timedelta(days=n)).isoformat()


def row(domain="acme.com", name="Acme", profil=9, timing_pts=0, angle=""):
    total = profil + timing_pts
    return {"domaine": domain, "entreprise": name, "score": total, "score_profil": profil, "score_timing": timing_pts,
            "tier": sp.tier_of(total, timing_pts, GRID), "detail_score": "SaaS/tech +3", "angle_suggere": angle}


class TestDecay(unittest.TestCase):
    def test_half_life(self):
        self.assertEqual(sp.decayed(3, 0, 180), 3)
        self.assertEqual(sp.decayed(4, 180, 180), 2)    # une demi-vie : moitié
        self.assertEqual(sp.decayed(4, 360, 180), 1)    # deux demi-vies : quart
        self.assertEqual(sp.decayed(3, 2000, 180), 0)   # trop vieux : disparaît
        self.assertEqual(sp.decayed(3, -5, 180), 3)     # date future (annonce datée de demain) : pas de bonus

    def test_recent_funding_beats_old_funding(self):
        rec = {k: "" for k in sp.COLUMNS} | {"company": "Acme", "domain": "acme.com"}
        fresh = sp.score(rec | {"last_raised": ago(14)}, {}, {}, TODAY)
        old = sp.score(rec | {"last_raised": ago(330)}, {}, {}, TODAY)
        self.assertEqual((fresh["score_timing"], old["score_timing"]), (3, 1))
        self.assertIn("Levée il y a 14 j +3", fresh["detail_score"])


class TestTierRule(unittest.TestCase):
    def test_a_needs_timing(self):
        self.assertEqual(sp.tier_of(12, 0, GRID), "B")   # bon profil, rien ne bouge : pas prioritaire
        self.assertEqual(sp.tier_of(12, 1, GRID), "A")
        self.assertEqual(sp.tier_of(8, 0, GRID), "B")
        self.assertEqual(sp.tier_of(7, 5, GRID), "C")

    def test_profile_only_company_capped_at_b(self):
        rec = {k: "" for k in sp.COLUMNS} | {"company": "Acme", "domain": "acme.com", "industry": "Software",
                                              "employees": "50", "state": "Ontario"}
        out = sp.score(rec, {"hubspot": "Oui", "autres_outils": "Pipedrive"}, {}, TODAY)
        self.assertEqual((out["score"], out["score_profil"], out["score_timing"], out["tier"]), (12, 12, 0, "B"))

    def test_config_can_disable_rule_and_tune_decay(self):
        with patch.object(sp, "load_config", return_value={"scoring": {"tiers": {"A_timing_min": 0},
                                                                       "decay": {"funding": 30}}}):
            grid = sp.scoring_config()
        self.assertEqual(sp.tier_of(12, 0, grid), "A")
        self.assertEqual(grid["decay"]["funding"], 30)
        with patch.object(sp, "load_config", return_value={"scoring": {"decay": {"fundng": 30}}}):
            with self.assertRaises(ValueError):
                sp.scoring_config()


class TestEvents(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.run = db.start_run(self.conn, {"test": True})

    def tearDown(self):
        self.conn.close()

    def event(self, day, kind, detail, domain="acme.com", ref=""):
        db._event(self.conn, day, domain, kind, detail, self.run, ref=ref or f"{kind}:{detail}:{day}")
        self.conn.commit()

    def kinds(self, domain="acme.com"):
        return {s["type"]: s["points"] for s in timing.event_signals(self.conn, domain, TODAY, GRID)}

    def test_unused_events_now_count(self):
        self.event(ago(3), "leadership_hire", "VP Sales")
        self.event(ago(10), "posting_stale", "RevOps Manager")
        self.event(ago(0), "tool_added", "HubSpot")
        self.assertEqual(self.kinds(), {"leadership_hire": 3, "posting_stale": 2, "tool_added": 3})

    def test_decay_and_window(self):
        self.event(ago(60), "leadership_hire", "VP Sales")     # une demi-vie : 3 -> 2 (1,5 arrondi)
        self.event(ago(200), "posting_build", "AE")            # hors fenêtre de 180 jours
        self.event(ago(80), "hiring_spike", "3 → 7")           # demi-vie 30 j : 3 -> 0,3 -> 0, disparaît
        self.assertEqual(self.kinds(), {"leadership_hire": 2})

    def test_one_signal_per_type_the_strongest(self):
        self.event(ago(100), "leadership_hire", "CRO")
        self.event(ago(5), "leadership_hire", "VP Marketing")
        signals = timing.event_signals(self.conn, "acme.com", TODAY, GRID)
        self.assertEqual(len(signals), 1)
        self.assertIn("VP Marketing", signals[0]["label"])

    def test_non_crm_tool_and_funding_ignored(self):
        self.event(ago(1), "tool_added", "Calendly")
        self.event(ago(1), "funding", "Seed")        # déjà comptée par score() avec sa propre demi-vie
        self.event(ago(1), "posting_opened", "AE")   # l'offre ouverte compte via l'état, pas l'événement
        self.assertEqual(self.kinds(), {})

    def test_future_events_ignored(self):
        self.event((TODAY + timedelta(days=2)).isoformat(), "leadership_hire", "CRO")
        self.assertEqual(self.kinds(), {})

    def test_apply_timing_promotes_and_resorts(self):
        rows = [row("quiet.com", "Quiet", profil=12, angle="Levée récente : structurer"),
                row("acme.com", "Acme", profil=9, angle="Ancien angle"),
                {"domaine": "gone.com", "entreprise": "Gone", "tier": "Exclu : candidature CDI", "score": ""}]
        self.event(ago(2), "leadership_hire", "VP Sales")
        self.event(ago(2), "posting_crm_mismatch", "Sales Ops : demande Salesforce, site : HubSpot")
        self.assertEqual(rows[0]["tier"], "B")   # 12 de profil sans timing : B
        touched = timing.apply_timing(self.conn, rows, TODAY, GRID)
        self.assertEqual(touched, 1)
        acme = rows[0]
        self.assertEqual((acme["domaine"], acme["tier"], acme["score"], acme["score_timing"]), ("acme.com", "A", 15, 6))
        self.assertIn("Recrute à la direction : VP Sales, il y a 2 j +3", acme["detail_score"])
        angles = acme["angle_suggere"].split(" | ")
        self.assertEqual(len(angles), 2)          # deux angles au plus, les événements d'abord
        self.assertNotIn("Ancien angle", angles)
        self.assertEqual([r["domaine"] for r in rows], ["acme.com", "quiet.com", "gone.com"])

    def test_score_parts_recorded(self):
        db.record_score(self.conn, "acme.com", self.run, TODAY,
                        {"tier": "A", "score": 15, "score_profil": 9, "score_timing": 6})
        self.assertEqual(tuple(self.conn.execute("SELECT profile, timing FROM scores").fetchone()), (9, 6))


if __name__ == "__main__":
    unittest.main()
