"""Tests sans réseau des fonctions qui font des requêtes HTTP. Lancer : python -m unittest tests.test_fetchers -v"""
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import score_prospects as sp  # noqa: E402
from detect_stack import analyze  # noqa: E402

BASE_URL = "https://acme.com"


def pages_fetcher(pages):
    """Faux fetch(url, timeout) : renvoie (url, html) pour les URL connues, sinon lève URLError."""
    def fake(url, *args, **kwargs):
        if url in pages:
            return url, pages[url]
        raise urllib.error.URLError("x")
    return fake


def texts_fetcher(texts):
    """Faux fetch_text(url, timeout) : renvoie le texte JSON attendu, sinon lève URLError."""
    def fake(url, *args, **kwargs):
        if url in texts:
            return texts[url]
        raise urllib.error.URLError("x")
    return fake


def rec_bolster():
    # Slug deviné "bolster" (>= 5 caractères) ; le domaine n'apparaît pas dans les offres d'Acme.
    return {k: "" for k in sp.COLUMNS} | {
        "company": "Bolster Capital", "website": "bolster.io", "domain": "bolster.io",
    }


class TestAnalyze(unittest.TestCase):
    def test_hubspot_on_homepage(self):
        home = '<html><script src="https://js.hs-scripts.com/1234567.js"></script></html>'
        with patch("detect_stack.fetch_home", return_value=(BASE_URL, home)), \
                patch("detect_stack.fetch", side_effect=pages_fetcher({})):
            out = analyze("acme.com", 5)
        self.assertEqual(out["hubspot"], "Oui")
        self.assertEqual(out["portal_id"], "1234567")

    def test_unreachable_site_is_error(self):
        with patch("detect_stack.fetch_home", side_effect=urllib.error.URLError("x")), \
                patch("detect_stack.fetch", side_effect=pages_fetcher({})):
            out = analyze("acme.com", 5)
        self.assertEqual(out["hubspot"], "Erreur")
        self.assertTrue(out["erreur"])

    def test_hubspot_via_google_tag_manager(self):
        home = '<html><script>window.gtmId = "GTM-ABC123";</script></html>'
        gtm_url = "https://www.googletagmanager.com/gtm.js?id=GTM-ABC123"
        pages = {
            BASE_URL: home,
            gtm_url: 'var s = "https://js.hs-scripts.com/1234567.js";',
        }
        with patch("detect_stack.fetch_home", return_value=(BASE_URL, home)), \
                patch("detect_stack.fetch", side_effect=pages_fetcher(pages)):
            out = analyze("acme.com", 5)
        self.assertEqual(out["hubspot"], "Oui")
        self.assertIn("via-gtm", out["signaux_hubspot"])

    def test_lever_link_on_careers_page(self):
        careers_url = BASE_URL + "/careers"
        pages = {careers_url: '<a href="https://jobs.lever.co/acme">Jobs</a>'}
        with patch("detect_stack.fetch_home", return_value=(BASE_URL, "<html></html>")), \
                patch("detect_stack.fetch", side_effect=pages_fetcher(pages)):
            out = analyze("acme.com", 5)
        self.assertIn("lever:acme", out["ats"])


class TestFetchJobs(unittest.TestCase):
    def test_slug_from_site_is_trusted(self):
        rec = {k: "" for k in sp.COLUMNS} | {"company": "Acme", "website": "acme.com", "domain": "acme.com"}
        texts = {
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs":
                json.dumps({"jobs": [{"title": "RevOps Manager"}]}),
        }
        with patch("score_prospects.fetch_text", side_effect=texts_fetcher(texts)):
            out = sp.fetch_jobs(rec, {"ats": "greenhouse:acme"}, 5)
        self.assertEqual(out["titles"], ["RevOps Manager"])
        self.assertIn("lien du site", out["ats"])

    def test_guessed_board_of_another_company_is_rejected(self):
        rec = rec_bolster()
        texts = {
            # Board "bolster" appartenant à une autre entreprise : ni le domaine ni le nom ne correspondent.
            "https://boards-api.greenhouse.io/v1/boards/bolster/jobs":
                json.dumps({"jobs": [{"title": "Account Executive", "absolute_url": "https://other.com/j/1"}]}),
            "https://boards-api.greenhouse.io/v1/boards/bolster":
                json.dumps({"name": "Other Co Inc"}),
        }
        with patch("score_prospects.fetch_text", side_effect=texts_fetcher(texts)):
            out = sp.fetch_jobs(rec, {}, 5)
        # « Aucun board » : board vide et ok=True (recherche aboutie, rien trouvé) — distinct d'un board en échec.
        self.assertEqual(out, {"ats": "", "titles": [], "board": "", "ok": True})

    def test_linked_board_with_failing_api_is_not_ok(self):
        rec = {k: "" for k in sp.COLUMNS} | {"company": "Acme", "website": "acme.com", "domain": "acme.com"}
        with patch("score_prospects.fetch_text", side_effect=texts_fetcher({})):
            out = sp.fetch_jobs(rec, {"ats": "greenhouse:acme"}, 5)
        self.assertEqual(out, {"ats": "", "titles": [], "board": "greenhouse:acme", "ok": False})

    def test_linked_empty_board_is_ok(self):
        rec = {k: "" for k in sp.COLUMNS} | {"company": "Acme", "website": "acme.com", "domain": "acme.com"}
        texts = {"https://boards-api.greenhouse.io/v1/boards/acme/jobs": json.dumps({"jobs": []})}
        with patch("score_prospects.fetch_text", side_effect=texts_fetcher(texts)):
            out = sp.fetch_jobs(rec, {"ats": "greenhouse:acme"}, 5)
        self.assertEqual((out["board"], out["ok"], out["postings"]), ("greenhouse:acme", True, []))

    def test_guessed_board_verified_by_domain_in_postings(self):
        rec = rec_bolster()
        texts = {
            "https://boards-api.greenhouse.io/v1/boards/bolster/jobs":
                json.dumps({"jobs": [{"title": "RevOps Lead", "absolute_url": "https://bolster.io/careers/1"}]}),
        }
        with patch("score_prospects.fetch_text", side_effect=texts_fetcher(texts)):
            out = sp.fetch_jobs(rec, {}, 5)
        self.assertEqual(out["titles"], ["RevOps Lead"])
        self.assertIn("vérifié", out["ats"])


if __name__ == "__main__":
    unittest.main()
