"""gh#553 VP review round 1, fix 3: Stats' backlog_history chart must paint in under 1s, but
its own `_cached()` TTL only ever refreshes on the request that happens to miss it -- so the
first viewer after any 120s gap pays the full ~27s of live `gh issue list --limit 1000` +
`gh pr list --limit 500` calls (measured on philanthropy). Fixed by precomputing it on a
timer (`refresh_backlog_history_forever`) instead of on the request.

RED without the fix: `/api/stats/backlog_history` always calls `_gh` itself on a cache miss;
there is no background refresher, so any request more than 120s after the last one blocks on
the live gh calls.
"""
import json
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

FAKE_ISSUES = json.dumps([
    {"number": 1, "createdAt": "2026-08-20T00:00:00Z", "closedAt": None},
    {"number": 2, "createdAt": "2026-08-25T00:00:00Z", "closedAt": "2026-09-01T00:00:00Z"},
])
FAKE_PRS = json.dumps([
    {"createdAt": "2026-08-28T00:00:00Z", "mergedAt": "2026-08-29T00:00:00Z"},
])


class BacklogHistoryPayload(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("fleet_view_server", None)
        import fleet_view_server as fvs
        self.fvs = fvs

    def test_payload_shape_and_cacheable_on_real_data(self):
        with unittest.mock.patch.object(self.fvs, "_gh",
                                         side_effect=[FAKE_ISSUES, FAKE_PRS]):
            payload, cacheable = self.fvs._backlog_history_payload(14)
        self.assertTrue(cacheable)
        self.assertEqual(set(payload), {"days", "new_prs", "merged_prs"})

    def test_not_cacheable_when_gh_fails(self):
        # _gh() swallows a failed/timed-out call into "" -- must never be cached, or a
        # transient gh blip pins a false flatline over the real trend for the whole TTL.
        with unittest.mock.patch.object(self.fvs, "_gh", return_value=""):
            _, cacheable = self.fvs._backlog_history_payload(14)
        self.assertFalse(cacheable)


class CachedServesAWarmEntryWithoutCallingGh(unittest.TestCase):
    """Proves `_cached()`'s TTL-hit branch: given an entry already sitting in `_TTL_CACHE`
    with a fresh timestamp, a request through `_cached()` reads it back verbatim and never
    calls produce() (and so never calls `_gh`). This does NOT exercise
    `refresh_backlog_history_forever` itself -- that entry is hand-written below, not produced
    by the timer thread. See RefreshBacklogHistoryForever for coverage of the timer loop's own
    body (gh#860)."""

    def setUp(self):
        sys.modules.pop("fleet_view_server", None)
        import fleet_view_server as fvs
        self.fvs = fvs
        self.fvs._TTL_CACHE.clear()

    def test_request_hits_a_cache_a_background_refresh_already_warmed(self):
        calls = []

        def fake_gh(*args, **kwargs):
            calls.append(args)
            return FAKE_ISSUES if args[0] == "issue" else FAKE_PRS

        with unittest.mock.patch.object(self.fvs, "_gh", side_effect=fake_gh):
            value, cacheable = self.fvs._backlog_history_payload(self.fvs._BACKLOG_HISTORY_DAYS)
            self.assertTrue(cacheable)
            key = f"backlog_history:{self.fvs._BACKLOG_HISTORY_DAYS}"
            with self.fvs._TTL_LOCK:
                self.fvs._TTL_CACHE[key] = (time.time(), value)

        calls.clear()
        # Now read it back through the same _cached() path the request handler uses --
        # a fresh entry must short-circuit produce() entirely.
        with unittest.mock.patch.object(self.fvs, "_gh", side_effect=fake_gh):
            served = self.fvs._cached(
                key, 120.0,
                lambda: self.fvs._backlog_history_payload(self.fvs._BACKLOG_HISTORY_DAYS))
        self.assertEqual(served, value)
        self.assertEqual(calls, [], "a warm precomputed entry must not trigger a live gh call")


class RefreshBacklogHistoryForever(unittest.TestCase):
    """Calls `refresh_backlog_history_forever` itself -- not a hand-rolled copy of its body --
    so a wrong cache key, a dropped lock, or a silently-eaten exception would fail here (gh#860,
    marie PRD Part C4)."""

    def setUp(self):
        sys.modules.pop("fleet_view_server", None)
        import fleet_view_server as fvs
        self.fvs = fvs
        self.fvs._TTL_CACHE.clear()

    def _run_one_iteration(self):
        # time.sleep(interval_s) is the last call in each pass of the function's `while True`
        # loop; raising from it stops the loop after exactly one iteration without touching
        # the function under test.
        with unittest.mock.patch.object(self.fvs.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                self.fvs.refresh_backlog_history_forever(interval_s=0)

    def test_writes_the_cache_on_a_cacheable_result(self):
        payload = {"days": [], "new_prs": [], "merged_prs": []}
        with unittest.mock.patch.object(self.fvs, "_backlog_history_payload",
                                         return_value=(payload, True)):
            self._run_one_iteration()
        key = f"backlog_history:{self.fvs._BACKLOG_HISTORY_DAYS}"
        self.assertIn(key, self.fvs._TTL_CACHE)
        _, value = self.fvs._TTL_CACHE[key]
        self.assertEqual(value, payload)

    def test_leaves_the_cache_untouched_when_not_cacheable(self):
        with unittest.mock.patch.object(self.fvs, "_backlog_history_payload",
                                         return_value=({"days": []}, False)):
            self._run_one_iteration()
        key = f"backlog_history:{self.fvs._BACKLOG_HISTORY_DAYS}"
        self.assertNotIn(key, self.fvs._TTL_CACHE)

    def test_logs_via_log_sync_error_and_keeps_looping_on_a_raised_exception(self):
        boom = RuntimeError("boom")
        with unittest.mock.patch.object(self.fvs, "_backlog_history_payload",
                                         side_effect=boom), \
             unittest.mock.patch.object(self.fvs, "log_sync_error") as mock_log:
            self._run_one_iteration()
        mock_log.assert_called_once_with(boom)


class MainWiresUpTheBackgroundRefresher(unittest.TestCase):
    def test_main_starts_the_refresher_thread_and_warms_once_before_serving(self):
        src = (KIT / "scripts" / "fleet_view_server.py").read_text()
        main_body = src[src.index("\ndef main() -> int:"):]
        self.assertIn("_backlog_history_payload(_BACKLOG_HISTORY_DAYS)", main_body,
                      "main() must warm the cache once before serve_forever(), same pattern "
                      "as STATE.gh's own synchronous poll")
        self.assertIn("threading.Thread(target=refresh_backlog_history_forever", main_body)
        self.assertLess(main_body.index("refresh_backlog_history_forever"),
                         main_body.index("server.serve_forever()"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
