#!/usr/bin/env python3
"""claim_history -- has this item already been claimed-and-abandoned too many times?

THE GAP THIS CLOSES (gh#64). gru.md step 2b filters candidates by `fleet:claimed` and
`fleet:needs-human-op`, but nothing distinguishes "never tried" from "tried and dead-ended N
times" -- the same chronically-blocked item gets reclaimed and respawned every hour, burning a
full claim/spawn/clear cycle each time, because nothing upstream of the claim step counts prior
attempts.

WHY COUNTING RUN_IDS IS ENOUGH, WITHOUT ALSO CHECKING FOR A MERGE. gru.md step 2b's candidate
list is read straight from `gh issue list --state open`. An item a prior minion pass actually
fixed is already gone from that list -- a merged PR referencing it (`Fixes #N`) autocloses the
issue. So every prior minion run against a candidate that is STILL in this list is, by
construction, a claim that did not resolve it -- checking run history is enough; this
deliberately does not also call `gh pr view` per candidate to reconfirm "no merge", which would
add one API round-trip per candidate to every single gru pass.

Pure core (`dead_end_claim_count`/`is_dead_end_blocked`), thin DB seam
(`minion_runs_for_item`), CLI (`main`) -- same split as cost_bridge.py.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_db  # noqa: E402

# UNKNOWN in gh#64's own PRD: "the exact dead-end threshold (N claims within what time window)
# ... needs a human call or a reasoned default stated explicitly in the PR." This is that
# reasoned default, not a human call:
#   - 3 strikes: 1-2 prior claims is well within "just needed another try" (real items in this
#     fleet's own history have succeeded on a 2nd or 3rd claim); 3 dead ends with the issue
#     still open is the point gru.md's own filing of this issue calls "chronically blocked."
#   - 14 days: long enough to span many of gru's real hourly passes (so a genuinely dead item
#     gets caught, not just one still cooling from its last attempt), short enough that an item
#     fixed, reopened, and now being retried fresh does not inherit a stale strike count from
#     months back.
DEFAULT_DEAD_END_THRESHOLD = 3
DEFAULT_WINDOW_DAYS = 14.0


def dead_end_claim_count(run_ids: list[str], item_number: int) -> int:
    """How many of `run_ids` are minion runs against `item_number`.

    `run_ids` should already be scoped by the caller to the recent window and to
    member='minion' (see `minion_runs_for_item`) -- this only matches the run_id SHAPE gru.md
    step 7 already documents minion's own runs follow: `minion-item<n>-<pid>-<timestamp>`.
    """
    prefix = f"minion-item{item_number}-"
    return sum(1 for run_id in run_ids if (run_id or "").startswith(prefix))


def is_dead_end_blocked(run_ids: list[str], item_number: int,
                        threshold: int = DEFAULT_DEAD_END_THRESHOLD) -> bool:
    return dead_end_claim_count(run_ids, item_number) >= threshold


def minion_runs_for_item(conn, item_number: int,
                         window_days: float = DEFAULT_WINDOW_DAYS) -> list[str]:
    """Real run_ids from fleet.db: every minion run against `item_number` in the last
    `window_days`, regardless of that run's own reported status -- see the module docstring for
    why status doesn't matter here (the issue still being open is the proof of no merge)."""
    since = time.time() - window_days * 86400
    cur = conn.execute(
        "SELECT run_id FROM runs WHERE member = 'minion' AND item_id = ? AND recorded_at >= ?",
        (str(item_number), since),
    )
    return [row[0] for row in cur.fetchall()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Has this item already been claimed-and-abandoned past the dead-end "
                     "threshold? Exit 1 (BLOCKED) if so -- gru.md's claim step should skip it "
                     "and name it in the report, not silently reclaim it.")
    ap.add_argument("--item", type=int, required=True)
    ap.add_argument("--threshold", type=int, default=DEFAULT_DEAD_END_THRESHOLD)
    ap.add_argument("--window-days", type=float, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--db-path", help="override fleet.db path (default: fleet_db.DB_FILE)")
    a = ap.parse_args(argv)

    conn = fleet_db.connect(Path(a.db_path) if a.db_path else None)
    fleet_db.sync(conn)
    run_ids = minion_runs_for_item(conn, a.item, window_days=a.window_days)
    count = dead_end_claim_count(run_ids, a.item)
    blocked = is_dead_end_blocked(run_ids, a.item, threshold=a.threshold)
    print(f"{'BLOCKED' if blocked else 'ok'} count={count} threshold={a.threshold}")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
