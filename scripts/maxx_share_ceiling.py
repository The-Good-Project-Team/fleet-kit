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
already at or past sustainable pace, once other reservations are subtracted -- OR maxx's
`verdict=="over"`, its own definitive hard-stop, distinct from an unreadable meter) is a
real, honest answer -- always printed, never suppressed.
"""
from __future__ import annotations

import sys

import maxx_lease
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

    fraction, label, budget = get_headroom()

    if fraction is None:
        # Unreadable meter -- print nothing. The caller's documented contract: no ceiling
        # means "fall back to whatever fixed cap you already had," never "reserve 0."
        print("")
        return 0

    if fraction == 0.0 and label == "over":
        # fleet-code-review BLOCK on this PR: `verdict=="over"` is maxx's own DEFINITIVE
        # "stop" signal, not an unreadable meter -- get_headroom() returns fraction=0.0 (never
        # None) for it specifically so a real stop can't be confused with "no reading" (see
        # maxx_reader.py's own header/OVER_VERDICTS comment). The hourly fields below
        # (sustainable_pct_per_hour, per_diem_hourly_pct) are populated independently of
        # verdict and can still look like real headroom during an "over" reading (over on
        # session/week terms, healthy-looking hourly numbers) -- computing the ceiling from
        # them alone, without also checking this, would spend straight through maxx's own
        # hard stop. An honest zero, not suppressed: this IS a real reading, distinct from
        # the unreadable-meter branch above.
        print("0.0000")
        return 0

    sustainable = budget.get("sustainable_pct_per_hour")
    hourly_used = budget.get("per_diem_hourly_pct")
    if sustainable is None or hourly_used is None:
        print("")
        return 0

    # `budget["reserved_pct"]` (from get_headroom(), the plain function) only ever carries
    # whatever the REMOTE maxx endpoint reports -- which today is nothing (the remote never
    # learns about local leases). The merge with maxx_lease.total_reserved_pct() normally
    # happens inside maxx_reader.py's own CLI main(), which this script never goes through.
    # Real BLOCK finding on this PR: without this line, two concurrent callers (this
    # instance's own judge-judy running twice, or the OTHER instance) each compute the same
    # generous ceiling and each reserve against it, seeing none of each other's live leases --
    # reproducing, in a new form, the exact "no coordination" problem #173/this PR set out to
    # fix. Fails open the same way maxx_reader.py's own merge does: a broken local lease file
    # must never crash this CLI's otherwise-guaranteed always-parseable output.
    try:
        local_reserved = maxx_lease.total_reserved_pct()
    except Exception:
        local_reserved = 0.0
    reserved = (budget.get("reserved_pct") or 0.0) + local_reserved
    hourly_headroom_pct = max(0.0, sustainable - hourly_used - reserved)
    ceiling_pct = hourly_headroom_pct * share

    print(f"{ceiling_pct:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
