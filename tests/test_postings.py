"""Tests sans réseau de l'extraction structurée des offres. Lancer : python -m unittest tests.test_postings -v"""
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import score_prospects as sp  # noqa: E402

REC = {k: "" for k in sp.COLUMNS} | {"company": "Acme", "website": "acme.com", "domain": "acme.com"}


def fetch_with(url, payload):
    def fake(u, *args, **kwargs):
        if u == url:
            return json.dumps(payload)
        raise urllib.error.URLError("x")
    return patch("score_prospects.fetch_text", side_effect=fake)


def jobs_for(provider, payload):
    url = sp.ATS_API[provider][0].format(slug="acme")
    with fetch_with(url, payload):
        return sp.fetch_jobs(REC, {"ats": f"{provider}:acme"}, 5)


class TestPostings(unittest.TestCase):
    def test_greenhouse(self):
        out = jobs_for("greenhouse", {"jobs": [
            {"id": 4012345, "title": "Revenue Operations Manager", "location": {"name": "Toronto, ON"},
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/4012345", "updated_at": "2026-10-01T10:00:00-04:00",
             "first_published": "2026-09-15T09:30:00-04:00"},
            {"id": 7, "title": "Account Executive", "location": {"name": "Remote"}, "absolute_url": "https://x/7",
             "updated_at": "2026-10-01T10:00:00-04:00"},
        ]})
        first, second = out["postings"]
        self.assertEqual(first, {"posting_id": "4012345", "title": "Revenue Operations Manager", "location": "Toronto, ON",
                                 "url": "https://boards.greenhouse.io/acme/jobs/4012345", "published_at": "2026-09-15",
                                 "category": "revops"})
        self.assertEqual(second["published_at"], "")  # updated_at n'est pas une date de publication
        self.assertEqual(out["titles"], ["Revenue Operations Manager", "Account Executive"])

    def test_lever(self):
        out = jobs_for("lever", [{"id": "abc-123", "text": "VP of Sales", "categories": {"location": "Montreal"},
                                  "hostedUrl": "https://jobs.lever.co/acme/abc-123", "createdAt": 1757929800000}])
        p = out["postings"][0]
        self.assertEqual((p["posting_id"], p["location"], p["url"], p["published_at"], p["category"]),
                         ("abc-123", "Montreal", "https://jobs.lever.co/acme/abc-123", "2025-09-15", "leadership"))

    def test_ashby(self):
        out = jobs_for("ashby", {"jobs": [{"id": "uuid-1", "title": "Head of Growth", "location": "Toronto",
                                           "jobUrl": "https://jobs.ashbyhq.com/acme/uuid-1", "publishedAt": "2026-08-20T14:00:00.000+00:00"}]})
        p = out["postings"][0]
        self.assertEqual((p["posting_id"], p["location"], p["url"], p["published_at"], p["category"]),
                         ("uuid-1", "Toronto", "https://jobs.ashbyhq.com/acme/uuid-1", "2026-08-20", "leadership"))

    def test_workable(self):
        out = jobs_for("workable", {"name": "Acme", "jobs": [
            {"shortcode": "A1B2C3", "title": "SDR", "city": "Ottawa", "country": "Canada",
             "url": "https://apply.workable.com/j/A1B2C3", "published_on": "2026-07-01", "created_at": "2026-06-01"}]})
        p = out["postings"][0]
        self.assertEqual((p["posting_id"], p["location"], p["url"], p["published_at"], p["category"]),
                         ("A1B2C3", "Ottawa, Canada", "https://apply.workable.com/j/A1B2C3", "2026-07-01", "sales"))

    def test_missing_fields_do_not_raise(self):
        cases = {
            "greenhouse": {"jobs": [{"title": "Engineer"}, {"id": 1}, "junk", {"title": "X", "location": None}]},
            "lever": [{"text": "Engineer"}, {"id": "z"}, None, {"text": "Y", "categories": None}],
            "ashby": {"jobs": [{"title": "Engineer"}, {}, {"title": "Y", "publishedAt": "garbage"}]},
            "workable": {"jobs": [{"title": "Engineer"}, {"city": "Paris"}, {"title": "Y", "published_on": None}]},
        }
        for provider, payload in cases.items():
            with self.subTest(provider=provider):
                out = jobs_for(provider, payload)
                self.assertEqual(len(out["postings"]), 2)  # les offres sans titre sont ignorées
                for p in out["postings"]:
                    self.assertTrue(p["title"])
                    self.assertTrue(p["posting_id"])
                    self.assertEqual(p["published_at"], "")
                self.assertEqual(out["postings"][0]["posting_id"], sp.norm_company("Engineer"))
                self.assertEqual(out["titles"], [p["title"] for p in out["postings"]])

    def test_category_of_representative_titles(self):
        expected = {
            "Revenue Operations Manager": "revops",
            "Salesforce Admin": "revops",
            "HubSpot Specialist": "revops",
            "VP of Sales": "leadership",
            "Vice President, Marketing": "leadership",
            "Head of Growth": "leadership",
            "Chief Revenue Officer": "leadership",
            "CMO": "leadership",
            "Director of Marketing": "leadership",
            "Account Executive": "sales",
            "SDR": "sales",
            "Senior Backend Engineer": "other",
        }
        for title, category in expected.items():
            with self.subTest(title=title):
                self.assertEqual(sp.job_category(title), category)


if __name__ == "__main__":
    unittest.main()
