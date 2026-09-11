#!/usr/bin/env python3
"""pacing_hold_check.py -- pages when a pacing hold zeroes the WHOLE fleet for multiple
consecutive hourly ticks with nobody told (gh#812).

WHY: PR#797 added `maxx_share_ceiling.py`'s `block_over_pace()` branch, a deliberate hard
stop that holds spend when the account's 5h session block is running ahead of linear pace
(maxx's own `verdict=="over"` hits the same real-zero path). Both are honest, correct
zeroes -- but once one trips, nothing told anyone. On 2026-09-10 it held nearly every
fleet-kit member at `status=paced` for ~19 consecutive hours (~4 session blocks back to
back); the only trace was `paced` rows in runs.jsonl/fleet.db and a per-member log line
(run_member.sh's `PACED: ...` line) that nothing pages on.

NOT THE SAME ALARM AS budget_read_check.sh. That script pages when the meter is
UNREADABLE (maxx_reader.py returning a None fraction / label like `not_configured` or
`parse_fail`). A `paced` row can only come from a REAL reading: pacing_gate.py's own
contract fails OPEN on an empty/unreadable ceiling (returns "run", never "paced") -- so
this check and budget_read_check.sh can never both be reacting to the same underlying
cause. compose_page() says so explicitly, so a human reading the page does not conflate a
real, sustained zero-ceiling hold with a stale/unreadable meter (the issue body draws this
same distinction; conflating them is exactly what it warns against).

WHAT IT WATCHES: runs.jsonl (via fleet_metrics.load_runs/default_runs_path -- the same
parsing every other reader of this file already uses), bucketed by UTC hour. An hour
counts as a fleet-wide pacing hold if it has at least PACING_HOLD_MIN_ROWS_PER_HOUR rows
(default 2) and EVERY row in it has status "paced" -- one member running fine that hour
is proof headroom existed, so the hour is not held. The minimum row count matters because
current_streak() deliberately looks at the CURRENT, still-forming hour too (see its own
docstring) -- without it, the very first member to tick in a fresh hour landing "paced"
would mark that whole hour "held" on a single row, before anyone else has had a chance to
run and disprove it. The real 2026-09-10 incident held 6-10 rows/hour throughout, so this
costs nothing against the actual failure this check exists to catch. Pages once a streak
of PACING_HOLD_MIN_HOURS (default 2) consecutive held hours, ending at the current hour,
is reached. A single held tick that clears on its own must never page (marie's AC6).

DEDUPE/RESOLUTION goes through alert_store.py directly, severity=critical: this check's
own streak requirement IS the debounce (waiting out alert_store's own extra
DEGRADED_MIN_SEC on top would just delay a page this check has already earned), and
alert_store's per-key latch is what keeps a single held episode paging exactly once
(AC2) and firing a resolution notice the tick it clears (AC3) -- same shape
prod_health_check.py already uses for the same reason. fleet_alert.sh's own --check
gate is deliberately bypassed (plain positional call): this script already asked
alert_store the page/no-page question, asking a second time could disagree with itself.
Delivery itself (email + ntfy, queued retry if both fail) is entirely fleet_alert.sh's
job -- AC5 (NTFY_TOPIC unset must still deliver by email or queue, never drop silently)
needs no code here, only reuse.

CLI: pacing_hold_check.py [--runs PATH] [--at EPOCH] [--min-hours N] [--state-file PATH]
Usage (host cron, hourly -- mirrors account_health_check.sh's own invocation shape):
  52 * * * * FLEET_LOG_DIR=/home/ubuntu/fleet-kit-logs NTFY_TOPIC=<topic> \\
    python3 scripts/pacing_hold_check.py >> .../pacing_hold_check.cron.log 2>&1
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alert_store  # noqa: E402
import fleet_metrics  # noqa: E402 -- load_runs()/default_runs_path(), reuse the one runs.jsonl parser

KIT_DIR = Path(__file__).resolve().parent.parent
CHECK = "pacing_hold"
PROBLEM = "fleet_wide_paced"

MIN_HOLD_HOURS = int(os.environ.get("PACING_HOLD_MIN_HOURS", "2"))
MIN_ROWS_PER_HOUR = int(os.environ.get("PACING_HOLD_MIN_ROWS_PER_HOUR", "2"))


def _hour_bucket(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H", time.gmtime(ts))


def held_hours(rows: list[dict]) -> set[str]:
    """Every UTC hour bucket in which at least MIN_ROWS_PER_HOUR rows landed and EVERY row
    in it has status "paced" -- a fleet-wide hold for that tick. The row-count floor (not
    just "at least one") guards the CURRENT, still-forming hour specifically: a single
    early ticker landing paced before anyone else has run that hour is not evidence the
    fleet is held, only that one member happened to go first."""
    by_hour: dict[str, list[dict]] = {}
    for r in rows:
        ts = r.get("ts")
        if ts is None:
            continue
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        by_hour.setdefault(_hour_bucket(ts), []).append(r)
    return {h for h, hour_rows in by_hour.items()
            if len(hour_rows) >= MIN_ROWS_PER_HOUR
            and all(r.get("status") == "paced" for r in hour_rows)}


def current_streak(rows: list[dict], at: float) -> int:
    """How many consecutive hourly ticks, walking backward from `at`'s own (possibly
    still-open) hour, were a fleet-wide hold. Starting from the CURRENT hour (not the last
    completed one) means a hold still in progress is counted while it is still happening --
    gh#812's 19h incident was still open when a human would have wanted the page, not only
    after it finished."""
    held = held_hours(rows)
    if not held:
        return 0
    streak = 0
    cursor = at
    while _hour_bucket(cursor) in held:
        streak += 1
        cursor -= 3600
    return streak


def compose_page(streak_hours: int) -> tuple[str, str]:
    title = f"pacing hold: fleet-wide paced for {streak_hours}+ consecutive hours"
    body = (
        f"Every run recorded in runs.jsonl for the last {streak_hours} consecutive hourly "
        "ticks landed status=paced -- maxx_share_ceiling.py computed a real 0.0000 ceiling "
        "(its block_over_pace branch, PR#797, or maxx's own verdict=over hard stop), and "
        "pacing_gate.py correctly held every pass rather than spend through it (fk#781). "
        "This is NOT the unreadable-meter case budget_read_check.sh already pages for "
        "(maxx_reader.py returning label=not_configured/parse_fail with fraction=None) -- "
        f"that meter IS readable here; it is reading a real, sustained zero (gh#812). "
        f"{streak_hours}h and counting."
    )
    return title, body


def compose_recovery() -> tuple[str, str]:
    return (
        "fleet-kit: pacing hold cleared",
        "The fleet-wide pacing hold (gh#812) has cleared -- the current hour is no longer "
        "entirely status=paced.",
    )


def _fleet_alert(title: str, body: str) -> bool:
    try:
        p = subprocess.run(
            ["bash", str(KIT_DIR / "scripts" / "fleet_alert.sh"), title, body],
            capture_output=True, text=True, timeout=90,
        )
        if p.returncode != 0:
            print(f"pacing_hold_check: fleet_alert.sh failed rc={p.returncode} "
                  f"{(p.stderr or p.stdout or '')[:300]}", file=sys.stderr)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"pacing_hold_check: could not page: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=None)
    ap.add_argument("--at", type=float, default=None)
    ap.add_argument("--min-hours", type=int, default=MIN_HOLD_HOURS)
    ap.add_argument("--state-file", default=None)
    args = ap.parse_args(argv)

    at = args.at if args.at is not None else time.time()
    runs_path = Path(args.runs) if args.runs else fleet_metrics.default_runs_path()
    rows = fleet_metrics.load_runs(runs_path)
    streak = current_streak(rows, at)
    held = streak >= args.min_hours

    still_open = {PROBLEM} if held else set()
    resolved = alert_store.resolve_check(CHECK, keep=still_open, state_file=args.state_file)

    if not held:
        if any(r["was_paged"] for r in resolved):
            _fleet_alert(*compose_recovery())
            print("pacing_hold_check: recovery reported")
        print(f"pacing_hold_check: ok -- streak={streak}h (< {args.min_hours}h threshold)")
        return 0

    verdict = alert_store.record(CHECK, PROBLEM, "critical",
                                 detail=f"streak={streak}h", state_file=args.state_file)
    if not verdict.get("page"):
        print(f"pacing_hold_check: suppressed ({verdict.get('reason')}) -- streak={streak}h")
        return 0

    title, body = compose_page(streak)
    if _fleet_alert(title, body):
        print(f"pacing_hold_check: PAGED -- streak={streak}h")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
