"""block_over_pace() fails CLOSED when the block is unreadable (2026-09-11 incident).

Reif was locked out for 40 minutes mid-block. The 5h clamp existed and never fired once,
because `session_used_pct` is null on an unanchored handle and the guard read null as "fine"
-- dead code on exactly the handles that needed it, while the hourly tier, which cannot see a
burst, let the block burn out.

This is the one place in maxx_share_ceiling.py where fail-open is wrong: an unreadable BLOCK
is not a healthy block, and the hourly ceiling still governs throughput, so clamping here
costs pace rather than progress.

Run: python3 scripts/test_block_pace_fails_closed.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from maxx_share_ceiling import BLOCK_S, block_over_pace  # noqa: E402


class BlockPaceTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("FLEET_BLOCK_PACE_REQUIRE_ANCHOR", None)
        os.environ.pop("FLEET_BLOCK_PACE_SLACK_PCT", None)

    def test_an_unanchored_handle_holds_the_fleet(self):
        self.assertTrue(block_over_pace({"session_used_pct": None, "five_reset_in_sec": 900}))
        self.assertTrue(block_over_pace({}))

    def test_the_opt_out_is_there_for_eyes_open_use(self):
        os.environ["FLEET_BLOCK_PACE_REQUIRE_ANCHOR"] = "0"
        self.assertFalse(block_over_pace({"session_used_pct": None, "five_reset_in_sec": 900}))

    def test_a_readable_block_under_pace_still_runs(self):
        # 10% of the window elapsed, 5% of it spent -- the fleet is behind pace, let it work.
        self.assertFalse(block_over_pace(
            {"session_used_pct": 5, "five_reset_in_sec": BLOCK_S * 0.9}, slack_pct=10))

    def test_a_readable_block_burning_ahead_of_pace_is_held(self):
        # 10% elapsed, 77% spent: the 2026-09-09 shape this clamp was written for.
        self.assertTrue(block_over_pace(
            {"session_used_pct": 77, "five_reset_in_sec": BLOCK_S * 0.9}, slack_pct=10))

    def test_a_garbage_value_is_not_treated_as_an_unreadable_block(self):
        # Unparseable != absent. This path stays fail-open, as the file's header says.
        self.assertFalse(block_over_pace({"session_used_pct": "abc", "five_reset_in_sec": 900}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
