#!/usr/bin/env python3
"""fleet_stats — aggregation for the Stats page's charts. Same "no DB, derive from what already
exists" philosophy as the rest of this kit (see fleet_view_server.py's own header): runs_summary
and token_usage_by_hour read STATE.runs (already in memory, maintained by the live server,
nothing new to persist); backlog_history reconstructs open-issue count over time from GitHub's
own createdAt/closedAt fields on a single `gh issue list --state all` call (638 issues, one
call, confirmed live 2026-08-25 -- no rate-limit concern, no need to have been recording a time
series all along since the fleet started).
"""
from __future__ import annotations

import datetime as _dt
import json


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


# A run that never got to execute (blocked by budget/rate limits before doing any work) --
# these dilute "is the fleet actually doing good work" if counted alongside real attempts, so
# signal rate is computed over executed runs only. Every other status counts as executed,
# including reported_nothing/quiet -- those DID run, they just found nothing worth reporting.
_NOT_EXECUTED_STATUSES = {"budget_declined", "timed_out", "killed"}
_OK_STATUSES = {"ok"}

# gh#437: run_report.py's provisional pre-launch row (gh#145, STATUS_STARTED) is written before
# the real completion row lands -- it is not a second run and not a failed one, just a lifecycle
# marker. It must never contribute to total/executed/signal_rate/agent_rates (that's the 3rd
# instance of gh#150/gh#254's "a new run_report.py status doesn't reach this file" class), but it
# still belongs in `dormant`'s view: a member with only a started row this window is mid-run, not
# walled off, and folding it into _NOT_EXECUTED_STATUSES there would misreport an in-flight pass
# as a budget decline.
_PROVISIONAL_STATUSES = {"started"}


def runs_summary(runs: list[dict], hours: float = 24.0, roster: list[dict] | None = None) -> dict:
    """One payload for the whole Recent Runs card: headline KPIs (signal rate, budget-wall rate,
    total runs, dormant-agent count), an hourly stacked-bar of run outcomes, and per-agent signal
    rate over executed runs. Replaces the old per-run scatter (member x time), which answered
    "when did each agent run" -- a question nobody was asking -- with "is the fleet's output any
    good," which is the one that matters. One endpoint, one fetch, since all of it is the same
    windowed pass over `runs` (STATE.runs, already in memory).

    `roster` (gh#190) is the full member list from `member_spec.load_all()`, optional so
    existing callers (selftest.py) that don't have it keep today's behavior: with no roster, a
    member absent entirely from the window can never be flagged dormant, only members with
    in-window runs that are all budget_declined/timed_out/killed. Passed a roster, any
    `enabled: true` member missing from the window entirely -- the single worst case this tile
    exists to catch -- is added too. `enabled: false` members (nerd/minion, #166) are excluded
    the same way the sidebar dot excludes them: intentional non-scheduling is not dormancy.
    """
    cutoff = _now_epoch() - hours * 3600
    windowed = [r for r in runs if r.get("ts") is not None and r.get("ts") >= cutoff]
    # excludes "started" rows entirely (gh#437) -- a started row paired with its own completion
    # row (also in-window) would otherwise count twice: once here as an extra total, once as an
    # executed-but-not-ok failure. `windowed` itself is kept intact (with started rows) below,
    # purely for `dormant`'s in-flight-run check.
    non_provisional = [r for r in windowed if (r.get("status") or "") not in _PROVISIONAL_STATUSES]

    total = len(non_provisional)
    executed = [r for r in non_provisional if (r.get("status") or "") not in _NOT_EXECUTED_STATUSES]
    declined = total - len(executed)
    ok = sum(1 for r in executed if (r.get("status") or "") in _OK_STATUSES)

    # gh#153: `None` here (not `0`) when the denominator is empty -- a real 0% (every executed
    # run failed, or every run got budget-declined) is a different diagnosis from "nothing ran
    # in this window at all," and the two must not render as the same number downstream.
    signal_rate = round(100 * ok / len(executed)) if executed else None
    budget_wall = round(100 * declined / total) if total else None

    # dormant: members with runs in the window whose most recent run was budget_declined AND
    # who logged nothing else -- i.e. every attempt in-window got walled off, not just the last one
    by_member: dict[str, list[dict]] = {}
    for r in windowed:
        by_member.setdefault(r.get("member") or "unknown", []).append(r)
    dormant = [m for m, rs in by_member.items()
               if rs and all((r.get("status") or "") in _NOT_EXECUTED_STATUSES for r in rs)]
    if roster:
        dormant += [spec["name"] for spec in roster
                    if spec.get("enabled") and spec["name"] not in by_member]

    # hourly stacked-bar: count per (hour, status). Uses non_provisional -- "started" has no
    # STATUS_COLOR legend entry in fleet_view.html and must not render as an outcome (gh#437).
    hour_buckets: dict[int, dict[str, int]] = {}
    for r in non_provisional:
        hour = int(r["ts"] // 3600) * 3600
        status = r.get("status") or "unknown"
        b = hour_buckets.setdefault(hour, {})
        b[status] = b.get(status, 0) + 1
    statuses = sorted({s for b in hour_buckets.values() for s in b})
    hourly = [{"ts": hour, **{s: b.get(s, 0) for s in statuses}}
              for hour, b in sorted(hour_buckets.items())]

    # minions spawned per hour. gru decides how many minions an hour can afford (fanout.py packs
    # by complexity), so this series is the fleet's actual BUILD throughput -- the run-outcome
    # chart above counts all nine members together and buries it. Every hour in the window is
    # emitted, zeros included: a gap in a sparse series reads as "no data", while an explicit 0
    # reads as "gru ran and chose to spawn nothing", which is a real and different signal.
    minion_buckets: dict[int, int] = {}
    for r in non_provisional:
        if (r.get("member") or "") != "minion":
            continue
        hour = int(r["ts"] // 3600) * 3600
        minion_buckets[hour] = minion_buckets.get(hour, 0) + 1
    first_hour = int(cutoff // 3600) * 3600
    last_hour = int(_now_epoch() // 3600) * 3600
    minions_hourly = [{"ts": h, "count": minion_buckets.get(h, 0)}
                      for h in range(first_hour, last_hour + 3600, 3600)]

    # per-agent signal rate, executed runs only -- also excludes "started" (gh#437), same reason
    # as `executed`/`total` above; `by_member` itself still includes started rows (dormant needs
    # them), so the exclusion has to happen here rather than by switching to a non_provisional-only
    # by_member.
    agent_rates = []
    for member, rs in sorted(by_member.items()):
        member_executed = [r for r in rs if (r.get("status") or "") not in _NOT_EXECUTED_STATUSES
                            and (r.get("status") or "") not in _PROVISIONAL_STATUSES]
        if not member_executed:
            continue
        member_ok = sum(1 for r in member_executed if (r.get("status") or "") in _OK_STATUSES)
        agent_rates.append({
            "member": member,
            "signal_rate": round(100 * member_ok / len(member_executed)),
            "executed": len(member_executed),
        })
    agent_rates.sort(key=lambda a: -a["signal_rate"])

    return {
        "hours": hours,
        "signal_rate": signal_rate,
        "executed": len(executed),
        "total": total,
        "budget_wall": budget_wall,
        "declined": declined,
        "dormant": dormant,
        "hourly": hourly,
        "statuses": statuses,
        "minions_hourly": minions_hourly,
        "agent_rates": agent_rates,
    }


def lost_passes(runs: list[dict], grace_minutes: float = 90.0) -> list[dict]:
    """gh#145: run_ids with a "started" row (run_report.py's build_started_record, written by
    run_member.sh before `claude -p` is even invoked) and no completion row -- normal-exit or
    the SIGTERM trap's `killed` record, any status other than "started" counts -- landed after
    at least `grace_minutes` have passed. That pairing is what makes a pass killed before
    either of run_member.sh's own two write points (SIGKILL, container replacement, OOM)
    detectable as a gap instead of silently reading as "never ran" (the incident this issue is
    named for: 4 real `gh issue close` calls, zero runs.jsonl trace).

    Pairs purely on `run_id` -- both legs of a healthy run share the exact same one (see
    run_member.sh's RUN_ID) -- so this reads correctly regardless of `runs`' ordering (jsonl is
    append-only, so in practice a "started" row always precedes its completion, but nothing
    here depends on that).

    `grace_minutes` default (90) is a flat fallback, not tuned per member -- the PRD (gh#145)
    left the choice of a smarter one (e.g. 2x a member's typical pass duration) an open
    question; a caller with that data can pass its own value.
    """
    now = _now_epoch()
    started: dict[str, dict] = {}
    completed_ids: set[str] = set()
    for r in runs:
        rid = r.get("run_id")
        if not rid:
            continue
        if r.get("status") == "started":
            started[rid] = r
        else:
            completed_ids.add(rid)

    out = []
    for rid, r in started.items():
        if rid in completed_ids:
            continue
        ts = r.get("ts")
        if ts is None:
            continue
        age_minutes = (now - ts) / 60.0
        if age_minutes < grace_minutes:
            continue
        out.append({
            "run_id": rid,
            "member": r.get("member"),
            "item_id": r.get("item_id"),
            "started_ts": ts,
            "age_minutes": round(age_minutes, 1),
        })
    out.sort(key=lambda x: -x["age_minutes"])
    return out


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


def pr_activity_by_day(prs_json: str, days: int = 14) -> dict:
    """New PRs opened + PRs merged (shipped), per day -- the throughput counterpart to
    backlog_history's open-issue-count line, both day-keyed on the same x-axis so growth vs
    throughput read on one chart. `prs_json` is the raw stdout of `gh pr list --state all --json
    createdAt,mergedAt --limit 500` -- one call covers both series (open+merged+closed PRs all
    have createdAt; only merged ones have mergedAt), same "parse it here" pattern as the rest of
    this module.
    """
    try:
        prs = json.loads(prs_json) if prs_json else []
    except json.JSONDecodeError:
        return {"new_prs": [], "merged_prs": []}
    today = _dt.datetime.now(_dt.timezone.utc).date()
    start = today - _dt.timedelta(days=days - 1)

    def _parse(ts: str | None) -> _dt.date | None:
        if not ts:
            return None
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()

    opened: dict[_dt.date, int] = {}
    merged: dict[_dt.date, int] = {}
    for pr in prs:
        d = _parse(pr.get("createdAt"))
        if d and start <= d <= today:
            opened[d] = opened.get(d, 0) + 1
        m = _parse(pr.get("mergedAt"))
        if m and start <= m <= today:
            merged[m] = merged.get(m, 0) + 1

    days_list = []
    day = start
    while day <= today:
        days_list.append(day)
        day += _dt.timedelta(days=1)

    return {
        "new_prs": [{"date": d.isoformat(), "count": opened.get(d, 0)} for d in days_list],
        "merged_prs": [{"date": d.isoformat(), "count": merged.get(d, 0)} for d in days_list],
    }
