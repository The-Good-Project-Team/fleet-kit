"""Regression test for gh#340.

fleet:needs-human-op had zero notification surface: nothing but gru's own build-candidate
filter ever read it, so an issue like gh#278/gh#301 could sit silently for 11h40m+ with no
human ever seeing it. poll_gh_state() now makes an independent `gh issue list
--label fleet:needs-human-op` call (not a filter over the fleet:backlog list, since nothing
enforces that pairing) and returns a count + oldest age in hours.

Run: python3 scripts/test_needs_human_op.py
"""
from __future__ import annotations

import datetime
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import fleet_view_server as fvs  # noqa: E402


def _iso_hours_ago(hours: float) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fake_gh(needs_human_op_issues):
    def _gh(*args, **kwargs):
        if "fleet:needs-human-op" in args:
            return json.dumps(needs_human_op_issues)
        if args and args[0] in ("pr", "issue"):
            return "[]"
        return ""
    return _gh


class NeedsHumanOpSurface(unittest.TestCase):
    def test_zero_open_issues_yields_zero_count(self):
        with unittest.mock.patch.object(fvs, "_gh", side_effect=_fake_gh([])):
            gh = fvs.poll_gh_state()
        self.assertEqual(gh["needs_human_op"], {"count": 0, "oldest_age_hours": 0.0})

    def test_count_and_oldest_age_reflect_the_independent_call(self):
        issues = [
            {"number": 278, "title": "deploy pipeline down", "createdAt": _iso_hours_ago(11.7)},
            {"number": 301, "title": "pagers crashing", "createdAt": _iso_hours_ago(3.0)},
        ]
        with unittest.mock.patch.object(fvs, "_gh", side_effect=_fake_gh(issues)):
            gh = fvs.poll_gh_state()
        self.assertEqual(gh["needs_human_op"]["count"], 2)
        # Oldest of the two (~11.7h), not the newest and not a sum.
        self.assertAlmostEqual(gh["needs_human_op"]["oldest_age_hours"], 11.7, delta=0.1)

    def test_missing_createdat_is_skipped_not_fatal(self):
        issues = [{"number": 999, "title": "no timestamp"}]
        with unittest.mock.patch.object(fvs, "_gh", side_effect=_fake_gh(issues)):
            gh = fvs.poll_gh_state()
        self.assertEqual(gh["needs_human_op"], {"count": 1, "oldest_age_hours": 0.0})

    def test_call_is_independent_of_the_fleet_backlog_label_filter(self):
        """gh#340 open question: a fleet:needs-human-op issue not co-labeled fleet:backlog
        must still be counted -- confirms this isn't a client-side filter over `issues`."""
        issues = [{"number": 361, "title": "budget deficit", "createdAt": _iso_hours_ago(1.0)}]
        calls = []

        def _gh(*args, **kwargs):
            calls.append(args)
            return _fake_gh(issues)(*args, **kwargs)

        with unittest.mock.patch.object(fvs, "_gh", side_effect=_gh):
            gh = fvs.poll_gh_state()
        self.assertEqual(gh["needs_human_op"]["count"], 1)
        needs_human_op_calls = [c for c in calls if "fleet:needs-human-op" in c]
        self.assertEqual(len(needs_human_op_calls), 1,
                          "expected exactly one independent gh call for fleet:needs-human-op")


if __name__ == "__main__":
    unittest.main()
