"""Regression test for vision_link_gate.py, gh#525's build-eligibility gate.

Written by dumbledore, 2026-09-07, after the gate produced FIVE separate live bugs in its
first ~30 hours (gh#576 started-row double-count, the #579 dash-less-maintenance fix, gh#584
dash-separator gap, gh#593 gate-vs-dead-end-filter ordering, gh#595 heading-style invisibility)
with zero regression coverage despite this repo's own test_*.py convention (see
test_needs_human_op.py, test_alert_store.py, etc.) covering every other script born around the
same time. Each of those bugs cost a live gru pass discovering it the hard way. This file locks
in every format variant already found live so the next one is caught here, not in gru.log.

Run: python3 scripts/test_vision_link_gate.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import vision_link_gate as vlg  # noqa: E402


class ClassifyValueTests(unittest.TestCase):
    def test_real_link_is_linked(self):
        status, raw = vlg._classify_value("Stripe MRR -- the number, gh#519")
        self.assertEqual(status, vlg.STATUS_LINKED)

    def test_maintenance_dash_form(self):
        # The original, always-worked form: "none (maintenance) -- <reason>".
        status, _ = vlg._classify_value("none (maintenance) -- fleet-internal tooling.")
        self.assertEqual(status, vlg.STATUS_MAINTENANCE)

    def test_maintenance_no_dash_trailing_qualifier_gh584(self):
        # gh#584: no dash separator at all, plus an extra qualifier inside the parens.
        status, _ = vlg._classify_value("none (fleet guardrail/maintenance).")
        self.assertEqual(status, vlg.STATUS_MAINTENANCE)

    def test_maintenance_case_and_spacing_insensitive(self):
        status, _ = vlg._classify_value("  None ( Maintenance )  ")
        self.assertEqual(status, vlg.STATUS_MAINTENANCE)

    def test_real_link_mentioning_maintenance_is_not_swallowed(self):
        # A real link that happens to use the word "maintenance" must not be mis-read as the
        # maintenance token just because the substring appears somewhere in the value.
        status, _ = vlg._classify_value(
            "Reduces support/maintenance load on the on-call rotation -- gh#601"
        )
        self.assertEqual(status, vlg.STATUS_LINKED)


class ClassifyCandidateTests(unittest.TestCase):
    def test_missing_when_no_vision_link_line(self):
        status, raw = vlg.classify_candidate("Just a plain issue body, no field at all.", [])
        self.assertEqual(status, vlg.STATUS_MISSING)
        self.assertIsNone(raw)

    def test_heading_style_is_missing_not_linked_gh595(self):
        # gh#595: marie.md's old template produced a `## Vision-link` heading with the value
        # on the next line. run_report._vision_claim requires the colon on the same line as
        # the label, so this must resolve to MISSING (never LINKED) -- a false LINKED would
        # silently starve every real maintenance candidate via the gate's crowd-out rule,
        # which is exactly what gh#593 named as the failure mode for a wrongly-LINKED value.
        body = "## Vision-link\nnone (maintenance) -- fleet-internal tooling.\n"
        status, raw = vlg.classify_candidate(body, [])
        self.assertEqual(status, vlg.STATUS_MISSING)

    def test_inline_form_is_maintenance(self):
        body = "## Out of scope\nNone.\n\nVision-link: none (maintenance) -- tooling only.\n"
        status, raw = vlg.classify_candidate(body, [])
        self.assertEqual(status, vlg.STATUS_MAINTENANCE)

    def test_newest_comment_wins_over_body(self):
        body = "Vision-link: none (maintenance) -- old value."
        comments = [
            {"body": "irrelevant earlier comment"},
            {"body": "Vision-link: Signal rate (KR2) -- rescored PRD."},
        ]
        status, raw = vlg.classify_candidate(body, comments)
        self.assertEqual(status, vlg.STATUS_LINKED)
        self.assertIn("Signal rate", raw)


class GateCandidatesOrderingTests(unittest.TestCase):
    def test_maintenance_only_pool_is_all_eligible(self):
        # gh#593: with no real-link candidate anywhere in the pool, every maintenance-only
        # candidate must be eligible -- a doomed-but-mislabeled LINKED candidate must never
        # be allowed to crowd out the whole pool (that half of gh#593 is claim_history's job
        # to filter before this gate runs; this gate's own contract is just: no linked
        # candidates present -> nothing gets dropped for "a linked-KR candidate is open").
        candidates = [
            {"number": 1, "body": "Vision-link: none (maintenance) -- a.", "comments": []},
            {"number": 2, "body": "Vision-link: none (maintenance) -- b.", "comments": []},
        ]
        result = vlg.gate_candidates(candidates)
        self.assertEqual(result["eligible"], [1, 2])
        self.assertEqual(result["dropped"], [])

    def test_real_link_crowds_out_maintenance(self):
        candidates = [
            {"number": 1, "body": "Vision-link: none (maintenance) -- a.", "comments": []},
            {"number": 2, "body": "Vision-link: Signal rate -- b.", "comments": []},
        ]
        result = vlg.gate_candidates(candidates)
        self.assertEqual(result["eligible"], [2])
        self.assertEqual([d["number"] for d in result["dropped"]], [1])


if __name__ == "__main__":
    unittest.main()
