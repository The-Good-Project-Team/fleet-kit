#!/usr/bin/env python3
"""test_prod_health_check.py -- gh#4898 AC2/AC4/AC5 and gh#727 AC1-AC6/AC9's own tests.

AC2 ("first run sends nothing, second triggers exactly one notification, not two, not
zero"), AC4 ("a failure of any one probe is attributable to which probe failed") and
AC5 ("the heartbeat pages independently of whether the HTTP probes pass") are each a
named test below, run against synthetic ProbeResult/HeartbeatResult sequences -- no
network, no live philanthropy.org.

gh#727's own acceptance criteria add: the flap-guard now also gates exactly one
issue-filing command (AC2), dedup by open issue title (AC4), a non-closing recovery
comment (AC5), the exact incident label set (AC6), an 8s slow-probe budget distinguishable
from a non-200 status (AC3), and the missing-bypass-token SKIPPED behavior (AC9) --
tested with a FakeGh stand-in for `gh issue {create,list,comment}`, same "pure builders,
mocked execution" split test_journey_issue_filer.py's FakeGh uses.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alert_store  # noqa: E402
import prod_health_check as phc  # noqa: E402


class FakeGh:
    """Replays enough of `gh issue {create,list,comment}` to drive the incident-filing
    functions for real, no network."""

    def __init__(self):
        self.issues = {}  # number -> {"title": str, "body": str, "open": bool, "comments": [str]}
        self._next = 100

    def __call__(self, cmd: list[str]) -> tuple[int, str]:
        assert cmd[0] == "gh" and cmd[1] == "issue"
        sub = cmd[2]
        if sub == "create":
            title, body = cmd[cmd.index("--title") + 1], cmd[cmd.index("--body") + 1]
            n = self._next
            self._next += 1
            self.issues[n] = {"title": title, "body": body, "open": True, "comments": []}
            return 0, f"https://github.com/x/y/issues/{n}"
        if sub == "list":
            open_issues = [
                {"number": n, "title": v["title"]} for n, v in self.issues.items() if v["open"]
            ]
            return 0, json.dumps(open_issues)
        if sub == "comment":
            n, body = int(cmd[3]), cmd[cmd.index("--body") + 1]
            self.issues[n]["comments"].append(body)
            return 0, "commented"
        raise AssertionError(f"unexpected gh subcommand: {sub}")


def ok(name: str) -> phc.ProbeResult:
    return phc.ProbeResult(name, True, "200")


def fail(name: str, detail: str = "http 503") -> phc.ProbeResult:
    return phc.ProbeResult(name, False, detail)


FRESH_HEARTBEAT = phc.HeartbeatResult(True, False, "age=1.0min ok=True")
STALE_HEARTBEAT = phc.HeartbeatResult(True, True, "age=20.0min ok=False")
UNREACHABLE_HEARTBEAT = phc.HeartbeatResult(False, False, "ConnectionError: refused")


class EvaluateTests(unittest.TestCase):
    """AC1/AC4: the pure decision function, given synthetic probe sequences."""

    def test_all_healthy_pages_nothing(self):
        probes = [ok("home"), ok("search"), ok("report")]
        self.assertEqual(phc.evaluate(probes, FRESH_HEARTBEAT), [])

    def test_one_probe_down_is_attributed_to_that_probe_only(self):
        probes = [ok("home"), fail("search", "http 403"), ok("report")]
        alerts = phc.evaluate(probes, FRESH_HEARTBEAT)
        self.assertEqual([a.problem for a in alerts], ["http_probe:search"])
        self.assertIn("http 403", alerts[0].title)
        # The body names the OTHER probes' results too, so a reader isn't left guessing
        # whether the rest of the site is also down.
        self.assertIn("home=ok", alerts[0].body)
        self.assertIn("report=ok", alerts[0].body)

    def test_two_probes_down_produce_two_separate_alerts(self):
        probes = [fail("home", "timeout"), ok("search"), fail("report", "http 403")]
        alerts = phc.evaluate(probes, FRESH_HEARTBEAT)
        self.assertEqual({a.problem for a in alerts}, {"http_probe:home", "http_probe:report"})

    def test_stale_heartbeat_pages_even_when_all_probes_ok(self):
        """AC5: an app serving 200s while its own canary has stopped running must still page."""
        probes = [ok("home"), ok("search"), ok("report")]
        alerts = phc.evaluate(probes, STALE_HEARTBEAT)
        self.assertEqual([a.problem for a in alerts], ["canary_heartbeat_stale"])

    def test_stale_heartbeat_and_failing_probes_both_page_independently(self):
        probes = [fail("home"), ok("search"), ok("report")]
        alerts = phc.evaluate(probes, STALE_HEARTBEAT)
        self.assertEqual({a.problem for a in alerts},
                          {"http_probe:home", "canary_heartbeat_stale"})

    def test_heartbeat_unreachable_does_not_double_page_a_probe_outage(self):
        """If dino can't even reach the heartbeat endpoint, that's the same outage the home
        probe already names -- alerting both would blame two things for one cause."""
        probes = [fail("home"), ok("search"), ok("report")]
        alerts = phc.evaluate(probes, UNREACHABLE_HEARTBEAT)
        self.assertEqual([a.problem for a in alerts], ["http_probe:home"])


class VerdictLineTests(unittest.TestCase):
    """AC1/AC6: the line status_data.py's classify() rolls up onto the status page."""

    def test_healthy_line_classifies_ok(self):
        import status_data
        line = phc.verdict_line([])
        self.assertEqual(status_data.classify(line), status_data.OK)

    def test_failed_line_classifies_bad(self):
        import status_data
        alerts = phc.evaluate([fail("home")], FRESH_HEARTBEAT)
        line = phc.verdict_line(alerts)
        self.assertEqual(status_data.classify(line), status_data.BAD)
        self.assertIn("http_probe:home", line)


class TwoTickPagingTest(unittest.TestCase):
    """AC2, end to end through the real alert_store (not a mock): first tick reports the
    condition and stays silent, second consecutive tick pages exactly once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = str(Path(self.tmp.name) / "alerts.json")
        self.addCleanup(self.tmp.cleanup)

    def _record(self, alert: phc.AlertCall):
        return alert_store.record(phc.CHECK, alert.problem, alert.severity,
                                   detail=alert.body, state_file=self.state)

    def test_first_tick_silent_second_tick_pages_once(self):
        alerts = phc.evaluate([fail("home")], FRESH_HEARTBEAT)
        self.assertEqual(len(alerts), 1)

        first = self._record(alerts[0])
        self.assertFalse(first["page"], f"paged on the first sighting: {first['reason']}")

        orig = alert_store.DEGRADED_MIN_SEC
        alert_store.DEGRADED_MIN_SEC = 0  # simulate the persistence window elapsing
        try:
            second = self._record(alerts[0])
            self.assertTrue(second["page"], f"never paged on the second tick: {second['reason']}")
            third = self._record(alerts[0])
            self.assertFalse(third["page"], "paged a third time for the same open condition")
        finally:
            alert_store.DEGRADED_MIN_SEC = orig


class SlowProbeBudgetTest(unittest.TestCase):
    """gh#727 AC3: a 200 that exceeds the 8s budget is its own failure, distinguishable
    from a non-200 status in the reason string."""

    def setUp(self):
        self._orig_get = phc._get
        self._orig_token = phc.CF_BYPASS_VALUE
        phc.CF_BYPASS_VALUE = "test-token"  # so the report probe isn't SKIPPED here
        self.addCleanup(self._restore)

    def _restore(self):
        phc._get = self._orig_get
        phc.CF_BYPASS_VALUE = self._orig_token

    def test_slow_200_fails_with_a_reason_distinct_from_a_status_code(self):
        phc._get = lambda url, timeout=phc.TIMEOUT_S: (200, b"ok", None, phc.SLOW_BUDGET_S + 1.0)
        probes = phc.run_probes()
        self.assertTrue(all(not p.ok for p in probes))
        for p in probes:
            self.assertIn("slow", p.detail)
            self.assertNotRegex(p.detail, r"^http \d+$")

    def test_fast_200_still_passes(self):
        phc._get = lambda url, timeout=phc.TIMEOUT_S: (200, b"ok", None, 0.2)
        probes = phc.run_probes()
        self.assertTrue(all(p.ok for p in probes))


class BypassTokenSkipTest(unittest.TestCase):
    """gh#727 AC9: with no bypass token configured, the report probe is SKIPPED (and does
    not count as a site outage), not silently treated as a failure or a false pass."""

    def setUp(self):
        self._orig_get = phc._get
        self._orig_token = phc.CF_BYPASS_VALUE
        phc.CF_BYPASS_VALUE = ""
        phc._get = lambda url, timeout=phc.TIMEOUT_S: (200, b"ok", None, 0.2)
        self.addCleanup(self._restore)

    def _restore(self):
        phc._get = self._orig_get
        phc.CF_BYPASS_VALUE = self._orig_token

    def test_missing_token_skips_report_probe_and_pages_nothing(self):
        probes = phc.run_probes()
        report = next(p for p in probes if p.name == "report")
        self.assertTrue(report.skipped)
        self.assertTrue(report.ok)
        self.assertIn("SKIPPED", report.detail)

        alerts = phc.evaluate(probes, FRESH_HEARTBEAT)
        self.assertEqual(alerts, [])

        line = phc.verdict_line(alerts, probes)
        self.assertIn("skipped", line.lower())
        self.assertIn("report", line)


class IncidentFilingTest(unittest.TestCase):
    """gh#727 AC4/AC5/AC6: one incident issue per open outage (deduped by open title),
    the exact label set, and a non-closing recovery comment -- against FakeGh, no network."""

    def test_first_failure_files_one_issue_with_exact_labels(self):
        gh = FakeGh()
        probes = [fail("home", "http 500"), ok("search"), ok("report")]
        alerts = phc.evaluate(probes, FRESH_HEARTBEAT)
        result = phc.file_or_update_incident(probes, alerts, runner=gh)
        self.assertEqual(result["action"], "filed")
        issue = gh.issues[result["issue"]]
        self.assertEqual(issue["title"], phc.incident_title(phc.BASE_URL + "/990"))

    def test_incident_labels_are_exactly_the_prd_set(self):
        cmd = phc.build_file_cmd("prod down: x", "body")
        labels = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--label"]
        self.assertEqual(
            labels, ["fleet:backlog", "lane:devops", "fleet:priority-high", "incident"])

    def test_further_failure_comments_not_a_second_issue(self):
        gh = FakeGh()
        probes = [fail("home", "http 500"), ok("search"), ok("report")]
        alerts = phc.evaluate(probes, FRESH_HEARTBEAT)
        first = phc.file_or_update_incident(probes, alerts, runner=gh)
        second = phc.file_or_update_incident(probes, alerts, runner=gh)
        self.assertEqual(second["action"], "commented")
        self.assertEqual(second["issue"], first["issue"])
        self.assertEqual(len(gh.issues), 1)

    def test_recovery_comments_but_leaves_the_issue_open(self):
        gh = FakeGh()
        probes = [fail("home", "http 500"), ok("search"), ok("report")]
        alerts = phc.evaluate(probes, FRESH_HEARTBEAT)
        filed = phc.file_or_update_incident(probes, alerts, runner=gh)
        rec = phc.report_recovery(runner=gh)
        self.assertEqual(rec["issue"], filed["issue"])
        self.assertTrue(gh.issues[filed["issue"]]["open"])
        self.assertIn("Recovered", gh.issues[filed["issue"]]["comments"][-1])

    def test_recovery_with_no_open_incident_is_a_noop(self):
        gh = FakeGh()
        self.assertIsNone(phc.report_recovery(runner=gh))


class PageGatesIncidentFilingTest(unittest.TestCase):
    """gh#727 AC2: page()'s return value -- driven by the same alert_store debounce as
    TwoTickPagingTest above -- is what a caller uses to decide whether to ALSO file/update
    the incident issue this tick. First sighting: suppressed, nothing delivered. Second
    consecutive tick: pages exactly once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_state_env = os.environ.get("FLEET_ALERT_STATE_FILE")
        os.environ["FLEET_ALERT_STATE_FILE"] = str(Path(self.tmp.name) / "alerts.json")
        self.addCleanup(self._restore_env)

        self.delivered = []
        self._orig_run = phc.subprocess.run

        def fake_run(cmd, **kwargs):
            self.delivered.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        phc.subprocess.run = fake_run
        self.addCleanup(lambda: setattr(phc.subprocess, "run", self._orig_run))

    def _restore_env(self):
        if self._orig_state_env is None:
            os.environ.pop("FLEET_ALERT_STATE_FILE", None)
        else:
            os.environ["FLEET_ALERT_STATE_FILE"] = self._orig_state_env

    def test_first_tick_suppressed_second_tick_pages_exactly_once(self):
        a = phc.evaluate([fail("home")], FRESH_HEARTBEAT)[0]

        first = phc.page(a.problem, a.severity, a.title, a.body)
        self.assertFalse(first)
        self.assertEqual(self.delivered, [])

        orig = alert_store.DEGRADED_MIN_SEC
        alert_store.DEGRADED_MIN_SEC = 0
        try:
            second = phc.page(a.problem, a.severity, a.title, a.body)
            self.assertTrue(second)
            self.assertEqual(len(self.delivered), 1)

            third = phc.page(a.problem, a.severity, a.title, a.body)
            self.assertFalse(third)
            self.assertEqual(len(self.delivered), 1)
        finally:
            alert_store.DEGRADED_MIN_SEC = orig


if __name__ == "__main__":
    unittest.main()
