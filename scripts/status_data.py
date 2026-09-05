#!/usr/bin/env python3
"""status_data.py -- roll the fleet's health-check cron logs up into an incident.io-style
component status: one row per component, one cell per hour, plus an uptime percentage.

WHY FROM THE CRON LOGS: five checks already run every 5 minutes and have been writing
plain-text verdicts for days (account pool, public path, tunnel, maxx anchor per handle,
budget meter). That IS the uptime history -- it just had no reader. Nothing new needs to be
recorded, and no check has to change to feed this.

WHY A LINE-PREFIX PARSE, not a log format change: rewriting five working alarms to emit JSON
would risk the alarms themselves to gain a dashboard. The verdict word is always the first
token after the bracketed check name, so classification is a prefix match and an unparseable
line is counted as UNKNOWN rather than silently as healthy -- the same law the budget chain
learned the hard way (a bad read may never be rendered as good news).
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path

LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", "/home/ubuntu/fleet-kit-logs"))

OK = "ok"
BAD = "down"
UNKNOWN = "unknown"

# (component label, candidate log filenames, description). Candidates, not one path: the
# SAME check writes a different filename depending on where it runs -- on the host the cron
# lines suffix per instance (path_health_check.philanthropy.cron.log) while inside the
# container the entrypoint's crontab writes the bare name (path_health_check.log). Hardcoding
# either one renders a page that says "no data" for five healthy components, which is worse
# than no page. First candidate that exists wins.
COMPONENTS = [
    ("Account pool",
     ["account_health_check.cron.log", "account_health_check.log"],
     "Claude accounts the fleet spends from"),
    ("Public path",
     ["path_health_check.philanthropy.cron.log", "path_health_check.log"],
     "public URL for this instance"),
    ("Tunnel",
     ["tunnel_health_check.cron.log", "tunnel_health_check.log"],
     "cloudflared tunnel to dino"),
    ("Budget meter (tgp)",
     ["anchor_staleness.reif_tgp.cron.log"],
     "maxx anchor freshness for reif_tgp"),
    ("Budget meter (gmail)",
     ["anchor_staleness.reif.cron.log", "anchor_staleness.cron.log"],
     "maxx anchor freshness for reif"),
    ("Deploy",
     ["deploy_staleness_check.cron.log", "deploy_staleness_check.log"],
     "fleet-kit's own deploy pipeline"),
]


def _resolve(names) -> Path | None:
    """First candidate that exists. None -> the component renders as 'no data', never green."""
    for n in names:
        p = LOG_DIR / n
        if p.exists():
            return p
    return None


# Verdict words each check emits. Anything unmatched is UNKNOWN, never assumed healthy.
GOOD_WORDS = ("healthy", "fresh", "ok")
BAD_WORDS = ("STALE", "ALARM", "DOWN", "FAILED", "unhealthy", "WARNING", "unreachable")

_TS = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\]")


def classify(line: str) -> str:
    if any(w in line for w in BAD_WORDS):
        return BAD
    body = line.split("]", 1)[1] if "]" in line else line
    if any(body.lstrip().startswith(w) for w in GOOD_WORDS):
        return OK
    return UNKNOWN


def read_component(names, hours: int = 72) -> tuple[list[str], float | None]:
    """Return (per-hour buckets oldest->newest, uptime pct). No file -> all unknown."""
    path = _resolve(names if isinstance(names, (list, tuple)) else [names])
    now = _dt.datetime.now(_dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    buckets: dict[int, list[str]] = {}
    if path is not None:
        # Only the tail matters: these logs grow to hundreds of KB and a status page must not
        # read the whole file on every request.
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 400_000))
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            text = ""
        mtime = _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.timezone.utc)
        lines = [l for l in text.splitlines() if l.strip()]
        # Most of these lines carry no timestamp of their own, so distribute them backwards
        # from the file's mtime at the known 5-minute cadence. That is an approximation and is
        # labelled as such in the UI rather than presented as exact.
        for i, line in enumerate(reversed(lines)):
            m = _TS.search(line)
            if m:
                ts = _dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=_dt.timezone.utc)
            else:
                ts = mtime - _dt.timedelta(minutes=5 * i)
            age_h = int((now - ts.replace(minute=0, second=0, microsecond=0)).total_seconds() // 3600)
            if 0 <= age_h < hours:
                buckets.setdefault(age_h, []).append(classify(line))

    out: list[str] = []
    good = total = 0
    for h in range(hours - 1, -1, -1):
        vals = buckets.get(h, [])
        if not vals:
            out.append(UNKNOWN)
            continue
        # An hour is down if ANY check in it failed: a status page that averages away a real
        # outage is worse than none.
        state = BAD if BAD in vals else (OK if OK in vals else UNKNOWN)
        out.append(state)
        if state in (OK, BAD):
            total += 1
            good += 1 if state == OK else 0
    pct = (good / total * 100.0) if total else None
    return out, pct


def snapshot(hours: int = 72) -> dict:
    comps = []
    worst = OK
    for label, names, desc in COMPONENTS:
        cells, pct = read_component(names, hours)
        current = next((c for c in reversed(cells) if c != UNKNOWN), UNKNOWN)
        if current == BAD:
            worst = BAD
        elif current == UNKNOWN and worst == OK:
            worst = UNKNOWN
        comps.append({"label": label, "description": desc, "cells": cells,
                      "uptime_pct": pct, "current": current})
    return {
        "overall": worst,
        "components": comps,
        "hours": hours,
        "members": members(hours),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


# ---------------------------------------------------------------------------
# Fleet members: last runs, from fleet.db rather than the cron logs.
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("FLEET_DB", "/var/log/fleet-kit/fleet.db")
CONTAINER = os.environ.get("FLEET_CONTAINER_NAME", "philanthropy")

# A member's run status is NOT a health verdict and must not be rendered as one.
# `budget_declined` is the single most common outcome for several members (46 of gru's
# last 72h) and it means the fleet correctly REFUSED to spend -- pacing working, not
# breaking. Painting it red would make a healthy, well-behaved fleet look like an
# outage. `killed` is likewise expected: deploy.sh cuts over mid-pass after its drain
# bound, and run_member.sh records those as killed, safe to re-run.
RUN_STATE = {
    "ok": OK,
    "quiet": OK,               # ran, correctly found nothing to do
    "reported_nothing": OK,    # ran, produced no report -- weak, not down
    "budget_declined": "spare",
    "killed": "spare",
    "timed_out": BAD,
    "error": BAD,
}


def _sqlite(query: str) -> str:
    """Query fleet.db. Runs through the container because the DB lives inside it."""
    import subprocess
    try:
        r = subprocess.run(
            ["podman", "exec", CONTAINER, "sqlite3", DB_PATH, query],
            capture_output=True, text=True, timeout=20)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def members(hours: int = 72) -> list[dict]:
    """One row per member: last run, its age, and the recent status mix."""
    rows = _sqlite(
        "SELECT member, status, recorded_at, COALESCE(cost_usd,0) "
        "FROM runs WHERE recorded_at > strftime('%%s','now','-%d hours') "
        "ORDER BY recorded_at;" % hours
    )
    if not rows.strip():
        return []
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    agg: dict[str, dict] = {}
    for line in rows.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        name, status, ts, cost = parts[0], parts[1], parts[2], parts[3]
        try:
            ts = float(ts); cost = float(cost)
        except ValueError:
            continue
        m = agg.setdefault(name, {"name": name, "runs": 0, "ok": 0, "bad": 0,
                                  "spare": 0, "cost": 0.0, "last": 0.0,
                                  "last_status": ""})
        m["runs"] += 1
        m["cost"] += cost
        state = RUN_STATE.get(status, UNKNOWN)
        if state == OK:
            m["ok"] += 1
        elif state == BAD:
            m["bad"] += 1
        elif state == "spare":
            m["spare"] += 1
        if ts > m["last"]:
            m["last"] = ts
            m["last_status"] = status

    out = []
    for m in agg.values():
        age_min = (now - m["last"]) / 60.0
        m["ago"] = ("%dm ago" % age_min if age_min < 90
                    else "%.0fh ago" % (age_min / 60))
        # Silence is the real failure mode for a scheduled member, and it is the one
        # the run table cannot show as a row: a member that stopped running writes
        # nothing at all. Age of the LAST run is what surfaces it.
        m["state"] = BAD if m["bad"] else (UNKNOWN if age_min > 240 else OK)
        m["cost_str"] = "$%.2f" % m["cost"]
        out.append(m)
    return sorted(out, key=lambda x: (-x["runs"], x["name"]))


if __name__ == "__main__":
    import json
    print(json.dumps(snapshot(), indent=2))