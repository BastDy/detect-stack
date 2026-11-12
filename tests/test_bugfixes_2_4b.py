"""Tests sans réseau : sous-pages/GTM toujours scannés (bug 2) et pré-filtre ATS (bug 4b). Lancer : python -m unittest tests.test_bugfixes_2_4b -v"""
import copy
import sys
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import score_prospects as sp  # noqa: E402
from detect_stack import analyze  # noqa: E402

BASE_URL = "https://acme.com"
HUBSPOT_HOME = '<html><script src="https://js.hs-scripts.com/1234567.js"></script>%s</html>'
GTM_URL = "https://www.googletagmanager.com/gtm.js?id=GTM-ABCD123"


def pages_fetcher(pages, calls=None):
    def fake(url, *args, **kwargs):
        if calls is not None:
            calls.append(url)
        if url in pages:
            return url, pages[url]
        raise urllib.error.URLError("x")
    return fake


class TestAnalyzeAlwaysScansExtras(unittest.TestCase):
    def test_calendly_on_demo_page_with_hubspot_on_home(self):
        calls = []
        pages = {f"{BASE_URL}/demo": '<a href="https://calendly.com/acme/demo">Book</a>'}
        with patch("detect_stack.fetch_home", return_value=(BASE_URL, HUBSPOT_HOME % "")), \
                patch("detect_stack.fetch", side_effect=pages_fetcher(pages, calls)):
            out = analyze("acme.com", 5)
        self.assertEqual(out["hubspot"], "Oui")
        self.assertIn("Calendly", out["autres_outils"])
        for path in ("/contact", "/contact-us", "/demo", "/pricing"):
            self.assertIn(BASE_URL + path, calls)

    def test_pipedrive_in_gtm_without_via_gtm(self):
        home = HUBSPOT_HOME % "<script>GTM-ABCD123</script>"
        pages = {GTM_URL: "https://webforms.pipedrive.com/f/abc"}
        with patch("detect_stack.fetch_home", return_value=(BASE_URL, home)), \
                patch("detect_stack.fetch", side_effect=pages_fetcher(pages)):
            out = analyze("acme.com", 5)
        self.assertIn("Pipedrive", out["autres_outils"])
        self.assertNotIn("via-gtm", out["signaux_hubspot"])

    def test_via_gtm_still_added_when_gtm_brings_hubspot(self):
        home = "<html>GTM-ABCD123</html>"
        pages = {GTM_URL: "https://js.hs-scripts.com/1234567.js"}
        with patch("detect_stack.fetch_home", return_value=(BASE_URL, home)), \
                patch("detect_stack.fetch", side_effect=pages_fetcher(pages)):
            out = analyze("acme.com", 5)
        self.assertEqual(out["hubspot"], "Oui")
        self.assertIn("via-gtm", out["signaux_hubspot"])


def rec(domain, **kw):
    return {k: "" for k in sp.COLUMNS} | {"company": domain, "website": domain, "domain": domain} | kw


class TestAtsPrefilter(unittest.TestCase):
    def run_records(self, records, **kwargs):
        grid = copy.deepcopy(sp.DEFAULT_SCORING)
        with tempfile.TemporaryDirectory() as tmp, \
                patch("score_prospects.scoring_config", return_value=grid), \
                patch("score_prospects.analyze", return_value={"hubspot": "Non détecté"}), \
                patch("score_prospects.fetch_jobs", return_value={"titles": [], "ats": ""}) as fj:
            sp.score_records(records, set(), cache_dir=tmp, workers=1, today=date(2026, 1, 1), **kwargs)
        return [c.args[0]["domain"] for c in fj.call_args_list]

    def test_low_prescore_company_is_checked_with_auto_rule(self):
        # Canada +1 : 1 + 5 (RevOps) + 2 (sales) = 8 = tier B
        self.assertEqual(self.run_records([rec("maple.ca", country="Canada")]), ["maple.ca"])

    def test_company_that_cannot_reach_b_is_not_checked(self):
        # 0 + 5 + 2 = 7 < 8
        self.assertEqual(self.run_records([rec("nowhere.com", country="France")]), [])

    def test_explicit_ats_min_keeps_old_behaviour(self):
        records = [rec("maple.ca", country="Canada")]
        self.assertEqual(self.run_records(records, ats_min=5), [])
        self.assertEqual(self.run_records(records, ats_min=1), ["maple.ca"])


if __name__ == "__main__":
    unittest.main()
