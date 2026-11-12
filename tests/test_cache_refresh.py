"""Tests sans réseau du Cache en mode --refresh (max_age_days=0)."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import Cache  # noqa: E402

NOW = datetime(2026, 11, 11, 12, 0, 0, 123456)  # avec microsecondes
OLD = (NOW - timedelta(seconds=5)).replace(microsecond=0).isoformat(timespec="seconds")


class CacheRefreshTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "cache.jsonl"

    def write_old(self):
        entry = {"domain": "old.com", "fetched_at": OLD, "data": {"hubspot": "Non"}}
        self.path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    def test_refresh_keeps_entries_of_current_run(self):
        c = Cache(self.path, 0, now=NOW)
        c.put("acme.com", {"hubspot": "Oui"})
        self.assertEqual(c.get("acme.com"), {"hubspot": "Oui"})
        self.assertEqual(c.data(), {"acme.com": {"hubspot": "Oui"}})
        self.assertEqual(c.stale(["acme.com"]), [])

    def test_refresh_expires_old_entries(self):
        self.write_old()
        c = Cache(self.path, 0, now=NOW)
        self.assertIsNone(c.get("old.com"))
        self.assertEqual(c.stale(["old.com"]), ["old.com"])
        c.put("new.com", {"hubspot": "Oui"})
        self.assertEqual(list(c.data()), ["new.com"])

    def test_compact_after_refresh_keeps_run_entries(self):
        self.write_old()
        c = Cache(self.path, 0, now=NOW)
        c.put("acme.com", {"hubspot": "Oui"})
        c.compact()
        lines = [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual([e["domain"] for e in lines], ["acme.com"])
        self.assertEqual(Cache(self.path, 0, now=NOW).data(), {"acme.com": {"hubspot": "Oui"}})


if __name__ == "__main__":
    unittest.main()
