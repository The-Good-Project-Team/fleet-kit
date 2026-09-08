"""Regression test for quality_gate.py (fk#649 + fk#651): gru builds only what carries a
quality label and testable Given/When/Then criteria; world-class waits for Reif's design.

Run: python3 scripts/test_quality_gate.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import quality_gate as qg  # noqa: E402

GWT = ("1. **Given** a signed-out visitor, **when** they POST /claim, **then** the API returns "
       "401 and writes no row.")
VAGUE = "1. Handles errors gracefully.\n2. Feels fast."


def item(n, labels, body="", comments=()):
    return {"number": n, "labels": [{"name": l} for l in labels], "body": body,
            "comments": [{"body": c} for c in comments]}


class GateTests(unittest.TestCase):
    def test_no_quality_label_is_dropped(self):
        out = qg.gate_candidates([item(1, ["fleet:priority-high", "fleet:prd"], comments=[GWT])])
        self.assertEqual(out["eligible"], [])
        self.assertIn("no quality: label", out["dropped"][0]["reason"])

    def test_vague_criteria_are_dropped(self):
        out = qg.gate_candidates([item(2, ["quality:solid"], comments=[VAGUE])])
        self.assertEqual(out["eligible"], [])
        self.assertIn("Given/When/Then", out["dropped"][0]["reason"])

    def test_label_plus_gwt_is_eligible(self):
        out = qg.gate_candidates([item(3, ["quality:ship-it"], comments=[GWT])])
        self.assertEqual(out["eligible"], [3])

    def test_gwt_in_body_counts(self):
        out = qg.gate_candidates([item(4, ["quality:solid"], body="Given x, when y, then z.")])
        self.assertEqual(out["eligible"], [4])

    def test_multiline_gwt_counts(self):
        text = ("## Acceptance criteria\n- Given a thread of 200 messages\n  When a new one "
                "arrives\n  Then the scroll position does not move")
        self.assertEqual(qg.count_gwt(text), 1)

    def test_two_quality_labels_is_dropped(self):
        out = qg.gate_candidates([item(5, ["quality:solid", "quality:ship-it"], comments=[GWT])])
        self.assertIn("more than one", out["dropped"][0]["reason"])

    def test_world_class_without_design_is_dropped(self):
        out = qg.gate_candidates([item(6, ["quality:world-class"], comments=[GWT])])
        self.assertEqual(out["eligible"], [])
        self.assertIn("research pass", out["dropped"][0]["reason"])

    def test_world_class_research_slice_is_eligible(self):
        prd = "References: Telegram, iMessage, WhatsApp\n" + GWT
        out = qg.gate_candidates([item(7, ["quality:world-class"], comments=[prd])])
        self.assertEqual(out["eligible"], [7])

    def test_world_class_design_approved_is_eligible(self):
        out = qg.gate_candidates([item(8, ["quality:world-class"],
                                       comments=[GWT, "Design approved: ask #12 answered yes by Reif"])])
        self.assertEqual(out["eligible"], [8])

    def test_newest_criteria_comment_wins(self):
        out = qg.gate_candidates([item(9, ["quality:solid"], comments=[VAGUE, GWT])])
        self.assertEqual(out["eligible"], [9])

    def test_dropped_with_fleet_prd_is_flagged_stale(self):
        out = qg.gate_candidates([item(10, ["fleet:priority-high", "fleet:prd"])])
        self.assertEqual(out["eligible"], [])
        self.assertTrue(out["dropped"][0]["stale_prd"])

    def test_dropped_without_fleet_prd_is_not_flagged_stale(self):
        out = qg.gate_candidates([item(11, ["fleet:priority-high"])])
        self.assertEqual(out["eligible"], [])
        self.assertNotIn("stale_prd", out["dropped"][0])


if __name__ == "__main__":
    unittest.main()
