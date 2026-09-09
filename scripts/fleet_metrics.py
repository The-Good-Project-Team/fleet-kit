#!/usr/bin/env python3
"""fleet_metrics.py -- the small catalog of numbers a prediction can be made about.

WHY (fleet-kit#782, part of #586). dumbledore writes a `Prediction:` line every pass and
nobody checks it: it is prose ("gru signal rate 48% -> 60% within 3 days") that no code can
resolve, so a wrong prediction costs nothing and a right one proves nothing. The Magikarp
grader then has to guess attribution from day counts and swings 42 -> 35 -> 15 in nine hours
on the same fleet. A prediction that can be scored has to name a metric this file can compute
for any window, from data the fleet already writes (runs.jsonl). predict.py stores the
prediction; this file is the ruler it is measured with.

METRICS (name[:member]; every one is computed over a window of `hours` ending at `at`):
  signal_rate:<member>            ok / (ok + quiet + reported_nothing)         -- 0..1
  quiet_rate:<member>             (quiet + reported_nothing) / executed         -- 0..1
  reported_nothing_per_day:<m>    reported_nothing rows, normalized to per day
  status_per_day:<status>[:<m>]   rows with that status, normalized to per day
  budget_declined_per_hr[:<m>]    budget_declined rows per hour
  paced_per_hr[:<m>]              paced rows per hour (fleet-kit#781)
  avg_turns:<member>              mean tokens.num_turns over executed rows
  avg_cost_usd:<member>           mean tokens.cost_usd over executed rows
  avg_duration_s:<member>         mean tokens.duration_ms / 1000 over executed rows
  self_critique_rate:<member>     rows whose Self-critique is not empty/none / executed
  runs_per_day[:<member>]         executed rows per day (any status that ran)
`<member>` may be `*` (or omitted where shown optional) for the whole fleet.

None means "unavailable" (no rows in the window, or a zero denominator) -- never 0, so a
prediction against an empty window resolves as `unavailable`, not as a hit or a miss.

CLI: fleet_metrics.py <metric> [--at EPOCH] [--hours 24] [--runs PATH]
     fleet_metrics.py list
Prints the value (repr of a float) or `unavailable`. --runs defaults to
$FLEET_LOG_DIR/runs.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

EXECUTED = {"ok", "quiet", "reported_nothing", "no_vision_link", "incomplete_fanout", "report_lost"}
SIGNAL_DENOM = {"ok", "quiet", "reported_nothing"}
PROVISIONAL = {"started"}

CATALOG = {
    "signal_rate": "ok / (ok + quiet + reported_nothing) for <member>",
    "quiet_rate": "(quiet + reported_nothing) / executed for <member>",
    "reported_nothing_per_day": "reported_nothing rows per day for <member>",
    "status_per_day": "rows with <status>[:<member>] per day",
    "budget_declined_per_hr": "budget_declined rows per hour [:<member>]",
    "paced_per_hr": "paced rows per hour [:<member>]",
    "avg_turns": "mean num_turns over executed rows for <member>",
    "avg_cost_usd": "mean cost_usd over executed rows for <member>",
    "avg_duration_s": "mean duration in seconds over executed rows for <member>",
    "self_critique_rate": "share of executed rows with a real Self-critique for <member>",
    "runs_per_day": "executed rows per day [:<member>]",
}


def default_runs_path() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit")) / "runs.jsonl"


def load_runs(path: Path | None = None) -> list[dict]:
    p = path or default_runs_path()
    rows: list[dict] = []
    if not p.exists():
        return rows
    for line in p.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("status") not in PROVISIONAL:
            rows.append(r)
    return rows


def parse(name: str) -> tuple[str, list[str]]:
    """'signal_rate:gru' -> ('signal_rate', ['gru']). Raises ValueError for an unknown metric."""
    parts = [p.strip() for p in (name or "").split(":")]
    base = parts[0]
    if base not in CATALOG:
        raise ValueError(f"unknown metric {base!r}; known: {', '.join(sorted(CATALOG))}")
    return base, parts[1:]


def _window(rows: list[dict], at: float, hours: float) -> list[dict]:
    lo = at - hours * 3600.0
    return [r for r in rows if lo < float(r.get("ts") or 0) <= at]


def _for_member(rows: list[dict], member: str | None) -> list[dict]:
    if not member or member == "*":
        return rows
    return [r for r in rows if r.get("member") == member]


def _mean(values: list[float]) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return (sum(vals) / len(vals)) if vals else None


def _tok(r: dict, key: str):
    t = r.get("tokens") or {}
    v = t.get(key)
    if v is None and key == "cost_usd":
        v = t.get("total_cost_usd")
    return v


def compute(name: str, rows: list[dict], at: float | None = None, hours: float = 24.0) -> float | None:
    base, args = parse(name)
    at = time.time() if at is None else float(at)
    hours = float(hours)
    win = _window(rows, at, hours)
    days = hours / 24.0

    if base == "signal_rate":
        rs = [r for r in _for_member(win, args[0] if args else None) if r.get("status") in SIGNAL_DENOM]
        return (sum(1 for r in rs if r.get("status") == "ok") / len(rs)) if rs else None
    if base == "quiet_rate":
        rs = [r for r in _for_member(win, args[0] if args else None) if r.get("status") in EXECUTED]
        return (sum(1 for r in rs if r.get("status") in ("quiet", "reported_nothing")) / len(rs)) if rs else None
    if base == "reported_nothing_per_day":
        rs = _for_member(win, args[0] if args else None)
        return sum(1 for r in rs if r.get("status") == "reported_nothing") / days if rs else None
    if base == "status_per_day":
        if not args:
            raise ValueError("status_per_day needs :<status>")
        rs = _for_member(win, args[1] if len(args) > 1 else None)
        return sum(1 for r in rs if r.get("status") == args[0]) / days if rs else None
    if base in ("budget_declined_per_hr", "paced_per_hr"):
        status = base.split("_per_hr")[0]
        rs = _for_member(win, args[0] if args else None)
        return sum(1 for r in rs if r.get("status") == status) / hours if rs else None
    if base in ("avg_turns", "avg_cost_usd", "avg_duration_s"):
        rs = [r for r in _for_member(win, args[0] if args else None) if r.get("status") in EXECUTED]
        key = {"avg_turns": "num_turns", "avg_cost_usd": "cost_usd", "avg_duration_s": "duration_ms"}[base]
        vals = [_tok(r, key) for r in rs]
        m = _mean([v for v in vals if v is not None])
        return (m / 1000.0) if (m is not None and base == "avg_duration_s") else m
    if base == "self_critique_rate":
        rs = [r for r in _for_member(win, args[0] if args else None) if r.get("status") in EXECUTED]
        if not rs:
            return None
        real = 0
        for r in rs:
            sc = (r.get("self_critique") or "").strip().lower()
            if sc and not sc.startswith("none"):
                real += 1
        return real / len(rs)
    if base == "runs_per_day":
        rs = [r for r in _for_member(win, args[0] if args else None) if r.get("status") in EXECUTED]
        return len(rs) / days if rs else None
    raise ValueError(base)  # unreachable: parse() already rejected unknown names


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("metric", help="metric name, or `list`")
    ap.add_argument("--at", type=float, default=None, help="window end, epoch seconds (default now)")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--runs", type=Path, default=None)
    a = ap.parse_args(argv[1:])
    if a.metric == "list":
        for k, v in CATALOG.items():
            print(f"{k:28} {v}")
        return 0
    try:
        v = compute(a.metric, load_runs(a.runs), a.at, a.hours)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print("unavailable" if v is None else repr(round(v, 6)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
