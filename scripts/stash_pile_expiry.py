#!/usr/bin/env python3
"""stash_pile_expiry -- bounds the $REPO auto-stash pile postflight_dirty_check.sh creates.

gh#714 (Part C4 of marie's PRD comment, 2026-09-08T10:42:18Z): postflight_dirty_check.sh
already rescues a leaked write into the shared checkout by auto-stashing it, and that rescue
works -- but its own POLICY log line says the pile is "never auto-expired; a human needs to
run `git -C $REPO stash list` and decide what to land or drop". Nothing in this repo ever did
that. The pile grew from 5 (its own warn threshold) to 40 to 134 with no mechanism draining it.

This is deliberately NOT a second preflight guard (worktree_guard_hook.py, gh#605/#708/#837,
already does that job) and does NOT decide that a leaked diff should become a real commit --
that stays a human call (gh#714 PRD non-goals). All this does is remove entries that are
PROVABLY redundant (the change they captured has since landed on $REPO's HEAD by some other
route, so the stash is dead weight) and make the remaining pile's size/age visible on demand,
so "the expiry is working but the pile is still too big" is a distinguishable, greppable state
from "nothing is running at all".

Usage:
    stash_pile_expiry.py run      # drop redundant entries, log what was dropped
    stash_pile_expiry.py report   # print depth / oldest age / last run's drop count
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fleet_db  # noqa: E402

LOG_DIR_DEFAULT = fleet_db.LOG_DIR
STATE_FILE_NAME = ".stash_pile_expiry_state.json"
ALERT_LOG_NAME = "repo_dirty_alerts.log"  # same cross-member file postflight_dirty_check.sh
                                            # already writes ALERT/REMEDIATED/POLICY lines to.

# Must match postflight_dirty_check.sh's `stash_msg` exactly -- this is how gh#714 AC3 tells a
# rescue entry THIS mechanism created apart from any other pass's manual `git stash` on the
# same shared stack (that stack is shared repo-wide; a bare `git stash` from any concurrent
# session lands on it too, and this script must never touch those).
MANAGED_MARKER = "postflight-dirty-check auto-stash run="

# gh#714 PRD, "Out of scope / open questions": UNKNOWN what the ceiling should be -- picking
# one before AC6's rate re-measurement is guesswork, so this reuses
# postflight_dirty_check.sh's own STASH_PILE_WARN_THRESHOLD (5) as the stated provisional
# value, same as the PRD instructs if a builder must ship a placeholder.
DEFAULT_CEILING = int(os.environ.get("STASH_PILE_WARN_THRESHOLD", 5))


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def list_entries(repo):
    """[(index, message, commit_iso_ts)] in `git stash list` order (index 0 = newest).

    Uses the reflog directly (not `git stash list`) so each entry carries its own commit
    timestamp for free, without a second `git show` per entry.
    """
    out = _git(repo, "log", "-g", "--format=%gd|%cI|%gs", "refs/stash")
    if out.returncode != 0:
        return []
    entries = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        gd, ts, msg = line.split("|", 2)
        m = re.match(r"stash@\{(\d+)\}", gd)
        if not m:
            continue
        entries.append((int(m.group(1)), msg, ts))
    return entries


def is_managed(message):
    return MANAGED_MARKER in message


def diff_is_empty(repo, idx):
    """True when stash@{idx}'s snapshot is already fully reflected in $REPO's current HEAD --
    i.e. whatever this entry leaked has since landed by another route, so the stash carries no
    change a human could still land (gh#714 AC1/AC2)."""
    out = _git(repo, "diff", "--quiet", "HEAD", f"stash@{{{idx}}}")
    return out.returncode == 0


def run_expiry(repo, log_dir, ceiling):
    entries = list_entries(repo)
    # Process strictly from the highest index (oldest entry) down to 0 (newest). Dropping
    # stash@{k} only ever renumbers entries with index > k (the ones further down the stack,
    # i.e. older) -- and in this order those are always entries already handled. Anything with
    # index < k (newer, not yet visited) never shifts. So indices captured by the single
    # list_entries() call above stay valid for every drop below, with no re-listing needed.
    managed_checked = 0
    dropped = []
    for idx, message, _ts in sorted(entries, key=lambda e: -e[0]):
        if not is_managed(message):
            continue  # AC3: a foreign stash entry is never even diff-checked, let alone dropped
        managed_checked += 1
        if diff_is_empty(repo, idx):
            drop_out = _git(repo, "stash", "drop", f"stash@{{{idx}}}")
            if drop_out.returncode == 0:
                dropped.append((idx, message))

    alert_log = Path(log_dir) / ALERT_LOG_NAME
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    with open(alert_log, "a") as f:
        for idx, message in dropped:
            line = (f"[{stamp}] EXPIRED stash@{{{idx}}} "
                     f"(diff empty against current HEAD, already landed another way): {message}")
            print(line)
            f.write(line + "\n")

    depth_after = len(list_entries(repo))
    state = {
        "last_run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checked": managed_checked,
        "dropped": len(dropped),
        "depth_after": depth_after,
    }
    _state_path(log_dir).write_text(json.dumps(state))

    if depth_after > ceiling:
        # Distinct, greppable from ordinary log noise on purpose (gh#714 AC5): this line means
        # "expiry ran and the pile is still too deep" -- a different, worse state than expiry
        # simply never having run, which a human/monitor must be able to tell apart at a glance.
        warn = (f"STASH_PILE_CEILING_EXCEEDED depth={depth_after} ceiling={ceiling} "
                f"dropped_this_run={len(dropped)} checked_this_run={managed_checked}")
        print(warn)
        with open(alert_log, "a") as f:
            f.write(f"[{stamp}] {warn}\n")

    return state


def _state_path(log_dir):
    return Path(log_dir) / STATE_FILE_NAME


def _read_state(log_dir):
    p = _state_path(log_dir)
    if not p.exists():
        return {"last_run_at": None, "checked": 0, "dropped": 0, "depth_after": None}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"last_run_at": None, "checked": 0, "dropped": 0, "depth_after": None}


def report(repo, log_dir):
    """Single parseable line: current depth, oldest entry's age, and what the last `run` did
    (gh#714 AC4) -- an operator or a later pass gets the pile's state from one command instead
    of hand-eyeballing `git -C $REPO stash list`."""
    entries = list_entries(repo)
    depth = len(entries)
    if entries:
        oldest_ts = min(e[2] for e in entries)
        oldest_age_hours = (time.time() - datetime.datetime.fromisoformat(oldest_ts).timestamp()) / 3600.0
    else:
        oldest_age_hours = 0.0
    state = _read_state(log_dir)
    fields = {
        "depth": depth,
        "oldest_age_hours": round(oldest_age_hours, 2),
        "last_run_dropped": state.get("dropped", 0),
        "last_run_checked": state.get("checked", 0),
        "last_run_at": state.get("last_run_at") or "never",
    }
    print(" ".join(f"{k}={v}" for k, v in fields.items()))
    return fields


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["run", "report"])
    p.add_argument("--repo", default=os.environ.get("FLEET_REPO"),
                    help="the shared checkout to inspect (FLEET_REPO)")
    p.add_argument("--log-dir", default=str(LOG_DIR_DEFAULT))
    p.add_argument("--ceiling", type=int, default=DEFAULT_CEILING)
    args = p.parse_args(argv)

    if not args.repo:
        print("FLEET_REPO not set and --repo not given -- nothing to check", file=sys.stderr)
        return 1
    os.makedirs(args.log_dir, exist_ok=True)

    if args.command == "run":
        run_expiry(args.repo, args.log_dir, args.ceiling)
    else:
        report(args.repo, args.log_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
