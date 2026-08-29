#!/usr/bin/env python3
"""maxx_share_ceiling.py <fraction> -- this instance's ceiling on how much of the fleet's
current hourly headroom a member may reserve, in PERCENT-OF-WEEK units (maxx's own scale,
matching maxx_reserve's `pct` argument -- 100% would be the whole week's bank).

Replaces the old maxx_share_check.py, which computed a single dollar figure and handed it to
the member as a fixed cap. That put the SIZING decision in run_member.sh, before the member
ever ran -- a quiet judge-judy tick (one PR) and a backlog tick (five PRs) got the same
number. Reif, 2026-08-29: "agent self-reserves based on assumptions and needs." This script
now prints only the CEILING -- the most a member may ask for -- and the member's own runner
decides how much of that ceiling its actual queue/task needs, then calls
`maxx_lease.py reserve --pct <its own number, <= ceiling> --label ... --ttl-sec ...` itself,
and `maxx_lease.py release --lease-id ...` when done. See judge-judy.sh for a worked example.

WHY HOURLY, NOT THE WEEK BANK: the old formula multiplied by `week_bank_pct` -- a LAGGING,
already-spent number. The moment the week goes over pace (bank negative), that clamps to 0.0
and every member's ceiling goes to zero even during an hour with real headroom. This version
uses `sustainable_pct_per_hour` (the burn rate that finishes the week on pace) minus
`per_diem_hourly_pct` (this hour's actual burn so far) minus `reserved_pct` (every OTHER
instance/member's currently-live reservation, gh#161 part 2/PR #163) -- a leading, real-time,
already-coordinated number. FLEET_SHARE_FRACTION slices that hourly headroom the same way it
always sliced the old one.

Prints exactly one number to stdout: the ceiling in pct-of-week units, or empty string if
there is no trustworthy reading (unreadable meter -- FAILS OPEN, same law as
maxx_reader.py's own header: a bad reading must only ever be usable to CONSERVE, i.e. an
absent ceiling means "the caller falls back to its own pre-existing fixed cap," never
"reserve nothing is the same as reserve unlimited"). A ceiling of exactly 0.0 (this hour
already at or past sustainable pace, once other reservations are subtracted) is a real,
honest answer, distinct from an unreadable meter -- always printed, never suppressed.
"""
from __future__ import annotations

import sys

from maxx_reader import get_headroom


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: maxx_share_ceiling.py <fraction>", file=sys.stderr)
        return 2

    try:
        share = float(argv[1])
    except ValueError:
        print(f"bad fraction: {argv[1]!r}", file=sys.stderr)
        return 2
    # Clamped <= 1.0 -- an operator typo like FLEET_SHARE_FRACTION=1.5 must never raise a
    # member's ceiling above the fleet's own real hourly headroom.
    share = min(share, 1.0)

    fraction, _label, budget = get_headroom()

    if fraction is None:
        # Unreadable meter -- print nothing. The caller's documented contract: no ceiling
        # means "fall back to whatever fixed cap you already had," never "reserve 0."
        print("")
        return 0

    sustainable = budget.get("sustainable_pct_per_hour")
    hourly_used = budget.get("per_diem_hourly_pct")
    if sustainable is None or hourly_used is None:
        print("")
        return 0

    reserved = budget.get("reserved_pct") or 0.0
    hourly_headroom_pct = max(0.0, sustainable - hourly_used - reserved)
    ceiling_pct = hourly_headroom_pct * share

    print(f"{ceiling_pct:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
