"""Tests sans réseau : Workable (slugs jamais devinés, limiteur dédié) et non-écrasement des sorties du jour.
Lancer : python -m unittest tests.test_workable_and_outputs -v"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402
import pipeline  # noqa: E402
import score_prospects as sp  # noqa: E402

REC = {"company": "Acmecorp", "domain": "acmecorp.com", "website": "acmecorp.com"}


class AtsCandidatesTests(unittest.TestCase):
    def test_guessed_slugs_never_use_workable(self):
        candidates, source = sp.ats_candidates(REC, {})
        self.assertEqual(source, "slug deviné")
        self.assertTrue(candidates)
        self.assertEqual({p for p, _ in candidates}, {"greenhouse", "lever", "ashby"})

    def test_guessed_fetch_never_calls_workable(self):
        urls = []

        def fake_fetch(url, timeout):
            urls.append(url)
            raise OSError("pas de board")

        with patch.object(sp, "fetch_text", fake_fetch):
            sp.fetch_jobs(REC, {}, 1)
        self.assertTrue(urls)
        self.assertFalse(any("workable" in u for u in urls))

    def test_site_linked_workable_board_is_still_read(self):
        body = '{"name": "Acmecorp", "jobs": [{"title": "RevOps Manager", "shortcode": "A1", "url": "https://x/1"}]}'
        urls = []

        def fake_fetch(url, timeout):
            urls.append(url)
            return body

        detection = {"ats": "workable:acmecorp"}
        candidates, source = sp.ats_candidates(REC, detection)
        self.assertEqual((candidates, source), ([["workable", "acmecorp"]], "lien du site"))
        with patch.object(sp, "fetch_text", fake_fetch):
            result = sp.fetch_jobs(REC, detection, 1)
        self.assertEqual(urls, ["https://apply.workable.com/api/v1/widget/accounts/acmecorp"])
        self.assertEqual(result["board"], "workable:acmecorp")
        self.assertTrue(result["ok"])


class HostLimitTests(unittest.TestCase):
    def setUp(self):
        common._HOSTS.clear()
        self.addCleanup(common._HOSTS.clear)

    def test_workable_override(self):
        state = common._host_state("apply.workable.com")
        self.assertEqual(state.min_interval, 1.5)
        self.assertTrue(state.slots.acquire(blocking=False))
        self.assertFalse(state.slots.acquire(blocking=False))  # 1 seule requête concurrente

    def test_other_hosts_keep_defaults(self):
        state = common._host_state("boards-api.greenhouse.io")
        self.assertEqual(state.min_interval, common.HOST_MIN_INTERVAL)
        got = [state.slots.acquire(blocking=False) for _ in range(common.HOST_MAX_CONCURRENT + 1)]
        self.assertEqual(got, [True] * common.HOST_MAX_CONCURRENT + [False])

    def test_acquire_spaces_workable_requests(self):
        sleeps = []
        clock = [100.0]
        with patch.object(common.time, "monotonic", lambda: clock[0]), \
                patch.object(common.time, "sleep", lambda d: (sleeps.append(d), clock.__setitem__(0, clock[0] + d))):
            common._acquire("apply.workable.com").slots.release()
            common._acquire("apply.workable.com").slots.release()
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 1.5, places=3)


class OutputNamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        patcher = patch.object(pipeline, "OUTPUT", self.out)
        patcher.start()
        self.addCleanup(patcher.stop)

    def touch(self, *names):
        for n in names:
            (self.out / n).write_text("x", encoding="utf-8")

    def test_first_run_of_the_day_keeps_plain_name(self):
        csv_path, summary = pipeline.next_output_paths(date(2026, 11, 12))
        self.assertEqual((csv_path.name, summary.name), ("prospects-2026-11-12.csv", "resume-2026-11-12.md"))

    def test_never_overwrites_existing_output(self):
        day = date(2026, 11, 12)
        self.touch("prospects-2026-11-12.csv", "resume-2026-11-12.md")
        self.assertEqual(pipeline.next_output_paths(day)[0].name, "prospects-2026-11-12-2.csv")
        self.touch("prospects-2026-11-12-2.csv", "resume-2026-11-12-2.md")
        csv_path, summary = pipeline.next_output_paths(day)
        self.assertEqual((csv_path.name, summary.name), ("prospects-2026-11-12-3.csv", "resume-2026-11-12-3.md"))

    def test_existing_summary_alone_also_blocks_the_name(self):
        self.touch("resume-2026-11-12.md")
        self.assertEqual(pipeline.next_output_paths(date(2026, 11, 12))[1].name, "resume-2026-11-12-2.md")

    def test_previous_run_includes_earlier_run_of_same_day(self):
        self.touch("prospects-2026-11-11.csv", "prospects-2026-11-12.csv")
        current = pipeline.next_output_paths(date(2026, 11, 12))[0]
        self.assertEqual(pipeline.previous_run(current).name, "prospects-2026-11-12.csv")

    def test_previous_run_never_itself_and_ignores_later_and_latest(self):
        self.touch("prospects-2026-11-12.csv", "prospects-2026-11-12-2.csv", "prospects-2026-11-13.csv",
                   "prospects-latest.csv")
        self.assertEqual(pipeline.previous_run(self.out / "prospects-2026-11-12-2.csv").name, "prospects-2026-11-12.csv")
        self.assertIsNone(pipeline.previous_run(self.out / "prospects-2026-11-12.csv"))

    def test_sort_order_is_chronological(self):
        self.touch("prospects-2026-11-12.csv", "prospects-2026-11-12-2.csv", "prospects-2026-11-12-10.csv",
                   "prospects-2026-11-13.csv")
        self.assertEqual(pipeline.previous_run(self.out / "prospects-2026-11-13.csv").name, "prospects-2026-11-12-10.csv")
        self.assertEqual(pipeline.previous_run(self.out / "prospects-2026-11-12-10.csv").name, "prospects-2026-11-12-2.csv")

    def test_no_previous_run(self):
        self.assertIsNone(pipeline.previous_run(self.out / "prospects-2026-11-12.csv"))


if __name__ == "__main__":
    unittest.main()
