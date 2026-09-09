#!/usr/bin/env python3
"""pacing_gate.py <ceiling> <spec-json-on-stdin> -- should this pass run, or wait for headroom?

WHY (fleet-kit#781, Reif 2026-09-09: "we keep hitting session limits - likely something the
fleet needs to do about that"). run_member.sh already computes FLEET_SHARE_CEILING_PCT from
maxx_share_ceiling.py on every pass. That number was 0.0000 for hours on 2026-09-09 while
budget_read_check.log read "the week is -65.7% over pace" -- and every member launched
`claude -p` anyway, because the ceiling was advisory: only gru reads it. 200 quiet passes on
an over-pace day is the spend that ran both accounts dry by Wednesday.

Prints exactly one word:
  run    -- spend. The ceiling is unreadable (empty: fails open, same law as maxx_reader.py --
            a bad reading may only ever CONSERVE, never invent an outage), or it is above zero,
            or the member's spec says "pacing": "exempt" (the-fixer, the brief, the merge gate).
  paced  -- do not spend. The ceiling is a real 0.0000: this hour is at or past sustainable
            pace once other instances' reservations are subtracted, or maxx said verdict=over.

Pure: no network, no files. The only inputs are the two arguments.
"""
from __future__ import annotations

import json
import sys

EXIT_CODE = 75  # EX_TEMPFAIL -- run_report.py maps it to status "paced"


def decide(ceiling: str | None, spec: dict | None) -> str:
    if (spec or {}).get("pacing") == "exempt":
        return "run"
    raw = (ceiling or "").strip()
    if not raw:
        return "run"  # unreadable meter: fail open
    try:
        value = float(raw)
    except ValueError:
        return "run"
    return "paced" if value <= 0.0 else "run"


def main(argv: list[str]) -> int:
    ceiling = argv[1] if len(argv) > 1 else ""
    try:
        spec = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except (ValueError, OSError):
        spec = {}
    print(decide(ceiling, spec if isinstance(spec, dict) else {}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
