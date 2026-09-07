#!/usr/bin/env python3
"""worktree_guard_hook.py -- gh#592: a mechanical PreToolUse hook, not another paragraph.

`members/minion/minion.md` step 1c already asks the model to run `pwd`/`git worktree list`
before its first Edit/Write -- prose, added by PR#577. A minion pass hit the identical failure
again under 12 hours after that fix deployed (gh#592's own writeup). This is the mechanical
layer underneath the prose: a PreToolUse hook that BLOCKS (not warns on) an Edit/Write, or a
mutating Bash command, whose target resolves under the SHARED checkout ($REPO) while this pass
has been isolated into its own worktree ($WT_PATH) -- see run_member.sh's own WORKTREE_ENABLED/
WT_PATH block, which is the ONLY thing that ever sets WT_PATH.

Exempt whenever $WT_PATH is unset/empty: that is exactly run_member.sh's own signal that this
pass is NOT worktree-isolated (`llm.worktree: false`, e.g. jefe's advisory pass -- see
jefe.fleet.json). No new flag invented for that -- this reuses the one the runner already sets,
per gh#592's own instruction not to invent names beyond what run_member.sh actually exports.

`postflight_dirty_check.sh` (gh#78/#183) stays as the second, after-the-fact backstop; this is
the first, preventive layer -- gh#592's own non-goals keep both.

BASH MATCHING IS A BEST-EFFORT HEURISTIC, NOT A PARSER -- gh#592's own PRD flags this as an
open spike ("whether Claude Code's PreToolUse hook API can reliably inspect a Bash command's
shell-parsed target path"). It catches the common mutating shapes named in the issue (git
commit/checkout --/reset/add/mv/rm/sed -i/shell redirection/`git -C $REPO <verb>`) that name a
path under $REPO, and deliberately leaves read-only forms (`git show`, `git diff`, `git log`,
`git status`, `cat`, `ls`, ...) alone rather than false-block them. A sufficiently obfuscated
command (a variable holding the path, a wrapper script) can still slip past it -- this is a
guard rail, not a sandbox, the same caveat postflight_dirty_check.sh's own header states for
its layer.

Reads one PreToolUse hook payload (JSON) from stdin. Exit 0 = allow, exit 2 = block (Claude
Code shows stderr back to the model as the reason) -- the documented hook contract.
"""
from __future__ import annotations

import json
import os
import re
import sys

# Verbs that mutate a git checkout or the filesystem. Deliberately excludes read-only verbs
# (show, diff, log, status, ls, cat, grep, less, head, tail) so a read-only reference to $REPO
# (gh#592 AC3: `git show origin/main:<path>`) is never blocked.
_MUTATING_BASH_RE = re.compile(
    r"\bgit\s+(commit|checkout\s+--|reset|add|merge|rebase|push|stash\s+pop|clean)\b"
    r"|\b(rm|mv|cp|sed\s+-i|mkdir|touch|chmod|chown|tee)\b"
    r"|(^|[;&|]\s*)>>?\S"
)
_GIT_DASH_C_RE = re.compile(r"git\s+-C\s+(\S+)\s+(\S+)")
_MUTATING_SUBCOMMANDS = {"commit", "checkout", "reset", "add", "merge", "rebase", "push", "stash", "clean", "rm", "mv"}
_PATH_TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\S+")


def _resolve(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _under(path: str, root: str) -> bool:
    path = path.rstrip("/") + "/"
    root = root.rstrip("/") + "/"
    return path == root or path.startswith(root)


def _bash_targets_repo(command: str, repo_real: str) -> bool:
    if not command:
        return False

    m = _GIT_DASH_C_RE.search(command)
    if m:
        subcmd = m.group(2).strip("'\"")
        try:
            target_dir = _resolve(m.group(1).strip("'\""))
        except OSError:
            target_dir = None
        if target_dir and subcmd in _MUTATING_SUBCOMMANDS and _under(target_dir, repo_real):
            return True

    if not _MUTATING_BASH_RE.search(command):
        return False

    for raw_tok in _PATH_TOKEN_RE.findall(command):
        tok = raw_tok.strip("'\"")
        if not tok or not (tok.startswith("/") or tok.startswith("./") or tok.startswith("../") or tok.startswith("~")):
            continue
        if ":" in tok.split("/")[-1] and not os.path.exists(tok):
            continue  # e.g. origin/main:path -- a git ref, not a real filesystem path
        try:
            if _under(_resolve(tok), repo_real):
                return True
        except OSError:
            continue
    return False


def decide(payload: dict, env: dict) -> str | None:
    """Returns a block reason, or None to allow."""
    wt_path = (env.get("WT_PATH") or "").strip()
    repo = (env.get("REPO") or "").strip()
    if not wt_path or not repo:
        return None  # not worktree-isolated this pass -- nothing to enforce (AC4)

    try:
        repo_real = _resolve(repo)
        wt_real = _resolve(wt_path)
    except OSError:
        return None  # can't resolve either path -- fail open rather than block on our own error

    if repo_real == wt_real:
        return None  # WT_PATH somehow equals REPO -- not actually isolated

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool_name in ("Edit", "Write", "NotebookEdit"):
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not file_path:
            return None
        try:
            target_real = _resolve(file_path)
        except OSError:
            return None
        if _under(target_real, repo_real) and not _under(target_real, wt_real):
            return (f"BLOCKED by worktree_guard_hook.py (gh#592): {file_path} resolves under the "
                     f"SHARED checkout {repo}, but this pass is isolated in its own worktree "
                     f"{wt_path}. Make this change under {wt_path} instead.")
        return None

    if tool_name == "Bash":
        command = tool_input.get("command") or ""
        if _bash_targets_repo(command, repo_real):
            return (f"BLOCKED by worktree_guard_hook.py (gh#592): this Bash command appears to "
                     f"mutate the SHARED checkout {repo} directly, but this pass is isolated in "
                     f"its own worktree {wt_path}. Target {wt_path} instead (a read-only "
                     f"reference like `git show origin/main:<path>` is not blocked).")
        return None

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        # Malformed input from the CLI itself is not this pass's mutation to judge -- fail
        # open rather than block every tool call fleet-wide on a hook bug.
        print(f"worktree_guard_hook.py: could not parse stdin ({exc}), allowing", file=sys.stderr)
        return 0

    reason = decide(payload, os.environ)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
