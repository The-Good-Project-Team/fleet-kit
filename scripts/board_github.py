#!/usr/bin/env python3
"""board_github — the fleet's work queue as GitHub Issues.

Provenance: genericized from nonprofit-atlas's `scripts/fleet/board_github.py` (2026-08-12).
That product previously ran its board on a database on its own production box — meaning the
fleet's brain rode on the exact service it existed to maintain (prod down = fleet blind
precisely when it was needed). Issues invert that: fleet state survives any box, `gh` is
already authenticated everywhere the fleet runs, and any repo gets a working board for free.

CONTRACT:
  file  -> `gh issue create` with labels <prefix>backlog [+ <prefix>lane:<lane>]; evidence
           lives in the body (labels enumerate, bodies explain).
  claim -> add label <prefix>claimed + a "claimed-by: <worker>" comment. NOT atomic — GitHub
           has no test-and-set; single-claimer discipline comes from the caller running ONE
           sequential claim loop (see `worktree_builder.sh`'s claim step), not from this file.
           list_unclaimed() re-reads live state before every claim.
  done  -> `gh issue close --comment`.
  list  -> open issues labeled <prefix>backlog, unclaimed first.

Label prefix is `FLEET_LABEL_PREFIX` (env, default "fleet:") so multiple fleet-kit instances
or an existing labeling scheme don't collide.

All GitHub access goes through the `gh` CLI (already authenticated; no token handling here).
Pure command-BUILDERS are separated from execution so a test can assert the exact argv
without a network call.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

PREFIX = os.environ.get("FLEET_LABEL_PREFIX", "fleet:")
LABEL_BACKLOG = f"{PREFIX}backlog"
LABEL_CLAIMED = f"{PREFIX}claimed"


# --- pure command builders (unit-tested; never executed by tests) -----------------------------

def build_file_cmd(title: str, body: str, lane: str = "") -> list[str]:
    labels = LABEL_BACKLOG + (f",{PREFIX}lane:{lane}" if lane else "")
    return ["gh", "issue", "create", "--title", title, "--body", body, "--label", labels]


def build_list_cmd() -> list[str]:
    return [
        "gh", "issue", "list", "--state", "open", "--label", LABEL_BACKLOG,
        "--limit", "200", "--json", "number,title,body,labels",
    ]


def build_claim_cmds(number: int, worker: str) -> list[list[str]]:
    return [
        ["gh", "issue", "edit", str(number), "--add-label", LABEL_CLAIMED],
        ["gh", "issue", "comment", str(number), "--body", f"claimed-by: {worker}"],
    ]


def build_done_cmd(number: int, note: str) -> list[str]:
    return ["gh", "issue", "close", str(number), "--comment", note or "done"]


def is_claimed(issue: dict) -> bool:
    return any(lb.get("name") == LABEL_CLAIMED for lb in issue.get("labels") or [])


def to_board_item(issue: dict) -> dict:
    """The {'id','text','context'} shape every caller consumes."""
    return {
        "id": issue["number"],
        "text": issue.get("title") or "",
        "context": issue.get("body") or "",
    }


# --- execution ---------------------------------------------------------------------------------

def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)


def ensure_labels() -> None:
    """Idempotent: create the two fleet labels if absent (gh errors on duplicates; a failed
    create against an existing label is fine and ignored)."""
    for name, color, desc in (
        (LABEL_BACKLOG, "3e694a", "fleet work queue item"),
        (LABEL_CLAIMED, "d4a72c", "claimed by a fleet worker"),
    ):
        _run(["gh", "label", "create", name, "--color", color, "--description", desc])


def file_item(title: str, body: str, lane: str = "") -> int:
    """Returns the new issue NUMBER (>0) on success, 0 on failure."""
    ensure_labels()
    rc, out = _run(build_file_cmd(title, body, lane))
    if rc != 0:
        print(f"board_github: file FAILED: {out[:300]}", file=sys.stderr)
        return 0
    print(out)  # the issue URL — callers log it as the durable id
    m = re.search(r"/issues/(\d+)\s*$", out)
    return int(m.group(1)) if m else 0


def list_unclaimed() -> list[dict]:
    rc, out = _run(build_list_cmd())
    if rc != 0:
        print(f"board_github: list FAILED: {out[:300]}", file=sys.stderr)
        return []
    try:
        issues = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [to_board_item(i) for i in issues if not is_claimed(i)]


def claim_next_n(worker: str, n: int) -> list[dict]:
    """Sequential claim of up to n unclaimed items — stops the moment the queue runs dry
    rather than padding the result (a short queue is the real state, not an error)."""
    claimed: list[dict] = []
    for item in list_unclaimed():
        if len(claimed) >= max(0, n):
            break
        ok = True
        for cmd in build_claim_cmds(item["id"], worker):
            rc, out = _run(cmd)
            if rc != 0:
                print(f"board_github: claim #{item['id']} step FAILED: {out[:200]}", file=sys.stderr)
                ok = False
                break
        if ok:
            claimed.append(item)
    return claimed


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: board_github.py file <title> [--context <body>] [--lane <lane>] | "
              "claim <worker> <n> | list | done <number> [note]", file=sys.stderr)
        return 2
    cmd = args[0]
    if cmd == "file":
        if len(args) < 2:
            print("file needs a title", file=sys.stderr)
            return 2
        title = args[1]
        body, lane = "", ""
        rest = args[2:]
        i = 0
        while i < len(rest):
            if rest[i] == "--context" and i + 1 < len(rest):
                body = rest[i + 1]; i += 2
            elif rest[i] == "--lane" and i + 1 < len(rest):
                lane = rest[i + 1]; i += 2
            else:
                i += 1
        return 0 if file_item(title, body, lane) else 1
    if cmd == "claim":
        if len(args) != 3:
            print("claim needs <worker> <n>", file=sys.stderr)
            return 2
        print(json.dumps(claim_next_n(args[1], int(args[2]))))
        return 0
    if cmd == "list":
        print(json.dumps(list_unclaimed()))
        return 0
    if cmd == "done":
        if len(args) < 2:
            print("done needs <number>", file=sys.stderr)
            return 2
        rc, out = _run(build_done_cmd(int(args[1]), " ".join(args[2:])))
        if rc != 0:
            print(f"board_github: done FAILED: {out[:200]}", file=sys.stderr)
        return rc
    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
