#!/usr/bin/env python3
"""intent_digest.py -- one dated listing of everything the human said to the fleet, for the
daily librarian pass to distill into INTENT.md (fleet-kit#784, job 3 of philanthropy#4410).

SOURCES (all read-only, all fail-open: a missing source is a line in the header, never a crash)
  1. $FLEET_LOG_DIR/intent/*.jsonl   human turns shipped from the human's own machine by
                                     scripts/intent_capture.py (one file per host)
  2. $FLEET_LOG_DIR/fleet.db  asks   every answered ask: the question the fleet put to the
                                     human and the answer that came back (ask.py)
  3. --gh-repo OWNER/NAME (repeatable) issue/PR comments whose body starts with `Reif:` (the
                                     veto shape vp.md names), via `gh api`; skipped without --gh-repo

OUTPUT: $FLEET_LOG_DIR/intent/listing.md -- newest day first, one line per entry:
  - HH:MMZ [source/project] text (single line, <=300 chars)
Exact-duplicate texts collapse. The pass reads this and writes INTENT.md (<=40 lines: standing
decisions, corrections, reversals, each with a date and a short quote). It never writes here.

Usage: intent_digest.py [--days 14] [--max 150] [--out PATH] [--gh-repo OWNER/NAME ...] [--json]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

MAX_LINE = 300


def log_dir() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit"))


def _one_line(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t if len(t) <= MAX_LINE else t[:MAX_LINE - 1] + "…"


def from_captures(d: Path, cutoff: float) -> tuple[list[dict], str]:
    files = sorted(glob.glob(str(d / "intent" / "*.jsonl")))
    rows = []
    for f in files:
        if Path(f).name == "listing.jsonl":
            continue
        for line in Path(f).read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = float(r.get("ts") or 0)
            if ts >= cutoff and r.get("text"):
                rows.append({"ts": ts, "source": f"{r.get('host', '?')}/{r.get('project', '?')}", "text": r["text"]})
    return rows, f"{len(files)} capture file(s), {len(rows)} turn(s)"


def from_asks(d: Path, cutoff: float) -> tuple[list[dict], str]:
    db = d / "fleet.db"
    if not db.exists():
        return [], "fleet.db absent"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.execute("SELECT id, why, answer, answered_by, answered_at FROM asks "
                          "WHERE answered_at IS NOT NULL AND answered_at >= ? ORDER BY answered_at", (cutoff,))
        rows = [{"ts": float(a_at), "source": f"ask#{i} answered by {by or '?'}",
                 "text": f"Q: {q}  A: {a}"} for i, q, a, by, a_at in cur.fetchall()]
        con.close()
        return rows, f"{len(rows)} answered ask(s)"
    except sqlite3.Error as e:
        return [], f"asks unreadable ({type(e).__name__})"


def from_gh(repos: list[str], cutoff: float) -> tuple[list[dict], str]:
    rows, notes = [], []
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))
    for repo in repos:
        try:
            out = subprocess.run(["gh", "api", f"repos/{repo}/issues/comments?since={since}&per_page=100"],
                                 capture_output=True, text=True, timeout=60, check=True).stdout
            for c in json.loads(out):
                body = c.get("body") or ""
                if body.startswith("Reif:"):
                    ts = time.mktime(time.strptime(c["created_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
                    rows.append({"ts": ts, "source": f"{repo.split('/')[-1]} comment", "text": body})
            notes.append(f"{repo}: ok")
        except Exception as e:  # noqa: BLE001 -- fail-open, named in the header
            notes.append(f"{repo}: skipped ({type(e).__name__})")
    return rows, "; ".join(notes) if notes else "no --gh-repo"


def build(days: float, max_entries: int, d: Path, repos: list[str], now: float | None = None) -> tuple[str, dict]:
    now = now or time.time()
    cutoff = now - days * 86400
    caps, n1 = from_captures(d, cutoff)
    asks, n2 = from_asks(d, cutoff)
    gh, n3 = from_gh(repos, cutoff)
    rows = caps + asks + gh
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda r: -r["ts"]):
        key = _one_line(r["text"]).lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    uniq = uniq[:max_entries]
    lines = [f"# Intent listing -- last {days:.0f} days, {len(uniq)} entries "
             f"(generated {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(now))})",
             f"# sources: captures: {n1}; asks: {n2}; gh: {n3}", ""]
    day = None
    for r in uniq:
        d_ = time.strftime("%Y-%m-%d", time.gmtime(r["ts"]))
        if d_ != day:
            lines.append(f"## {d_}")
            day = d_
        lines.append(f"- {time.strftime('%H:%MZ', time.gmtime(r['ts']))} [{r['source']}] {_one_line(r['text'])}")
    return "\n".join(lines) + "\n", {"entries": len(uniq), "captures": len(caps), "asks": len(asks), "gh": len(gh)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--max", type=int, default=150)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--log-dir", type=Path, default=None)
    ap.add_argument("--gh-repo", action="append", default=[])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv[1:])
    d = a.log_dir or log_dir()
    text, stats = build(a.days, a.max, d, a.gh_repo)
    out = a.out or (d / "intent" / "listing.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    stats["path"] = str(out)
    print(json.dumps(stats) if a.json else f"intent_digest: {stats['entries']} entries -> {out} "
          f"(captures {stats['captures']}, asks {stats['asks']}, gh {stats['gh']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
