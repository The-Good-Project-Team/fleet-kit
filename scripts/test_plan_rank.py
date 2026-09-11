"""Regression test for plan_rank.py, gh#572's plan-bet preference tier.

Run: python3 scripts/test_plan_rank.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import plan_rank as pr  # noqa: E402


class ParseBetsTests(unittest.TestCase):
    def test_bet_line_names_its_issue(self):
        bets, diagnostic = pr.parse_bets(
            "# Plan\n\n## Bets\n1. Verified Org from HQ -- #4494\n2. Atlas in front -- #4495\n")
        self.assertIsNone(diagnostic)
        self.assertEqual([b["issues"] for b in bets], [[4494], [4495]])
        self.assertEqual(bets[0]["text"], "Verified Org from HQ -- #4494")

    def test_bullet_style_and_multiple_issues_per_bet(self):
        bets, diagnostic = pr.parse_bets("## Bets\n- Reactivate cohort -- #10 #11\n")
        self.assertIsNone(diagnostic)
        self.assertEqual(bets[0]["issues"], [10, 11])

    def test_no_bets_heading_is_malformed_ac4(self):
        bets, diagnostic = pr.parse_bets("# Plan\n\nJust prose, no heading at all.\n")
        self.assertEqual(bets, [])
        self.assertEqual(diagnostic, "no '## Bets' heading found")

    def test_prose_only_bets_section_is_not_malformed_ac3(self):
        bets, diagnostic = pr.parse_bets(
            "## Bets\nGrow the top of funnel.\nShip claim-then-verify as one screen.\n")
        self.assertIsNone(diagnostic)
        self.assertTrue(bets)
        self.assertEqual(pr.issue_bet_map(bets), {})

    def test_section_stops_at_next_same_level_heading(self):
        bets, _ = pr.parse_bets("## Bets\n- Verified Org -- #123\n## Checkpoints\n- #999 by Q4\n")
        self.assertEqual(pr.issue_bet_map(bets), {123: "Verified Org -- #123"})


class RankCandidatesTests(unittest.TestCase):
    def test_ac1_bet_named_candidate_outranks_regardless_of_input_order(self):
        ranked = pr.rank_candidates([456, 123], {123: "Verified Org"})
        self.assertEqual(ranked, [123, 456])

    def test_ac2_empty_bet_map_is_byte_identical_to_input(self):
        self.assertEqual(pr.rank_candidates([456, 123, 789], {}), [456, 123, 789])

    def test_non_bet_candidates_keep_relative_order(self):
        ranked = pr.rank_candidates([5, 1, 123, 9], {123: "Verified Org"})
        self.assertEqual(ranked, [123, 5, 1, 9])


class LoadBetsTests(unittest.TestCase):
    def test_ac2_missing_plan_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bets, diagnostic = pr.load_bets(Path(tmp) / "nonexistent-instance.md")
            self.assertEqual(bets, [])
            self.assertIsNone(diagnostic)

    def test_ac4_malformed_plan_names_the_file_in_the_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instance.md"
            path.write_text("no bets heading here\n")
            bets, diagnostic = pr.load_bets(path)
            self.assertEqual(bets, [])
            self.assertIn(str(path), diagnostic)
            self.assertIn("no '## Bets' heading found", diagnostic)


class RankEndToEndTests(unittest.TestCase):
    def test_ac1_full_rank_with_a_real_plan_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instance.md"
            path.write_text("## Bets\n1. Verified Org -- #123\n")
            out = pr.rank([456, 123], plan_path=path)
            self.assertEqual(out["ranked"], [123, 456])
            self.assertEqual(out["bet_by_issue"], {"123": "Verified Org -- #123"})

    def test_ac2_no_plan_file_ranks_unchanged_and_bet_by_issue_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pr.rank([456, 123], plan_path=Path(tmp) / "nope.md")
            self.assertEqual(out["ranked"], [456, 123])
            self.assertEqual(out["bet_by_issue"], {})

    def test_ac3_prose_only_plan_ranks_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instance.md"
            path.write_text("## Bets\nJust prose, no issue numbers.\n")
            out = pr.rank([456, 123], plan_path=path)
            self.assertEqual(out["ranked"], [456, 123])


class ResolveInstanceTests(unittest.TestCase):
    """fk#559 VP review fix 2: FLEET_INSTANCE_NAME carries a deploy-slot suffix live."""

    def _resolve(self, value):
        with unittest.mock.patch.dict("os.environ", {"FLEET_INSTANCE_NAME": value}, clear=False):
            return pr.resolve_instance()

    def test_plain_name_is_unchanged(self):
        self.assertEqual(self._resolve("fleet-kit-server-fleet"), "fleet-kit-server-fleet")

    def test_green_suffix_is_stripped(self):
        self.assertEqual(self._resolve("fleet-kit-server-fleet-green"), "fleet-kit-server-fleet")

    def test_blue_suffix_is_stripped(self):
        self.assertEqual(self._resolve("fleet-kit-server-fleet-blue"), "fleet-kit-server-fleet")

    def test_missing_env_falls_back_to_default(self):
        env = dict(os.environ)
        env.pop("FLEET_INSTANCE_NAME", None)
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(pr.resolve_instance(), "default")


class SchemaExampleTests(unittest.TestCase):
    """fk#559 VP review fix 5: parse the real checked-in example, not a fixture string."""

    def test_example_plan_file_parses_per_the_schema(self):
        example = KIT / "docs" / "plan" / "EXAMPLE.md"
        self.assertTrue(example.exists(), f"{example} must exist per docs/plan/SCHEMA.md")
        out = pr.rank([4495, 4494, 999], plan_path=example)
        self.assertEqual(out["ranked"], [4495, 4494, 999])
        self.assertIn("4494", out["bet_by_issue"])
        self.assertIn("4495", out["bet_by_issue"])
        self.assertNotIn("999", out["bet_by_issue"])

    def test_example_plan_file_names_a_multi_issue_bet(self):
        example = KIT / "docs" / "plan" / "EXAMPLE.md"
        bets, diagnostic = pr.load_bets(example)
        self.assertIsNone(diagnostic)
        multi = [b for b in bets if len(b["issues"]) > 1]
        self.assertTrue(multi, "EXAMPLE.md should keep its two-issue bet line")
        self.assertEqual(multi[0]["issues"], [10, 11])


class CliTests(unittest.TestCase):
    def _run(self, items, plan_path=None):
        argv = [sys.executable, str(KIT / "scripts" / "plan_rank.py"), "--items", items]
        if plan_path is not None:
            argv += ["--plan-path", str(plan_path)]
        return subprocess.run(argv, capture_output=True, text=True)

    def test_ac2_cli_exits_0_with_unchanged_order_when_no_plan_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run("[456, 123]", plan_path=Path(tmp) / "nope.md")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), '{"ranked": [456, 123], "bet_by_issue": {}}')
        self.assertEqual(out.stderr, "")

    def test_ac4_cli_exits_0_prints_one_diagnostic_line_never_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instance.md"
            path.write_text("no bets heading at all\n")
            out = self._run("[456, 123]", plan_path=path)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), '{"ranked": [456, 123], "bet_by_issue": {}}')
        self.assertEqual(len(out.stderr.strip().splitlines()), 1, out.stderr)
        self.assertNotIn("Traceback", out.stderr)


if __name__ == "__main__":
    unittest.main()
