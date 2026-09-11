"""Regression test for closes_gate.py's decomposed-children parser (fk#882): marie's
`decomposed into #a, #b, ...` comment almost always carries a per-child `(seq:N -- ...)`
parenthetical, or a range/slash shorthand, none of which the old `,`/`and`-only alternation
tolerated -- it silently truncated after the first entry, so `epic_closable()` reported
`closable: true` on a list it had only partly read. Covers AC1-6 of the fk#882 PRD comment
(2026-09-11). No network.

Run: python3 scripts/test_closes_gate.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import closes_gate as cg  # noqa: E402


def comment(created_at, body):
    return {"createdAt": created_at, "body": body}


class DecomposedChildrenTests(unittest.TestCase):
    def test_ac1_parenthetical_descriptions_do_not_truncate_the_list(self):
        body = (
            "decomposed into #560 (home=the number), #561 (percent-of-week topbar), "
            "#562 (collapse started+completion rows), #563 (Stats skeletons+perf+bold verdict), "
            "#564 (rename Self-Evolution tab), #565 (members list visual states) (Part C2b) "
            "— tracking-only from here"
        )
        iss = {"comments": [comment("2026-09-06T03:42:49Z", body)]}
        self.assertEqual(cg.decomposed_children(iss), [560, 561, 562, 563, 564, 565])

    def test_ac2_parenthetical_marker_is_not_read_as_a_child(self):
        body = "decomposed into #560 (home), #561 (topbar) (Part C2b) — tracking-only from here"
        iss = {"comments": [comment("x", body)]}
        self.assertNotIn(2, cg.decomposed_children(iss))
        self.assertEqual(cg.decomposed_children(iss), [560, 561])

    def test_ac3_bare_comma_list_still_works(self):
        body = "decomposed into #794, #795, #796 (Part C2b) — tracking-only from here"
        iss = {"comments": [comment("x", body)]}
        self.assertEqual(cg.decomposed_children(iss), [794, 795, 796])

    def test_ac4_range_shorthand_returns_only_the_named_endpoints(self):
        iss = {"comments": [comment("x", "decomposed into #4642-#4645")]}
        self.assertEqual(cg.decomposed_children(iss), [4642, 4645])

    def test_ac4_slash_shorthand_returns_every_number(self):
        iss = {"comments": [comment("x", "decomposed into #794/#795/#796")]}
        self.assertEqual(cg.decomposed_children(iss), [794, 795, 796])

    def test_ac5_prose_quoting_the_phrase_with_no_real_numbers_yields_empty_and_does_not_overwrite(self):
        iss = {
            "comments": [
                comment("2026-09-01T00:00:00Z", "marie: decomposed into #10, #11 (Part C2b)"),
                comment("2026-09-11T06:07:33Z", "this issue was decomposed into #a, #b, … per the filer"),
            ]
        }
        self.assertEqual(cg.decomposed_children(iss), [10, 11])

    def test_ac6_a_child_whose_state_could_not_be_fetched_blocks_the_epic(self):
        epic = {
            "title": "epic",
            "labels": [{"name": "fleet:epic"}],
            "body": "",
            "comments": [comment("a", "decomposed into #10, #11 (Part C2b)")],
        }
        # #11 is absent from `issues` -- a fetch failure, not a confirmed close
        issues = {634: epic, 10: {"state": "CLOSED"}}
        ec = cg.epic_closable(epic, issues)
        self.assertFalse(ec["closable"])
        self.assertIn("11", ec["reason"])

    def test_ac6_all_children_confirmed_closed_is_still_closable(self):
        epic = {
            "title": "epic",
            "labels": [{"name": "fleet:epic"}],
            "body": "",
            "comments": [comment("a", "decomposed into #10, #11 (Part C2b)")],
        }
        issues = {634: epic, 10: {"state": "CLOSED"}, 11: {"state": "CLOSED"}}
        ec = cg.epic_closable(epic, issues)
        self.assertTrue(ec["closable"])


if __name__ == "__main__":
    unittest.main()
