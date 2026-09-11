"""Tests for fixer_fire_path.py (gh#728, marie's PRD -- AC1, AC2, AC3, AC4, AC7).

No real ssh/gh/deploy-driver is touched: every test injects a fake `run(cmd) -> (rc, out)`
callable, same technique board_github.py's own command-builder split is designed for. Where a
real subprocess IS exercised (diagnose() against a real fake driver script), it's a plain local
bash stub with no network or host dependency -- same shape test_deploy_health_and_retire.py's
own header describes for gh#672's AC5/AC6.

Run: python3 scripts/test_fixer_fire_path.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixer_fire_path as ffp  # noqa: E402


def _fake_run(script: dict[tuple, tuple[int, str]], calls: list):
    """Returns a `run(cmd)` callable keyed on tuple(cmd); records every call in `calls` (in
    order) so a test can assert call ORDER, not just outcomes -- AC1 is entirely about order."""
    def run(cmd, timeout=60):
        calls.append(tuple(cmd))
        key = tuple(cmd)
        if key in script:
            return script[key]
        raise AssertionError(f"unexpected command: {cmd!r}")
    return run


class DiagnoseBeforeRollbackOrderingTests(unittest.TestCase):
    """AC1: given a fake diag driver, the fire path calls it before attempting any rollback."""

    def test_diag_runs_before_current_sha_and_rollback(self):
        calls: list = []
        # current_sha is called twice (before/after rollback); same value both times -- ordering,
        # not verification outcome, is what's under test here.
        script = {
            ("diag-driver", "pg"): (0, "pg looks fine"),
            ("deploy-driver", "current_sha"): (0, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            ("deploy-driver", "rollback"): (0, ""),
            ("gh", "issue", "list", "--state", "open", "--label", "incident",
             "--search", ffp.INCIDENT_MARKER, "--json", "number", "--limit", "5"): (0, "[]"),
        }
        base_run = _fake_run(script, calls)

        def run(cmd, timeout=60):
            if cmd[:3] == ["gh", "issue", "create"]:
                calls.append(tuple(cmd))
                return 0, "https://github.com/acme/prod/issues/1"
            return base_run(cmd, timeout=timeout)

        result = ffp.run_fire_path(
            diag_driver="diag-driver", diag_section="pg", deploy_driver="deploy-driver", run=run,
        )

        self.assertEqual(calls[0], ("diag-driver", "pg"), "diag driver was not called first")
        self.assertLess(
            calls.index(("diag-driver", "pg")), calls.index(("deploy-driver", "rollback")),
            "rollback was attempted before diagnosis",
        )
        self.assertIn("pg looks fine", result["diag_output"])

    def test_real_diag_driver_output_reaches_the_incident_body(self):
        """The captured diag output must appear in the incident issue body verbatim, not just
        be logged somewhere the charter has to go find (AC1's second half)."""
        with tempfile.TemporaryDirectory() as tmp:
            driver = Path(tmp) / "diag.sh"
            driver.write_text("#!/bin/bash\necho \"section=$1 conns=42 wait=Lock\"\n")
            driver.chmod(0o755)

            calls: list = []
            sha = "b" * 40
            script = {
                ("deploy-driver", "current_sha"): (0, sha),
                ("deploy-driver", "rollback"): (0, ""),
                ("gh", "issue", "list", "--state", "open", "--label", "incident",
                 "--search", ffp.INCIDENT_MARKER, "--json", "number", "--limit", "5"): (0, "[]"),
            }
            stubbed = _fake_run(script, calls)
            # gh issue create's cmd includes the body text, which is only known at call time --
            # intercept it generically instead of pre-registering the exact string. The diag
            # driver is a real script on disk, so let its call fall through to a real subprocess
            # instead of the stub (which only knows about the deploy-driver/gh commands above).
            def run_and_capture_create(cmd, timeout=60):
                if cmd[:3] == ["gh", "issue", "create"]:
                    calls.append(tuple(cmd))
                    return 0, "https://github.com/acme/prod/issues/99"
                if cmd[0] == str(driver):
                    calls.append(tuple(cmd))
                    return ffp._run(cmd, timeout=timeout)
                return stubbed(cmd, timeout=timeout)

            result = ffp.run_fire_path(
                diag_driver=str(driver), diag_section="pg", deploy_driver="deploy-driver",
                run=run_and_capture_create,
            )
            create_cmd = next(c for c in calls if c[:3] == ("gh", "issue", "create"))
            body = create_cmd[create_cmd.index("--body") + 1]
            self.assertIn("section=pg conns=42 wait=Lock", body)
            self.assertIn("section=pg conns=42 wait=Lock", result["diag_output"])


class RollbackVerificationTests(unittest.TestCase):
    """AC2 (the PRD's own 'single most important criterion') + AC3."""

    def _run_rollback_only(self, current_sha_sequence):
        calls: list = []
        seq = iter(current_sha_sequence)

        def run(cmd, timeout=60):
            calls.append(tuple(cmd))
            if tuple(cmd) == ("driver", "current_sha"):
                return next(seq)
            if tuple(cmd) == ("driver", "rollback"):
                return (0, "")
            raise AssertionError(cmd)

        return ffp.roll_back_and_verify("driver", run=run), calls

    def test_unchanged_sha_reports_rollback_failed_not_success(self):
        """AC2: same sha before and after rollback -> ROLLBACK FAILED, never success -- this is
        the exact 'silent no-op reported as success' failure mode the PRD calls out by name."""
        same = "c" * 40
        result, _ = self._run_rollback_only([(0, same), (0, same)])
        self.assertFalse(result.verified)
        self.assertIn("ROLLBACK FAILED", result.message)

    def test_changed_sha_reports_success_with_both_shas(self):
        """AC3: sha changes after rollback -> success, and both old and new sha are recorded."""
        old, new = "d" * 40, "e" * 40
        result, _ = self._run_rollback_only([(0, old), (0, new)])
        self.assertTrue(result.verified)
        self.assertEqual(result.old_sha, old)
        self.assertEqual(result.new_sha, new)

    def test_incident_records_old_and_new_sha_on_success(self):
        old, new = "f" * 40, "1" * 40
        calls: list = []
        seq = iter([(0, old), (0, new)])

        def run(cmd, timeout=60):
            calls.append(tuple(cmd))
            if tuple(cmd) == ("driver", "current_sha"):
                return next(seq)
            if tuple(cmd) == ("driver", "rollback"):
                return (0, "")
            if tuple(cmd) == ("diag-driver", "pg"):
                return (0, "diag text")
            if cmd[:3] == ["gh", "issue", "list"]:
                return (0, "[]")
            if cmd[:3] == ["gh", "issue", "create"]:
                return (0, "https://github.com/acme/prod/issues/7")
            raise AssertionError(cmd)

        result = ffp.run_fire_path(
            diag_driver="diag-driver", diag_section="pg", deploy_driver="driver", run=run,
        )
        self.assertTrue(result["rollback_verified"])
        create_cmd = next(c for c in calls if c[:3] == ("gh", "issue", "create"))
        body = create_cmd[create_cmd.index("--body") + 1]
        self.assertIn(old, body)
        self.assertIn(new, body)

    def test_rollback_exit_nonzero_is_never_verified_even_if_sha_moved(self):
        """A driver reporting its own rollback command failed is never trusted into a success,
        even in the (unlikely) case current_sha happened to differ anyway."""
        calls: list = []

        def run(cmd, timeout=60):
            calls.append(tuple(cmd))
            if tuple(cmd) == ("driver", "current_sha"):
                return (0, "old-or-new")
            if tuple(cmd) == ("driver", "rollback"):
                return (1, "connection refused")
            raise AssertionError(cmd)

        result = ffp.roll_back_and_verify("driver", run=run)
        self.assertFalse(result.verified)
        self.assertIn("ROLLBACK FAILED", result.message)


class PostPromoteBreachTests(unittest.TestCase):
    """AC4: two consecutive breaching samples fire; a single isolated breach does not."""

    def test_two_consecutive_error_rate_breaches_fire(self):
        samples = [{"error_pct": 6.0, "p95_s": 1.0}, {"error_pct": 7.0, "p95_s": 1.0}]
        self.assertTrue(ffp.should_fire_post_promote(samples))

    def test_two_consecutive_latency_breaches_fire(self):
        samples = [{"error_pct": 0.0, "p95_s": 6.5}, {"error_pct": 0.0, "p95_s": 7.0}]
        self.assertTrue(ffp.should_fire_post_promote(samples))

    def test_single_breach_then_clean_sample_does_not_fire(self):
        samples = [{"error_pct": 9.0, "p95_s": 1.0}, {"error_pct": 0.0, "p95_s": 1.0}]
        self.assertFalse(ffp.should_fire_post_promote(samples))

    def test_isolated_breaches_separated_by_a_clean_sample_do_not_fire(self):
        samples = [
            {"error_pct": 9.0, "p95_s": 1.0},
            {"error_pct": 0.0, "p95_s": 1.0},
            {"error_pct": 9.0, "p95_s": 1.0},
        ]
        self.assertFalse(ffp.should_fire_post_promote(samples))

    def test_all_clean_never_fires(self):
        samples = [{"error_pct": 0.1, "p95_s": 0.5}] * 5
        self.assertFalse(ffp.should_fire_post_promote(samples))


class PostPromoteCliWiringTests(unittest.TestCase):
    """VP follow-up on gh#728 (2026-09-11): should_fire_post_promote() had no caller anywhere --
    check.sh never took samples for it, so "degraded" (this issue's own title) was never
    actually detected. check.sh now calls the `--check-post-promote` CLI added to
    fixer_fire_path.py every tick; these tests exercise that CLI exactly as check.sh does (a
    real subprocess, a samples-state file that persists across calls, plain floats as argv) --
    never should_fire_post_promote() directly, which PostPromoteBreachTests above already
    covers as a pure function."""

    def _check(self, state_file: Path, error_pct: float, p95_s: float):
        return subprocess.run(
            [sys.executable, str(Path(ffp.__file__)), "--check-post-promote",
             str(state_file), str(error_pct), str(p95_s)],
            capture_output=True, text=True, timeout=10,
        )

    def test_two_consecutive_error_rate_breaches_fire_with_a_healthy_page(self):
        """The PRD's own scenario: a healthy (200) health page proves nothing about the page's
        own error rate -- two consecutive breaching samples must still fire."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "the-fixer.postpromote"
            clean = self._check(state, 1.0, 0.8)
            self.assertEqual(clean.returncode, 1)
            self.assertEqual(clean.stdout.strip(), "no-fire")

            first_breach = self._check(state, 8.0, 1.0)
            self.assertEqual(first_breach.returncode, 1, "one breach alone must not fire")

            second_breach = self._check(state, 9.0, 1.0)
            self.assertEqual(second_breach.returncode, 0)
            self.assertEqual(second_breach.stdout.strip(), "fire")

    def test_one_breach_then_one_clean_sample_does_not_fire(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "the-fixer.postpromote"
            breach = self._check(state, 9.0, 1.0)
            self.assertEqual(breach.returncode, 1)

            clean = self._check(state, 0.5, 1.0)
            self.assertEqual(clean.returncode, 1)
            self.assertEqual(clean.stdout.strip(), "no-fire")

    def test_recovery_after_a_fired_pair_does_not_keep_re_firing(self):
        """Caught building this wiring: a history window wider than 2 let a long-past breaching
        pair keep tripping the verdict on a tick that was itself clean, because the old pair
        hadn't aged out of the trimmed history yet. Two breaches then a clean sample must read
        as recovered, same as PostPromoteBreachTests' pure-function case above -- this asserts
        it holds through the persisted CLI wrapper too, not just the in-memory function."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "the-fixer.postpromote"
            self._check(state, 9.0, 1.0)                       # breach 1
            fired = self._check(state, 9.5, 1.0)                # breach 2 -> fires
            self.assertEqual(fired.returncode, 0)

            recovered = self._check(state, 0.2, 1.0)             # clean tick after the fire
            self.assertEqual(recovered.returncode, 1, "a clean tick right after a fire must not keep firing")
            self.assertEqual(recovered.stdout.strip(), "no-fire")

    def test_state_file_persists_across_separate_process_invocations(self):
        """Each call is a fresh `python3 ... --check-post-promote` process, same as check.sh
        invoking it once per tick -- the breach memory has to live in the state file, not in
        any in-process state, or every tick would see history of exactly one sample."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "the-fixer.postpromote"
            self._check(state, 8.0, 1.0)
            history = json.loads(state.read_text())
            self.assertEqual(history, [{"error_pct": 8.0, "p95_s": 1.0}])

            self._check(state, 9.0, 1.0)
            history = json.loads(state.read_text())
            self.assertEqual(len(history), 2)


class IncidentDedupTests(unittest.TestCase):
    """AC7: repeat firing updates the same incident issue rather than filing a second one."""

    def test_no_existing_incident_creates_one(self):
        calls: list = []

        def run(cmd, timeout=60):
            calls.append(tuple(cmd))
            if cmd[:3] == ["gh", "issue", "list"]:
                return (0, "[]")
            if cmd[:3] == ["gh", "issue", "create"]:
                return (0, "https://github.com/acme/prod/issues/42")
            raise AssertionError(cmd)

        number, created = ffp.file_or_update_incident("title", "body", run=run)
        self.assertEqual(number, 42)
        self.assertTrue(created)

    def test_existing_open_incident_is_commented_on_not_duplicated(self):
        calls: list = []

        def run(cmd, timeout=60):
            calls.append(tuple(cmd))
            if cmd[:3] == ["gh", "issue", "list"]:
                return (0, json.dumps([{"number": 17}]))
            if cmd[:3] == ["gh", "issue", "comment"]:
                return (0, "")
            raise AssertionError(cmd)

        number, created = ffp.file_or_update_incident("title", "body", run=run)
        self.assertEqual(number, 17)
        self.assertFalse(created)
        self.assertFalse(any(c[:3] == ("gh", "issue", "create") for c in calls),
                          "a second incident was filed even though one was already open")

    def test_incident_search_is_scoped_to_the_marker_so_unrelated_incidents_are_ignored(self):
        cmd = ffp.build_incident_search_cmd()
        self.assertIn(ffp.INCIDENT_MARKER, cmd)
        self.assertIn(ffp.INCIDENT_LABEL, cmd)


class IncidentLabelTests(unittest.TestCase):
    def test_create_command_carries_priority_high_and_incident_labels(self):
        cmd = ffp.build_incident_create_cmd("t", "b")
        label_idx = cmd.index("--label") + 1
        labels = cmd[label_idx].split(",")
        self.assertIn(ffp.PRIORITY_HIGH_LABEL, labels)
        self.assertIn(ffp.INCIDENT_LABEL, labels)


if __name__ == "__main__":
    unittest.main()
