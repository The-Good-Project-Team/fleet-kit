"""Regression + contention test for worktree_lock.sh (gh#672).

The old `mkdir "$LOCK"` + `sleep 2` spin in run_member.sh/worktree_builder.sh had no memory of
arrival order: a lock release and a brand-new contender's very first `mkdir` attempt could land
in the same instant and win exactly as often as a process that had been polling for minutes --
because a sleeping poller isn't even attempting the mkdir at the moment the lock frees. That
starved the messenger inbox pass (fk#669/#670) for 6 minutes under real load on 2026-09-07.

This test proves the flock-based replacement fixes the mechanism, WITHOUT needing a loaded box
(AC7): a helper process holds the lock while N waiters queue behind it.

Run: python3 scripts/test_worktree_lock.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
LOCK_SCRIPT = KIT / "scripts" / "worktree_lock.sh"


def run_bash(script: str, env: dict, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                           text=True, timeout=timeout)


class WorktreeLockSourceTests(unittest.TestCase):
    """AC3: the lock is flock-based, and the old mkdir spin is gone."""

    def test_run_member_no_longer_spins_on_mkdir(self):
        text = (KIT / "scripts" / "run_member.sh").read_text()
        self.assertNotIn('mkdir "$LOCK"', text)
        self.assertIn("worktree_lock.sh", text)
        self.assertIn("worktree_lock_acquire", text)

    def test_worktree_builder_no_longer_spins_on_mkdir(self):
        text = (KIT / "scripts" / "worktree_builder.sh").read_text()
        self.assertNotIn('mkdir "$LOCK"', text)
        self.assertIn("worktree_lock.sh", text)
        self.assertIn("worktree_lock_acquire", text)

    def test_lock_uses_flock(self):
        code_lines = [l for l in LOCK_SCRIPT.read_text().splitlines() if not l.strip().startswith("#")]
        code = "\n".join(code_lines)
        self.assertIn("flock", code)
        self.assertNotIn("mkdir", code)


class WorktreeLockContentionTests(unittest.TestCase):
    """AC1/AC2/AC7: N concurrent waiters, driven by a helper process holding the lock --
    no real git operations, no loaded box required."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-worktree-lock-test-")
        # Isolate this test's lock file from any real one on the box: worktree_lock.sh derives
        # its path from $TMPDIR.
        self.env = dict(os.environ)
        self.env["TMPDIR"] = self.tmp
        self.order_file = os.path.join(self.tmp, "order.log")

    def _waiter_script(self, tag: str, hold_s: float = 0.05) -> str:
        return textwrap.dedent(f"""
            set -u
            . "{LOCK_SCRIPT}"
            worktree_lock_acquire 30 || {{ echo "TIMEOUT {tag}" >> "{self.order_file}"; exit 1; }}
            echo "OK {tag}" >> "{self.order_file}"
            sleep {hold_s}
            worktree_lock_release
        """)

    def test_n20_concurrent_all_acquire_no_timeout(self):
        """AC1: 20 concurrent attempts, none times out."""
        procs = [subprocess.Popen(["bash", "-c", self._waiter_script(f"w{i}")], env=self.env)
                 for i in range(20)]
        for p in procs:
            self.assertEqual(p.wait(timeout=30), 0, "a waiter timed out acquiring the lock")
        lines = Path(self.order_file).read_text().splitlines()
        self.assertEqual(len(lines), 20)
        self.assertTrue(all(l.startswith("OK ") for l in lines), lines)

    def test_first_requester_is_not_starved_by_later_arrivals(self):
        """AC2: a waiter queued since before K later contenders arrived acquires the lock at
        least once, within a bounded number of releases -- not perpetually last."""
        holder_script = textwrap.dedent(f"""
            set -u
            . "{LOCK_SCRIPT}"
            worktree_lock_acquire 30
            echo "HOLDING" >> "{self.order_file}"
            sleep 0.5
            worktree_lock_release
        """)
        holder = subprocess.Popen(["bash", "-c", holder_script], env=self.env)
        # Give the holder time to actually take the lock before "first" starts queuing.
        import time
        time.sleep(0.15)

        first = subprocess.Popen(["bash", "-c", self._waiter_script("first")], env=self.env)
        # Let "first" register as a blocked waiter before the later contenders show up.
        time.sleep(0.15)

        later = [subprocess.Popen(["bash", "-c", self._waiter_script(f"later{i}")], env=self.env)
                 for i in range(10)]

        holder.wait(timeout=10)
        first.wait(timeout=10)
        for p in later:
            p.wait(timeout=10)

        lines = Path(self.order_file).read_text().splitlines()
        acquired = [l.split()[1] for l in lines if l.startswith("OK ")]
        self.assertIn("first", acquired, "the first-requested waiter never acquired the lock")
        rank = acquired.index("first")
        # The old mkdir-spin bug let ANY fresh contender win regardless of arrival order --
        # "first" could land anywhere, including dead last, every single time. flock's kernel
        # wait queue means a waiter already blocked when the lock frees is not systematically
        # outrun by one that starts trying afterward: assert "first" is not the last-served of
        # the 11 total waiters (not "exactly first" -- flock is not a strict FIFO guarantee,
        # just no-longer-starved).
        self.assertLess(rank, len(acquired) - 1,
                         f"'first' was served last ({rank+1}/{len(acquired)}) -- starved: {lines}")


if __name__ == "__main__":
    unittest.main()
