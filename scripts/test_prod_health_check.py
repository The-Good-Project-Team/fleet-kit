#!/usr/bin/env python3
"""test_prod_health_check.py -- gh#4898 AC2/AC4/AC5's own tests.

AC2 ("first run sends nothing, second triggers exactly one notification, not two, not
zero"), AC4 ("a failure of any one probe is attributable to which probe failed") and
AC5 ("the heartbeat pages independently of whether the HTTP probes pass") are each a
named test below, run against synthetic ProbeResult/HeartbeatResult sequences -- no
network, no live philanthropy.org.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alert_store  # noqa: E402
import prod_health_check as phc  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
