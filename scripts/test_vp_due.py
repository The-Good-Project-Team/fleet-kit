"""Regression test for vp_due.py: a world-class item is due for a VP review when a merged PR that
references it is newer than the newest VP verdict, unless Reif spoke after the merge.

Run: python3 scripts/test_vp_due.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import vp_due  # noqa: E402

T1, T2, T3 = "2026-09-08T13:00:00Z", "2026-09-08T14:00:00Z", "2026-09-08T15:00:00Z"


def item(n, comments=(), merged=()):
    return {"number": n,
            "comments": [{"body": b, "createdAt": t} for b, t in comments],
            "merged_prs": [{"number": 1, "mergedAt": t} for t in merged]}


class DueTests(unittest.TestCase):
    def test_merge_and_no_verdict_is_due(self):
        ok, why = vp_due.is_due(item(1, merged=[T1]))
        self.assertTrue(ok); self.assertEqual(why, "no verdict yet")

    def test_nothing_merged_is_not_due(self):
        self.assertFalse(vp_due.is_due(item(2))[0])

    def test_verdict_after_merge_is_not_due(self):
        it = item(3, comments=[("Not yet (VP review): thin evidence\n1. ...", T2)], merged=[T1])
        self.assertFalse(vp_due.is_due(it)[0])

    def test_merge_after_verdict_is_due(self):
        it = item(4, comments=[("**Not yet (VP review):** thin", T1)], merged=[T2])
        ok, why = vp_due.is_due(it)
        self.assertTrue(ok); self.assertEqual(why, "merge newer than last verdict")

    def test_design_approved_after_merge_is_not_due(self):
        it = item(5, comments=[("Design approved (VP review): clears the bar", T2)], merged=[T1])
        self.assertFalse(vp_due.is_due(it)[0])

    def test_reif_comment_after_merge_blocks(self):
        it = item(6, comments=[("Reif: hold this, I want to look first", T3)], merged=[T2])
        ok, why = vp_due.is_due(it)
        self.assertFalse(ok); self.assertIn("Reif", why)

    def test_reif_comment_before_merge_does_not_block(self):
        it = item(7, comments=[("Reif: go", T1)], merged=[T2])
        self.assertTrue(vp_due.is_due(it)[0])

    def test_running_vp_is_skipped(self):
        self.assertFalse(vp_due.is_due(item(8, merged=[T1]), running={8})[0])

    def test_non_verdict_comments_are_ignored(self):
        it = item(9, comments=[("claimed-by: gru", T2), ("marie: priority=high", T3)], merged=[T1])
        self.assertTrue(vp_due.is_due(it)[0])

    def test_due_items_shape(self):
        out = vp_due.due_items([item(10, merged=[T1]), item(11)])
        self.assertEqual(out["due"], [10])
        self.assertEqual(out["skipped"][0]["number"], 11)


if __name__ == "__main__":
    unittest.main()
