"""Tests sans réseau : limite par hôte, retries 429/503, compteur de ralentissements, recherche d'offres non concluante,
fraîcheur « aucun board » (14 j). Lancer : python -m unittest tests.test_rate_limit -v"""
import io
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import date, timedelta
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import common  # noqa: E402
import db  # noqa: E402
import pipeline  # noqa: E402
import score_prospects as sp  # noqa: E402


class FakeResponse:
    def __init__(self, url, body="ok"):
        self.url, self.body = url, body

    def geturl(self):
        return self.url

    def read(self, n=-1):
        return self.body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(url, code, retry_after=None):
    """HTTPError avec un corps en mémoire (fermable sans ResourceWarning)."""
    hdrs = Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(url, code, "err", hdrs, io.BytesIO(b""))


def not_found(url):
    return http_error(url, 404)


class LimiterBase(unittest.TestCase):
    def setUp(self):
        common._HOSTS.clear()
        common.reset_throttle_stats()
        for name, value in (("HOST_MIN_INTERVAL", 0), ("HOST_MAX_CONCURRENT", 4)):
            patcher = patch.object(common, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(common._HOSTS.clear)
        self.addCleanup(common.reset_throttle_stats)


class TestLimiter(LimiterBase):
    def run_threads(self, urls, fake):
        with patch.object(common.urllib.request, "urlopen", fake):
            threads = [threading.Thread(target=common.fetch, args=(u, 5)) for u in urls]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

    def test_max_concurrent_per_host(self):
        lock, active, peak = threading.Lock(), {}, {}

        def fake(req, timeout):
            host = req.host
            with lock:
                active[host] = active.get(host, 0) + 1
                peak[host] = max(peak.get(host, 0), active[host])
            time.sleep(0.05)
            with lock:
                active[host] -= 1
            return FakeResponse(req.full_url)

        self.run_threads([f"https://a.example/{i}" for i in range(12)], fake)
        self.assertLessEqual(peak["a.example"], 4)
        self.assertGreater(peak["a.example"], 1)

    def test_other_host_not_blocked_by_saturated_host(self):
        release = threading.Event()
        started = threading.Semaphore(0)

        def fake(req, timeout):
            if req.host == "a.example":
                started.release()
                release.wait(5)
            return FakeResponse(req.full_url)

        with patch.object(common.urllib.request, "urlopen", fake):
            slow = [threading.Thread(target=common.fetch, args=(f"https://a.example/{i}", 5)) for i in range(6)]
            for t in slow:
                t.start()
            for _ in range(4):
                self.assertTrue(started.acquire(timeout=2))  # les 4 créneaux de A sont pris
            t0 = time.monotonic()
            common.fetch("https://b.example/x", 5)
            self.assertLess(time.monotonic() - t0, 1)
            release.set()
            for t in slow:
                t.join()

    def test_min_interval_between_starts(self):
        starts = []

        def fake(req, timeout):
            starts.append(time.monotonic())
            return FakeResponse(req.full_url)

        with patch.object(common, "HOST_MIN_INTERVAL", 0.05):
            self.run_threads([f"https://a.example/{i}" for i in range(4)], fake)
        starts.sort()
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertTrue(all(g >= 0.04 for g in gaps), gaps)


class TestRetries(LimiterBase):
    def fetch_with(self, errors, **kwargs):
        """fetch() sur un urlopen qui lève les erreurs données puis réussit ; renvoie (résultat, sleeps, appels)."""
        queue, sleeps, calls = list(errors), [], []

        def fake(req, timeout):
            calls.append(req.full_url)
            if queue:
                raise queue.pop(0)
            return FakeResponse(req.full_url)

        with patch.object(common.urllib.request, "urlopen", fake), patch.object(common.time, "sleep", sleeps.append):
            result = common.fetch("https://a.example/x", 5, **kwargs)
        return result, sleeps, calls

    def test_retry_after_honored(self):
        result, sleeps, calls = self.fetch_with([http_error("https://a.example/x", 429, 7)])
        self.assertEqual(result[1], "ok")
        self.assertEqual(sleeps, [7])
        self.assertEqual(len(calls), 2)

    def test_retry_after_http_date(self):
        stamp = (common.datetime.now(common.timezone.utc) + timedelta(seconds=10)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        _, sleeps, _ = self.fetch_with([http_error("https://a.example/x", 503, stamp)])
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(8 <= sleeps[0] <= 10, sleeps)

    def test_retry_after_capped(self):
        _, sleeps, _ = self.fetch_with([http_error("https://a.example/x", 429, 999)])
        self.assertEqual(sleeps, [common.RETRY_AFTER_MAX])

    def test_backoff_without_retry_after(self):
        _, sleeps, _ = self.fetch_with([http_error("https://a.example/x", 429), http_error("https://a.example/x", 429)])
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(1.5 <= sleeps[0] <= 2.0 and 3.0 <= sleeps[1] <= 3.5, sleeps)

    def test_three_throttle_retries_max(self):
        errors = [http_error("https://a.example/x", 429) for _ in range(10)]
        calls = []
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            queue = list(errors)

            def fake(req, timeout):
                calls.append(1)
                raise queue.pop(0)

            with patch.object(common.urllib.request, "urlopen", fake), patch.object(common.time, "sleep", lambda s: None):
                common.fetch("https://a.example/x", 5)
        self.assertEqual(ctx.exception.code, 429)
        self.assertEqual(len(calls), 4)  # 1 essai + 3 nouvelles tentatives
        self.assertEqual(common.throttle_stats(), {"a.example": 4})
        for err in errors:
            err.close()

    def test_other_transient_keeps_single_retry(self):
        _, sleeps, calls = self.fetch_with([urllib.error.URLError(TimeoutError())])
        self.assertEqual((sleeps, len(calls)), ([1.5], 2))
        with self.assertRaises(urllib.error.URLError):
            self.fetch_with([urllib.error.URLError(TimeoutError()), urllib.error.URLError(TimeoutError())])

    def test_no_retry_on_404_or_dns(self):
        err = http_error("https://a.example/x", 404)
        with self.assertRaises(urllib.error.HTTPError):
            self.fetch_with([err])
        err.close()
        with self.assertRaises(urllib.error.URLError):
            self.fetch_with([urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))])

    def test_slot_released_before_sleeping(self):
        seen = []

        def fake_sleep(delay):
            state = common._HOSTS["a.example"]
            got = [state.slots.acquire(blocking=False) for _ in range(common.HOST_MAX_CONCURRENT)]
            seen.append(all(got))  # tous les créneaux doivent être libres pendant l'attente
            for ok in got:
                if ok:
                    state.slots.release()

        queue = [http_error("https://a.example/x", 429, 1)]

        def fake(req, timeout):
            if queue:
                raise queue.pop(0)
            return FakeResponse(req.full_url)

        with patch.object(common.urllib.request, "urlopen", fake), patch.object(common.time, "sleep", fake_sleep):
            common.fetch("https://a.example/x", 5)
        self.assertEqual(seen, [True])


class TestThrottleCounter(LimiterBase):
    def test_counts_per_host_and_reset(self):
        queues = {"a.example": [429, 503, 429], "b.example": [503]}

        def fake(req, timeout):
            if queues[req.host]:
                raise http_error(req.full_url, queues[req.host].pop(0), 0)
            return FakeResponse(req.full_url)

        with patch.object(common.urllib.request, "urlopen", fake), patch.object(common.time, "sleep", lambda s: None):
            common.fetch("https://a.example/x", 5)
            common.fetch("https://b.example/y", 5)
        stats = common.throttle_stats()
        self.assertEqual(stats, {"a.example": 3, "b.example": 1})
        stats["a.example"] = 99  # copie : sans effet sur le compteur
        self.assertEqual(common.throttle_stats()["a.example"], 3)
        common.reset_throttle_stats()
        self.assertEqual(common.throttle_stats(), {})

    def test_is_error_flags_inconclusive_lookup(self):
        self.assertTrue(common.is_error({"ats": "", "titles": [], "board": "", "ok": False}))
        self.assertFalse(common.is_error({"ats": "", "titles": [], "board": "", "ok": True}))
        self.assertFalse(common.is_error({"hubspot": "Oui"}))

    def test_summary_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.md"
            pipeline.write_summary(path, [], [], [], None, 1, 0, today=date(2026, 11, 11), throttled={"a": 2, "b": 1})
            self.assertIn("- Requêtes ralenties (429/503) : 3", path.read_text(encoding="utf-8"))
            pipeline.write_summary(path, [], [], [], None, 1, 0, today=date(2026, 11, 11))
            self.assertNotIn("ralenties", path.read_text(encoding="utf-8"))


def rec(company="Bolster Capital", domain="bolster.io"):
    return {k: "" for k in sp.COLUMNS} | {"company": company, "website": domain, "domain": domain}


GH = "https://boards-api.greenhouse.io/v1/boards/"
OTHER_JOBS = '{"jobs": [{"title": "AE", "absolute_url": "https://other.com/1"}]}'


class TestFetchJobsInconclusive(unittest.TestCase):
    def fetcher(self, texts=None, failing=None):
        texts, failing = texts or {}, failing or {}

        def fake(url, timeout):
            if url in failing:
                raise failing[url]
            if url in texts:
                return texts[url]
            raise not_found(url)
        return fake

    def run_jobs(self, fake, record=None):
        with patch("score_prospects.fetch_text", side_effect=fake):
            return sp.fetch_jobs(record or rec(), {}, 5)

    def test_429_on_one_candidate_is_inconclusive(self):
        out = self.run_jobs(self.fetcher(failing={GH + "bolster/jobs": http_error(GH, 429)}))
        self.assertEqual(out, {"ats": "", "titles": [], "board": "", "ok": False})

    def test_all_404_is_ok_no_board(self):
        out = self.run_jobs(self.fetcher())
        self.assertEqual(out, {"ats": "", "titles": [], "board": "", "ok": True})

    def test_unparseable_json_is_absent(self):
        out = self.run_jobs(self.fetcher(texts={GH + "bolster/jobs": "<html>nope</html>"}))
        self.assertTrue(out["ok"])

    def test_timeout_on_candidate_is_inconclusive(self):
        out = self.run_jobs(self.fetcher(failing={GH + "bolster/jobs": urllib.error.URLError(TimeoutError())}))
        self.assertFalse(out["ok"])

    def test_greenhouse_name_check_timeout_is_inconclusive(self):
        fake = self.fetcher(texts={GH + "bolster/jobs": OTHER_JOBS},
                            failing={GH + "bolster": urllib.error.URLError(TimeoutError())})
        self.assertEqual(self.run_jobs(fake), {"ats": "", "titles": [], "board": "", "ok": False})

    def test_greenhouse_name_check_404_is_definitive(self):
        out = self.run_jobs(self.fetcher(texts={GH + "bolster/jobs": OTHER_JOBS}))
        self.assertEqual(out, {"ats": "", "titles": [], "board": "", "ok": True})

    def test_board_belongs_to_returns_none_on_unknown_error(self):
        with patch("score_prospects.fetch_json", side_effect=urllib.error.URLError(TimeoutError())):
            self.assertIsNone(sp.board_belongs_to(rec(), "greenhouse", "bolster", "x", {}, 5))
        with patch("score_prospects.fetch_json", side_effect=not_found(GH)):
            self.assertFalse(sp.board_belongs_to(rec(), "greenhouse", "bolster", "x", {}, 5))

    def test_later_verified_candidate_wins_despite_earlier_failure(self):
        record = rec("Acmecorp Inc", "acmecorp.io")
        jobs = '{"jobs": [{"title": "AE", "absolute_url": "https://acmecorp.io/1"}]}'
        lever = sp.ATS_API["lever"][0].format(slug="acmecorp")
        fake = self.fetcher(texts={GH + "acmecorp/jobs": jobs}, failing={lever: http_error(lever, 503)})
        out = self.run_jobs(fake, record)
        self.assertTrue(out["ok"])
        self.assertEqual(out["board"], "greenhouse:acmecorp")


class TestStoreAndFreshness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = db.connect(Path(tmp.name) / "h.db")
        self.addCleanup(self.conn.close)
        self.run = db.start_run(self.conn, {})
        self.today = date(2026, 11, 11)

    def test_put_records_inconclusive_lookup_as_failure(self):
        store = pipeline.HistoryStore(self.conn, "jobs", self.run, self.today, ["a.com", "b.com", "c.com"])
        store.put("a.com", {"ats": "", "titles": [], "board": "", "ok": False})
        store.put("b.com", {"ats": "", "titles": [], "board": "", "ok": True})
        store.put("c.com", {"erreur": "boom"})
        self.assertEqual([r["ok"] for r in self.conn.execute("SELECT ok FROM job_reads ORDER BY id")], [0, 1, 0])

    def test_runs_table_has_throttled(self):
        db.finish_run(self.conn, self.run, throttled=37)
        self.assertEqual(self.conn.execute("SELECT throttled FROM runs").fetchone()[0], 37)

    def test_migration_adds_column_to_old_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = db.connect(path)
            conn.execute("ALTER TABLE runs DROP COLUMN throttled")
            conn.commit()
            conn.close()
            conn = db.connect(path)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
            conn.close()
        self.assertIn("throttled", cols)

    def due(self):
        return db.due_for_jobs(self.conn, ["acme.com"], self.today) == ["acme.com"]

    def read(self, ats, ok, days_ago):
        day = (self.today - timedelta(days=days_ago)).isoformat()
        self.conn.execute("INSERT INTO job_reads (domain, ats, run_id, day, ok) VALUES ('acme.com', ?, ?, ?, ?)",
                          (ats, self.run, day, ok))
        self.conn.commit()

    def tier_a(self):
        db.record_score(self.conn, "acme.com", self.run, self.today - timedelta(days=30), {"tier": "A", "score": 90})

    def test_no_board_waits_14_days_even_for_tier_a(self):
        self.tier_a()
        self.read("", 1, 3)
        self.assertFalse(self.due())
        self.conn.execute("DELETE FROM job_reads")
        self.read("", 1, 13)
        self.assertFalse(self.due())
        self.conn.execute("DELETE FROM job_reads")
        self.read("", 1, 14)
        self.assertTrue(self.due())

    def test_known_board_tier_a_still_daily(self):
        self.tier_a()
        self.read("greenhouse:acme", 1, 1)
        self.assertTrue(self.due())

    def test_failure_retried_next_day(self):
        self.tier_a()
        self.read("", 0, 1)
        self.assertTrue(self.due())

    def test_policy_without_no_board_key_falls_back_to_cold(self):
        self.read("", 1, 8)
        policy = {"jobs": {"hot": 1, "cold": 7, "error": 1}, "scan": db.DEFAULT_REFRESH["scan"]}
        self.assertEqual(db.due_for_jobs(self.conn, ["acme.com"], self.today, policy), ["acme.com"])


if __name__ == "__main__":
    unittest.main()
