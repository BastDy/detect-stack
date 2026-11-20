"""Tests sans réseau ni API de qualify_agent.py. Lancer : python -m unittest tests.test_qualify_agent -v"""
import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import qualify_agent as qa  # noqa: E402
from posting_signals import FatalAPIError  # noqa: E402

TODAY = date(2026, 11, 13)


def verdict(v="qualifie", **overrides):
    data = {"verdict": v, "dette_donnees": {"niveau": "fort", "preuves": ["HubSpot + Salesforce"]},
            "pression_ia": {"niveau": "inconnu" if v == "incertain" else "moyen", "preuves": []},
            "ia_native": False, "raison": "…", "angle": "Angle" if v == "qualifie" else "",
            "a_chercher": ["Annonce IA ?"] if v == "incertain" else []}
    data.update(overrides)
    return data


def text_response(data, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps(data))],
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=200, server_tool_use=None))


def search_response(stop="pause_turn", report=None, searches=2):
    content = [SimpleNamespace(type="server_tool_use", name="web_search", input={"query": "acme ai"})]
    if report is not None:
        content.append(SimpleNamespace(type="tool_use", name="report_signals", id="t1", input=report))
        stop = "tool_use"
    return SimpleNamespace(stop_reason=stop, content=content,
                           usage=SimpleNamespace(input_tokens=5000, output_tokens=500,
                                                 server_tool_use=SimpleNamespace(web_search_requests=searches)))


class FakeClient:
    """Imite client.beta.messages.create : renvoie les réponses dans l'ordre, ou lève une exception."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


SIGNALS = {"signaux": [{"type": "initiative_ia", "date": "2026-10-02", "resume": "Lance un assistant IA",
                        "url": "https://acme.com/blog/ai", "citation": "we launched"},
                       {"type": "nouveau_leader", "date": "", "resume": "Nouveau CRO",
                        "url": "https://news.example/cro", "citation": "joins as CRO"}],
           "bilan": "IA trouvée."}


def make_db():
    conn = db.connect(":memory:")
    day = str(TODAY)
    conn.execute("INSERT INTO companies (domain, name, employees, city, province, country) "
                 "VALUES ('acme.com', 'Acme', 80, 'Toronto', 'ON', 'Canada')")
    conn.execute("INSERT INTO scores (domain, run_id, day, tier, score, detail) "
                 "VALUES ('acme.com', 1, ?, 'B', 9, 'SaaS +3')", (day,))
    conn.execute("INSERT INTO companies (domain, name) VALUES ('zeta.com', 'Zeta')")
    conn.execute("INSERT INTO scores (domain, run_id, day, tier, score) VALUES ('zeta.com', 1, ?, 'C', 4)", (day,))
    conn.execute("INSERT INTO stack (domain, tool, first_seen, last_seen) VALUES ('acme.com', 'HubSpot', ?, ?)", (day, day))
    conn.execute("INSERT INTO events (day, domain, type, detail) VALUES (?, 'acme.com', 'leadership_hire', 'VP Sales')",
                 (day,))
    conn.commit()
    return conn


class DossierTests(unittest.TestCase):
    def test_contains_known_facts(self):
        text = qa.dossier(make_db(), "acme.com", TODAY)
        for fact in ("Acme (acme.com)", "80 salariés", "tier B", "HubSpot", "leadership_hire : VP Sales"):
            self.assertIn(fact, text)


class ConditionTests(unittest.TestCase):
    def account(self):
        return {"domain": "acme.com", "name": "Acme", "tier": "B", "dossier": "Entreprise : Acme"}

    def test_clear_verdict_does_not_search(self):
        client = FakeClient(text_response(verdict("qualifie")))
        result = qa.run_account(client, qa.MODEL, self.account(), TODAY)
        self.assertIsNone(result["search"])
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("tools", client.calls[0])

    def test_uncertain_searches_then_requalifies(self):
        client = FakeClient(text_response(verdict("incertain")), search_response(report=SIGNALS),
                            text_response(verdict("qualifie")))
        result = qa.run_account(client, qa.MODEL, self.account(), TODAY)
        self.assertEqual(result["first"]["verdict"], "incertain")
        self.assertEqual(result["verdict"]["verdict"], "qualifie")
        self.assertEqual(len(client.calls), 3)
        self.assertIn("Annonce IA ?", client.calls[1]["messages"][0]["content"])   # cherche ce qui manque
        self.assertIn("Lance un assistant IA", client.calls[2]["messages"][0]["content"])
        self.assertEqual(result["usage"]["web_searches"], 2)

    def test_no_search_flag(self):
        client = FakeClient(text_response(verdict("incertain")))
        result = qa.run_account(client, qa.MODEL, self.account(), TODAY, search=False)
        self.assertEqual(result["verdict"]["verdict"], "incertain")
        self.assertEqual(len(client.calls), 1)


class SearchLoopTests(unittest.TestCase):
    def test_pause_turn_resumes_without_new_user_message(self):
        client = FakeClient(search_response(), search_response(report=SIGNALS))
        found, usage = qa.find_signals(client, qa.MODEL, "Acme", "acme.com", ["IA ?"])
        self.assertEqual(len(found["signaux"]), 2)
        second = client.calls[1]["messages"]
        self.assertEqual([m["role"] for m in second], ["user", "assistant"])
        self.assertEqual(usage["web_searches"], 4)

    def test_nudges_once_then_fails(self):
        client = FakeClient(search_response(stop="end_turn"), search_response(report={"signaux": [], "bilan": "rien"}))
        found, _ = qa.find_signals(client, qa.MODEL, "Acme", "acme.com", [])
        self.assertEqual(found["signaux"], [])
        self.assertEqual(client.calls[1]["messages"][-1]["role"], "user")
        client = FakeClient(search_response(stop="end_turn"), search_response(stop="end_turn"))
        with self.assertRaises(RuntimeError):
            qa.find_signals(client, qa.MODEL, "Acme", "acme.com", [])


class RecordTests(unittest.TestCase):
    def test_pending_skips_recent_and_filters_tiers(self):
        conn = make_db()
        self.assertEqual([a["domain"] for a in qa.pending(conn, TODAY)], ["acme.com"])
        qa.record(conn, {"domain": "acme.com"}, TODAY - timedelta(days=5), qa.MODEL,
                  {"verdict": verdict(), "usage": {}})
        self.assertEqual(qa.pending(conn, TODAY), [])
        self.assertEqual(len(qa.pending(conn, TODAY, force=True)), 1)
        self.assertEqual(len(qa.pending(conn, TODAY + timedelta(days=40))), 1)

    def test_failed_account_retried(self):
        conn = make_db()
        qa.record(conn, {"domain": "acme.com"}, TODAY, qa.MODEL, error="boom")
        self.assertEqual(len(qa.pending(conn, TODAY)), 1)

    def test_signals_become_events_once(self):
        conn = make_db()
        result = {"first": verdict("incertain"), "verdict": verdict(), "search": SIGNALS, "usage": {}}
        self.assertEqual(qa.record(conn, {"domain": "acme.com"}, TODAY, qa.MODEL, result), 2)
        rows = conn.execute("SELECT day, type, ref FROM events WHERE type LIKE 'web_%' ORDER BY type").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("2026-10-02", "web_initiative_ia", "https://acme.com/blog/ai"),
                                                    (str(TODAY), "web_nouveau_leader", "https://news.example/cro")])
        self.assertEqual(qa.record(conn, {"domain": "acme.com"}, TODAY + timedelta(days=1), qa.MODEL, result), 0)
        row = conn.execute("SELECT verdict, searched FROM account_qualifications").fetchone()
        self.assertEqual(tuple(row), ("qualifie", 1))


class RunTests(unittest.TestCase):
    def test_qualify_pending_end_to_end(self):
        conn = make_db()
        client = FakeClient(text_response(verdict("incertain")), search_response(report=SIGNALS),
                            text_response(verdict("qualifie")))
        stats = qa.qualify_pending(conn, TODAY, client=client, log=lambda *_: None)
        self.assertEqual((stats["done"], stats["searched"], stats["events"]), (1, 1, 2))
        self.assertEqual(stats["verdicts"], {"qualifie": 1})

    def test_fatal_error_stops_run(self):
        class APIError(Exception):
            status_code = 401
        conn = make_db()
        with self.assertRaises(FatalAPIError):
            qa.qualify_pending(conn, TODAY, client=FakeClient(APIError("bad key")), log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
