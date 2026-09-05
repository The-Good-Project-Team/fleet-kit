#!/usr/bin/env python3
"""test_alert_store.py -- the alarm-noise regression suite.

Each test below is a replay of a REAL page that reached a human, or a real page that must keep
reaching one. The first two are the 2026-09-05 incidents; the rest guard the properties that
make the first two stay fixed.
"""
import json, subprocess, sys, tempfile, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alert_store  # noqa: E402


class AlertStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = str(Path(self.tmp.name) / "alerts.json")
        self.addCleanup(self.tmp.cleanup)

    def rec(self, check="budget_read", problem="meter unreadable", sev="transient", **kw):
        return alert_store.record(check, problem, sev, state_file=self.state, **kw)

    # ---- the two incidents -------------------------------------------------

    def test_container_restart_does_not_page(self):
        """2026-09-05 02:45 UTC: philanthropy restarted; the meter read failed for ~6 min and
        paged 'meter UNREADABLE (label=parse_fail)'. Being unable to observe is not a fault."""
        v = self.rec(sev="transient")
        self.assertFalse(v["page"], f"transient paged on first sight: {v['reason']}")
        # Still blind two ticks later (~10 min in) -- a restart, still not an outage.
        v = self.rec(sev="transient")
        self.assertFalse(v["page"], f"transient paged while still young: {v['reason']}")

    def test_one_outage_across_two_handles_pages_once(self):
        """One maxx outage paged 11 times because anchor_staleness_check latches PER HANDLE and
        runs for both reif and reif_tgp every 5 minutes. Identity is (check, problem)."""
        pages = 0
        for _ in range(6):                       # 30 minutes of 5-minute ticks
            for handle in ("reif", "reif_tgp"):  # both handles, same underlying outage
                v = alert_store.record("anchor_staleness", "anchor stale", "critical",
                                       handle=handle, state_file=self.state)
                pages += 1 if v["page"] else 0
        self.assertEqual(pages, 1, f"one outage paged {pages} times")
        snap = alert_store.snapshot(self.state)
        self.assertEqual(len(snap["open"]), 1)
        self.assertCountEqual(snap["open"][0]["handles"], ["reif", "reif_tgp"])

    # ---- the properties that keep them fixed -------------------------------

    def test_critical_pages_immediately(self):
        """Suppression must never touch the class that matters. A rejected probe token is
        broken until a person acts, so it pages on first observation, no debounce."""
        v = self.rec(check="probe_token", problem="token rejected", sev="critical")
        self.assertTrue(v["page"], f"critical did not page: {v['reason']}")

    def test_degraded_pages_only_after_it_persists(self):
        """Real but possibly self-healing: stay quiet one tick, page if it is still true."""
        v = self.rec(sev="degraded")
        self.assertFalse(v["page"], "degraded paged on first sight")
        alert_store.DEGRADED_MIN_SEC, orig = 0, alert_store.DEGRADED_MIN_SEC
        try:
            v = self.rec(sev="degraded")
            self.assertTrue(v["page"], f"degraded never paged: {v['reason']}")
        finally:
            alert_store.DEGRADED_MIN_SEC = orig

    def test_transient_escalates_when_we_stay_blind(self):
        """A container down 6 minutes is a restart. Down 30 is an outage -- that must page,
        or this whole change becomes a way to hide real breakage."""
        orig = alert_store.TRANSIENT_ESCALATE_SEC
        alert_store.TRANSIENT_ESCALATE_SEC = 0
        try:
            v = self.rec(sev="transient")
            self.assertTrue(v["page"], f"blind past the window never escalated: {v['reason']}")
            self.assertEqual(v["severity"], "degraded")
        finally:
            alert_store.TRANSIENT_ESCALATE_SEC = orig

    def test_severity_rises_but_never_falls(self):
        """Once critical, a later tick that can only prove 'transient' must not downgrade an
        open alarm into silence."""
        self.rec(sev="critical")
        v = self.rec(sev="transient")
        self.assertEqual(v["severity"], "critical")

    def test_recovery_reports_whether_a_human_was_told(self):
        self.rec(sev="transient")                       # never paged
        r = alert_store.resolve("budget_read", "meter unreadable", state_file=self.state)
        self.assertTrue(r["was_open"])
        self.assertFalse(r["was_paged"], "would send 'recovered' for a never-sent alarm")

    def test_recurrence_after_resolve_pages_again(self):
        """A resolved condition coming back is a NEW problem, not a duplicate."""
        self.rec(check="probe_token", problem="token rejected", sev="critical")
        alert_store.resolve("probe_token", "token rejected", state_file=self.state)
        v = self.rec(check="probe_token", problem="token rejected", sev="critical")
        self.assertTrue(v["page"], "recurrence after recovery stayed silent")

    def test_snapshot_tells_the_fleet_when_budget_is_untrustworthy(self):
        """The self-heal contract: gru reads budget_safe instead of silently falling back to
        a constant, which is what cost an entire account for 60h."""
        self.assertTrue(alert_store.snapshot(self.state)["budget_safe"])
        self.rec(sev="transient")
        self.assertTrue(alert_store.snapshot(self.state)["budget_safe"],
                        "one unreadable tick throttled the whole fleet")
        self.rec(check="anchor", problem="anchor stale", sev="critical")
        snap = alert_store.snapshot(self.state)
        self.assertFalse(snap["budget_safe"], "real alarm left budget marked safe")
        self.assertEqual(snap["worst"], "critical")

    def test_corrupt_state_pages_rather_than_going_quiet(self):
        """Fail OPEN. A broken store must never be a muted pager."""
        Path(self.state).write_text("{not json")
        v = self.rec(sev="critical")
        self.assertTrue(v["page"])

    def test_concurrent_writers_do_not_lose_an_alarm(self):
        """Six checks share 2/5/10/15-minute crons and collide on the same minute."""
        script = Path(__file__).resolve().parent / "alert_store.py"
        procs = [subprocess.Popen(
            [sys.executable, str(script), "record", "--check", f"c{i}",
             "--problem", "p", "--severity", "critical"],
            env={"FLEET_ALERT_STATE_FILE": self.state, "PATH": "/usr/bin:/bin"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(8)]
        for p in procs:
            p.wait(timeout=30)
        self.assertEqual(len(alert_store.snapshot(self.state)["open"]), 8,
                         "a concurrent write was lost")


if __name__ == "__main__":
    unittest.main(verbosity=2)
