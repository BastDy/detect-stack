"""Tests sans réseau ni API de posting_signals.py. Lancer : python -m unittest tests.test_posting_signals -v"""
import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import posting_signals as ps  # noqa: E402

TODAY = date(2026, 11, 12)
LONG_TEXT = "We are hiring our first Account Executive. " * 20


def analysis(**overrides):
    data = {"crm": [], "outils": [], "premier_recrutement": False, "a_construire": False,
            "equipe_en_croissance": False, "taille_equipe": 0, "rattachement": "", "marches": [],
            "problemes": [], "preuves": [], "resume": "Rien de notable."}
    data.update(overrides)
    return data


class FakeClient:
    """Imite client.beta.messages.create : renvoie les analyses dans l'ordre, ou lève une exception."""

    def __init__(self, *results):
        self.results, self.calls = list(results), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        stop = result.pop("_stop", "end_turn") if isinstance(result, dict) else "end_turn"
        return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps(result))],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


class APIError(Exception):
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.status_code = status


def make_db(postings=(("greenhouse:acme", "1", "Founding Account Executive", "sales"),), crm_on_site="HubSpot"):
    conn = db.connect(":memory:")
    day = str(TODAY)
    conn.execute("INSERT INTO companies (domain, name) VALUES ('acme.com', 'Acme')")
    conn.execute("INSERT INTO scores (domain, run_id, day, tier, score) VALUES ('acme.com', 1, ?, 'A', 14)", (day,))
    if crm_on_site:
        conn.execute("INSERT INTO stack (domain, tool, first_seen, last_seen) VALUES ('acme.com', ?, ?, ?)",
                     (crm_on_site, day, day))
    for ats, pid, title, category in postings:
        conn.execute("""INSERT INTO job_postings (domain, ats, posting_id, title, category, first_seen, last_seen)
                        VALUES ('acme.com', ?, ?, ?, ?, ?, ?)""", (ats, pid, title, category, day, day))
    conn.commit()
    return conn


def run(conn, client, day=TODAY, **kwargs):
    with patch.object(ps, "posting_text", return_value=LONG_TEXT):
        return ps.analyze_pending(conn, day, client=client, workers=1, log=lambda *_: None, **kwargs)


class TestHtmlText(unittest.TestCase):
    def test_escaped_greenhouse_html(self):
        raw = "&lt;div&gt;&lt;p&gt;We use &lt;b&gt;Salesforce&lt;/b&gt;.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Own the CRM&lt;/li&gt;&lt;/ul&gt;&lt;/div&gt;"
        self.assertEqual(ps.html_text(raw), "We use Salesforce.\n- Own the CRM")

    def test_scripts_dropped(self):
        self.assertEqual(ps.html_text("<p>Hi</p><script>var x = 1;</script><style>p{}</style><p>there</p>"), "Hi\nthere")


class TestPostingText(unittest.TestCase):
    def fake_fetch(self, payloads):
        def fetch(url, timeout, accept=None):
            for key, payload in payloads.items():
                if key in url:
                    return url, json.dumps(payload)
            raise AssertionError(f"URL inattendue : {url}")
        return patch.object(ps, "fetch", side_effect=fetch)

    def test_each_ats(self):
        payloads = {
            "boards-api.greenhouse.io/v1/boards/acme/jobs/1": {"content": "&lt;p&gt;Greenhouse text&lt;/p&gt;"},
            "api.lever.co/v0/postings/acme/2": {"descriptionPlain": "Lever intro", "additionalPlain": "Outro",
                                                "lists": [{"text": "You will", "content": "<li>Build reports</li>"}]},
            "api.ashbyhq.com/posting-api/job-board/acme": {"jobs": [{"id": "3", "descriptionPlain": "Ashby text"}]},
            "apply.workable.com/api/v2/accounts/acme/jobs/4": {"description": "<p>Workable</p>", "requirements": "<p>Req</p>"},
        }
        ps._ASHBY_BOARDS.clear()
        with self.fake_fetch(payloads):
            self.assertEqual(ps.posting_text("greenhouse:acme", "1"), "Greenhouse text")
            self.assertEqual(ps.posting_text("lever:acme", "2"), "Lever intro\n\nYou will\n\n- Build reports\n\nOutro")
            self.assertEqual(ps.posting_text("ashby:acme", "3"), "Ashby text")
            self.assertEqual(ps.posting_text("workable:acme", "4"), "Workable\n\nReq")
            with self.assertRaises(LookupError):
                ps.posting_text("ashby:acme", "missing")


class TestAnalyzePending(unittest.TestCase):
    def test_signals_become_events_once(self):
        conn = make_db()
        client = FakeClient(analysis(crm=["Salesforce"], premier_recrutement=True, a_construire=True,
                                     outils=["Outreach", "Gong"]))
        stats = run(conn, client)
        self.assertEqual((stats["read"], stats["errors"], stats["events"]), (1, 0, 3))
        events = {r["type"]: r["detail"] for r in conn.execute("SELECT type, detail FROM events")}
        self.assertEqual(events["posting_crm_mismatch"], "Founding Account Executive : demande Salesforce, site : HubSpot")
        self.assertIn("posting_first_hire", events)
        self.assertIn("posting_build", events)
        # Le schéma JSON et la consigne partent bien dans la requête ; l'offre est encadrée comme une donnée.
        call = client.calls[0]
        self.assertEqual(call["output_config"]["format"]["schema"], ps.SCHEMA)
        self.assertIn("<offre>", call["messages"][0]["content"])
        # Déjà lue : rien à refaire, aucun appel le lendemain.
        self.assertEqual(run(conn, FakeClient(), day=TODAY + timedelta(days=1))["read"], 0)
        self.assertEqual(ps.signals_by_domain(conn)["acme.com"],
                         "CRM : Salesforce (site : HubSpot) · 1er recrutement : Founding Account Executive"
                         " · process à construire · outils : Gong, Outreach")

    def test_same_crm_as_site_is_not_an_event(self):
        conn = make_db()
        run(conn, FakeClient(analysis(crm=["HubSpot"])))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_no_crm_on_site_is_not_a_mismatch(self):
        conn = make_db(crm_on_site=None)
        run(conn, FakeClient(analysis(crm=["Salesforce"])))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_error_is_stored_and_retried_next_day(self):
        conn = make_db()
        stats = run(conn, FakeClient(APIError(529)))
        self.assertEqual((stats["read"], stats["errors"]), (0, 1))
        row = conn.execute("SELECT ok, error FROM posting_analyses").fetchone()
        self.assertEqual(row["ok"], 0)
        self.assertEqual(run(conn, FakeClient())["read"], 0)                  # pas retenté le même jour
        stats = run(conn, FakeClient(analysis()), day=TODAY + timedelta(days=1))
        self.assertEqual(stats["read"], 1)

    def test_refusal_and_truncated_answer_are_errors(self):
        conn = make_db(postings=(("greenhouse:acme", "1", "AE", "sales"), ("greenhouse:acme", "2", "SDR", "sales")))
        stats = run(conn, FakeClient(analysis(_stop="refusal"), analysis(_stop="max_tokens")))
        self.assertEqual((stats["read"], stats["errors"]), (0, 2))

    def test_auth_error_stops_the_run(self):
        conn = make_db()
        with self.assertRaises(ps.FatalAPIError):
            run(conn, FakeClient(APIError(401)))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM posting_analyses").fetchone()[0], 0)

    def test_filters(self):
        conn = make_db(postings=(("greenhouse:acme", "1", "AE", "sales"), ("greenhouse:acme", "2", "Engineer", "other")))
        self.assertEqual([p["posting_id"] for p in ps.pending(conn, TODAY)], ["1"])          # catégorie other ignorée
        self.assertEqual(ps.pending(conn, TODAY, tiers=("B",)), [])                          # acme est en A
        self.assertEqual(len(ps.pending(conn, TODAY, tiers=None)), 1)
        run(conn, FakeClient(analysis()))
        self.assertEqual(ps.pending(conn, TODAY), [])
        self.assertEqual(len(ps.pending(conn, TODAY, force=True)), 1)
        with patch.object(ps, "PROMPT_VERSION", "v2"):                                       # nouveau prompt : relu
            self.assertEqual(len(ps.pending(conn, TODAY)), 1)

    def test_closed_posting_leaves_the_summary(self):
        conn = make_db()
        run(conn, FakeClient(analysis(premier_recrutement=True)))
        conn.execute("UPDATE job_postings SET closed_on = ?", (str(TODAY),))
        self.assertEqual(ps.signals_by_domain(conn), {})


if __name__ == "__main__":
    unittest.main()
