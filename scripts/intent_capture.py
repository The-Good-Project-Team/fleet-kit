#!/usr/bin/env python3
"""intent_capture.py -- ship what the human actually said to the fleet (fleet-kit#784, job 3
of philanthropy#4410).

WHERE INTENT LIVES. Not on the fleet box: 0 `Reif:` comments in 14 days on either repo, 2 asks
ever answered. It lives in the Claude Code session transcripts on Reif's own machine
(`~/.claude*/projects/**/*.jsonl`, 301 files touched in the last 7 days on 2026-09-09), where
every `type: user` turn with a string body is a thing he typed: a decision, a correction, a
"no, not that". No member could read any of it. This script runs THERE (cron, every 30 min),
keeps only the human turns, redacts credential shapes, and scp's one small jsonl per host into
each instance's logs/intent/ -- the daily `librarian` pass distills it into INTENT.md.

STANDALONE ON PURPOSE. The machine this runs on has no fleet-kit checkout, so the redaction
patterns are a copy of members/librarian/librarian.py's (selftest asserts the two redact a
fixture identically). Runs on the stdlib alone.

WHAT COUNTS AS A HUMAN TURN: `type == "user"`, `origin.kind == "human"` (the harness's own
mark for a typed/queued prompt -- SDK and `claude -p` sessions other programs start carry
none), not `isMeta`, not `isSidechain` (a sidechain's "user" is the parent agent), body is a
string or a list of `text` blocks (never a `tool_result`), and the text does not start with
`<` (the harness wrapping a slash command, a caveat, or a system reminder -- not the person).

Usage:
  intent_capture.py [--roots GLOB ...] [--days 3] [--out FILE] [--state FILE]
                    [--ship HOST:DIR ...] [--keep-days 30] [--dry-run]
Defaults: roots ~/.claude*/projects, out ~/.cache/fleet-kit/intent/<hostname>.jsonl. Each
--ship does `ssh HOST mkdir -p DIR && scp out HOST:DIR/<hostname>.jsonl`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

MAX_TEXT = 2000
_B = r"(?:(?<!\w)|(?<=\\[nrt]))"
PATTERNS = [
    (re.compile(rf"{_B}({p})_[A-Za-z0-9]{{8,255}}\b"), lambda m: f"[REDACTED:{m.group(1)}]")
    for p in ("gho", "ghp", "ghs", "ghu", "ghr")
] + [
    (re.compile(rf"{_B}sk-ant-[A-Za-z0-9\-_]{{20,}}\b"), lambda m: "[REDACTED:sk-ant]"),
    (re.compile(rf"{_B}PGPASSWORD=\S+"), lambda m: "[REDACTED:pgpassword]"),
    (re.compile(rf"{_B}postgres(?:ql)?://[^:\s]+:[^@\s]+@\S+"), lambda m: "[REDACTED:postgres_url]"),
    (re.compile(rf"{_B}([A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY)[A-Z0-9_]*)=(?!\[REDACTED:)(\S+)"),
     lambda m: f"{m.group(1)}=[REDACTED:secret_env]"),
]


def redact(text: str) -> str:
    for rx, sub in PATTERNS:
        text = rx.sub(sub, text)
    return text


def human_text(row: dict) -> str | None:
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
        return None
    # Only what a person typed. `origin.kind == "human"` is the harness's own mark for a typed,
    # queued or accepted-suggestion prompt; SDK/`claude -p` sessions started by other programs
    # (profiled 2026-09-09: 236 of 326 "user" turns in one day were an app's appraisal prompt)
    # carry no origin, and a sidechain's "user" is the parent agent, not the person.
    if not isinstance(row.get("origin"), dict) or row["origin"].get("kind") != "human":
        return None
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return None
    parts = [p.strip() for p in parts if isinstance(p, str) and p.strip() and not p.lstrip().startswith("<")]
    if not parts:
        return None
    text = "\n".join(parts)
    return text[:MAX_TEXT] if len(text) > 3 else None


def _epoch(ts) -> float | None:
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def project_of(row: dict, path: Path) -> str:
    cwd = row.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd).name
    # ~/.claude*/projects/-Users-reify-Classified-philanthropy/<session>.jsonl
    return path.parent.name.split("-")[-1] or path.parent.name


def scan(roots: list[str], days: float, since_uuids: set[str], host: str) -> list[dict]:
    cutoff = time.time() - days * 86400
    rows = []
    for root in roots:
        for f in glob.glob(os.path.join(os.path.expanduser(root), "**", "*.jsonl"), recursive=True):
            p = Path(f)
            if "/memory/" in f or p.stat().st_mtime < cutoff:
                continue
            try:
                lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                uid = row.get("uuid")
                if not uid or uid in since_uuids:
                    continue
                text = human_text(row)
                if text is None:
                    continue
                ts = _epoch(row.get("timestamp"))
                if ts is None or ts < cutoff:
                    continue
                rows.append({"uuid": uid, "ts": ts, "host": host, "project": project_of(row, p),
                             "session": p.stem[:8], "text": redact(text)})
                since_uuids.add(uid)
    rows.sort(key=lambda r: r["ts"])
    return rows


def load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roots", nargs="*", default=None)
    ap.add_argument("--days", type=float, default=3.0)
    ap.add_argument("--keep-days", type=float, default=30.0)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--ship", action="append", default=[], help="HOST:DIR, repeatable")
    ap.add_argument("--host", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv[1:])

    host = a.host or socket.gethostname().split(".")[0]
    roots = a.roots or sorted(glob.glob(os.path.expanduser("~/.claude*/projects")))
    out = a.out or Path(os.path.expanduser("~/.cache/fleet-kit/intent")) / f"{host}.jsonl"
    existing = load_jsonl(out)
    keep_cutoff = time.time() - a.keep_days * 86400
    existing = [r for r in existing if float(r.get("ts") or 0) >= keep_cutoff]
    seen = {r.get("uuid") for r in existing}
    new = scan(roots, a.days, seen, host)
    if a.dry_run:
        for r in new:
            print(f"{time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(r['ts']))} [{r['project']}] {r['text'][:120]!r}")
        print(f"intent_capture: {len(new)} new human turn(s), {len(existing)} kept (dry-run)")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = existing + new
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    print(f"intent_capture: {len(new)} new, {len(rows)} total -> {out}")
    rc = 0
    for target in a.ship:
        h, _, d = target.partition(":")
        if not (h and d):
            print(f"intent_capture: bad --ship {target!r} (want HOST:DIR)", file=sys.stderr)
            rc = 2
            continue
        try:
            subprocess.run(["ssh", "-o", "BatchMode=yes", h, f"mkdir -p {d}"], check=True, timeout=60)
            subprocess.run(["scp", "-q", "-o", "BatchMode=yes", str(out), f"{h}:{d}/{host}.jsonl"], check=True, timeout=120)
            print(f"intent_capture: shipped to {target}/{host}.jsonl")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"intent_capture: ship to {target} failed: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
