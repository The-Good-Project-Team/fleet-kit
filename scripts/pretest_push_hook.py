#!/usr/bin/env python3
"""pretest_push_hook.py -- the tests run BEFORE the push, not after it in CI.

MEASURED, 95 CI runs on philanthropy (2026-09-11): the `test` job produced 50 green, 22 RED
and 23 CANCELLED. Nearly half of every run of the most expensive gate in the repo -- 1,202 of
the suite's 2,152 minutes -- was thrown away, because a member pushes, lets CI discover the
break, pushes a fix, and the new push cancels the run still grinding on the old one. The gate
was doing discovery work the member could have done locally for free.

Cutting gates does not touch that. `lighthouse-budget` burned 471 minutes and went red ZERO
times in those 95 runs, which is a real cut and still smaller than the waste above.

So this is the mechanical layer under the prose, the same shape as worktree_guard_hook.py: a
PreToolUse hook that BLOCKS a `git push` out of a worktree whose current content has no
passing test receipt. `scripts/verified_test.sh` writes that receipt; nothing else does.

WHAT THE RECEIPT IS KEYED ON. Not HEAD -- a member typically runs the suite, then commits,
and committing changes HEAD while changing nothing about what was tested. It is keyed on the
CONTENT: `git stash create` builds a commit object from the current worktree without touching
it, and its tree is the hash of exactly what is there now. Identical before and after a commit
of the same content, different the moment a file changes. So "I tested this, then committed
it" passes, and "I tested this, then edited one more line" does not.

KNOWN GAP, deliberate: `git stash create` does not see UNTRACKED files. A brand-new file that
was never `git add`ed is invisible to the hash, so a receipt taken before it existed still
matches. The failure direction is the safe one once the member commits (HEAD's tree then
includes the file, the hash moves, the receipt goes stale and this blocks) -- run the wrapper
after staging, which is what members/*/*.md tells them to do.

Exempt, in order: no $WT_PATH (this pass is not worktree-isolated -- the same signal
worktree_guard_hook.py reuses rather than inventing a flag); the command is not a git push;
the branch changes only documentation. Everything else needs a receipt.

Reads one PreToolUse hook payload (JSON) on stdin. Exit 0 = allow, exit 2 = block.
Also usable as `pretest_push_hook.py --content-hash <dir>`, which is how verified_test.sh
computes the receipt -- one implementation, so the writer and the reader cannot drift.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

RECEIPT_NAME = "fleet-test-receipt.json"
# A push that only moves prose does not need a 12-minute suite. Anything that can change
# behaviour -- code, config, workflow, dependency manifest -- does.
DOC_SUFFIXES = (".md", ".txt", ".rst")


def _git(args: list[str], cwd: str) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    return p.stdout.strip() if p.returncode == 0 else ""


def receipt_path(wt: str) -> Path:
    """Inside the git dir, never in the worktree itself.

    A receipt sitting at the top of the checkout is an untracked file: it shows up in
    `git status`, which makes postflight_dirty_check.sh (gh#78/#183) call the pass dirty, and
    it lands in `_only_docs`'s own path list. `--absolute-git-dir` resolves to
    .git/worktrees/<name> for a linked worktree, so each one carries its own receipt and none
    of them can ever be committed.
    """
    return Path(_git(["rev-parse", "--absolute-git-dir"], wt) or Path(wt) / ".git") / RECEIPT_NAME


def content_hash(wt: str) -> str:
    """Hash of what is in the worktree NOW, stable across committing that same content."""
    stash = _git(["stash", "create"], wt)          # empty when the worktree is clean
    ref = f"{stash}^{{tree}}" if stash else "HEAD^{tree}"
    return _git(["rev-parse", ref], wt)


def _is_git_push(command: str) -> bool:
    # `git push`, `git -C <path> push`, `git --no-pager push`, and the same inside a chain.
    return bool(re.search(r"(^|[;&|]\s*)git\b[^;&|]*\bpush\b", command))


def _only_docs(wt: str) -> bool:
    """True iff nothing this branch changes can alter behaviour."""
    base = _git(["merge-base", "HEAD", "origin/main"], wt) or "origin/main"
    committed = _git(["diff", "--name-only", f"{base}...HEAD"], wt)
    working = _git(["status", "--porcelain=v1"], wt)
    paths = [ln for ln in committed.splitlines() if ln]
    paths += [ln[3:] for ln in working.splitlines() if ln[3:]]
    if not paths:
        return False  # nothing to judge -> do not hand out a free pass
    return all(p.endswith(DOC_SUFFIXES) for p in paths)


def decide(payload: dict, env: dict) -> str | None:
    """None = allow. A string = the reason to block, shown to the model."""
    wt = (env.get("WT_PATH") or "").strip()
    if not wt or not Path(wt).is_dir():
        return None  # not worktree-isolated: run_member.sh's own signal, same as gh#592's hook
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not _is_git_push(command):
        return None
    if _only_docs(wt):
        return None

    want = content_hash(wt)
    receipt = receipt_path(wt)
    if not receipt.exists():
        return (f"No test receipt. Run `bash {env.get('FLEET_KIT', '/fleet-kit')}/scripts/"
                f"verified_test.sh` in {wt} and push once it is green.\n"
                "CI is confirmation, not discovery: 45 of the last 95 `test` runs were red or "
                "cancelled because pushes went out untested.")
    try:
        data = json.loads(receipt.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return f"Test receipt at {receipt} is unreadable ({exc}). Re-run verified_test.sh."

    if data.get("status") != "pass":
        return (f"The last test run recorded `{data.get('status')}`, not a pass. "
                "Fix the failure and re-run verified_test.sh before pushing.")
    if data.get("content") != want:
        return ("The worktree changed after the last green test run "
                f"(tested {str(data.get('content'))[:12]}, now {want[:12]}). "
                "Re-run verified_test.sh so the push carries a receipt for what it pushes.")
    return None


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("--content-hash", "--receipt-path"):
        target = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()
        print(content_hash(target) if sys.argv[1] == "--content-hash" else receipt_path(target))
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never wedge a pass on a payload we cannot read
    reason = decide(payload, os.environ)
    if reason is None:
        return 0
    print(f"pretest_push_hook: {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
