"""Test d'intégration sans réseau : pipeline.py branché sur l'historique SQLite. Lancer : python -m unittest tests.test_pipeline_db -v

Deux runs à deux jours d'intervalle, base / sorties / caches dans un dossier temporaire (data/ n'est jamais touché).
"""
import contextlib
import copy
import csv
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline  # noqa: E402
import score_prospects as sp  # noqa: E402

DAY1, DAY2 = date(2026, 11, 10), date(2026, 11, 11)
GREENHOUSE_ACME = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
LEVER_COLDCO = sp.ATS_API["lever"][0].format(slug="coldco")


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["domaine"]: r for r in csv.DictReader(f)}


class FakeWorld:
    """Sites et boards simulés ; compte les lectures."""

    def __init__(self):
        self.day = DAY1
        self.analyzed, self.fetched = [], []

    def analyze(self, domain, timeout):
        self.analyzed.append((self.day, domain))
        base = {"domaine": domain, "portal_id": "", "region": "", "indices_saas": "", "pages_analysees": 3, "erreur": ""}
        if domain == "acme.com":
            hubspot = self.day >= DAY2  # HubSpot installé entre les deux runs
            return {**base, "hubspot": "Oui" if hubspot else "Non détecté", "signaux_hubspot": "tracking" if hubspot else "",
                    "autres_outils": "", "ats": "greenhouse:acme"}
        if domain == "coldco.ca":
            return {**base, "hubspot": "Non détecté", "signaux_hubspot": "", "autres_outils": "Calendly",
                    "indices_saas": "pricing", "ats": "lever:coldco"}
        return {**base, "hubspot": "Non détecté", "signaux_hubspot": "", "autres_outils": "", "ats": ""}

    def fetch_text(self, url, timeout):
        self.fetched.append((self.day, url))
        if url == GREENHOUSE_ACME:
            jobs = [{"id": 1, "title": "Account Executive"}, {"id": 2, "title": "SDR"}, {"id": 3, "title": "Growth Marketer"}]
            if self.day >= DAY2:
                jobs.append({"id": 4, "title": "RevOps Manager"})
            return json.dumps({"jobs": jobs})
        if url == LEVER_COLDCO:
            return json.dumps([{"id": "c1", "text": "Account Executive"}])
        raise urllib.error.URLError("hors ligne")


class FakeDate(date):
    value = DAY1

    @classmethod
    def today(cls):
        return cls.value


class TestPipelineHistory(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.out, self.db_path = root / "output", root / "history.db"
        inputs, cache = root / "inputs", root / "cache"
        for d in (self.out, inputs, cache):
            d.mkdir()
        write_csv(inputs / "apollo.csv", [
            {"Company": "Acme", "Website": "acme.com", "# Employees": "80", "Industry": "Computer Software",
             "Company State": "Ontario", "Company Country": "Canada", "Last Raised At": "2026-09-01",
             "Montant": "5 million", "Devise": "CAD", "Tour": "Seed", "Source": "https://betakit.com/acme"},
            {"Company": "Cold Co", "Website": "coldco.ca", "# Employees": "50", "Industry": "",
             "Company State": "", "Company Country": "Canada", "Last Raised At": "",
             "Montant": "", "Devise": "", "Tour": "", "Source": ""},
            {"Company": "Tiny", "Website": "tiny.ca", "# Employees": "5", "Industry": "Software",
             "Company State": "Ontario", "Company Country": "Canada", "Last Raised At": "",
             "Montant": "", "Devise": "", "Tour": "", "Source": ""},
        ])
        tracker = root / "tracker.csv"
        write_csv(tracker, [{"company": "Personne"}])
        self.args = ["--inputs", str(inputs), "--no-betakit", "--no-yc", "--no-linkedin", "--tracker", str(tracker),
                     "--db", str(self.db_path), "--workers", "2"]
        self.world = FakeWorld()
        grid = copy.deepcopy(sp.DEFAULT_SCORING)  # indépendant du config.json local
        for p in (patch.object(pipeline, "OUTPUT", self.out), patch.object(pipeline, "LATEST", self.out / "prospects-latest.csv"),
                  patch.object(pipeline, "CACHE", cache), patch.object(pipeline, "date", FakeDate),
                  patch("pipeline.scoring_config", return_value=grid), patch("score_prospects.scoring_config", return_value=grid),
                  patch("score_prospects.analyze", side_effect=self.world.analyze),
                  patch("score_prospects.fetch_text", side_effect=self.world.fetch_text)):
            p.start()
            self.addCleanup(p.stop)

    def run_on(self, day):
        self.world.day = FakeDate.value = day
        sink = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")  # sys.stdout.reconfigure doit exister
        with contextlib.redirect_stdout(sink):
            pipeline.main(self.args)
        return read_csv(self.out / f"prospects-{day.isoformat()}.csv"), \
            (self.out / f"resume-{day.isoformat()}.md").read_text(encoding="utf-8")

    def query(self, sql, *params):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def test_two_runs(self):
        rows1, summary1 = self.run_on(DAY1)
        self.assertEqual((rows1["acme.com"]["tier"], rows1["acme.com"]["score"]), ("A", "12"))
        self.assertEqual(rows1["coldco.ca"]["tier"], "C")
        self.assertEqual(self.query("SELECT type FROM events WHERE type LIKE 'tool_%' OR type LIKE 'posting_%'"), [])
        self.assertEqual(rows1["acme.com"]["signaux_recents"], "")  # levée du 2026-09-01 : hors fenêtre de 30 jours
        self.assertIn("- Événements détectés : **0**", summary1)
        self.assertIn("Aucun changement détecté", summary1)

        rows2, summary2 = self.run_on(DAY2)
        # Tier A relu le lendemain ; Cold Co (tier C, lu la veille) n'est pas dû : ni site ni offres relus.
        self.assertEqual([d for day, d in self.world.analyzed if day == DAY2], ["acme.com"])
        self.assertEqual([u for day, u in self.world.fetched if day == DAY2], [GREENHOUSE_ACME])

        events = self.query("SELECT day, domain, type, detail FROM events WHERE day = ? ORDER BY id", DAY2.isoformat())
        self.assertEqual(events, [("2026-11-11", "acme.com", "tool_added", "HubSpot"),
                                  ("2026-11-11", "acme.com", "posting_opened", "RevOps Manager")])
        acme = rows2["acme.com"]
        self.assertEqual((acme["tier"], acme["score"]), ("A", "19"))  # 12 + HubSpot 2 + RevOps 5
        self.assertEqual(acme["signaux_recents"],
                         "2026-11-11 HubSpot installé · 2026-11-11 offre RevOps ouverte : RevOps Manager")
        self.assertEqual(acme["ats"], "greenhouse:acme (lien du site)")

        # Domaine non relu : sa ligne est reconstruite depuis la base, identique à celle du run 1.
        keys = ("tier", "score", "detail_score", "angle_suggere", "ats", "autres_outils", "indices_saas",
                "nb_offres_sales_marketing", "hubspot_site", "erreur_site")
        self.assertEqual({k: rows2["coldco.ca"][k] for k in keys}, {k: rows1["coldco.ca"][k] for k in keys})
        self.assertEqual(rows2["coldco.ca"]["ats"], "lever:coldco (lien du site)")

        self.assertIn("- Événements détectés : **2**", summary2)
        self.assertLess(summary2.index("## Ce qui a changé"), summary2.index("## Top 20"))
        self.assertIn("### Outils installés (1)", summary2)
        self.assertIn("- Acme (acme.com) : HubSpot", summary2)
        self.assertIn("- Acme (acme.com) : RevOps Manager", summary2)

        # Entreprises, levée, exclusions, scores, runs.
        self.assertEqual(self.query("SELECT domain, excluded_reason FROM companies ORDER BY domain"),
                         [("acme.com", None), ("coldco.ca", None), ("tiny.ca", "Exclu : moins de 10 salariés")])
        self.assertEqual(self.query("SELECT announced_on, amount, currency, round, article_url FROM funding_rounds"),
                         [("2026-09-01", 5_000_000.0, "CAD", "Seed", "https://betakit.com/acme")])
        self.assertEqual(self.query("SELECT COUNT(*) FROM scores WHERE domain = 'tiny.ca'"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM scores"), [(4,)])
        runs = self.query("SELECT companies, sites_read, errors, finished_at IS NOT NULL FROM runs "
                          "WHERE json_extract(options, '$.import') IS NULL ORDER BY id")
        self.assertEqual(runs, [(3, 2, 0, 1), (3, 1, 0, 1)])
        self.assertEqual(self.query("SELECT ats, ok, postings FROM job_reads ORDER BY id"),
                         [("greenhouse:acme", 1, 3), ("lever:coldco", 1, 1), ("greenhouse:acme", 1, 4)])

    def test_failed_board_and_crash_are_recorded(self):
        def analyze(domain, timeout):
            if domain == "coldco.ca":
                raise RuntimeError("boom")  # un domaine qui plante
            return self.world.analyze(domain, timeout)

        def offline(url, timeout):
            raise urllib.error.URLError("ATS en panne")

        with patch("score_prospects.fetch_text", side_effect=offline), \
                patch("score_prospects.analyze", side_effect=analyze):
            rows, _ = self.run_on(DAY1)
        # Site qui plante : lecture enregistrée en erreur, comme avant.
        self.assertEqual(self.query("SELECT ok, error FROM site_scans WHERE domain = 'coldco.ca'"),
                         [(0, "RuntimeError: boom")])
        self.assertIn("boom", rows["coldco.ca"]["erreur_site"])
        # acme : lien du site mais API en échec -> relevé ok = 0 au nom du board, aucune offre conclue.
        # coldco : plus de lien (site en erreur), slugs devinés en échec « inconnu » (ATS en panne) -> recherche non
        # concluante (ats '', ok = 0), retentée le lendemain.
        self.assertEqual(self.query("SELECT domain, ats, ok FROM job_reads ORDER BY domain"),
                         [("acme.com", "greenhouse:acme", 0), ("coldco.ca", "", 0)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM job_postings"), [(0,)])
        self.assertEqual(rows["acme.com"]["ats"], "")
        self.assertEqual(self.query("SELECT errors FROM runs WHERE json_extract(options, '$.import') IS NULL"), [(1,)])

    def test_limit_still_finishes_run(self):
        self.args += ["--limit", "1"]
        self.run_on(DAY1)
        self.assertEqual(self.query("SELECT companies, finished_at IS NOT NULL, json_extract(warnings, '$[0]') FROM runs "
                                    "WHERE json_extract(options, '$.import') IS NULL"),
                         [(1, 1, "Run partiel : --limit 1")])


if __name__ == "__main__":
    unittest.main()
