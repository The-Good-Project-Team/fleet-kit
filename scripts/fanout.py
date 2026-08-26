#!/usr/bin/env python3
"""fanout — fill one hour's token allowance with work, in PERCENT OF WEEK.

THE JOB IS SELECTION, NOT COUNTING. Reif, 2026-08-26: "its more about choosing jobs to run
(that max out the availability) than it is about choosing minions to spawn." N is an output
of packing the hour, never an input. Earlier versions of this file answered "how many minions
can I afford?" and both were wrong: the first in dollars, the second still counting minions
as if every item were the same size.

WHY PERCENT. The fleet runs on a subscription, so dollars are not the constraint and never
were -- the constraint is share of the weekly token allowance. maxx is the authority, and it
already applies BOTH buffers before we see a number:

    weekly_max 0.925    holds back 7.5% of the week
    per_diem_use 0.95   holds back another 5% of the day
    per_diem_hourly_pct = the post-buffer allowance for ONE hour

So a caller spends TO per_diem_hourly_pct. It must not hold back a further reserve of its own
-- an earlier FLEET_HEADROOM_FRACTION=0.5 halved an already-buffered allowance, which is a
large part of why the fleet chronically underspent.

WHY THE HOUR IS THE UNIT. gru runs hourly precisely so each pass consumes one hour's slice.
An unspent hour does NOT roll over -- the allowance refills and the remainder is simply gone.
That makes underspending exactly as wrong as overspending, which is the opposite of how a
budget usually behaves and the single most important thing for a caller to understand.

COMPLEXITY, NOT COUNT. marie scores every item fleet:complexity-1..10 on an exponential
ladder (base 1.35, so a 10 is ~15x a 1 -- calibrated against a measured 9x p90/p10 and 22x
max/min spread across 142 real minion runs). cost_pct(item) = unit_pct * 1.35^(c-5), anchored
at 5 = the median item. Two 3s may fit an hour that one 9 would blow.

SELF-CORRECTING. `unit_pct` is not a constant to be guessed -- the caller derives it from what
passes ACTUALLY spent (calibrate() below) and re-derives it every pass. A wrong estimate is
therefore a one-pass error, not a permanent bias.

Pure: no network, no filesystem, no env reads at import. The caller supplies live numbers.
"""
from __future__ import annotations

import argparse
import json
import sys

# marie's ladder. Base and anchor must match members/marie/marie.md's Part C2 table exactly --
# if one moves without the other, gru silently mis-sizes every item it schedules.
COMPLEXITY_BASE = 1.35
COMPLEXITY_ANCHOR = 5  # the median real item; cost multiplier here is exactly 1.0
DEFAULT_COMPLEXITY = COMPLEXITY_ANCHOR  # an unlabelled item is assumed median, never free


def complexity_multiplier(c: int | None, base: float = COMPLEXITY_BASE) -> float:
    """How many median-items one complexity-c item is worth. c=5 -> 1.0, c=10 -> ~4.5, c=1 -> ~0.29."""
    if c is None:
        c = DEFAULT_COMPLEXITY
    c = max(1, min(10, int(c)))
    return base ** (c - COMPLEXITY_ANCHOR)


def calibrate(observed: list[dict], base: float = COMPLEXITY_BASE) -> float | None:
    """Derive the cost of ONE median (complexity-5) item, as % of week, from real passes.

    `observed` is [{"pct": <what it actually spent>, "complexity": <its label>}, ...]. Each
    pass is normalised by its own multiplier before averaging, so a week of mostly-easy items
    doesn't drag the unit down and make everything look cheap.

    Returns None when there is nothing usable -- the caller must then say so rather than
    invent a number. A fabricated unit silently mis-sizes every future pass.
    """
    units = [o["pct"] / complexity_multiplier(o.get("complexity"), base)
             for o in observed
             if isinstance(o.get("pct"), (int, float)) and o["pct"] > 0]
    return (sum(units) / len(units)) if units else None


def pack(items: list[dict], allowance_pct: float, unit_pct: float,
         base: float = COMPLEXITY_BASE, min_items: int = 0) -> dict:
    """Choose the item set that fills `allowance_pct` without exceeding it.

    `items` are ALREADY in the caller's priority order (marie ranks; gru does not re-rank).
    Greedy in that order, skipping an item too big for the remaining room but continuing --
    so a cheap high-priority item still gets in behind an expensive one that didn't fit. It
    does NOT reorder by size: shipping the most important work beats shipping the most work.

    `min_items` floors the selection so a thin allowance still moves something forward rather
    than idling the hour entirely -- but the caller is told, via `over_allowance`, when the
    floor pushed it past the line. Silent overspend is the one outcome this must never produce.
    """
    if unit_pct <= 0:
        raise ValueError(f"unit_pct must be > 0, got {unit_pct!r}")

    chosen, skipped, spent = [], [], 0.0
    for it in items:
        c = it.get("complexity")
        cost = unit_pct * complexity_multiplier(c, base)
        if spent + cost <= allowance_pct:
            chosen.append({**it, "est_pct": round(cost, 5)})
            spent += cost
        else:
            skipped.append({**it, "est_pct": round(cost, 5), "why": "would exceed the hour"})

    forced = 0
    while len(chosen) < min_items and skipped:
        nxt = skipped.pop(0)
        nxt.pop("why", None)
        chosen.append(nxt)
        spent += nxt["est_pct"]
        forced += 1

    return {
        "n": len(chosen),
        "chosen": chosen,
        "skipped": skipped,
        "est_spend_pct": round(spent, 5),
        "allowance_pct": round(allowance_pct, 5),
        "headroom_left_pct": round(allowance_pct - spent, 5),
        "utilization": round(spent / allowance_pct, 4) if allowance_pct > 0 else None,
        "unit_pct": round(unit_pct, 6),
        "forced_over_floor": forced,
        "over_allowance": spent > allowance_pct,
        "binding": ("nothing_claimable" if not items else
                    "min_items_floor" if forced else
                    "backlog_exhausted" if not skipped else "allowance"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Pack one hour's token allowance with backlog work (percent of week).")
    ap.add_argument("--allowance-pct", type=float, required=True,
                    help="this pass's share, already buffered (e.g. per_diem_hourly_pct * 0.70)")
    ap.add_argument("--unit-pct", type=float,
                    help="cost of one complexity-5 item as %% of week; omit to derive from --observed")
    ap.add_argument("--items", required=True,
                    help='JSON list in priority order: [{"number":123,"complexity":4}, ...] or "-" for stdin')
    ap.add_argument("--observed",
                    help='JSON list of real past passes to calibrate from: [{"pct":0.08,"complexity":5}, ...]')
    ap.add_argument("--min-items", type=int, default=0)
    ap.add_argument("--base", type=float, default=COMPLEXITY_BASE)
    a = ap.parse_args(argv)

    items = json.loads(sys.stdin.read() if a.items == "-" else a.items)
    unit = a.unit_pct
    if unit is None:
        if not a.observed:
            print("ERROR: pass --unit-pct or --observed; refusing to invent a unit cost",
                  file=sys.stderr)
            return 2
        unit = calibrate(json.loads(a.observed), base=a.base)
        if unit is None:
            print("ERROR: --observed had no usable pass; refusing to invent a unit cost",
                  file=sys.stderr)
            return 2

    try:
        result = pack(items, a.allowance_pct, unit, base=a.base, min_items=a.min_items)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
