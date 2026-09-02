#!/usr/bin/env python3
"""gru_allowance.py -- gru's hourly allowance, in PERCENT-OF-WEEK units.

WHY THIS IS A SCRIPT AND NOT PROSE IN gru.md: the charter itself says "do not do this
arithmetic in your head -- you are provably bad at it," and then asked gru to do exactly
that. It got it wrong in a way no one could see from the outside, for two compounding
reasons (Reif, 2026-09-02, auditing the Settings dials):

  1. WRONG BASE. The charter said to multiply `per_diem_hourly_pct`. That field is the hour's
     burn SO FAR -- consumption, not headroom (maxx_share_ceiling.py subtracts it from
     `sustainable_pct_per_hour` for precisely that reason). gru was slicing the wrong number:
     as the hour got MORE expensive, gru's computed allowance went UP.

  2. WRONG COMPOSITION. FLEET_SHARE_FRACTION and FLEET_GRU_ALLOWANCE_FRACTION were combined
     with min(), but they answer two nested questions and must MULTIPLY:

         "what share of the whole account may this instance use?"   FLEET_SHARE_FRACTION
         "what share of OUR slice may gru have?"                    FLEET_GRU_ALLOWANCE_FRACTION

     Under min() the smaller one simply won and the other became dead config. Live on
     fleet-kit-server-fleet: per_diem_hourly_pct=0.349, ceiling(0.20)=0.0142, so
     min(0.349*F, 0.0142) == 0.0142 for ANY F above ~0.04. Setting the dial to 0.25, 0.75 or
     0.99 produced an identical number, and that number was the instance's ENTIRE slice --
     gru took 100% of it, leaving nothing reserved for the other eight members.

The correct composition is one multiply on the already-coordinated ceiling:

    allowance = FLEET_SHARE_CEILING_PCT * FLEET_GRU_ALLOWANCE_FRACTION

FLEET_SHARE_CEILING_PCT already IS this instance's share of real, cross-instance-coordinated
hourly headroom (run_member.sh exports it from maxx_share_ceiling.py, which subtracts other
instances' live reservations). Taking gru's fraction OF that yields both nested percentages
and leaves 1-F of the instance's slice for everyone else -- which is what the dial always
claimed to mean.

FAILS OPEN, same law as maxx_reader.py and maxx_share_ceiling.py: no ceiling exported (meter
unreadable, or the operator never set FLEET_SHARE_FRACTION) means this prints nothing and
gru falls back to its own documented conservative default. An absent reading must only ever
be usable to CONSERVE. A ceiling of exactly 0.0 is a real, honest answer and is printed as
0.0000 -- distinct from "no reading".
"""
from __future__ import annotations

import os
import sys

DEFAULT_FRACTION = 0.70


def compute(ceiling_pct: str | None, fraction_raw: str | None) -> str:
    """Return the allowance as a formatted string, or "" when there is no trustworthy input."""
    if ceiling_pct is None or str(ceiling_pct).strip() == "":
        return ""   # fail open: no ceiling => caller keeps its own fallback
    try:
        ceiling = float(ceiling_pct)
    except ValueError:
        return ""
    if ceiling < 0:
        return ""

    try:
        fraction = float(fraction_raw) if str(fraction_raw or "").strip() else DEFAULT_FRACTION
    except ValueError:
        fraction = DEFAULT_FRACTION
    # An operator typo (1.5, or a negative) must never hand gru more than the instance's own
    # slice, nor a negative allowance. Clamp rather than fail: the ceiling is still a real,
    # safe number and refusing it entirely would idle the fleet over a config slip.
    fraction = max(0.0, min(fraction, 1.0))

    return f"{ceiling * fraction:.4f}"


def main(argv: list[str]) -> int:
    ceiling = argv[1] if len(argv) > 1 else os.environ.get("FLEET_SHARE_CEILING_PCT")
    fraction = os.environ.get("FLEET_GRU_ALLOWANCE_FRACTION")
    print(compute(ceiling, fraction))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
