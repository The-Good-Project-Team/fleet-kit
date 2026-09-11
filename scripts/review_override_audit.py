#!/usr/bin/env python3
"""review_override_audit -- count how many judge-judy blocks were later merged unfixed
(gh#806 AC4).

judge-judy.sh appends one line to $LOG_DIR/judge-judy-blocks.jsonl every time it blocks a PR:
`{"pr": <n>, "head": <sha>, "blocked_at": <unix ts>}`. Before this, the only way to know
whether a block actually held was a marie pass reconstructing it by hand from `gh pr view` --
real work, done at least 3 times on gh#806 itself, that a script can now do in one pass.

An OVERRIDE is a blocked PR that later merged at the EXACT head judge-judy blocked -- no
remediation commit landed between the block and the merge, so the finding that blocked it was
never addressed, only bypassed. A PR that merged at a DIFFERENT head (a real follow-up push,
then presumably a clean re-review) is not an override; neither is a PR still open.

Usage: `python3 review_override_audit.py [--blocks-file PATH] [--repo OWNER/NAME]`
Prints a JSON summary to stdout: {"total_blocks": N, "overrides": [...], "override_count": N,
"override_rate": 0.NN}.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit"))
DEFAULT_BLOCKS_FILE = DEFAULT_LOG_DIR / "judge-judy-blocks.jsonl"


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)


def read_blocks(path: Path) -> list[dict]:
    """Every recorded block, newest last. A torn/partial line (a crash mid-write) is skipped,
    never allowed to crash the whole audit -- same rule overrides.py's live_overrides() applies
    to its own append-only jsonl store."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "pr" in row and "head" in row:
            out.append(row)
    return out


def build_pr_state_cmd(pr, repo: str | None = None) -> list[str]:
    cmd = ["gh", "pr", "view", str(pr), "--json", "state,headRefOid,mergedAt"]
    if repo:
        cmd += ["--repo", repo]
    return cmd


def check_one(block: dict, repo: str | None = None, run=_run) -> dict | None:
    """Returns an override record if this block's PR merged at the exact blocked head, else
    None (still open, closed-not-merged, or merged after a real follow-up push)."""
    rc, out = run(build_pr_state_cmd(block["pr"], repo))
    if rc != 0 or not out:
        return None
    try:
        state = json.loads(out)
    except json.JSONDecodeError:
        return None
    if state.get("state") != "MERGED":
        return None
    if state.get("headRefOid") != block["head"]:
        return None
    return {"pr": block["pr"], "head": block["head"], "blocked_at": block.get("blocked_at"),
            "merged_at": state.get("mergedAt")}


def audit(blocks: list[dict], repo: str | None = None, run=_run) -> dict:
    # Only the newest block per PR matters -- an earlier block at a head that was later
    # superseded by a real push (and possibly blocked again) must not be double-counted as a
    # second override once the PR finally merges.
    newest: dict[int, dict] = {}
    for b in blocks:
        newest[b["pr"]] = b
    overrides = []
    for block in newest.values():
        rec = check_one(block, repo, run=run)
        if rec is not None:
            overrides.append(rec)
    total = len(newest)
    return {
        "total_blocks": total,
        "overrides": overrides,
        "override_count": len(overrides),
        "override_rate": (len(overrides) / total) if total else 0.0,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--blocks-file", type=Path, default=DEFAULT_BLOCKS_FILE)
    ap.add_argument("--repo", default=None)
    a = ap.parse_args(argv)
    blocks = read_blocks(a.blocks_file)
    result = audit(blocks, repo=a.repo)
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
