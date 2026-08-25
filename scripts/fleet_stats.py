#!/usr/bin/env python3
"""fleet_stats — aggregation for the Stats page's three charts. Same "no DB, derive from what
already exists" philosophy as the rest of this kit (see fleet_view_server.py's own header):
runs_timeline and token_usage read fleet_db.py's sqlite mirror of runs.jsonl (already
maintained by the live server, nothing new to persist); backlog_history reconstructs open-issue
count over time from GitHub's own createdAt/closedAt fields on a single `gh issue list --state
all` call (638 issues, one call, confirmed live 2026-08-25 -- no rate-limit concern, no need to
have been recording a time series all along since the fleet started).
"""
from __future__ import annotations

import datetime as _dt
import json


def runs_timeline(runs: list[dict], hours: float = 24.0) -> list[dict]:
    """Recent runs as timeline points: one per run, member/status/ts/cost -- the frontend plots
    these as a scatter (member on one axis, time on the other) rather than this module owning
    any chart-shape opinion. `runs` is STATE.runs (already in memory, ts as unix epoch seconds
    per run_report.py's contract) -- no new query, just a time-windowed pass-through with only
    the fields a chart needs (dropping outcome/evidence prose, which can be large).
    """
    cutoff = _now_epoch() - hours * 3600
    out = []
    for r in runs:
        ts = r.get("ts")
        if ts is None or ts < cutoff:
            continue
        out.append({
            "ts": ts,
            "member": r.get("member"),
            "status": r.get("status"),
            "cost_usd": (r.get("tokens") or {}).get("cost_usd"),
        })
    out.sort(key=lambda r: r["ts"])
    return out


def token_usage_by_hour(runs: list[dict], hours: float = 24.0) -> list[dict]:
    """Total input+output tokens per hour bucket, windowed -- one point per hour so the chart
    stays small and readable even across a multi-day window, rather than one point per run
    (a busy fleet can log 50+ runs/hour, unreadable as individual points on a line chart).
    """
    cutoff = _now_epoch() - hours * 3600
    buckets: dict[int, dict] = {}
    for r in runs:
        ts = r.get("ts")
        if ts is None or ts < cutoff:
            continue
        tokens = r.get("tokens") or {}
        hour = int(ts // 3600) * 3600
        b = buckets.setdefault(hour, {"input": 0, "output": 0, "cost_usd": 0.0})
        b["input"] += tokens.get("input_tokens") or 0
        b["output"] += tokens.get("output_tokens") or 0
        b["cost_usd"] += tokens.get("cost_usd") or 0.0
    return [
        {"ts": hour, "input_tokens": b["input"], "output_tokens": b["output"],
         "cost_usd": round(b["cost_usd"], 4)}
        for hour, b in sorted(buckets.items())
    ]


def _now_epoch() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


def backlog_history(issues_json: str, days: int = 30) -> list[dict]:
    """Open fleet:backlog issue count per day over the last `days` days, reconstructed from
    every issue's createdAt/closedAt (a still-open issue has closedAt=None, counts as open at
    every day from its creation through today). `issues_json` is the raw stdout of
    `gh issue list --state all --label fleet:backlog --json number,createdAt,closedAt --limit
    1000` -- parsed here, not in the caller, so this module owns its own error handling the
    same way fleet_kpi.py owns its own regex patterns.

    Approximate by design: a day boundary, not an hour-by-hour reconstruction -- this chart
    answers "is the backlog growing or shrinking," not "what was open at 3:47pm on the 12th."
    """
    try:
        issues = json.loads(issues_json) if issues_json else []
    except json.JSONDecodeError:
        return []
    today = _dt.datetime.now(_dt.timezone.utc).date()
    start = today - _dt.timedelta(days=days - 1)

    def _parse(ts: str | None) -> _dt.date | None:
        if not ts:
            return None
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()

    parsed = [(_parse(i.get("createdAt")), _parse(i.get("closedAt"))) for i in issues]

    out = []
    day = start
    while day <= today:
        count = sum(1 for created, closed in parsed
                    if created and created <= day and (closed is None or closed > day))
        out.append({"date": day.isoformat(), "count": count})
        day += _dt.timedelta(days=1)
    return out


def merged_prs_by_day(prs_json: str, days: int = 30) -> list[dict]:
    """Merged PR count per day over the last `days` days -- a second series for the same Backlog
    chart, so growth (backlog size) and throughput (PRs actually landing) sit on one timeline
    instead of forcing a second card. `prs_json` is the raw stdout of `gh pr list --state merged
    --json mergedAt --limit 500`, same "parse it here, own the error handling" pattern as
    backlog_history above.

    Same day-bucket granularity as backlog_history (not hour-by-hour) so the two series line up
    on one shared x-axis without a second date-parsing pass in the frontend.
    """
    try:
        prs = json.loads(prs_json) if prs_json else []
    except json.JSONDecodeError:
        return []
    today = _dt.datetime.now(_dt.timezone.utc).date()
    start = today - _dt.timedelta(days=days - 1)

    def _parse(ts: str | None) -> _dt.date | None:
        if not ts:
            return None
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()

    counts: dict[_dt.date, int] = {}
    for pr in prs:
        d = _parse(pr.get("mergedAt"))
        if d and start <= d <= today:
            counts[d] = counts.get(d, 0) + 1

    out = []
    day = start
    while day <= today:
        out.append({"date": day.isoformat(), "count": counts.get(day, 0)})
        day += _dt.timedelta(days=1)
    return out
