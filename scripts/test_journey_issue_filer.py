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
import contextlib
import io
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
        self.label_create_calls = []  # argv of every "gh label create" call, in order

    def __call__(self, cmd: list[str]) -> tuple[int, str]:
        assert cmd[0] == "gh"
        if cmd[1] == "label":
            self.label_create_calls.append(cmd)
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


class LookupFailureTest(unittest.TestCase):
    """gh#914: a `gh issue list` call that fails must never be read the same as "no open issue
    found" -- that misreading is exactly what filed #911/#912 as duplicates of #814/#884."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"

    def _write(self, name: str, results: dict) -> Path:
        p = Path(self.tmp.name) / name
        p.write_text(json.dumps(results))
        return p

    def test_ac1_list_failure_returns_a_distinct_sentinel_not_none(self):
        def list_fails(cmd):
            if cmd[1] == "label":
                return 0, "ok"
            assert cmd[2] == "list"
            return 1, "rate limited"

        self.assertIs(jif.find_open_issue("some::key", runner=list_fails), jif.LOOKUP_FAILED)

    def test_ac1_unparsable_list_output_also_returns_the_sentinel(self):
        def bad_json(cmd):
            if cmd[1] == "label":
                return 0, "ok"
            return 0, "not json"

        self.assertIs(jif.find_open_issue("some::key", runner=bad_json), jif.LOOKUP_FAILED)

    def test_ac2_lookup_failure_files_no_issue_and_is_recorded_as_skipped(self):
        create_calls = []

        def flaky(cmd):
            if cmd[1] == "label":
                return 0, "ok"
            if cmd[2] == "list":
                return 1, "rate limited"
            if cmd[2] == "create":
                create_calls.append(cmd)
                return 0, "https://github.com/x/y/issues/999"
            raise AssertionError(f"unexpected call: {cmd}")

        r = self._write("r.json", _results("fail", "run-1", "sha1"))
        summary = jif.process(r, self.state_path, runner=flaky)
        self.assertEqual(create_calls, [], "must not file when the dedup lookup couldn't run")
        self.assertEqual(summary["filed"], [])
        self.assertEqual(summary["commented"], [])
        self.assertEqual(len(summary["skipped"]), 1)
        self.assertEqual(summary["skipped"][0]["key"], jif.step_key("send-message", 0))
        self.assertNotIn("skipped due to lookup failure", str(summary["errors"]))

    def test_ac3_genuine_no_match_still_files_the_working_path_does_not_regress(self):
        def clean(cmd):
            if cmd[1] == "label":
                return 0, "ok"
            if cmd[2] == "list":
                return 0, "[]"
            if cmd[2] == "create":
                return 0, "https://github.com/x/y/issues/1"
            raise AssertionError(cmd)

        r = self._write("r.json", _results("fail", "run-1", "sha1"))
        summary = jif.process(r, self.state_path, runner=clean)
        self.assertEqual(len(summary["filed"]), 1)
        self.assertEqual(summary["skipped"], [])

    def test_ac4_lookup_success_with_a_match_still_comments(self):
        gh = FakeGh()
        r1 = self._write("r1.json", _results("fail", "run-1", "sha1"))
        summary1 = jif.process(r1, self.state_path, runner=gh)
        issue_no = summary1["filed"][0]["issue"]

        r2 = self._write("r2.json", _results("fail", "run-2", "sha1"))
        summary2 = jif.process(r2, self.state_path, runner=gh)
        self.assertEqual(len(summary2["commented"]), 1)
        self.assertEqual(summary2["commented"][0]["issue"], issue_no)
        self.assertEqual(summary2["skipped"], [])

    def test_ac5_one_failed_lookup_does_not_abort_the_rest_of_the_run(self):
        list_calls = {"n": 0}

        def flaky(cmd):
            if cmd[1] == "label":
                return 0, "ok"
            if cmd[2] == "list":
                list_calls["n"] += 1
                # first key's lookup fails, the second key's succeeds cleanly
                return (1, "rate limited") if list_calls["n"] == 1 else (0, "[]")
            if cmd[2] == "create":
                return 0, "https://github.com/x/y/issues/2"
            raise AssertionError(cmd)

        results = _results("fail", "run-1", "sha1")
        results["journeys"].append({
            "id": "second-journey", "name": "Second journey",
            "steps": [{"index": 0, "action": "do a second thing", "observable_result": "ok",
                       "status": "fail"}],
        })
        r = self._write("r.json", results)
        summary = jif.process(r, self.state_path, runner=flaky)
        self.assertEqual(len(summary["skipped"]), 1)
        self.assertEqual(len(summary["filed"]), 1,
                          "the key whose lookup succeeded must still be processed normally")


def _step0_results(run: str, sha: str, desktop_status: str, mobile_status: str) -> dict:
    """A results.json where journey `x` step 0 ran at both viewports, per journey_walker.py's
    own id convention (`x` for desktop, `x--mobile_390` for the other)."""
    step = {
        "index": 0,
        "action": "Navigate to the fleet console URL.",
        "observable_result": "The console loads.",
    }
    desktop = {**step, "status": desktop_status,
               "screenshot": f"qa-out/{run}/journeys/x/desktop/0.png"}
    mobile = {**step, "status": mobile_status,
              "screenshot": f"qa-out/{run}/journeys/x/mobile_390/0.png"}
    return {
        "run": run,
        "deploy_sha": sha,
        "journeys": [
            {"id": "x", "name": "Fleet console loads", "steps": [desktop]},
            {"id": "x--mobile_390", "name": "Fleet console loads (mobile_390)", "steps": [mobile]},
        ],
    }


class ViewportCollapsingTest(unittest.TestCase):
    """gh#660 PRD (Part C4): a step-0 (viewport-independent) failure at both viewports must
    file/comment/close ONCE, not once per viewport -- live proof #690/#691."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"
        self.gh = FakeGh()

    def _write(self, name: str, results: dict) -> Path:
        p = Path(self.tmp.name) / name
        p.write_text(json.dumps(results))
        return p

    def test_ac1_one_issue_not_two_for_same_step0_defect(self):
        r = self._write("r1.json", _step0_results("run-1", "sha1", "fail", "fail"))
        summary = jif.process(r, self.state_path, runner=self.gh)
        self.assertEqual(len(summary["filed"]), 1, "must collapse both viewports into one issue")
        self.assertEqual(len(self.gh.issues), 1)

    def test_ac2_body_names_both_affected_viewports(self):
        r = self._write("r1.json", _step0_results("run-1", "sha1", "fail", "fail"))
        summary = jif.process(r, self.state_path, runner=self.gh)
        issue_no = summary["filed"][0]["issue"]
        body = self.gh.issues[issue_no]["body"]
        self.assertIn("desktop", body)
        self.assertIn("mobile_390", body)

    def test_ac3_non_step0_failure_keeps_per_viewport_key(self):
        step = {"index": 3, "action": "Check layout.", "observable_result": "Renders correctly."}
        results = {
            "run": "run-1",
            "deploy_sha": "sha1",
            "journeys": [
                {"id": "x", "name": "X", "steps": [{**step, "status": "pass"}]},
                {"id": "x--mobile_390", "name": "X (mobile_390)", "steps": [{**step, "status": "fail"}]},
            ],
        }
        r = self._write("r1.json", results)
        summary = jif.process(r, self.state_path, runner=self.gh)
        self.assertEqual(len(summary["filed"]), 1)
        self.assertEqual(summary["filed"][0]["key"], "x--mobile_390::step3")

    def test_ac4_recurrence_of_collapsed_failure_comments_once(self):
        r1 = self._write("r1.json", _step0_results("run-1", "sha1", "fail", "fail"))
        jif.process(r1, self.state_path, runner=self.gh)

        r2 = self._write("r2.json", _step0_results("run-2", "sha1", "fail", "fail"))
        summary2 = jif.process(r2, self.state_path, runner=self.gh)
        self.assertEqual(summary2["filed"], [])
        self.assertEqual(len(summary2["commented"]), 1)
        self.assertEqual(len(self.gh.issues), 1)

    def test_ac5_full_recovery_at_both_viewports_closes_once(self):
        r1 = self._write("r1.json", _step0_results("run-1", "sha1", "fail", "fail"))
        summary1 = jif.process(r1, self.state_path, runner=self.gh)
        issue_no = summary1["filed"][0]["issue"]

        r2 = self._write("r2.json", _step0_results("run-2", "sha2", "pass", "pass"))
        summary2 = jif.process(r2, self.state_path, runner=self.gh)
        self.assertEqual(len(summary2["closed"]), 1)
        self.assertEqual(summary2["closed"][0]["issue"], issue_no)
        self.assertFalse(self.gh.issues[issue_no]["open"])

    def test_new_issue_names_only_the_viewports_that_actually_failed(self):
        # desktop passes, mobile_390 fails -- a first-ever occurrence, so a new issue is filed;
        # its body must not claim desktop is affected just because it shares the collapsed key.
        r = self._write("r1.json", _step0_results("run-1", "sha1", "pass", "fail"))
        summary = jif.process(r, self.state_path, runner=self.gh)
        self.assertEqual(len(summary["filed"]), 1)
        issue_no = summary["filed"][0]["issue"]
        body = self.gh.issues[issue_no]["body"]
        self.assertIn("mobile_390", body)
        self.assertNotIn("desktop, mobile_390", body)
        self.assertNotIn("mobile_390, desktop", body)

    def test_ac6_partial_recovery_does_not_close(self):
        r1 = self._write("r1.json", _step0_results("run-1", "sha1", "fail", "fail"))
        jif.process(r1, self.state_path, runner=self.gh)

        # desktop recovers, mobile_390 still fails: must not read as a full recovery.
        r2 = self._write("r2.json", _step0_results("run-2", "sha2", "pass", "fail"))
        summary2 = jif.process(r2, self.state_path, runner=self.gh)
        self.assertEqual(summary2["closed"], [])
        self.assertEqual(len(summary2["commented"]), 1)
        self.assertTrue(all(v["open"] for v in self.gh.issues.values()))


class LabelDescriptionTest(unittest.TestCase):
    """gh#892 AC1: every profile's label description must fit GitHub's 100-char cap -- past it,
    `gh label create` 422s, the label is never made, and every later `--label` issue create
    fails 'not found'. Iterates PROFILES so a future profile can't reintroduce this."""

    def test_every_profile_description_fits_the_100_char_cap(self):
        for key, profile in jif.PROFILES.items():
            self.assertLessEqual(
                len(profile.desc), 100,
                f"{key} profile's label description is {len(profile.desc)} chars, over "
                f"GitHub's 100-char cap: {profile.desc!r}",
            )


class EnsureLabelTest(unittest.TestCase):
    """gh#892 AC2-4: ensure_label() creates RED's label within the cap, stays silent on the
    benign duplicate-create (today's intended idempotent behaviour), and surfaces -- rather
    than swallows -- any other create failure."""

    def test_ac2_red_create_argv_fits_cap_and_names_red_and_785(self):
        calls = []

        def runner(cmd):
            calls.append(cmd)
            return 0, ""

        result = jif.ensure_label(runner, jif.RED)
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        desc = calls[0][calls[0].index("--description") + 1]
        self.assertLessEqual(len(desc), 100)
        self.assertIn("red", desc.lower())
        self.assertIn("785", desc)

    def test_ac3_duplicate_create_failure_is_silent(self):
        def runner(cmd):
            return 1, ('label with name "fleet:red-team" already exists; '
                       "use `--force` to update its color and description")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = jif.ensure_label(runner, jif.RED)
        self.assertIsNone(result)
        self.assertEqual(stderr.getvalue(), "")

    def test_ac4_non_duplicate_failure_is_surfaced_on_stderr(self):
        def runner(cmd):
            return 1, ("HTTP 422: Validation Failed\n"
                       "description is too long (maximum is 100 characters)")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = jif.ensure_label(runner, jif.RED)
        self.assertIsNotNone(result)
        self.assertIn("fleet:red-team", stderr.getvalue())
        self.assertIn("too long", stderr.getvalue())

    def test_ac4_zero_finding_run_cannot_report_clean_errors_when_label_create_fails(self):
        def runner(cmd):
            if cmd[1] == "label":
                return 1, ("HTTP 422: Validation Failed\n"
                           "description is too long (maximum is 100 characters)")
            assert cmd[1] == "issue" and cmd[2] == "list"
            return 0, "[]"  # no prior issue -- a passing step with no history is a true noop

        with tempfile.TemporaryDirectory() as tmp:
            results_path = Path(tmp) / "clean.json"
            results_path.write_text(json.dumps(_results("pass", "run-1", "sha1")))
            summary = jif.process(
                results_path, Path(tmp) / "state.json", runner=runner, profile=jif.RED
            )
        self.assertTrue(
            summary["errors"],
            "a failed label create must not report a clean {'errors': []}",
        )


class RedProfileEndToEndTest(unittest.TestCase):
    """gh#892 AC5: --profile red works end to end (label create + issue file) against a repo
    that does not yet have the fleet:red-team label."""

    def test_red_profile_creates_label_and_files_issue_from_cold_start(self):
        gh = FakeGh()
        with tempfile.TemporaryDirectory() as tmp:
            results_path = Path(tmp) / "r.json"
            results_path.write_text(json.dumps(_results("fail", "run-1", "sha1")))
            summary = jif.process(
                results_path, Path(tmp) / "state.json", runner=gh, profile=jif.RED
            )
        self.assertEqual(summary["errors"], [])
        self.assertEqual(len(summary["filed"]), 1)
        issue_no = summary["filed"][0]["issue"]
        self.assertIn("fleet:red-team", gh.label_create_calls[0])
        self.assertIn("LANDED", gh.issues[issue_no]["body"])


class RepoArgTest(unittest.TestCase):
    """gh#770: the filer must target an explicit repo, not whatever `gh` defaults to for the
    sandbox's ambient checkout (gh#151's nondeterministic repoint)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"
        self.gh = FakeGh()

    def _write(self, name: str, results: dict) -> Path:
        p = Path(self.tmp.name) / name
        p.write_text(json.dumps(results))
        return p

    def test_ac1_main_defaults_repo_to_philanthropy(self):
        captured = {}

        def fake_process(*args, **kwargs):
            captured["repo"] = kwargs.get("repo")
            return {"errors": []}

        results_path = self._write("r.json", _results("fail", "run-1", "sha1"))
        orig_argv, orig_process = sys.argv, jif.process
        sys.argv = ["journey_issue_filer.py", "--results", str(results_path)]
        jif.process = fake_process
        try:
            jif.main()
        finally:
            sys.argv, jif.process = orig_argv, orig_process
        self.assertEqual(captured["repo"], jif.DEFAULT_REPO)
        self.assertEqual(jif.DEFAULT_REPO, "The-Good-Project-Team/philanthropy")

    def test_ac2_build_file_cmd_includes_repo_and_label(self):
        cmd = jif.build_file_cmd("t", "b", repo="owner/name")
        idx = cmd.index("--repo")
        self.assertEqual(cmd[idx + 1], "owner/name")
        self.assertIn("--label", cmd)

    def test_ac3_build_list_cmd_includes_repo(self):
        cmd = jif.build_list_cmd(repo="owner/name")
        idx = cmd.index("--repo")
        self.assertEqual(cmd[idx + 1], "owner/name")

    def test_ac3_build_comment_cmd_includes_repo(self):
        cmd = jif.build_comment_cmd(5, "note", repo="owner/name")
        idx = cmd.index("--repo")
        self.assertEqual(cmd[idx + 1], "owner/name")

    def test_ac3_build_close_cmd_includes_repo(self):
        cmd = jif.build_close_cmd(5, "note", repo="owner/name")
        idx = cmd.index("--repo")
        self.assertEqual(cmd[idx + 1], "owner/name")

    def test_ac4_dry_run_prints_repo_and_calls_no_gh(self):
        results_path = self._write("r.json", _results("fail", "run-1", "sha1"))

        def exploding_runner(cmd):
            raise AssertionError("gh must not be called during --dry-run")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            jif.process(results_path, self.state_path, runner=exploding_runner, dry_run=True, repo="owner/name")
        self.assertIn("--repo owner/name", buf.getvalue())

    def test_unset_repo_omits_flag_entirely(self):
        # non-goal: an unset --repo must keep working for anyone running from a correct checkout.
        self.assertNotIn("--repo", jif.build_file_cmd("t", "b"))
        self.assertNotIn("--repo", jif.build_list_cmd())
        self.assertNotIn("--repo", jif.build_comment_cmd(5, "n"))
        self.assertNotIn("--repo", jif.build_close_cmd(5, "n"))

    def test_gh922_ac1_ensure_label_includes_repo(self):
        calls = []

        def runner(cmd):
            calls.append(cmd)
            return 0, ""

        jif.ensure_label(runner, jif.SENTRY, repo="owner/name")
        idx = calls[0].index("--repo")
        self.assertEqual(calls[0][idx + 1], "owner/name")

    def test_gh922_ac2_ensure_label_omits_repo_when_unset(self):
        calls = []

        def runner(cmd):
            calls.append(cmd)
            return 0, ""

        jif.ensure_label(runner, jif.SENTRY)
        self.assertNotIn("--repo", calls[0])

    def test_gh922_ac3_process_forwards_repo_to_every_board_call_including_label(self):
        # gh#922: the real regression -- ensure_label() was the one call site process() made
        # that dropped `repo`, so it created the label in the wrong repo on a fresh target and
        # every later `--label` issue create failed "not found". This drives process() end to
        # end and checks EVERY captured gh invocation, label create included.
        calls = []
        fake = FakeGh()

        def runner(cmd):
            calls.append(cmd)
            return fake(cmd)

        results_path = self._write("r.json", _results("fail", "run-1", "sha1"))
        summary = jif.process(results_path, self.state_path, runner=runner, repo="owner/name")

        self.assertTrue(calls, "process() made no gh calls to check")
        for cmd in calls:
            self.assertIn("--repo", cmd, f"missing --repo in {cmd}")
            idx = cmd.index("--repo")
            self.assertEqual(cmd[idx + 1], "owner/name")
        self.assertEqual(fake.label_create_calls[0][fake.label_create_calls[0].index("--repo") + 1], "owner/name")

    def test_gh922_ac4_cold_start_target_repo_files_clean_with_no_errors(self):
        # gh#922 AC4: a target repo where the label does not yet exist must still end with an
        # empty error summary and the finding filed -- the end-to-end scenario the defect broke.
        fake = FakeGh()
        results_path = self._write("r.json", _results("fail", "run-1", "sha1"))
        summary = jif.process(results_path, self.state_path, runner=fake, repo="owner/name")
        self.assertEqual(summary["errors"], [])
        self.assertEqual(len(summary["filed"]), 1)


if __name__ == "__main__":
    unittest.main()
