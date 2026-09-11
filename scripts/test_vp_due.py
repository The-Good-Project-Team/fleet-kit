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

    def test_three_not_yet_rounds_stop_further_reviews(self):
        """fleet-kit#798: the builder cap binds the reviewer too. Without this, an item that
        `redo_due` refuses to build is re-reviewed on every merge that mentions it (live:
        project-sketchyswap#5, six rounds, zero builds)."""
        it = item(7, comments=[("Not yet (VP review): a", T1), ("Not yet (VP review): b", T2),
                               ("Not yet (VP review): c", T3)], merged=["2026-09-08T16:00:00Z"])
        ok, why = vp_due.is_due(it)
        self.assertFalse(ok); self.assertIn("3 Not-yet rounds", why)

    def test_reif_after_the_last_not_yet_lifts_the_cap(self):
        """The release is a human one, the same override vp.md already gives him."""
        it = item(8, comments=[("Not yet (VP review): a", T1), ("Not yet (VP review): b", T2),
                               ("Not yet (VP review): c", T3),
                               ("Reif: re-scoped, look again", "2026-09-08T16:00:00Z")],
                  merged=["2026-09-08T17:00:00Z"])
        ok, why = vp_due.is_due(it)
        self.assertTrue(ok); self.assertEqual(why, "merge newer than last verdict")

    def test_two_not_yet_rounds_still_due(self):
        it = item(9, comments=[("Not yet (VP review): a", T1), ("Not yet (VP review): b", T2)],
                  merged=[T3])
        self.assertTrue(vp_due.is_due(it)[0])

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

    def test_redo_after_not_yet_with_no_newer_merge(self):
        it = item(20, comments=[("Not yet (VP review): thin\n1. ...", T2)], merged=[T1])
        ok, why = vp_due.redo_due(it)
        self.assertTrue(ok); self.assertIn("round 1", why)

    def test_no_redo_when_merge_is_newer_than_not_yet(self):
        it = item(21, comments=[("Not yet (VP review): thin", T1)], merged=[T2])
        self.assertFalse(vp_due.redo_due(it)[0])

    def test_no_redo_after_three_rounds(self):
        it = item(22, comments=[("Not yet (VP review): a", T1), ("Not yet (VP review): b", T2), ("Not yet (VP review): c", T3)])
        ok, why = vp_due.redo_due(it)
        self.assertFalse(ok); self.assertIn("3 Not-yet rounds", why)

    def test_no_redo_when_approved(self):
        it = item(23, comments=[("Not yet (VP review): a", T1), ("Design approved (VP review): ok", T2)])
        self.assertFalse(vp_due.redo_due(it)[0])

    def test_no_redo_when_minion_running(self):
        it = item(24, comments=[("Not yet (VP review): a", T1)])
        self.assertFalse(vp_due.redo_due(it, running_minions={24})[0])

    def test_no_redo_when_reif_spoke_after_verdict(self):
        it = item(25, comments=[("Not yet (VP review): a", T1), ("Reif: leave it", T2)])
        self.assertFalse(vp_due.redo_due(it)[0])

    def test_running_items_reads_runs_rows_not_ps(self):
        now = 1_000_000.0
        rows = [
            {"member": "minion", "run_id": "a", "item_id": "4863", "status": "started", "ts": now - 300},
            {"member": "minion", "run_id": "b", "item_id": "3235", "status": "started", "ts": now - 200},
            {"member": "minion", "run_id": "b", "item_id": "3235", "status": "ok", "ts": now - 100},   # finished: newest row wins
            {"member": "minion", "run_id": "c", "item_id": "111", "status": "started", "ts": now - 5000},  # older than timeout: dead
            {"member": "vp", "run_id": "d", "item_id": "4863", "status": "started", "ts": now - 60},
        ]
        self.assertEqual(vp_due.running_items(rows, "minion", now), {4863})
        self.assertEqual(vp_due.running_items(rows, "vp", now), {4863})
        self.assertEqual(vp_due.running_items(rows, "marie", now), set())

    def test_due_items_shape(self):
        out = vp_due.due_items([item(10, merged=[T1]), item(11)])
        self.assertEqual(out["due"], [10])
        self.assertEqual(out["skipped"][0]["number"], 11)


class ClaimsItemTests(unittest.TestCase):
    """gh#636 VP round-2 fix 7: `collect()` must only count a PR as a merged build slice for
    #N when its body actually CLAIMS #N (Fixes/Closes/Resolves/Part of), never a bare `#N`
    mention in prose. Live proof: PR #842's body says '...epics stop looking buildable to
    gru... #636...' -- a sentence about a different item's fix, not a claim on #636."""

    def test_fixes_hash_n_claims(self):
        self.assertTrue(vp_due._claims_item("Fixes #636", 636))

    def test_closes_hash_n_claims(self):
        self.assertTrue(vp_due._claims_item("Some intro.\n\nCloses #636.", 636))

    def test_part_of_hash_n_claims(self):
        self.assertTrue(vp_due._claims_item("Part of #636 -- seam 3 of 7.", 636))

    def test_bare_mention_in_prose_does_not_claim(self):
        body = ("Tracking epics stop looking buildable to gru (gh#634 fix 1)\n\n"
                "VP's round-1 review of fk#634 found 5 of 6 open fleet:epic issues... #636 ...")
        self.assertFalse(vp_due._claims_item(body, 636))

    def test_does_not_match_a_different_number(self):
        self.assertFalse(vp_due._claims_item("Fixes #6360", 636))
        self.assertFalse(vp_due._claims_item("Fixes #63", 636))

    def test_numbered_list_prefix_still_claims(self):
        """`^\\W*` alone stops at a leading digit (digits are word characters), so a numbered
        PR checklist -- the exact style this codebase's own PR bodies use -- needs its own
        allowance or a real claim silently stops counting."""
        self.assertTrue(vp_due._claims_item("1. Fixes #636", 636))
        self.assertTrue(vp_due._claims_item("Changes:\n2) Closes #636 -- seam 2.", 636))

    def test_numbered_list_item_that_only_mentions_the_number_does_not_claim(self):
        self.assertFalse(vp_due._claims_item("1. See #636 for background.", 636))

    def test_collect_only_counts_claiming_prs(self):
        """End-to-end through the same filter collect() applies, without a live `gh` call."""
        prs = [
            {"number": 1, "mergedAt": T1, "body": "Fixes #636"},
            {"number": 2, "mergedAt": T2, "body": "unrelated PR that happens to say #636 in passing"},
        ]
        claiming = [p for p in prs if vp_due._claims_item(p["body"], 636)]
        self.assertEqual([p["number"] for p in claiming], [1])


def epic(n, comments=(), sub_issues=None):
    return {"number": n, "labels": ["fleet:epic"],
            "comments": [{"body": b, "createdAt": t} for b, t in comments],
            "subIssues": sub_issues}


def sub_issues(*numbers_and_states):
    """{"nodes": [...]} shaped like `gh issue list --json subIssues` -- (number, state) pairs."""
    return {"nodes": [{"number": n, "state": s} for n, s in numbers_and_states]}


class RealChildrenTests(unittest.TestCase):
    """fk#854 AC1: a real child comes from GitHub's own `subIssues` link when the epic has one,
    else marie's `decomposed into #a, #b, ...` comment -- never a number merely mentioned
    somewhere else on the issue."""

    def test_subissues_present_wins(self):
        it = epic(634, sub_issues=sub_issues((651, "OPEN"), (652, "CLOSED")))
        children, source = vp_due.real_children(it)
        self.assertEqual(children, [651, 652])
        self.assertEqual(source, "subIssues")

    def test_falls_back_to_decomposed_comment_when_no_subissues(self):
        it = epic(634, comments=[("marie: decomposed into #10, #11 (Part C2b)", T1)])
        children, source = vp_due.real_children(it)
        self.assertEqual(children, [10, 11])
        self.assertIn("decomposed into", source)

    def test_empty_subissues_nodes_falls_back_too(self):
        it = epic(634, comments=[("marie: decomposed into #10 (Part C2b)", T1)], sub_issues=sub_issues())
        children, source = vp_due.real_children(it)
        self.assertEqual(children, [10])

    def test_neither_source_returns_empty(self):
        it = epic(634, comments=[("**Not yet (VP review):** thin, no child named", T1)])
        children, source = vp_due.real_children(it)
        self.assertEqual(children, [])
        self.assertEqual(source, "no source")


class RedoTargetsTests(unittest.TestCase):
    """fk#634 round-2 fix 1: PR#842 closed gru's door onto a tracking-only epic; this one --
    vp_due spawning a redo minion straight at the epic itself -- was still open, and is the bug
    that built round 1's own fix.

    fk#854 round-3 fix 1: PR#845's own `named_children()` regex-scraped every `#N` out of the
    newest `Not yet (VP review):` comment with no parent/child check -- live on #634 this
    returned 10 "targets", 5 of them other teams' tracking epics, named only because the verdict
    was *counting* open epics ("all 6 open fleet:epic issues now drop..."). These tests replace
    that prose scrape with real children only (`vp_due.real_children`)."""

    def test_non_epic_redo_targets_itself(self):
        it = item(30, comments=[("Not yet (VP review): thin\n1. ...", T1)])
        targets, why = vp_due.redo_targets(it, is_open=lambda n: True)
        self.assertEqual(targets, [30])
        self.assertEqual(why, "single item")

    def test_epic_redo_targets_open_subissues_children(self):
        it = epic(634, sub_issues=sub_issues((652, "OPEN"), (653, "OPEN")))
        targets, why = vp_due.redo_targets(it, is_open=lambda n: True)
        self.assertEqual(targets, [652, 653])
        self.assertIn("open child issue(s) via subIssues", why)

    def test_epic_redo_ignores_numbers_named_only_in_the_verdicts_prose(self):
        """fk#854 AC1/AC3: #634's real (live) shape -- subIssues names #651/#653/#654/#655 as
        open children, but the newest `Not yet` comment's prose also names five OTHER open
        `fleet:epic` issues (#785/#728/#636/#559/#553) purely because it was counting them.
        Those five must never appear in `targets`, whatever named them."""
        body = ("**Not yet (VP review):** all 6 open `fleet:epic` issues (#785 #728 #636 #634 "
                "#559 #553) now drop with the tracking-only reason. Fixes belong to #651, #653, "
                "#654, #655, plus PR #842 which already merged.")
        it = epic(634, comments=[(body, T1)],
                  sub_issues=sub_issues((649, "CLOSED"), (650, "CLOSED"), (651, "OPEN"),
                                         (652, "CLOSED"), (653, "OPEN"), (654, "OPEN"),
                                         (655, "OPEN")))
        opened = {651, 653, 654, 655}
        targets, why = vp_due.redo_targets(it, is_open=lambda n: n in opened)
        self.assertEqual(targets, [651, 653, 654, 655])
        for other_epic in (785, 728, 636, 559, 553):
            self.assertNotIn(other_epic, targets)

    def test_epic_redo_falls_back_to_decomposed_comment_when_no_subissues(self):
        it = epic(634, comments=[("marie: decomposed into #10, #11 (Part C2b)", T1)])
        targets, why = vp_due.redo_targets(it, is_open=lambda n: True)
        self.assertEqual(targets, [10, 11])
        self.assertIn("decomposed into", why)

    def test_epic_redo_excludes_a_real_child_that_is_itself_an_epic(self):
        """fk#854 AC2: a real child that itself carries `fleet:epic` is never dispatched,
        whatever named it -- `is_open` (which in production also checks the candidate's own
        labels, see `vp_due._valid_redo_target`) is what excludes it here."""
        it = epic(634, sub_issues=sub_issues((651, "OPEN"), (656, "OPEN")))
        not_itself_an_epic = {651}  # 656 is open but is itself a nested fleet:epic
        targets, why = vp_due.redo_targets(it, is_open=lambda n: n in not_itself_an_epic)
        self.assertEqual(targets, [651])

    def test_epic_redo_falls_back_to_parent_when_no_child_named(self):
        it = epic(634, comments=[("**Not yet (VP review):** thin, no child named", T1)])
        targets, why = vp_due.redo_targets(it, is_open=lambda n: True)
        self.assertEqual(targets, [634])
        self.assertEqual(why, "epic-level redo: no child named")

    def test_epic_redo_falls_back_when_real_children_are_closed(self):
        """#649 and #650 are real subIssues (already closed and built) -- being real is not
        enough to dispatch a builder onto a closed issue."""
        it = epic(634, sub_issues=sub_issues((649, "CLOSED"), (650, "CLOSED")))
        targets, why = vp_due.redo_targets(it, is_open=lambda n: False)
        self.assertEqual(targets, [634])
        self.assertEqual(why, "epic-level redo: no child named")

    def test_redo_items_dispatches_epic_children(self):
        it = epic(634, comments=[("**Not yet (VP review):** #652 owns it.", T1)],
                  sub_issues=sub_issues((652, "OPEN")))
        out = vp_due.redo_items([it], is_open=lambda n: True)
        self.assertEqual(out["redo"], [{"number": 634, "targets": [652], "why": "1 open child issue(s) via subIssues"}])

    def test_redo_items_preserves_non_epic_shape(self):
        it = item(31, comments=[("Not yet (VP review): a", T1)])
        out = vp_due.redo_items([it])
        self.assertEqual(out["redo"], [{"number": 31, "targets": [31], "why": "single item"}])


if __name__ == "__main__":
    unittest.main()
