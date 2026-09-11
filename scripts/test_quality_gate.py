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
        out = qg.gate_candidates([item(4, ["quality:solid"],
                                       body="Given a logged-in user, when they open settings, then dark mode is off by default.")])
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

    def test_world_class_vp_review_form_is_eligible(self):
        out = qg.gate_candidates([item(10, ["quality:world-class"],
                                        comments=[GWT, "Design approved (VP review): clears the bar.\nChecked: 12 captures."])])
        self.assertEqual(out["eligible"], [10])

    def test_newest_criteria_comment_wins(self):
        out = qg.gate_candidates([item(9, ["quality:solid"], comments=[VAGUE, GWT])])
        self.assertEqual(out["eligible"], [9])

    def test_bare_template_phrase_is_not_a_criterion(self):
        """fk#765: the literal 15-char phrase `Given/When/Then` -- no clause text between the
        keywords -- must not count as an acceptance criterion. This is the exact string that
        certified #651/#661/#765 itself before this fix."""
        self.assertEqual(qg.count_gwt("no Given/When/Then acceptance criterion here"), 0)

    def test_multiline_real_criterion_still_counts_as_one(self):
        text = ("Given a signed-out visitor\nwhen they POST /claim\nthen the API returns 401 "
                "and writes no row")
        self.assertEqual(qg.count_gwt(text), 1)

    def test_decline_comment_naming_the_bare_phrase_is_dropped(self):
        out = qg.gate_candidates([item(16, ["quality:ship-it"], comments=[
            "marie: declined — no Given/When/Then criterion is testable from this repo."])])
        self.assertEqual(out["eligible"], [])
        self.assertIn("Given/When/Then", out["dropped"][0]["reason"])

    def test_bare_phrase_comment_does_not_shadow_an_earlier_real_prd(self):
        """fk#765: `_criteria_text()` takes the newest comment carrying any match. A later
        comment that only mentions the template phrase must not shadow an earlier PRD with
        real criteria -- the fix here (count_gwt itself ignores the bare phrase) makes
        _criteria_text() fall through to the real PRD comment automatically."""
        prd = "\n".join([
            "1. **Given** a signed-out visitor, **when** they POST /claim, **then** the API "
            "returns 401 and writes no row.",
            "2. **Given** a signed-in visitor, **when** they POST /claim, **then** the API "
            "returns 200 and writes one row.",
            "3. **Given** a duplicate claim, **when** they POST /claim twice, **then** the "
            "second call returns 409.",
        ])
        status_note = "Not re-ranking -- still Given/When/Then, per the PRD above."
        self.assertEqual(qg._criteria_text("", [{"body": prd}, {"body": status_note}]), prd)

        out = qg.gate_candidates([item(17, ["quality:solid"], comments=[prd, status_note])])
        self.assertEqual(out["eligible"], [17])
        _, reason = qg.classify_candidate(
            [{"name": "quality:solid"}], "", [{"body": prd}, {"body": status_note}])
        self.assertIn("3 testable criteria", reason)

    def test_dropped_with_fleet_prd_is_flagged_stale(self):
        out = qg.gate_candidates([item(10, ["fleet:priority-high", "fleet:prd"])])
        self.assertEqual(out["eligible"], [])
        self.assertTrue(out["dropped"][0]["stale_prd"])

    def test_dropped_without_fleet_prd_is_not_flagged_stale(self):
        out = qg.gate_candidates([item(11, ["fleet:priority-high"])])
        self.assertEqual(out["eligible"], [])
        self.assertNotIn("stale_prd", out["dropped"][0])

    def test_epic_is_never_eligible_even_with_label_and_gwt(self):
        """fk#634 fix 1: a tracking-only epic must not become buildable just because marie
        stamps a quality: label on it to clear this same gate (confirmed live 2026-09-11:
        5 of 6 open epics read eligible=True before this guard)."""
        out = qg.gate_candidates([item(13, ["fleet:epic", "quality:solid"], comments=[GWT])])
        self.assertEqual(out["eligible"], [])
        self.assertIn("fleet:epic", out["dropped"][0]["reason"])

    def test_epic_without_quality_label_still_reads_epic_reason_not_label_reason(self):
        out = qg.gate_candidates([item(14, ["fleet:epic"])])
        self.assertEqual(out["eligible"], [])
        self.assertIn("tracking-only parent", out["dropped"][0]["reason"])

    def test_reif_priority_epic_is_also_not_eligible_via_this_gate(self):
        """gru.md:123's Reif-priority-epic path never calls this gate on the epic itself --
        it reads the epic to find child issues/PRs instead -- but if it ever were called,
        the epic guard must still hold rather than special-casing fleet:reif-priority."""
        out = qg.gate_candidates([item(15, ["fleet:reif-priority", "fleet:epic", "quality:solid"],
                                       comments=[GWT])])
        self.assertEqual(out["eligible"], [])
        self.assertIn("fleet:epic", out["dropped"][0]["reason"])

    def test_real_decision_ask_approval_clears_the_world_class_gate(self):
        """gh#650 AC8: a `class=decision` ask filed through ask.py, answered, and referenced
        from a real `Design approved: <id>` comment must clear this gate end to end -- not
        just the string match `test_world_class_design_approved_is_eligible` above checks."""
        import sys
        import tempfile
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import ask, fleet_db

        with tempfile.TemporaryDirectory() as d:
            conn = fleet_db.connect(Path(d) / "fleet.db")
            ask_id = ask.file_ask(conn, "marie", "approve design spec for #12",
                                  ask_class="decision")
            ask.answer_ask(conn, ask_id, "approved", "reif")

            criteria = "References: Telegram, iMessage, WhatsApp\n" + GWT
            approval = f"Design approved: ask #{ask_id}"
            out = qg.gate_candidates([item(12, ["quality:world-class"],
                                           comments=[criteria, approval])])
            self.assertEqual(out["eligible"], [12])


if __name__ == "__main__":
    unittest.main()
