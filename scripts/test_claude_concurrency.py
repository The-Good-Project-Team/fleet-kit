"""Regression + contention test for claude_concurrency.sh (gh#672 AC4).

Every `claude -p` invocation -- gru's own pass, every minion/the-fixer sub-pass it fans out --
goes through run_member.sh's one call site. This proves the ceiling it now applies there
actually queues an attempt beyond the configured limit rather than letting it spawn
immediately, and that run_member.sh actually wires it in before invoking `claude -p`.

Run: python3 scripts/test_claude_concurrency.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
CONCURRENCY_SCRIPT = KIT / "scripts" / "claude_concurrency.sh"


class ClaudeConcurrencySourceTests(unittest.TestCase):
    def test_run_member_wires_in_the_ceiling_before_claude_p(self):
        text = (KIT / "scripts" / "run_member.sh").read_text()
        self.assertIn("claude_concurrency.sh", text)
        self.assertIn("claude_slot_acquire", text)
        acquire_pos = text.index("claude_slot_acquire")
        claude_p_pos = text.index('claude -p "$PROMPT"')
        self.assertLess(acquire_pos, claude_p_pos,
                         "claude_slot_acquire must run BEFORE claude -p is invoked")

    def test_default_documented_in_fleet_env_example(self):
        text = (KIT / "fleet.env.example").read_text()
        self.assertIn("FLEET_CLAUDE_CONCURRENCY", text)


class ClaudeConcurrencyContentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-claude-concurrency-test-")
        self.env = dict(os.environ)
        self.env["TMPDIR"] = self.tmp
        self.env["FLEET_CLAUDE_CONCURRENCY"] = "2"
        self.log = os.path.join(self.tmp, "order.log")

    def _worker_script(self, tag: str, hold_s: float = 0.3) -> str:
        return textwrap.dedent(f"""
            set -u
            . "{CONCURRENCY_SCRIPT}"
            claude_slot_acquire
            echo "START {tag} $(date +%s.%N)" >> "{self.log}"
            sleep {hold_s}
            echo "END {tag} $(date +%s.%N)" >> "{self.log}"
            claude_slot_release
        """)

    def test_beyond_ceiling_queues_instead_of_spawning_immediately(self):
        """5 attempts against a ceiling of 2: at no point are more than 2 STARTs open at once."""
        procs = [subprocess.Popen(["bash", "-c", self._worker_script(f"w{i}")], env=self.env)
                 for i in range(5)]
        for p in procs:
            self.assertEqual(p.wait(timeout=15), 0)

        events = []
        for line in Path(self.log).read_text().splitlines():
            kind, tag, ts = line.split()
            events.append((float(ts), kind))
        events.sort()

        concurrent = 0
        max_concurrent = 0
        for _, kind in events:
            concurrent += 1 if kind == "START" else -1
            max_concurrent = max(max_concurrent, concurrent)

        self.assertEqual(len([e for e in events if e[1] == "START"]), 5)
        self.assertLessEqual(max_concurrent, 2,
                              f"more than the configured ceiling ran at once: {events}")

    def test_acquire_still_returns_past_its_own_timeout(self):
        """A caller that waits longer than FLEET_CLAUDE_SLOT_TIMEOUT_S still gets a slot
        eventually (best-effort ceiling, not a hard cap a pass can be starved behind
        forever) -- exercised with a tiny timeout so the test itself stays fast."""
        env = dict(self.env)
        env["FLEET_CLAUDE_CONCURRENCY"] = "1"
        env["FLEET_CLAUDE_SLOT_TIMEOUT_S"] = "1"
        holder = subprocess.Popen(["bash", "-c", self._worker_script("holder", hold_s=3)], env=env)
        time.sleep(0.3)  # let the holder actually take the slot first
        waiter = subprocess.Popen(["bash", "-c", self._worker_script("waiter", hold_s=0)], env=env)
        self.assertEqual(waiter.wait(timeout=10), 0, "waiter never returned from claude_slot_acquire")
        self.assertEqual(holder.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
