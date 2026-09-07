#!/usr/bin/env python3
"""test_journey_issue_filer.py -- gh#660 acceptance criteria, exercised end to end against a
mocked `gh` (no network, no real GitHub issues -- see journey_issue_filer.py's own "PART OF
#660, not Closes" note for why this is the demonstration this PR ships instead of a live
sentry pass: #657's walker does not exist yet).

FakeGh below is a tiny in-memory stand-in for the real GitHub issue tracker: `create` appends
to a list, `list` returns open ones, `comment`/`close` mutate them by number. Passing it in as
`process()`'s `runner` is the same "pure builders, mocked execution" split board_github.py's
own tests use.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import journey_issue_filer as jif  # noqa: E402


class FakeGh:
    """Replays enough of `gh issue {create,list,comment,close}` to drive process() for real."""

    def __init__(self):
        self.issues = {}  # number -> {"body": str, "open": bool, "comments": [str]}
        self._next = 100

    def __call__(self, cmd: list[str]) -> tuple[int, str]:
        assert cmd[0] == "gh"
        if cmd[1] == "label":
            return 0, "label ensured"  # ensure_label() is idempotent no-op in this fake
        assert cmd[1] == "issue"
        sub = cmd[2]
        if sub == "create":
            title, body = cmd[cmd.index("--title") + 1], cmd[cmd.index("--body") + 1]
            n = self._next
            self._next += 1
            self.issues[n] = {"title": title, "body": body, "open": True, "comments": []}
            return 0, f"https://github.com/x/y/issues/{n}"
        if sub == "list":
            open_issues = [
                {"number": n, "body": v["body"]} for n, v in self.issues.items() if v["open"]
            ]
            return 0, json.dumps(open_issues)
        if sub == "comment":
            n, body = int(cmd[3]), cmd[cmd.index("--body") + 1]
            self.issues[n]["comments"].append(body)
            return 0, "commented"
        if sub == "close":
            n, body = int(cmd[3]), cmd[cmd.index("--comment") + 1]
            self.issues[n]["comments"].append(body)
            self.issues[n]["open"] = False
            return 0, "closed"
        raise AssertionError(f"unexpected gh subcommand: {sub}")


def _results(status: str, run: str, sha: str, extra_step=None) -> dict:
    step = {
        "index": 0,
        "action": "Fill the message compose box and press send.",
        "observable_result": "The message appears in the thread within 5s.",
        "status": status,
        "screenshot": f"qa-out/{run}/journeys/send-message/desktop/0.png",
    }
    if extra_step:
        step.update(extra_step)
    return {
        "run": run,
        "deploy_sha": sha,
        "journeys": [{"id": "send-message", "name": "Send a message", "steps": [step]}],
    }


class BuildersTest(unittest.TestCase):
    def test_title_is_plain_language_not_a_selector(self):
        title = jif.build_issue_title("Send a message", "Fill the message compose box and press send.")
        self.assertNotIn("selector", title.lower())
        self.assertNotIn("Traceback", title)
        self.assertIn("Send a message", title)
        self.assertIn("isn't working", title)

    def test_long_action_is_trimmed_not_raw(self):
        title = jif.build_issue_title("X", "a" * 200)
        self.assertLess(len(title), 100)

    def test_body_carries_marker_repro_and_screenshot(self):
        journey = {"id": "send-message", "name": "Send a message",
                   "steps": [{"index": 0, "action": "Open the thread.", "observable_result": "ok"}]}
        step = {"index": 0, "action": "Open the thread.", "observable_result": "ok",
                "screenshot": "qa-out/r1/journeys/send-message/desktop/0.png"}
        key = jif.step_key("send-message", 0)
        body = jif.build_issue_body(journey, step, "r1", "deadbeef", None, key)
        self.assertIn(jif.marker_for(key), body)
        self.assertIn("1. Open the thread.", body)
        self.assertIn("qa-out/r1/journeys/send-message/desktop/0.png", body)
        self.assertIn("unknown -- this is the first observed failure", body)

    def test_body_includes_last_pass_sha_when_known(self):
        journey = {"id": "j", "name": "J", "steps": [{"index": 0, "action": "a", "observable_result": "o"}]}
        step = journey["steps"][0]
        body = jif.build_issue_body(journey, step, "r2", "cafef00d", "abc1234", jif.step_key("j", 0))
        self.assertIn("abc1234", body)


class ProcessEndToEndTest(unittest.TestCase):
    """gh#660 AC5: a deliberately-broken-then-fixed journey files then auto-closes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"
        self.gh = FakeGh()

    def _write(self, name: str, results: dict) -> Path:
        p = Path(self.tmp.name) / name
        p.write_text(json.dumps(results))
        return p

    def test_fail_then_repeat_then_recover(self):
        # Run 1: the step fails -- exactly one issue is filed.
        r1 = self._write("r1.json", _results("fail", "run-1", "sha1"))
        summary1 = jif.process(r1, self.state_path, runner=self.gh)
        self.assertEqual(len(summary1["filed"]), 1)
        self.assertEqual(summary1["commented"], [])
        issue_no = summary1["filed"][0]["issue"]
        self.assertTrue(self.gh.issues[issue_no]["open"])

        # Run 2: same step fails again -- AC3, no duplicate, a comment on the existing issue.
        r2 = self._write("r2.json", _results("fail", "run-2", "sha1"))
        summary2 = jif.process(r2, self.state_path, runner=self.gh)
        self.assertEqual(summary2["filed"], [])
        self.assertEqual(len(summary2["commented"]), 1)
        self.assertEqual(summary2["commented"][0]["issue"], issue_no)
        self.assertEqual(len(self.gh.issues), 1, "must not have filed a second issue")

        # Run 3: the step passes -- AC4, the open issue closes with a comment naming the run.
        r3 = self._write("r3.json", _results("pass", "run-3", "sha2"))
        summary3 = jif.process(r3, self.state_path, runner=self.gh)
        self.assertEqual(len(summary3["closed"]), 1)
        self.assertEqual(summary3["closed"][0]["issue"], issue_no)
        self.assertFalse(self.gh.issues[issue_no]["open"])
        self.assertIn("run-3", self.gh.issues[issue_no]["comments"][-1])

        # State now remembers sha2 as the last-passing sha for this step.
        state = jif.load_state(self.state_path)
        self.assertEqual(state[jif.step_key("send-message", 0)]["last_pass_sha"], "sha2")

        # Run 4: it breaks again -- the NEW issue's body cites sha2 as the last-known-good sha.
        r4 = self._write("r4.json", _results("fail", "run-4", "sha3"))
        summary4 = jif.process(r4, self.state_path, runner=self.gh)
        self.assertEqual(len(summary4["filed"]), 1)
        new_issue_no = summary4["filed"][0]["issue"]
        self.assertNotEqual(new_issue_no, issue_no, "closed issue must not be reused")
        self.assertIn("sha2", self.gh.issues[new_issue_no]["body"])

    def test_pass_with_no_prior_issue_is_a_noop(self):
        r = self._write("clean.json", _results("pass", "run-1", "sha1"))
        summary = jif.process(r, self.state_path, runner=self.gh)
        self.assertEqual(summary["closed"], [])
        self.assertEqual(len(self.gh.issues), 0)

    def test_gh_failure_is_recorded_not_raised(self):
        def failing_runner(cmd):
            return 1, "gh: rate limited"

        r = self._write("r.json", _results("fail", "run-1", "sha1"))
        summary = jif.process(r, self.state_path, runner=failing_runner)
        self.assertTrue(summary["errors"])
        self.assertEqual(summary["filed"], [])


if __name__ == "__main__":
    unittest.main()
