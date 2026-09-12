"""Tests for pretest_push_hook.py -- the gate that moves testing off CI and onto the member.

The property that MUST hold and is easy to silently break: a receipt keys on CONTENT, not on
HEAD. A member runs the suite, then commits what it just tested; if the receipt died at the
commit, the hook would block every honest push and members would learn to route around it.
And the mirror of it: edit one line after the green run and the receipt must go stale.

Run: python3 scripts/test_pretest_push_hook.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pretest_push_hook as hook  # noqa: E402


def git(args: list[str], cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class Worktree:
    """A real git repo -- the hook shells out to git, so a fake would test nothing."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp()
        git(["init", "-q", "-b", "main"], self.dir)
        git(["config", "user.email", "t@example.com"], self.dir)
        git(["config", "user.name", "t"], self.dir)
        self.write("app.py", "x = 1\n")
        git(["add", "-A"], self.dir)
        git(["commit", "-qm", "init"], self.dir)
        git(["branch", "-f", "origin/main", "HEAD"], self.dir)  # stand-in for the remote ref

    def write(self, name: str, body: str) -> None:
        (Path(self.dir) / name).write_text(body)

    def receipt(self, status: str = "pass", content: str | None = None) -> None:
        content = hook.content_hash(self.dir) if content is None else content
        hook.receipt_path(self.dir).write_text(
            json.dumps({"status": status, "content": content, "args": "full", "exit": 0, "ts": 0}))

    def decide(self, command: str = "git push -u origin HEAD") -> str | None:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        return hook.decide(payload, {"WT_PATH": self.dir})


class PushGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wt = Worktree()

    def test_a_push_with_no_receipt_is_blocked(self):
        self.assertIn("No test receipt", self.wt.decide() or "")

    def test_a_push_with_a_matching_green_receipt_is_allowed(self):
        self.wt.receipt()
        self.assertIsNone(self.wt.decide())

    def test_the_receipt_survives_committing_the_very_content_it_certified(self):
        # The whole design: run the suite, THEN commit what you tested. If this regressed to
        # keying on HEAD, every honest push would be blocked and the gate would be routed
        # around within a day.
        self.wt.write("app.py", "x = 2\n")
        self.wt.receipt()
        git(["add", "app.py"], self.wt.dir)
        git(["commit", "-qm", "the tested change"], self.wt.dir)
        self.assertIsNone(self.wt.decide())

    def test_editing_after_the_green_run_goes_stale(self):
        self.wt.receipt()
        self.wt.write("app.py", "x = 999\n")
        self.assertIn("changed after the last green test run", self.wt.decide() or "")

    def test_a_red_receipt_is_not_a_pass(self):
        self.wt.receipt(status="fail")
        self.assertIn("not a pass", self.wt.decide() or "")

    def test_the_receipt_never_shows_up_as_an_untracked_file(self):
        # postflight_dirty_check.sh (gh#78/#183) calls a pass dirty on any leftover file, and
        # _only_docs reads `git status` too -- a receipt in the checkout would break both.
        self.wt.receipt()
        status = subprocess.run(["git", "status", "--porcelain=v1"], cwd=self.wt.dir,
                                capture_output=True, text=True).stdout
        self.assertNotIn("receipt", status)

    def test_a_docs_only_branch_needs_no_receipt(self):
        self.wt.write("README.md", "words\n")
        self.assertIsNone(self.wt.decide())

    def test_a_docs_only_branch_that_also_touches_code_still_needs_one(self):
        self.wt.write("README.md", "words\n")
        self.wt.write("app.py", "x = 3\n")
        self.assertIn("No test receipt", self.wt.decide() or "")

    def test_commands_that_are_not_a_push_are_left_alone(self):
        for cmd in ("git status", "git commit -m x", "pytest -q", "echo git push"):
            self.assertIsNone(self.wt.decide(cmd), cmd)

    def test_a_push_inside_a_chain_is_still_a_push(self):
        self.assertIn("No test receipt", self.wt.decide("git add -A && git push origin HEAD") or "")

    def test_a_pass_that_is_not_worktree_isolated_is_exempt(self):
        # run_member.sh leaves WT_PATH unset for `llm.worktree: false` passes -- the same
        # signal worktree_guard_hook.py reuses rather than inventing a second flag.
        payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}}
        self.assertIsNone(hook.decide(payload, {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
