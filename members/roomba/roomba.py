#!/usr/bin/env python3
"""roomba.py — safe worktree sweep. Removes stale/orphaned git worktrees.

Provenance: genericized + consolidated from nonprofit-atlas's scripts/worktree_sweep.py (886
lines, the worktree half) and scripts/lucky2/fleet_ghosts_core.py (the crew-liveness half).
Both real, incident-derived. Kept the load-bearing safety properties, dropped the
nonprofit-atlas-specific plumbing (dash_event/backlog API calls -> generic stdout report).

WORKTREE SAFETY (all must hold before a worktree is a removal candidate):
  1. branch merged into the default branch (ancestor OR patch-equivalent squash-merge), OR
     provably abandoned (pushed+synced, no open PR, commit older than --stale-days)
  2. tree is clean (no uncommitted/untracked changes)
  3. branch not in the protect list
  4. worktree at least --min-age-hours old (default 24h) -- never sweep an in-flight pass
  5. no live PID associated with it

SEPARATE fast path: a "dangling builder" worktree (path embeds its own spawning PID, e.g.
/tmp/gru-<item>-<pid>) is a candidate regardless of merge/dirty state once that PID is
confirmed dead -- a SIGKILL never runs a script's own cleanup trap, so a killed build's
worktree is orphaned forever otherwise. A short grace period still applies (defense in depth
against reading a PID a moment before it starts).

The crew-liveness half (dead/stale/crashlooping scheduled jobs) that used to live here moved
out to scripts/member_liveness_check.sh (fleet-kit#512/#514) -- a check running INSIDE this
container can't notice its own cron dying, which is exactly the outage that motivated the move.
See gh#204 for the history of this file's now-removed ghost-dedup state file.

Dry-run by default. Pass --execute to actually remove anything.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MIN_AGE_HOURS = float(os.environ.get("ROOMBA_MIN_AGE_HOURS", "24"))
DANGLING_MIN_AGE_HOURS = 0.25
STALE_COMMIT_DAYS = float(os.environ.get("ROOMBA_STALE_DAYS", "14"))
MAX_PID = 4_194_304  # real PIDs never exceed this on macOS or Linux

# Path convention a mechanical builder can opt into so roomba recognizes its own dangling
# worktrees: <anything>-<pid> or <anything>-<pid>-<idx>, embedding the spawning process's PID.
_BUILDER_PID_RE = re.compile(r"-(\d+)(?:-\d+)?/?$")


def _run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)


@dataclass
class WorktreeInfo:
    path: str
    branch: str = ""
    is_bare: bool = False


def list_worktrees(repo_dir: str) -> list[WorktreeInfo]:
    out = _run(["git", "worktree", "list", "--porcelain"], cwd=repo_dir).stdout
    trees, cur = [], {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur:
                trees.append(WorktreeInfo(**cur))
            cur = {"path": line.split(" ", 1)[1], "branch": "", "is_bare": False}
        elif line.startswith("branch "):
            cur["branch"] = line.split(" ", 1)[1].removeprefix("refs/heads/")
        elif line == "bare":
            cur["is_bare"] = True
    if cur:
        trees.append(WorktreeInfo(**cur))
    return trees


def is_tree_clean(path: str) -> bool:
    return _run(["git", "status", "--porcelain"], cwd=path).stdout.strip() == ""


def is_branch_reclaimable(repo_dir: str, branch: str, base: str) -> bool:
    if not branch:
        return False
    anc = _run(["git", "merge-base", "--is-ancestor", branch, base], cwd=repo_dir)
    if anc.returncode == 0:
        return True
    # squash-merge produces a new SHA with the identical diff -- ancestor check alone misses it
    patch = _run(["git", "cherry", base, branch], cwd=repo_dir).stdout
    return patch.strip() != "" and all(line.startswith("-") for line in patch.splitlines())


def is_branch_abandoned(repo_dir: str, branch: str, stale_days: float) -> bool:
    if not branch:
        return False
    synced = _run(["git", "diff", "--quiet", branch, f"origin/{branch}"], cwd=repo_dir).returncode == 0
    if not synced:
        return False
    has_pr = _run(["gh", "pr", "list", "--head", branch, "--json", "number"], cwd=repo_dir)
    if has_pr.returncode != 0:
        return False  # undeterminable -> never treat as abandoned
    if json.loads(has_pr.stdout or "[]"):
        return False
    age = _run(["git", "log", "-1", "--format=%ct", branch], cwd=repo_dir).stdout.strip()
    if not age.isdigit():
        return False
    age_days = (time.time() - int(age)) / 86400
    return age_days >= stale_days


def parse_dangling_pid(wt_path: str) -> int | None:
    m = _BUILDER_PID_RE.search(wt_path.rstrip("/"))
    if not m:
        return None
    pid = int(m.group(1))
    return pid if 0 < pid <= MAX_PID else None


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def worktree_age_hours(path: str) -> float | None:
    try:
        return (time.time() - Path(path).stat().st_ctime) / 3600
    except OSError:
        return None


def sweep(repo_dir: str, protect: set[str], base: str, execute: bool) -> list[dict]:
    trees = list_worktrees(repo_dir)
    if not trees:
        return []
    main_path = trees[0].path
    results = []
    for wt in trees[1:]:
        if wt.path == main_path or wt.is_bare:
            continue
        if not Path(wt.path).exists():
            results.append({"path": wt.path, "action": "kept", "reason": "orphaned entry, not reclaiming (needs a real prune pass)"})
            continue
        if wt.branch in protect:
            results.append({"path": wt.path, "action": "kept", "reason": "protected branch"})
            continue

        age = worktree_age_hours(wt.path)
        dangling_pid = parse_dangling_pid(wt.path)
        if dangling_pid is not None and age is not None and age >= DANGLING_MIN_AGE_HOURS:
            if not is_pid_alive(dangling_pid):
                reason = f"dangling builder worktree, pid {dangling_pid} confirmed dead"
                results.append({"path": wt.path, "action": "removed" if execute else "would-remove", "reason": reason})
                if execute:
                    _run(["git", "worktree", "remove", "--force", wt.path], cwd=repo_dir)
                continue

        if age is None or age < MIN_AGE_HOURS:
            results.append({"path": wt.path, "action": "kept", "reason": f"too young (age={age})"})
            continue
        if not is_tree_clean(wt.path):
            results.append({"path": wt.path, "action": "kept", "reason": "dirty tree"})
            continue
        reclaimable = is_branch_reclaimable(repo_dir, wt.branch, base) or is_branch_abandoned(repo_dir, wt.branch, STALE_COMMIT_DAYS)
        if not reclaimable:
            results.append({"path": wt.path, "action": "kept", "reason": "branch not merged or abandoned"})
            continue
        reason = "merged or abandoned, clean, old enough"
        results.append({"path": wt.path, "action": "removed" if execute else "would-remove", "reason": reason})
        if execute:
            _run(["git", "worktree", "remove", "--force", wt.path], cwd=repo_dir)
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("FLEET_REPO", "."))
    ap.add_argument("--base", default=os.environ.get("ROOMBA_BASE_BRANCH", "origin/main"))
    ap.add_argument("--protect", action="append", default=[])
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    results = sweep(args.repo, set(args.protect), args.base, args.execute)
    for r in results:
        print(f"{r['action']:>12}  {r['path']}  ({r['reason']})")
    acted = sum(1 for r in results if r["action"] in ("removed", "would-remove"))
    print(f"\n{len(results)} worktree(s) evaluated, {acted} {'removed' if args.execute else 'would be removed (dry-run)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
