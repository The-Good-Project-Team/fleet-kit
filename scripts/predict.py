#!/usr/bin/env python3
"""predict.py -- the predictions ledger: a change, a metric, a target, a deadline; code decides.

WHY (fleet-kit#782, part of #586). The point of this kit is a fleet that gets measurably
better at producing work (Reif: "the point is autonomous rsi"). That is only checkable if every
self-change comes with a falsifiable claim and something other than the author resolves it.
dumbledore's `Prediction:` line was that claim in prose; this file makes it a row that
fleet_metrics.py can score. The grader (self_improve_score.sh) reads this ledger instead of
guessing attribution from day counts.

LEDGER: $FLEET_LOG_DIR/predictions.jsonl, one JSON object per line, rewritten in place on
resolve (small file, tens of rows). Fields:
  id, ts, member, run_id, change, metric, baseline, target, by_hours, due_ts, note,
  status: open | hit | miss | unavailable, actual, resolved_ts, moved (bool)

COMMANDS
  add     --member M --change "fleet-kit#NNN" --metric signal_rate:gru --target 0.6
          [--baseline 0.48] [--by-hours 72] [--note "..."]
          baseline defaults to the metric's value now (24h window). Prints the row.
  resolve [--now EPOCH]   every open row past due: actual = metric over the 24h ending at
          due_ts; hit when actual reached the target in the baseline->target direction,
          miss otherwise, unavailable when the metric has no data. Prints what changed.
  ledger  [--days 14] [--now EPOCH] [--text]   JSON summary (or a short table with --text):
          rows, hit/miss/open/unavailable counts, hit_rate, and per row the num_turns and
          cost_usd of the pass that made it (leg 3 of the grader's rubric: is the next fix
          cheaper), so a trend in cost-per-verified-hit is visible.
  last    [--member M]   the newest row, one line (for a Last-verdict: line).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_metrics  # noqa: E402

RESOLVE_WINDOW_H = 24.0
DEFAULT_BY_HOURS = 72.0


def ledger_path() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit")) / "predictions.jsonl"


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def save(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    os.replace(tmp, path)


def new_id(rows: list[dict]) -> int:
    return (max((int(r.get("id") or 0) for r in rows), default=0) + 1)


def make(rows: list[dict], *, member: str, change: str, metric: str, target: float,
         baseline: float | None, by_hours: float, note: str, now: float,
         runs: list[dict] | None, run_id: str | None) -> dict:
    fleet_metrics.parse(metric)  # raises on an unknown metric
    if baseline is None:
        baseline = fleet_metrics.compute(metric, runs or [], now, RESOLVE_WINDOW_H)
    return {
        "id": new_id(rows), "ts": now, "member": member, "run_id": run_id,
        "change": change, "metric": metric,
        "baseline": baseline, "target": float(target), "by_hours": float(by_hours),
        "due_ts": now + float(by_hours) * 3600.0, "note": note,
        "status": "open", "actual": None, "resolved_ts": None, "moved": None,
    }


def judge(baseline: float | None, target: float, actual: float | None) -> tuple[str, bool | None]:
    if actual is None:
        return "unavailable", None
    if baseline is None:
        # No starting point: the only checkable claim is "reaches target".
        return ("hit" if actual >= target else "miss"), None
    if target > baseline:
        return ("hit" if actual >= target else "miss"), actual > baseline
    if target < baseline:
        return ("hit" if actual <= target else "miss"), actual < baseline
    return ("hit" if abs(actual - target) <= 1e-9 else "miss"), actual == baseline


def resolve(rows: list[dict], runs: list[dict], now: float) -> list[dict]:
    changed = []
    for r in rows:
        if r.get("status") != "open" or float(r.get("due_ts") or 0) > now:
            continue
        actual = fleet_metrics.compute(r["metric"], runs, float(r["due_ts"]), RESOLVE_WINDOW_H)
        status, moved = judge(r.get("baseline"), float(r["target"]), actual)
        r.update({"status": status, "actual": actual, "resolved_ts": now, "moved": moved})
        changed.append(r)
    return changed


def _authoring_pass(r: dict, runs: list[dict]) -> dict:
    """The run record of the pass that made this prediction: by run_id, else the member's
    newest row at or before the prediction's ts."""
    if r.get("run_id"):
        for run in runs:
            if run.get("run_id") == r["run_id"]:
                return run
    cands = [run for run in runs if run.get("member") == r.get("member")
             and float(run.get("ts") or 0) <= float(r.get("ts") or 0) + 3600]
    return max(cands, key=lambda run: float(run.get("ts") or 0)) if cands else {}


def summarize(rows: list[dict], runs: list[dict], now: float, days: float) -> dict:
    lo = now - days * 86400.0
    recent = [r for r in rows if float(r.get("ts") or 0) >= lo]
    out_rows = []
    for r in recent:
        p = _authoring_pass(r, runs)
        tok = p.get("tokens") or {}
        err = None
        if r.get("actual") is not None and r.get("baseline") is not None and r["target"] != r["baseline"]:
            err = abs(float(r["actual"]) - float(r["target"])) / abs(float(r["target"]) - float(r["baseline"]))
        out_rows.append({**r, "pass_num_turns": tok.get("num_turns"), "pass_cost_usd": tok.get("cost_usd"),
                         "error_ratio": (round(err, 3) if err is not None else None)})
    counts = {s: sum(1 for r in recent if r.get("status") == s) for s in ("open", "hit", "miss", "unavailable")}
    resolved = counts["hit"] + counts["miss"]
    hits = [r for r in out_rows if r["status"] == "hit"]
    return {
        "days": days, "now": now, "rows": out_rows, **counts,
        "hit_rate": (counts["hit"] / resolved) if resolved else None,
        "cost_per_hit_usd": (round(sum((h["pass_cost_usd"] or 0) for h in hits) / len(hits), 4) if hits else None),
        "by_hours_trend": [r["by_hours"] for r in out_rows],
        "error_trend": [r["error_ratio"] for r in out_rows if r["error_ratio"] is not None],
    }


def as_text(s: dict) -> str:
    lines = [f"predictions last {s['days']:.0f}d: {s['hit']} hit, {s['miss']} miss, {s['open']} open, "
             f"{s['unavailable']} unavailable; hit_rate={s['hit_rate']}; cost_per_hit_usd={s['cost_per_hit_usd']}"]
    for r in s["rows"]:
        due = time.strftime("%m-%d %H:%MZ", time.gmtime(float(r["due_ts"])))
        lines.append(f"  #{r['id']} {r['status']:<11} {r['member']} {r['change']} {r['metric']} "
                     f"{r['baseline']} -> {r['target']} by {due} actual={r['actual']} "
                     f"pass={r['pass_num_turns']}t/${r['pass_cost_usd']}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", type=Path, default=None)
    ap.add_argument("--runs", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--member", required=True)
    a.add_argument("--change", required=True, help='the PR/issue, e.g. "fleet-kit#760"')
    a.add_argument("--metric", required=True)
    a.add_argument("--target", type=float, required=True)
    a.add_argument("--baseline", type=float, default=None)
    a.add_argument("--by-hours", type=float, default=DEFAULT_BY_HOURS)
    a.add_argument("--note", default="")
    a.add_argument("--now", type=float, default=None)
    r = sub.add_parser("resolve")
    r.add_argument("--now", type=float, default=None)
    l = sub.add_parser("ledger")
    l.add_argument("--days", type=float, default=14.0)
    l.add_argument("--now", type=float, default=None)
    l.add_argument("--text", action="store_true")
    la = sub.add_parser("last")
    la.add_argument("--member", default=None)
    args = ap.parse_args(argv[1:])

    path = args.ledger or ledger_path()
    rows = load(path)
    runs = fleet_metrics.load_runs(args.runs)
    now = float(getattr(args, "now", None) or time.time())

    if args.cmd == "add":
        try:
            row = make(rows, member=args.member, change=args.change, metric=args.metric,
                       target=args.target, baseline=args.baseline, by_hours=args.by_hours,
                       note=args.note, now=now, runs=runs, run_id=os.environ.get("FLEET_RUN_ID"))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        rows.append(row)
        save(path, rows)
        print(json.dumps(row, sort_keys=True))
        return 0
    if args.cmd == "resolve":
        changed = resolve(rows, runs, now)
        if changed:
            save(path, rows)
        for r in changed:
            print(f"#{r['id']} {r['status']} {r['metric']} {r['baseline']} -> {r['target']} actual={r['actual']}")
        if not changed:
            print("nothing due")
        return 0
    if args.cmd == "ledger":
        s = summarize(rows, runs, now, args.days)
        print(as_text(s) if args.text else json.dumps(s, sort_keys=True))
        return 0
    if args.cmd == "last":
        cands = [r for r in rows if not args.member or r.get("member") == args.member]
        if not cands:
            print("none")
            return 0
        r = max(cands, key=lambda x: float(x.get("ts") or 0))
        print(f"#{r['id']} {r['status']} {r['change']} {r['metric']} {r['baseline']} -> {r['target']} "
              f"actual={r['actual']} due={time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(float(r['due_ts'])))}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
