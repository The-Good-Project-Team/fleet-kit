#!/usr/bin/env python3
"""cost_bridge -- converts real fleet.db cost_usd into fanout.py's %-of-week currency.

THE GAP THIS CLOSES (gh#4020 / fleet-kit#260). fanout.py's own docstring already promises
`unit_pct` is "SELF-CORRECTING... the caller derives it from what passes ACTUALLY spent" --
but nothing in the repo ever built that derivation, so every gru pass since at least
2026-08-30 called it with a hand-typed `--unit-pct 0.05` guess instead. fleet.db has real
`cost_usd` sitting in it (20+ minion runs at any given time); this module is the missing
function that turns that into the `{"pct": ..., "complexity": ...}` shape `fanout.py
--observed` expects.

THE DERIVATION. `allowance_pct` is the ONE real %-of-week quantity a gru pass already reads,
once, per pass (maxx_reader.get_headroom's per_diem_hourly_pct, already buffered -- see
gru.md step 1). This module does not read maxx again -- fleet-kit#260's own PRD ruled out a
second meter read per run (a before/after read around every minion would be more accurate but
adds a network round-trip to every single run, which conflicts with that PRD's AC5 and with
maxx_reader.py's fail-open ethos). Instead, `to_observed()` distributes that ONE already-known
allowance_pct across a set of real runs' `cost_usd`, proportional to each run's share of the
group's total spend: a run that cost twice another's dollars is assumed to have used twice its
share of the hour's real allowance. Wrong in any single pass, self-correcting like every other
number `fanout.py` computes -- `--observed` gets rebuilt fresh from real spend every pass, so a
one-pass miss is not a permanent bias.

Pure core (`to_observed`), thin DB seam (`recent_minion_costs`), CLI (`main`) -- same split as
fleet_db.py's spend()/query_runs() and fanout.py's calibrate()/main().
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fanout  # noqa: E402
import fleet_db  # noqa: E402


def to_observed(runs: list[dict], allowance_pct: float,
                complexity_by_item: dict[str, int] | None = None) -> list[dict]:
    """[{"item_id":.., "cost_usd":..}, ...] + this pass's allowance_pct -> fanout.py's
    --observed shape: [{"pct": float, "complexity": int}, ...].

    `complexity_by_item` maps an item's id (as a string, matching fleet_db's item_id column)
    to its `fleet:complexity-<1-10>` label. An item missing from the map is treated as
    fanout.DEFAULT_COMPLEXITY (median), never free -- same convention gru.md step 2 and
    fanout.py's own DEFAULT_COMPLEXITY already use for an unlabelled item.

    Returns [] when there is nothing usable (no positive cost_usd, or allowance_pct <= 0) --
    same "refuse to invent" discipline as fanout.calibrate(): the caller (fanout.py, fed this
    output as --observed) already errors loudly on an empty/unusable --observed rather than
    silently packing against a fabricated number, so this function does not need to duplicate
    that refusal.
    """
    complexity_by_item = complexity_by_item or {}
    usable = [r for r in runs
             if isinstance(r.get("cost_usd"), (int, float)) and r["cost_usd"] > 0]
    total_cost = sum(r["cost_usd"] for r in usable)
    if not usable or allowance_pct <= 0 or total_cost <= 0:
        return []
    return [
        {
            "pct": allowance_pct * (r["cost_usd"] / total_cost),
            "complexity": complexity_by_item.get(str(r.get("item_id")), fanout.DEFAULT_COMPLEXITY),
        }
        for r in usable
    ]


def recent_minion_costs(conn, member: str = "minion", hours: float = 2.0) -> list[dict]:
    """Real (item_id, cost_usd) pairs from fleet.db -- the same member/window gru.md step 3b's
    own calibration query already reads (`recorded_at > now - 2 hours`, member='minion')."""
    since = time.time() - hours * 3600
    cur = conn.execute(
        "SELECT item_id, cost_usd FROM runs "
        "WHERE member = ? AND recorded_at >= ? AND cost_usd IS NOT NULL",
        (member, since),
    )
    return [{"item_id": item_id, "cost_usd": cost_usd} for item_id, cost_usd in cur.fetchall()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert real fleet.db cost_usd into fanout.py's --observed %-of-week shape.")
    ap.add_argument("--allowance-pct", type=float, required=True,
                    help="this pass's per_diem_hourly_pct-derived allowance (already buffered, "
                         "same value passed to fanout.py --allowance-pct)")
    ap.add_argument("--member", default="minion",
                    help="whose runs to calibrate from (default: minion, the real builders)")
    ap.add_argument("--hours", type=float, default=2.0,
                    help="lookback window in hours (default: 2, matches gru.md step 3b)")
    ap.add_argument("--complexity",
                    help='JSON map of item_id -> fleet:complexity-<n>, e.g. \'{"3253":3}\'; '
                         'an item missing from this map is treated as median, never free')
    ap.add_argument("--db-path", help="override fleet.db path (default: fleet_db.DB_FILE)")
    a = ap.parse_args(argv)

    conn = fleet_db.connect(Path(a.db_path) if a.db_path else None)
    fleet_db.sync(conn)
    runs = recent_minion_costs(conn, member=a.member, hours=a.hours)
    complexity_by_item = json.loads(a.complexity) if a.complexity else {}
    observed = to_observed(runs, a.allowance_pct, complexity_by_item)
    print(json.dumps(observed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
