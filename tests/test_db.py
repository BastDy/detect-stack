"""Tests du stockage historique (db.py), base en mémoire. Lancer : python -m unittest tests.test_db -v"""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402


def det(hubspot="Oui", portals="", tools="", error=""):
    """Résultat au format detect_stack.analyze."""
    if error:
        return {"domaine": "acme.com", "hubspot": "Erreur", "erreur": error}
    return {"domaine": "acme.com", "hubspot": hubspot, "portal_id": portals, "region": "na1" if portals else "",
            "signaux_hubspot": "tracking" if hubspot == "Oui" else "", "autres_outils": tools,
            "indices_saas": "pricing, demo", "ats": "greenhouse:acme", "pages_analysees": 3, "erreur": ""}


def job(pid, title, category=None, published_at=None):
    return {"posting_id": pid, "title": title, "category": category, "location": "Toronto",
            "url": f"https://boards.greenhouse.io/acme/jobs/{pid}", "published_at": published_at}


def day(n):
    return (date(2026, 11, 1) + timedelta(days=n)).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.run = db.start_run(self.conn, {"test": True})

    def tearDown(self):
        self.conn.close()

    def events(self, kind=None):
        sql = "SELECT type, detail FROM events" + (" WHERE type = ?" if kind else "") + " ORDER BY id"
        return [tuple(r) for r in self.conn.execute(sql, (kind,) if kind else ())]

    def stack(self):
        return {r["tool"]: (r["status"], r["absent_count"]) for r in self.conn.execute("SELECT * FROM stack")}

    def scan(self, n, detection):
        db.record_scan(self.conn, "acme.com", detection, self.run, day(n) + "T09:00:00")

    def postings(self, n, postings, ats="greenhouse:acme"):
        db.record_postings(self.conn, "acme.com", ats, postings, self.run, day(n))


class TestSchema(Base):
    def test_version_and_reopen(self):
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "history.db"
            conn = db.connect(path)
            db.start_run(conn, {})
            conn.close()
            conn = db.connect(path)  # deuxième ouverture : ne recrée rien
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
            conn.close()

    def test_views_run(self):
        db.upsert_company(self.conn, {"domain": "acme.com", "company": "Acme"}, day(0))
        self.scan(0, det(tools="Calendly"))
        self.postings(0, [job("1", "RevOps Manager")])
        db.record_score(self.conn, "acme.com", self.run, date.today(), {"tier": "A", "score": 14})
        db.record_funding(self.conn, "acme.com", date.today(), 5_000_000, "CAD", "Seed", "https://x", self.run)
        for view in ("v_latest_scores", "v_current_stack", "v_open_postings", "v_recent_events"):
            self.assertTrue(self.conn.execute(f"SELECT * FROM {view}").fetchall(), view)
        name, age = self.conn.execute("SELECT name, age_days FROM v_open_postings").fetchone()
        self.assertEqual(name, "Acme")
        self.assertGreaterEqual(age, 0)

    def test_finish_run(self):
        db.finish_run(self.conn, self.run, companies=3, sites_read=2, errors=1, warnings=["x"])
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (self.run,)).fetchone()
        self.assertEqual((row["companies"], json.loads(row["warnings"])), (3, ["x"]))
        self.assertIsNotNone(row["finished_at"])
        with self.assertRaises(ValueError):
            db.finish_run(self.conn, self.run, typo=1)


class TestCompanies(Base):
    def test_empty_value_does_not_overwrite(self):
        db.upsert_company(self.conn, {"domain": "acme.com", "company": "Acme", "employees": "1,200", "city": "Toronto",
                                      "sources": "betakit; yc"}, day(0))
        db.upsert_company(self.conn, {"domain": "acme.com", "company": "", "employees": "", "city": "",
                                      "industry": "Software", "sources": "apollo"}, day(5))
        row = self.conn.execute("SELECT * FROM companies").fetchone()
        self.assertEqual((row["name"], row["employees"], row["city"], row["industry"]), ("Acme", 1200, "Toronto", "Software"))
        self.assertEqual(json.loads(row["sources"]), ["betakit", "yc", "apollo"])
        self.assertEqual((row["first_seen"], row["last_seen"]), (day(0), day(5)))

    def test_funding_event_only_once(self):
        self.assertTrue(db.record_funding(self.conn, "acme.com", "2026-10-01", 5e6, "CAD", "Series A", "u", self.run))
        self.assertFalse(db.record_funding(self.conn, "acme.com", "2026-10-01", 5e6, "CAD", "Series A", "u", self.run))
        self.assertEqual(len(self.events("funding")), 1)


class TestStack(Base):
    def test_first_scan_fills_stack_without_events(self):
        self.scan(0, det(tools="Calendly, Pipedrive"))
        self.assertEqual(set(self.stack()), {"HubSpot", "Calendly", "Pipedrive"})
        self.assertEqual(self.events(), [])

    def test_tool_added_after_first_scan(self):
        self.scan(0, det(tools="Calendly"))
        self.scan(1, det(tools="Calendly, Pipedrive"))
        self.assertEqual(self.events(), [("tool_added", "Pipedrive")])

    def test_error_scan_changes_nothing(self):
        self.scan(0, det(tools="Calendly"))
        before = self.stack()
        for n in (1, 2, 3):
            self.scan(n, det(error="URLError: timeout"))
        self.assertEqual(self.stack(), before)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM site_scans WHERE ok = 0").fetchone()[0], 3)

    def test_removed_only_after_two_successful_absences(self):
        self.scan(0, det(tools="Calendly"))
        self.scan(1, det(tools=""))
        self.assertEqual(self.stack()["Calendly"], ("present", 1))
        self.scan(2, det(error="boom"))  # une erreur entre les deux ne compte pas et ne remet pas à zéro
        self.assertEqual(self.stack()["Calendly"], ("present", 1))
        self.scan(3, det(tools=""))
        self.assertEqual(self.stack()["Calendly"], ("gone", 2))
        self.assertEqual(self.events(), [("tool_removed", "Calendly")])

    def test_reappearance_resets_and_readds(self):
        self.scan(0, det(tools="Calendly"))
        self.scan(1, det(tools=""))
        self.scan(2, det(tools="Calendly"))
        self.assertEqual(self.stack()["Calendly"], ("present", 0))
        self.assertEqual(self.events(), [])
        self.scan(3, det(tools=""))
        self.scan(4, det(tools=""))
        self.scan(5, det(tools="Calendly"))
        self.assertEqual(self.stack()["Calendly"], ("present", 0))
        self.assertEqual(self.events(), [("tool_removed", "Calendly"), ("tool_added", "Calendly")])
        gone_since = self.conn.execute("SELECT gone_since FROM stack WHERE tool = 'Calendly'").fetchone()[0]
        self.assertIsNone(gone_since)

    def test_hubspot_verdict_and_portals_are_tools(self):
        self.scan(0, det(hubspot="Probable", portals="123", tools=""))
        self.assertEqual(set(self.stack()), {"HubSpot", "HubSpot portal 123"})
        self.scan(1, det(hubspot="Non détecté", tools=""))
        self.scan(2, det(hubspot="Non détecté", tools=""))
        self.assertEqual({t for t, (s, _) in self.stack().items() if s == "gone"}, {"HubSpot", "HubSpot portal 123"})

    def test_second_portal_on_first_scan_and_once(self):
        self.scan(0, det(portals="123;456"))
        self.scan(1, det(portals="123;456"))
        self.assertEqual(self.events("second_portal"), [("second_portal", "portals HubSpot : 123, 456")])

    def test_second_portal_when_appearing(self):
        self.scan(0, det(portals="123"))
        self.scan(1, det(portals="123;789"))
        self.assertEqual([k for k, _ in self.events()], ["tool_added", "second_portal"])

    def test_same_day_replay_is_idempotent(self):
        self.scan(0, det(tools="Calendly"))
        for _ in range(3):
            self.scan(1, det(tools="Pipedrive"))
        # Trois rejeux le même jour = une seule lecture : Calendly absent une fois, pas retiré.
        self.assertEqual(self.stack()["Calendly"], ("present", 1))
        self.assertEqual(self.events(), [("tool_added", "Pipedrive")])


class TestPostings(Base):
    def test_failed_read_changes_nothing(self):
        self.postings(0, [job("1", "Account Executive")])
        for n in (1, 2, 3):
            self.postings(n, None)
        row = self.conn.execute("SELECT closed_on, absent_count FROM job_postings").fetchone()
        self.assertEqual(tuple(row), (None, 0))
        self.assertEqual(self.events(), [])

    def test_first_read_no_opened_but_leadership(self):
        self.postings(0, [job("1", "Account Executive"), job("2", "VP of Sales"), job("3", "Engineer")])
        self.assertEqual(self.events(), [("leadership_hire", "VP of Sales")])
        cats = dict(self.conn.execute("SELECT posting_id, category FROM job_postings").fetchall())
        self.assertEqual(cats, {"1": "sales", "2": "leadership", "3": "other"})

    def test_opened_closed_and_reopened(self):
        self.postings(0, [job("1", "Account Executive")])
        self.postings(1, [job("1", "Account Executive"), job("2", "SDR")])
        self.postings(2, [job("1", "Account Executive")])
        self.assertEqual(self.events(), [("posting_opened", "SDR")])
        self.postings(3, [job("1", "Account Executive")])
        self.assertEqual(self.events()[-1], ("posting_closed", "SDR"))
        self.assertEqual(self.conn.execute("SELECT closed_on FROM job_postings WHERE posting_id = '2'").fetchone()[0], day(3))
        self.postings(4, [job("1", "Account Executive"), job("2", "SDR")])
        self.assertEqual(self.events()[-1], ("posting_opened", "SDR"))

    def test_leadership_once_per_posting(self):
        self.postings(0, [job("9", "Head of Marketing")])
        self.postings(1, [job("9", "Head of Marketing")])
        self.assertEqual(len(self.events("leadership_hire")), 1)

    def test_stale_revops_once(self):
        old = day(-90)
        self.postings(0, [job("1", "Revenue Operations Manager", published_at=old),
                          job("2", "Account Executive", published_at=old)])
        self.postings(1, [job("1", "Revenue Operations Manager", published_at=old)])
        stale = self.events("posting_stale")
        self.assertEqual(len(stale), 1)
        self.assertIn("Revenue Operations Manager", stale[0][1])

    def test_stale_uses_first_seen_without_published_at(self):
        self.postings(0, [job("1", "Sales Operations Analyst")])
        self.postings(30, [job("1", "Sales Operations Analyst")])
        self.assertEqual(self.events("posting_stale"), [])
        self.postings(61, [job("1", "Sales Operations Analyst")])
        self.assertEqual(len(self.events("posting_stale")), 1)

    def test_hiring_spike(self):
        self.postings(0, [job(str(i), "Account Executive") for i in range(4)])  # premier relevé : pas de spike
        self.assertEqual(self.events("hiring_spike"), [])
        self.postings(1, [job(str(i), "Account Executive") for i in range(6)])  # +2
        self.assertEqual(self.events("hiring_spike"), [])
        self.postings(2, [job(str(i), "Account Executive") for i in range(9)])  # +3
        self.assertEqual(self.events("hiring_spike"), [("hiring_spike", "offres sales ouvertes : 6 → 9")])

    def test_same_day_replay_is_idempotent(self):
        self.postings(0, [job("1", "Account Executive"), job("2", "SDR")])
        for _ in range(3):
            self.postings(1, [job("1", "Account Executive"), job("3", "Director of Sales")])
        self.assertEqual(self.conn.execute("SELECT absent_count FROM job_postings WHERE posting_id = '2'").fetchone()[0], 1)
        self.assertEqual(sorted(self.events()), [("leadership_hire", "Director of Sales"), ("posting_opened", "Director of Sales")])


class TestScores(Base):
    def score(self, n, tier, run=None):
        db.record_score(self.conn, "acme.com", run or db.start_run(self.conn, {}), day(n),
                        {"tier": tier, "score": 10 if tier in db.TIERS else "", "detail_score": "x", "angle_suggere": "y"})

    def test_tier_changes(self):
        self.score(0, "C")
        self.score(1, "A")
        self.score(2, "Exclu : candidature CDI")  # ignoré
        self.score(3, "B")
        self.assertEqual(self.events(), [("tier_up", "C → A"), ("tier_down", "A → B")])
        self.assertEqual(db.last_tier(self.conn, "acme.com"), "B")

    def test_replay_same_run_no_duplicate(self):
        self.score(0, "C")
        run = db.start_run(self.conn, {})
        for _ in range(2):
            self.score(1, "A", run)
        self.assertEqual(self.events(), [("tier_up", "C → A")])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0], 2)


class TestDue(Base):
    def setUp(self):
        super().setUp()
        for domain, tier in (("hot.com", "A"), ("cold.com", "C"), ("unknown.com", None)):
            db.record_scan(self.conn, domain, det(), self.run, day(0) + "T08:00:00")
            if tier:
                db.record_score(self.conn, domain, self.run, day(0), {"tier": tier, "score": 5})
        db.record_scan(self.conn, "broken.com", det(error="x"), self.run, day(0) + "T08:00:00")

    def test_due_for_scan(self):
        domains = ["hot.com", "cold.com", "unknown.com", "broken.com", "never.com"]
        self.assertEqual(db.due_for_scan(self.conn, domains, day(0)), ["never.com"])
        self.assertEqual(db.due_for_scan(self.conn, domains, day(1)), ["hot.com", "broken.com", "never.com"])
        self.assertEqual(db.due_for_scan(self.conn, domains, day(30)), domains)
        policy = {"scan": {"hot": 3, "cold": 30, "error": 1}}
        self.assertEqual(db.due_for_scan(self.conn, domains, day(1), policy), ["broken.com", "never.com"])

    def test_due_for_jobs(self):
        db.record_postings(self.conn, "hot.com", "lever:hot", [], self.run, day(0))
        db.record_postings(self.conn, "cold.com", "lever:cold", [], self.run, day(0))
        db.record_postings(self.conn, "broken.com", "lever:broken", None, self.run, day(0))
        domains = ["hot.com", "cold.com", "broken.com", "never.com"]
        self.assertEqual(db.due_for_jobs(self.conn, domains, day(1)), ["hot.com", "broken.com", "never.com"])
        self.assertEqual(db.due_for_jobs(self.conn, domains, day(7)), domains)

    def test_last_scan_and_recent_events(self):
        scan = db.last_scan(self.conn, "hot.com")
        self.assertEqual((scan["ok"], scan["tools"], scan["saas_hints"]), (1, [], ["pricing", "demo"]))
        self.assertIsNone(db.last_scan(self.conn, "never.com"))
        db.record_scan(self.conn, "hot.com", det(tools="Zoho"), self.run, day(2) + "T08:00:00")
        db.record_scan(self.conn, "hot.com", det(tools="Zoho, Drift"), self.run, day(3) + "T08:00:00")
        events = db.recent_events(self.conn, "hot.com", day(1))
        self.assertEqual([(e["day"], e["detail"]) for e in events], [(day(3), "Drift"), (day(2), "Zoho")])
        self.assertEqual(db.recent_events(self.conn, "hot.com", day(3), limit=1)[0]["detail"], "Drift")


class TestLegacyImport(Base):
    def test_import_once_without_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "detection.jsonl").write_text("\n".join([
                json.dumps({"domain": "acme.com", "fetched_at": "2026-11-01T10:00:00", "data": det(portals="1;2", tools="Calendly")}),
                "{pas du json",
                json.dumps({"domain": "acme.com", "fetched_at": "2026-11-05T10:00:00", "data": det(portals="1;2", tools="Calendly, Zoho")}),
                json.dumps({"domain": "down.com", "fetched_at": "2026-11-05T10:00:00", "data": det(error="DNS")}),
            ]) + "\n", encoding="utf-8")
            (cache / "ats.jsonl").write_text("\n".join([
                json.dumps({"domain": "acme.com", "fetched_at": "2026-11-05T10:00:00",
                            "data": {"ats": "greenhouse:acme (lien du site)",
                                     "titles": ["VP  Sales", "Revenue Operations Manager", "VP Sales"]}}),
                json.dumps({"domain": "none.com", "fetched_at": "2026-11-05T10:00:00", "data": {"ats": "", "titles": []}}),
            ]) + "\n", encoding="utf-8")
            stats = db.import_legacy_caches(self.conn, cache)
            self.assertEqual(stats, {"scans": 3, "job_reads": 2, "skipped_lines": 1})
            self.assertEqual(db.import_legacy_caches(self.conn, cache), {"already_imported": True})
        self.assertEqual(self.events(), [])
        self.assertIn("Zoho", self.stack())
        rows = self.conn.execute("SELECT ats, posting_id, category FROM job_postings ORDER BY posting_id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("greenhouse:acme", "revenue operations manager", "revops"),
                                                     ("greenhouse:acme", "vp sales", "leadership")])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM site_scans").fetchone()[0], 3)
        # Après l'import, le scan suivant n'est plus un "premier scan" : les changements sont signalés.
        db.record_scan(self.conn, "acme.com", det(portals="1;2", tools="Calendly, Zoho, Drift"), self.run, "2026-11-12T09:00:00")
        self.assertEqual(self.events(), [("tool_added", "Drift")])


class TestCategorize(unittest.TestCase):
    def test_categories(self):
        cases = {"Revenue Operations Manager": "revops", "Director, Sales Operations": "revops",
                 "VP of Sales": "leadership", "Chief Revenue Officer": "leadership", "Head of Growth": "leadership",
                 "Account Executive": "sales", "Senior Software Engineer": "other", "Director of Engineering": "other"}
        for title, expected in cases.items():
            self.assertEqual(db.categorize(title), expected, title)


if __name__ == "__main__":
    unittest.main()
