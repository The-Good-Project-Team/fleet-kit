#!/usr/bin/env python3
"""test_fleet_db.py -- regression test for fleet-kit#241.

PR#217 (fleet-kit#212) widened `runs`' PRIMARY KEY from bare `run_id` to
`(run_id, recorded_at)` so two judge-judy reviews sharing a run_id stop clobbering each other
going forward -- but any pair that had ALREADY collided under the old bare key stayed lost:
sync()'s offset had already passed those runs.jsonl lines, so incremental sync() never
revisits them. `backfill()` is the one-time repair: reread runs.jsonl from scratch and insert
whatever the composite key is still missing, without touching what's already there.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_db  # noqa: E402


def _line(run_id, recorded_at, outcome, member="judge-judy"):
    return json.dumps({
        "run_id": run_id, "member": member, "kind": "review", "outcome": outcome,
        "ts": recorded_at, "_recorded_at": recorded_at,
    })


class FleetDbBackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "fleet.db"
        self.runs_path = Path(self.tmp.name) / "runs.jsonl"
        self.conn = fleet_db.connect(self.db_path)

    def _rows(self):
        return self.conn.execute(
            "SELECT run_id, recorded_at, outcome FROM runs ORDER BY run_id, recorded_at"
        ).fetchall()

    def test_backfill_recovers_the_lost_half_of_a_collided_pair(self):
        """gh#212's own worked example: two concurrent judge-judy reviews of the same PR head
        share a run_id (`review-<pr>-<sha>`) but land seconds apart -- one 'approved', one
        'blocked'. Simulate the pre-#217 world where only the second survived in fleet.db,
        while runs.jsonl (the source of truth) still has both."""
        run_id = "review-184-5e220a0c1cb9"
        self.runs_path.write_text(
            _line(run_id, 100.0, "approved") + "\n" +
            _line(run_id, 101.0, "blocked") + "\n" +
            _line("review-206-166c60ca49a9", 150.0, "blocked") + "\n"
        )
        # Pre-backfill state: only the "blocked" verdict and the unrelated run made it into
        # fleet.db (the "approved" one is the row #217's migration never recovers).
        fleet_db.sync(self.conn, runs_file=self.runs_path)
        self.conn.execute("DELETE FROM runs WHERE run_id = ? AND recorded_at = 100.0", (run_id,))
        self.conn.commit()
        self.assertEqual(len(self._rows()), 2, "setup should start one row short")

        n = fleet_db.backfill(self.conn, runs_file=self.runs_path)

        self.assertEqual(n, 1, f"backfill should insert exactly the missing row, inserted {n}")
        rows = self._rows()
        self.assertEqual(len(rows), 3)
        self.assertIn((run_id, 100.0, "approved"), rows, "the lost 'approved' verdict is still missing")
        self.assertIn((run_id, 101.0, "blocked"), rows, "backfill must not disturb the row that survived")

    def test_backfill_never_overwrites_a_row_already_present(self):
        """INSERT OR IGNORE, not sync()'s OR REPLACE -- a row that's already in fleet.db must
        come out of backfill() byte-identical, even if runs.jsonl were ever hand-edited."""
        run_id = "review-175-817e99187df8"
        self.runs_path.write_text(_line(run_id, 100.0, "approved") + "\n")
        fleet_db.sync(self.conn, runs_file=self.runs_path)
        self.assertEqual(self._rows(), [(run_id, 100.0, "approved")])

        n = fleet_db.backfill(self.conn, runs_file=self.runs_path)

        self.assertEqual(n, 0, "nothing was missing, backfill should insert nothing")
        self.assertEqual(self._rows(), [(run_id, 100.0, "approved")])

    def test_backfill_is_idempotent(self):
        """Safe to run any number of times against the same jsonl -- the second call finds
        nothing left to fill."""
        run_id = "review-177-a02596ba88a0"
        self.runs_path.write_text(
            _line(run_id, 100.0, "approved") + "\n" +
            _line(run_id, 101.0, "blocked") + "\n"
        )
        first = fleet_db.backfill(self.conn, runs_file=self.runs_path)
        second = fleet_db.backfill(self.conn, runs_file=self.runs_path)

        self.assertEqual(first, 2)
        self.assertEqual(second, 0, "re-running backfill on an already-repaired db inserted more rows")
        self.assertEqual(len(self._rows()), 2)

    def test_backfill_does_not_move_sync_offset(self):
        """backfill() rereads from byte 0 on purpose and must leave sync_state alone -- it is
        a supplemental repair pass, not a replacement for sync()'s own incremental progress."""
        self.runs_path.write_text(_line("review-999-abc", 100.0, "approved") + "\n")
        before = fleet_db.sync_offset(self.conn)

        fleet_db.backfill(self.conn, runs_file=self.runs_path)

        self.assertEqual(fleet_db.sync_offset(self.conn), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
