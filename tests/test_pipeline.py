"""Tests sans réseau. Lancer : python -m unittest discover -s tests"""
import csv
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import betakit_funding  # noqa: E402
import find_linkedin  # noqa: E402
import pipeline  # noqa: E402
import score_prospects as sp  # noqa: E402
import yc_companies  # noqa: E402
from common import Cache, normalize  # noqa: E402
from detect_stack import scan  # noqa: E402

TODAY = date(2026, 11, 11)


def rec(**kw):
    base = {k: "" for k in sp.COLUMNS}
    base.update(company="Acme", website="acme.com", domain="acme.com")
    base.update(kw)
    return base


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class TestNormalize(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(normalize("https://www.Acme.com/about"), "acme.com")
        self.assertEqual(normalize("acme.io"), "acme.io")
        self.assertEqual(normalize("http://user@app.acme.com:8080/x"), "app.acme.com")

    def test_not_a_domain(self):
        for value in ("", "   ", "#comment", "localhost", None):
            self.assertEqual(normalize(value), "")


class TestScore(unittest.TestCase):
    def test_tier_a_with_every_signal(self):
        r = rec(industry="Computer Software", employees="80", state="Ontario", last_raised="2026-06-01")
        det = {"hubspot": "Oui", "autres_outils": "Pipedrive, Calendly"}
        jobs = {"titles": ["Revenue Operations Manager", "Account Executive", "SDR", "Growth Marketer"]}
        out = sp.score(r, det, jobs, TODAY)
        # SaaS 3 + taille 2 + HubSpot 2 + Ontario 2 + RevOps 5 + levée 3 + 3 offres sales 2 + autre CRM 3
        self.assertEqual(out["score"], 22)
        self.assertEqual(out["tier"], "A")
        self.assertIn("Revenue Operations Manager", out["angle_suggere"])

    def test_tier_c_when_nothing_matches(self):
        out = sp.score(rec(), {}, {}, TODAY)
        self.assertEqual((out["score"], out["tier"]), (0, "C"))

    def test_old_funding_does_not_count(self):
        out = sp.score(rec(last_raised="2025-01-01"), {}, {}, TODAY)
        self.assertNotIn("Levée", out["detail_score"])

    def test_tier_thresholds(self):
        # Ontario 2 + RevOps 5 = 7 -> C ; + HubSpot 2 = 9 -> B
        r = rec(state="ON")
        jobs = {"titles": ["Sales Operations Analyst"]}
        self.assertEqual(sp.score(r, {}, jobs, TODAY)["tier"], "C")
        self.assertEqual(sp.score(r, {"hubspot": "Oui"}, jobs, TODAY)["tier"], "B")


class TestReadExport(unittest.TestCase):
    def test_apollo_columns_and_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "apollo.csv"
            write_csv(path, [
                {"Company": "AutoLeap", "Employees": "190", "Industries": "Computer Software", "Location (Canada)": "Toronto", "Domain": "autoleap.com"},
                {"Company": "NoSite", "Employees": "50", "Industries": "", "Location (Canada)": "", "Domain": ""},
                {"Company": "AutoLeap dup", "Employees": "190", "Industries": "", "Location (Canada)": "", "Domain": "https://www.autoleap.com"},
            ])
            records, without_site, _ = sp.read_export(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(without_site, 1)
        self.assertEqual((records[0]["state"], records[0]["country"], records[0]["industry"]), ("Ontario", "Canada", "Computer Software"))

    def test_missing_website_column_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            write_csv(path, [{"Company": "X", "Notes": "y"}])
            with self.assertRaises(ValueError):
                sp.read_export(path)


class TestTracker(unittest.TestCase):
    def test_missing_tracker_raises_instead_of_silently_skipping(self):
        with self.assertRaises(FileNotFoundError):
            sp.applied_companies(Path("n-existe-pas.csv"))

    def test_applied_companies_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracker.csv"
            write_csv(path, [{"date": "2026-10-01", "company": "Acme Inc."}])
            applied = sp.applied_companies(path)
        candidates, excluded = sp.exclusions([rec(company="Acme Inc"), rec(company="Small", domain="small.io", employees="4"),
                                              rec(company="Keep", domain="keep.io")], applied)
        self.assertEqual([c["domain"] for c in candidates], ["keep.io"])
        self.assertEqual(excluded, {"acme.com": "Exclu : candidature CDI", "small.io": "Exclu : moins de 10 salariés"})


class TestCache(unittest.TestCase):
    def test_ttl_errors_and_legacy_entries(self):
        now = datetime(2026, 11, 11, 12, 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.jsonl"
            lines = [
                {"domain": "fresh.com", "fetched_at": (now - timedelta(days=5)).isoformat(), "data": {"hubspot": "Oui"}},
                {"domain": "old.com", "fetched_at": (now - timedelta(days=40)).isoformat(), "data": {"hubspot": "Oui"}},
                {"domain": "err.com", "fetched_at": (now - timedelta(days=2)).isoformat(), "data": {"hubspot": "Erreur", "erreur": "x"}},
                {"domain": "legacy.com", "data": {"hubspot": "Oui"}},
            ]
            path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
            cache = Cache(path, max_age_days=30, now=now)
            self.assertEqual(cache.stale(["fresh.com", "old.com", "err.com", "legacy.com", "new.com"]),
                             ["old.com", "err.com", "legacy.com", "new.com"])
            cache.compact()
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_refresh_expires_everything(self):
        now = datetime(2026, 11, 11, 12, 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.jsonl"
            path.write_text(json.dumps({"domain": "a.com", "fetched_at": (now - timedelta(hours=1)).isoformat(), "data": {}}), encoding="utf-8")
            self.assertEqual(Cache(path, 0, now=now).stale(["a.com"]), ["a.com"])

    def test_run_stage_survives_a_crashing_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp) / "c.jsonl", 30)

            def worker(domain):
                if domain == "boom.com":
                    raise RuntimeError("boom")
                return {"ok": True}

            sp.run_stage("test", ["ok.com", "boom.com"], worker, cache, workers=2)
            self.assertEqual(cache.data()["ok.com"], {"ok": True})
            self.assertIn("boom", cache.data()["boom.com"]["erreur"])


class TestDetection(unittest.TestCase):
    def test_scan_finds_tools(self):
        found = {k: set() for k in ("strong", "weak", "other", "saas", "ats", "portal", "region")}
        html = ('<script src="//js-na2.hs-scripts.com/123456.js"></script><a href="https://jobs.lever.co/acme">Jobs</a>'
                '<script src="https://assets.calendly.com/x.js"></script><a href="/pricing">Pricing</a>')
        scan(html, found)
        self.assertIn("tracking", found["strong"])
        self.assertEqual(found["portal"], {"123456"})
        self.assertEqual(found["region"], {"na2"})
        self.assertIn("Calendly", found["other"])
        self.assertIn("lever:acme", found["ats"])
        self.assertIn("pricing", found["saas"])


class TestSources(unittest.TestCase):
    def test_betakit_company_from_title(self):
        self.assertEqual(betakit_funding.company_from_title("Toronto-based fintech Acme raises $10 million Series A"), "Acme")
        self.assertEqual(betakit_funding.company_from_title("Why AI matters for startups"), "")

    def test_yc_only_active_canadian(self):
        row = yc_companies.to_row({"name": "A", "website": "a.com", "status": "Active",
                                   "all_locations": "San Francisco, CA, USA; Toronto, ON, Canada", "industry": "B2B", "subindustry": "B2B -> Sales"})
        self.assertEqual((row["Company City"], row["Company State"], row["Industry"]), ("Toronto", "Ontario", "B2B / B2B -> Sales"))
        self.assertIsNone(yc_companies.to_row({"name": "B", "status": "Inactive", "all_locations": "Toronto, ON, Canada"}))
        self.assertIsNone(yc_companies.to_row({"name": "C", "status": "Active", "all_locations": "Austin, TX, USA"}))

    def test_linkedin_extract_prefers_company_page(self):
        html = '<a href="https://www.linkedin.com/in/ceo">CEO</a><a href="https://linkedin.com/company/acme/">Acme</a>'
        self.assertEqual(find_linkedin.extract(html)[0], "https://www.linkedin.com/company/acme")


class TestPipeline(unittest.TestCase):
    def test_merge_fills_gaps_and_keeps_latest_funding(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.csv", Path(tmp) / "b.csv"
            write_csv(a, [{"Company": "Acme", "Website": "acme.com", "Last Raised At": "2025-01-01", "# Employees": ""}])
            write_csv(b, [{"Company": "Acme", "Website": "https://www.acme.com", "Last Raised At": "2026-05-01", "# Employees": "50"}])
            merged = pipeline.merge_sources([("betakit", a), ("apollo", b)], [])
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0]["last_raised"], merged[0]["employees"], merged[0]["sources"]), ("2026-05-01", "50", "betakit; apollo"))

    def test_history_flags_new_and_promoted_and_keeps_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            old_output, old_latest = pipeline.OUTPUT, pipeline.LATEST
            pipeline.OUTPUT, pipeline.LATEST = out, out / "prospects-latest.csv"
            try:
                prev = [{"tier": "B", "domaine": "up.com", "vu_depuis": "2026-10-01"},
                        {"tier": "A", "domaine": "same.com", "vu_depuis": "2026-10-01"}]
                write_csv(out / "prospects-2026-11-04.csv", prev)
                write_csv(pipeline.LATEST, [{**p, "Notes": "appelé" if p["domaine"] == "same.com" else ""} for p in prev])
                rows = [{"tier": "A", "domaine": "up.com"}, {"tier": "A", "domaine": "same.com"}, {"tier": "B", "domaine": "new.com"}]
                user_fields, _ = pipeline.apply_history(rows, TODAY)
            finally:
                pipeline.OUTPUT, pipeline.LATEST = old_output, old_latest
        by = {r["domaine"]: r for r in rows}
        self.assertEqual(user_fields, ["Notes"])
        self.assertEqual(by["up.com"]["nouveau"], "Monte B → A")
        self.assertEqual(by["same.com"]["nouveau"], "")
        self.assertEqual(by["same.com"]["Notes"], "appelé")
        self.assertEqual(by["new.com"]["nouveau"], "Nouveau")
        self.assertEqual((by["up.com"]["vu_depuis"], by["new.com"]["vu_depuis"]), ("2026-10-01", "2026-11-11"))


if __name__ == "__main__":
    unittest.main()
