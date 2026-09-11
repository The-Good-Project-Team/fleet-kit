#!/usr/bin/env python3
"""test_red_walker.py -- gh#881 acceptance criteria for red_walker.py's --item selector.

Pure unit tests only (criterion 7): no Playwright, no network. select_attacks() and
fetch_item_text() are exercised directly with fixture PRD text and a fixture catalog;
main()'s zero-selection branch (criteria 2 and 3) is exercised by monkeypatching
load_catalog and fetch_item_text so the real gh CLI and PyYAML are never touched.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import red_walker as rw  # noqa: E402


FIXTURE_ATTACKS = [
    {"id": "reflect-990", "kind": "reflection", "target": {"path": "/990/"}},
    {"id": "overflow-report", "kind": "overflow", "target": {"path": "/990/report/"}},
    {"id": "idor-org", "kind": "idor",
     "target": {"path": "FIXTURE_OTHER_ORG_ADMIN_URL", "needs": ["FIXTURE_OTHER_ORG_ADMIN_URL"]}},
]


class SelectAttacksTest(unittest.TestCase):
    def test_criterion1_prd_text_contains_path_selects_it(self):
        # "/990/" is itself a substring of "/990/report/", so both attacks whose target path
        # is a substring of the PRD text are selected -- exactly the "best-effort substring
        # match" red_walker.py's own usage text documents; only idor-org's env-var target
        # (not a real path) is absent from this PRD and correctly excluded.
        prd = "## Acceptance criteria\n1. Given /990/report/ renders a claim link..."
        selected = rw.select_attacks(FIXTURE_ATTACKS, item_text=prd, attacks_filter=None)
        self.assertEqual([a["id"] for a in selected], ["reflect-990", "overflow-report"])

    def test_criterion2_no_path_named_selects_nothing(self):
        prd = "## Acceptance criteria\n1. Given the homepage loads..."
        selected = rw.select_attacks(FIXTURE_ATTACKS, item_text=prd, attacks_filter=None)
        self.assertEqual(selected, [])

    def test_criterion4_unscoped_considers_full_catalog(self):
        selected = rw.select_attacks(FIXTURE_ATTACKS, item_text=None, attacks_filter=None)
        self.assertEqual([a["id"] for a in selected], [a["id"] for a in FIXTURE_ATTACKS])

    def test_criterion6_item_and_attacks_filter_compose(self):
        prd = "/990/ and /990/report/ both matter here"
        selected = rw.select_attacks(FIXTURE_ATTACKS, item_text=prd, attacks_filter=["reflect-990"])
        self.assertEqual([a["id"] for a in selected], ["reflect-990"])

    def test_direction_is_prd_contains_path_not_issue_number_in_target_blob(self):
        # gh#881's bug: an issue number like "573" was checked against json.dumps(target),
        # which can never contain a bare issue number, so every attack was always skipped.
        # Confirm that shape stays gone: an issue number alone, no path, selects nothing.
        prd = "issue 573 with no page path named"
        selected = rw.select_attacks(FIXTURE_ATTACKS, item_text=prd, attacks_filter=None)
        self.assertEqual(selected, [])


class FetchItemTextTest(unittest.TestCase):
    def test_concatenates_body_and_comments(self):
        def fake_run(args, capture_output, text, timeout):
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "body": "the body names /990/report/",
                "comments": [{"body": "a later comment names /990/"}],
            }), stderr="")

        text = rw.fetch_item_text("881", run=fake_run)
        self.assertIn("/990/report/", text)
        self.assertIn("/990/", text)

    def test_criterion5_gh_failure_exits_nonzero_naming_issue(self):
        def fake_run(args, capture_output, text, timeout):
            return SimpleNamespace(returncode=1, stdout="", stderr="could not resolve to an issue")

        with self.assertRaises(SystemExit) as ctx:
            rw.fetch_item_text("999999", run=fake_run)
        self.assertIn("999999", str(ctx.exception))

    def test_criterion5_gh_timeout_exits_nonzero_naming_issue(self):
        import subprocess as sp

        def fake_run(args, capture_output, text, timeout):
            raise sp.TimeoutExpired(cmd=args, timeout=timeout)

        with self.assertRaises(SystemExit) as ctx:
            rw.fetch_item_text("881", run=fake_run)
        self.assertIn("881", str(ctx.exception))


class SelectAttacksEmptyAttacksFilterTest(unittest.TestCase):
    def test_empty_attacks_list_means_no_filter_not_zero_selection(self):
        # argparse's `nargs="*"` gives [] for a bare `--attacks` with no values; that must
        # keep meaning "no --attacks filter", same as the old truthiness check did, not
        # "filter to nothing."
        selected = rw.select_attacks(FIXTURE_ATTACKS, item_text=None, attacks_filter=[])
        self.assertEqual([a["id"] for a in selected], [a["id"] for a in FIXTURE_ATTACKS])


class MainZeroSelectionTest(unittest.TestCase):
    """Criteria 2 and 3: a scoped run that selects zero attacks writes a distinguishing
    field into the same results object vp reads (red_walker.py:307) and exits non-zero --
    never the 0 landed/0 blocked shape a clean run also produces."""

    def test_zero_selection_is_nonzero_exit_with_reason_in_results(self):
        orig_argv, orig_load, orig_fetch = sys.argv, rw.load_catalog, rw.fetch_item_text
        rw.load_catalog = lambda path: {"attacks": FIXTURE_ATTACKS, "viewports": {}}
        rw.fetch_item_text = lambda item: "no matching path named here"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sys.argv = ["red_walker.py", "--item", "573", "--out", tmp]
                rc = rw.main()
                results_files = list(Path(tmp).rglob("results.json"))
                self.assertEqual(len(results_files), 1)
                data = json.loads(results_files[0].read_text())
        finally:
            sys.argv, rw.load_catalog, rw.fetch_item_text = orig_argv, orig_load, orig_fetch

        self.assertNotEqual(rc, 0)
        self.assertEqual(data["selected"], 0)
        self.assertIn("573", data["reason"])
        self.assertEqual(data["summary"], {"attacks": 0, "landed": 0, "blocked": 0})


if __name__ == "__main__":
    unittest.main()
