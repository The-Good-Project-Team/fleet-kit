"""Regression test for gh#265 (marie PRD Part C4, AC3).

kpiTitleSuffix() (fleet_view.html) renders "· N <unit> today" next to every member's name in
the sidebar and on the Home page, but the count behind it was a rolling 24h sum from
`/api/kpi?hours=24` -- a run from late yesterday stayed counted as "today" for hours after the
real UTC day turned over. `/api/kpi?day=1` is the fix: it must count only runs recorded since
the current UTC midnight, exactly like `fleet_db.spend(..., since=fleet_db.utc_day_start())`
does for AC1/AC2 (see test_fleet_db.py's fleet_db-level tests for those). This test drives the
real HTTP route end to end -- the same fleet_view_server.py process a browser talks to -- rather
than re-testing the pure functions it's built from.

Run: python3 scripts/test_kpi_calendar_day_gh265.py
"""
from __future__ import annotations

import http.client
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import fleet_db  # noqa: E402
import fleet_view_server as fvs  # noqa: E402


class KpiCalendarDayTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), fvs.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.thread.join, timeout=5)

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        import json
        return json.loads(body)

    def test_day_mode_counts_only_runs_since_utc_midnight(self):
        midnight = fleet_db.utc_day_start()
        # gru's real KPI pattern ("PRs shipped") counts an "Opened pull/N, auto-merge armed."
        # outcome as 1 -- see fleet_kpi.py's _PR_SHIPPED_PATTERNS.
        outcome = "Opened pull/4488, auto-merge armed."
        before_midnight = {"member": "gru", "ts": midnight - 3600, "outcome": outcome}
        after_midnight = {"member": "gru", "ts": midnight + 3600, "outcome": outcome}
        fvs.STATE.runs = [before_midnight, after_midnight]

        day = self._get("/api/kpi?day=1")
        gru_day = next(k for k in day["kpi"] if k["member"] == "gru")
        self.assertEqual(gru_day["total"], 1,
                          f"day=1 total={gru_day['total']}, want 1 -- the pre-midnight run must not count")
        self.assertTrue(day.get("day"), "response must flag itself as a calendar-day reading")

        rolling = self._get("/api/kpi?hours=24")
        gru_rolling = next(k for k in rolling["kpi"] if k["member"] == "gru")
        self.assertEqual(gru_rolling["total"], 2,
                          "sanity check: the same two runs both fall inside a real rolling-24h window")

    def test_explicit_hours_request_keeps_its_documented_shape(self):
        """AC5 / README.md:428-429: an explicit hours= request is untouched by the day= addition."""
        fvs.STATE.runs = []
        resp = self._get("/api/kpi?hours=24")
        self.assertEqual(resp.get("hours"), 24.0)
        self.assertNotIn("day", resp)


if __name__ == "__main__":
    unittest.main()
