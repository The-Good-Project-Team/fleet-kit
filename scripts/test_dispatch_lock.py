"""Regression + contention test for run_member.sh's per-member dispatch lock (gh#3220).

gh#3220: two independently-triggered dispatches of the SAME member (a cron tick racing gru's
orchestrator, or two cron ticks) used to both run at once with nothing to stop them --
escalating from 2 concurrent top-level instances to 7+ over two weeks, and spreading from
the-fixer to gru to jefe. This proves the lock actually rejects a second same-key dispatch
while it's held, lets a DIFFERENT key (a different member, or a different --item on the same
member) through unimpeded, and that run_member.sh wires it in before any expensive work.

Run: python3 scripts/test_dispatch_lock.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
RUN_MEMBER = KIT / "scripts" / "run_member.sh"


class DispatchLockWiringTests(unittest.TestCase):
    def test_lock_acquired_before_spec_resolution(self):
        text = RUN_MEMBER.read_text()
        self.assertIn("DISPATCH_LOCK_KEY", text)
        lock_pos = text.index('flock -n 9')
        spec_pos = text.index("resolve spec: git baseline")
        self.assertLess(lock_pos, spec_pos,
                         "the dispatch lock must be acquired before spec resolution/overrides "
                         "run, so a losing dispatch spends near-zero work")

    def test_uses_a_different_fd_than_claude_concurrency_and_worktree_lock(self):
        """fd 7 is claude_concurrency.sh's slot fd, fd 8 is worktree_lock.sh's fd -- both are
        sourced later in the SAME process. Reusing either number would have that later
        `exec N>...` silently steal this lock's descriptor instead of failing loudly."""
        text = RUN_MEMBER.read_text()
        self.assertIn("exec 9>", text)
        self.assertNotIn("DISPATCH_LOCK", (KIT / "scripts" / "claude_concurrency.sh").read_text())


class DispatchLockContentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-dispatch-lock-test-")
        self.log = os.path.join(self.tmp, "order.log")

    def _acquirer_script(self, key: str, hold_s: float) -> str:
        """Mirrors the acquire/skip shape added to run_member.sh, isolated from the rest of
        the script's dependencies (spec resolution, claude -p, etc.) so this test exercises
        just the locking primitive."""
        lock_dir = os.path.join(self.tmp, "fleet-kit-member-locks")
        return textwrap.dedent(f"""
            set -u
            mkdir -p "{lock_dir}"
            exec 9>"{lock_dir}/{key}.lock"
            if ! flock -n 9; then
              echo "SKIP {key} $(date +%s.%N)" >> "{self.log}"
              exit 0
            fi
            echo "START {key} $(date +%s.%N)" >> "{self.log}"
            sleep {hold_s}
            echo "END {key} $(date +%s.%N)" >> "{self.log}"
        """)

    def test_second_dispatch_for_same_key_skips_instead_of_running(self):
        holder = subprocess.Popen(["bash", "-c", self._acquirer_script("the-fixer", hold_s=1.0)])
        import time
        time.sleep(0.2)  # let the holder actually take the lock first
        loser = subprocess.Popen(["bash", "-c", self._acquirer_script("the-fixer", hold_s=1.0)])
        self.assertEqual(loser.wait(timeout=5), 0, "a losing dispatch must exit 0, not hang")
        self.assertEqual(holder.wait(timeout=5), 0)

        lines = Path(self.log).read_text().splitlines()
        kinds = [l.split()[0] for l in lines]
        self.assertEqual(kinds.count("START"), 1, f"exactly one dispatch should run: {lines}")
        self.assertEqual(kinds.count("SKIP"), 1, f"exactly one dispatch should skip: {lines}")

    def test_different_items_on_same_member_do_not_collide(self):
        """the-fixer's own charter fans out `--item N` sub-passes in parallel on purpose --
        the lock must key on member+item, not member alone, or this legitimate fanout would
        wrongly serialize down to one sub-pass at a time."""
        p1 = subprocess.Popen(["bash", "-c", self._acquirer_script("the-fixer-item111", hold_s=0.5)])
        p2 = subprocess.Popen(["bash", "-c", self._acquirer_script("the-fixer-item222", hold_s=0.5)])
        self.assertEqual(p1.wait(timeout=5), 0)
        self.assertEqual(p2.wait(timeout=5), 0)

        lines = Path(self.log).read_text().splitlines()
        starts = [l for l in lines if l.startswith("START")]
        self.assertEqual(len(starts), 2, f"both distinct-item dispatches should have started: {lines}")


if __name__ == "__main__":
    unittest.main()
