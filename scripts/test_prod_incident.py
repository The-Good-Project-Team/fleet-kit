#!/usr/bin/env python3
"""test_prod_incident.py -- gh#728 VP follow-up (round 2, fix 3): prod_health_check.py (gh#727)
and fixer_fire_path.py (gh#728) used to dedupe against two incompatible keys (an exact issue
title vs. a hidden marker gh's own --search can't reliably find), so one real outage could file
two open incident tickets, one per member. These tests exercise the shared module directly, and
the cross-member scenario the VP review asked for by name: whichever member files first, the
other finds that SAME issue by the shared marker and comments instead of creating a second one.

Run: python3 scripts/test_prod_incident.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_incident as pi  # noqa: E402


class FakeGh:
    """Replays enough of `gh issue {create,list,comment}` against an in-memory store."""

    def __init__(self):
        self.issues: dict[int, dict] = {}
        self._next = 1

    def __call__(self, cmd: list[str]) -> tuple[int, str]:
        assert cmd[0] == "gh" and cmd[1] == "issue"
        sub = cmd[2]
        if sub == "create":
            title = cmd[cmd.index("--title") + 1]
            body = cmd[cmd.index("--body") + 1]
            n = self._next
            self._next += 1
            self.issues[n] = {"title": title, "body": body, "open": True, "comments": []}
            return 0, f"https://github.com/x/y/issues/{n}"
        if sub == "list":
            return 0, json.dumps([
                {"number": n, "body": v["body"]} for n, v in self.issues.items() if v["open"]
            ])
        if sub == "comment":
            n = int(cmd[3])
            self.issues[n]["comments"].append(cmd[cmd.index("--body") + 1])
            return 0, "commented"
        raise AssertionError(f"unexpected gh subcommand: {sub}")


class SearchNeverUsesGhFullTextSearchTests(unittest.TestCase):
    def test_list_command_has_no_search_flag(self):
        cmd = pi.build_list_open_incidents_cmd("acme/prod")
        self.assertNotIn("--search", cmd)
        self.assertIn("--repo", cmd)
        self.assertIn("acme/prod", cmd)
        self.assertIn(pi.INCIDENT_LABEL, cmd)

    def test_decoy_body_mentioning_markers_words_is_not_a_match(self):
        """A gh full-text search on the marker's words would match an issue that merely talks
        about it (verified live: --search on this marker returned 5 issues in this repo that
        never contained it). An exact substring test on the body must not."""
        gh = FakeGh()
        gh.issues[1] = {"title": "unrelated", "open": True,
                         "body": "discusses the fixer fire path incident marker but lacks it",
                         "comments": []}
        gh.issues[2] = {"title": "real one", "open": True,
                         "body": f"real\n\n{pi.INCIDENT_MARKER}", "comments": []}
        self.assertEqual(pi.find_open_incident("acme/prod", run=gh), 2)


class FileOrUpdateIncidentTests(unittest.TestCase):
    def test_no_existing_incident_creates_one_with_the_marker(self):
        gh = FakeGh()
        number, created = pi.file_or_update_incident("acme/prod", "t", "b", ["incident"], run=gh)
        self.assertTrue(created)
        self.assertIn(pi.INCIDENT_MARKER, gh.issues[number]["body"])

    def test_existing_marked_incident_is_commented_on_not_duplicated(self):
        gh = FakeGh()
        first, created = pi.file_or_update_incident("acme/prod", "t", "b", ["incident"], run=gh)
        second, created2 = pi.file_or_update_incident("acme/prod", "t2", "b2", ["incident"], run=gh)
        self.assertEqual(first, second)
        self.assertFalse(created2)
        self.assertEqual(len(gh.issues), 1)
        self.assertIn("b2", gh.issues[first]["comments"])


class CrossMemberDedupTests(unittest.TestCase):
    """The scenario the VP review named directly: one outage, two members watching, one
    ticket. Simulates prod_health_check.py filing first and fixer_fire_path.py firing second
    against the SAME fake gh backend -- and the reverse order."""

    def test_prod_health_check_files_first_fixer_fire_path_comments_on_the_same_issue(self):
        gh = FakeGh()
        # Shaped like prod_health_check.py's probe-down filing: title/labels differ from
        # fixer_fire_path.py's, but both go through the same shared helper.
        phc_number, phc_created = pi.file_or_update_incident(
            pi.INCIDENT_REPO, "prod down: https://philanthropy.org/990", "probe body",
            ["fleet:backlog", "lane:devops", "fleet:priority-high", "incident"], run=gh)
        self.assertTrue(phc_created)

        # Shaped like fixer_fire_path.py's own rollback filing: different title/labels.
        ffp_number, ffp_created = pi.file_or_update_incident(
            pi.INCIDENT_REPO, "PROD DOWN -- automatic rollback succeeded", "rollback body",
            ["fleet:priority-high", "incident"], run=gh)

        self.assertFalse(ffp_created, "fixer_fire_path filed a SECOND incident for the same outage")
        self.assertEqual(ffp_number, phc_number)
        self.assertEqual(len(gh.issues), 1)
        self.assertIn("rollback body", gh.issues[phc_number]["comments"])

    def test_fixer_fire_path_files_first_prod_health_check_comments_on_the_same_issue(self):
        gh = FakeGh()
        ffp_number, ffp_created = pi.file_or_update_incident(
            pi.INCIDENT_REPO, "PROD DOWN -- automatic rollback succeeded", "rollback body",
            ["fleet:priority-high", "incident"], run=gh)
        self.assertTrue(ffp_created)

        phc_number, phc_created = pi.file_or_update_incident(
            pi.INCIDENT_REPO, "prod down: https://philanthropy.org/990", "probe body",
            ["fleet:backlog", "lane:devops", "fleet:priority-high", "incident"], run=gh)

        self.assertFalse(phc_created, "prod_health_check filed a SECOND incident for the same outage")
        self.assertEqual(phc_number, ffp_number)
        self.assertEqual(len(gh.issues), 1)


if __name__ == "__main__":
    unittest.main()
