#!/usr/bin/env python3
"""lane_kpi -- turns a lane's raw log ticks into a stored, queryable KPI (gh#324).

kpi-doctrine.md rule 1: "an agent never computes its own number." `devops`
(auto_deploy.sh) has had no independent measurement of deploy_success_rate/deploy_count_7d --
only ad-hoc greps a nerd pass ran by hand against its own log, i.e. the lane grading itself.
This is the independent job: it shares no process context with any devops/nerd agent pass,
runs on its own cron tick (entrypoint.sh's crontab; see docstring below for why here and not
scripts/auto_deploy.sh itself), and only ever reads auto_deploy.log and writes fleet.db.

Currently computes one metric (devops/deploy_success_rate) because that is the one this issue
names -- the table and CLI are generic (lane, metric) so a second lane's job can reuse the same
store without a new table (kpi-doctrine.md rule 7's "every filed item names its KPI" already
implies KPIs are plural; nothing here should need to change to add one).

WHY THIS DOESN'T LIVE IN scripts/fleet_kpi.py: that module's _KPI_TABLE sums patterns out of a
member's own `outcome` prose -- self-reported, and structurally fine for THAT file's stated
purpose (per-member activity counts) but exactly what rule 1 forbids for a real KPI. Different
mechanism, different table, on purpose (this issue's own non-goals).

WHY THIS RUNS INSIDE THE CONTAINER (entrypoint.sh), NOT ON THE HOST like auto_deploy.sh: it
only reads/appends plain files under FLEET_LOG_DIR (auto_deploy.log, fleet.db) -- both already
reachable from inside the container over the same bind mount deploy_staleness_check.sh and
auto_deploy_race_check.sh (gh#201, gh#255) already use for the identical reason. No
podman/docker-in-docker needed, so there is no reason to pay the host-cron complexity
auto_deploy.sh's own header explains it needs for `podman build`/`podman run`.
"""
from __future__ import annotations

import argparse
import calendar
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fleet_db  # noqa: E402

LOG_DIR = fleet_db.LOG_DIR
DEPLOY_LOG = Path(os.environ.get("FLEET_AUTO_DEPLOY_LOG", LOG_DIR / "auto_deploy.log"))

# Trailing window this job measures, per the issue's own deploy_count_7d naming.
WINDOW_S = 7 * 24 * 3600.0
# This job's own entrypoint.sh cron line runs hourly -- see that file's comment for the exact
# minute and why. Kept as a constant here (not re-derived from the crontab) so read_latest()
# has a fixed, testable staleness budget; kpi-doctrine.md rule 5 wants 2x this before STALE.
EXPECTED_INTERVAL_S = 3600.0

LANE = "devops"
METRIC = "deploy_success_rate"

_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\]")
_SUCCESS_RE = re.compile(r"deploy OK at")
# AC2's exact three failure shapes (scripts/auto_deploy.sh:150,152,104,142). A `drain:` line
# (deploy.sh's own in-flight-pass wait, captured into the same log by auto_deploy.sh's
# `>> "$LOG" 2>&1`) matches none of these and so is correctly never counted as a tick --
# it's an intermediate line of a tick still in progress, not a resolution.
_FAILURE_RES = (
    re.compile(r"DEPLOY FAILED at"),
    re.compile(r"ABORT: working tree dirty"),
    re.compile(r"ABORT: local HEAD is not an ancestor"),
)


def _line_epoch(line: str) -> float | None:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def classify_ticks(log_path: Path, since: float) -> tuple[int, int]:
    """Returns (successes, total_ticks) for resolved ticks timestamped >= since.

    A "tick" here is one resolution line (deploy OK / DEPLOY FAILED / either ABORT) -- not a
    grouped span of the surrounding lines a tick may emit (e.g. `drain:`), since only the
    resolution line itself carries a verdict.
    """
    if not log_path.exists():
        return 0, 0
    successes = 0
    total = 0
    with log_path.open(errors="replace") as fh:
        for line in fh:
            ts = _line_epoch(line)
            if ts is None or ts < since:
                continue
            if _SUCCESS_RE.search(line):
                successes += 1
                total += 1
            elif any(p.search(line) for p in _FAILURE_RES):
                total += 1
    return successes, total


def compute_and_record(
    conn,
    log_path: Path = DEPLOY_LOG,
    *,
    lane: str = LANE,
    metric: str = METRIC,
    now: float | None = None,
) -> dict:
    """AC3: append one new row, never overwrite a prior one."""
    now = now if now is not None else time.time()
    since = now - WINDOW_S
    successes, total = classify_ticks(log_path, since)
    # A window with zero ticks has no rate -- NULL, not 0.0 (see fleet_db.SCHEMA's lane_kpi
    # comment: 0.0 there would be indistinguishable from a real 0% success rate).
    value = (successes / total) if total else None
    conn.execute(
        "INSERT INTO lane_kpi (lane, metric, value, denominator, computed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (lane, metric, value, total, now),
    )
    conn.commit()
    return {"lane": lane, "metric": metric, "value": value, "denominator": total, "computed_at": now}


def read_latest(
    conn,
    *,
    lane: str = LANE,
    metric: str = METRIC,
    now: float | None = None,
    expected_interval_s: float = EXPECTED_INTERVAL_S,
) -> dict | None:
    """AC6: a devops-lane pass reading this store must be able to tell "never run" (returns
    None here) from "stale" (`stale: True` on a real row) from a genuine fresh reading --
    never silently read either as a plain 0%."""
    row = conn.execute(
        "SELECT value, denominator, computed_at FROM lane_kpi "
        "WHERE lane = ? AND metric = ? ORDER BY computed_at DESC LIMIT 1",
        (lane, metric),
    ).fetchone()
    if row is None:
        return None
    value, denominator, computed_at = row
    now = now if now is not None else time.time()
    return {
        "lane": lane,
        "metric": metric,
        "value": value,
        "denominator": denominator,
        "computed_at": computed_at,
        "stale": (now - computed_at) > 2 * expected_interval_s,
    }


def main(argv=None) -> int:
    import json

    ap = argparse.ArgumentParser(description="Independent devops-lane KPI job (gh#324).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("record", help="parse auto_deploy.log, append one lane_kpi row")
    sub.add_parser("read", help="print the latest recorded reading (or null if never run)")
    a = ap.parse_args(argv)

    conn = fleet_db.connect()
    if a.cmd == "read":
        print(json.dumps(read_latest(conn)))
        return 0
    # default: record
    result = compute_and_record(conn)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
