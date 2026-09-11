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
#
# The redirect alternative used to require `>`/`>>` to sit at the very start of the command or
# right after a `;`/`&`/`|` separator -- which never matches an ordinary `cmd > file` (the `>`
# there is preceded by the command's own words, not a separator). Fixed as part of gh#715 AC4:
# require only that `>`/`>>` be preceded by whitespace/start/a separator (so it reads as an
# operator, not `-mmethod>Object` arrows or `>=` comparisons) and allow the usual optional
# space before the target.
#
# gh#834: `checkout` used to require a literal `--` right after it (`git checkout -- <file>`,
# the file-restore form) -- so `git checkout <branch>`/`git switch <branch>`, which move HEAD
# to a different branch entirely, matched nothing at all. That is the exact shape of the
# fleet's own #834 incident (host checkout of the self-hosted instance left on a stray feature
# branch): confirmed live that `git checkout rework-metric` and `git switch rework-metric` both
# returned exit 0 (allowed) against this hook before this fix. `switch` and `pull` are added for
# the same reason -- both move/rewrite HEAD and were simply absent from the verb list.
_MUTATING_BASH_RE = re.compile(
    r"\bgit\s+(commit|checkout|switch|reset|add|merge|rebase|push|pull|stash\s+pop|clean)\b"
    r"|\b(rm|mv|cp|sed\s+-i|mkdir|touch|chmod|chown|tee)\b"
    r"|(?:^|[\s;&|])>>?(?!=)\s*\S"
)
_MUTATING_GIT_VERB_RE = re.compile(
    r"\bgit\s+(?:commit|checkout|switch|reset|add|merge|rebase|push|pull|stash\s+pop|clean)\b")
_GIT_DASH_C_RE = re.compile(r"git\s+-C\s+(\S+)\s+(\S+)(?:\s+(\S+))?")
_MUTATING_SUBCOMMANDS = {"commit", "checkout", "switch", "reset", "add", "merge", "rebase", "push", "pull", "stash", "clean", "rm", "mv"}
# gh#894: a leading/compound `cd <path>` segment (`cd $REPO && git checkout <branch>`, or with
# `;`) changes the process's ACTUAL cwd by the time the git verb runs, but the Bash tool's own
# payload['cwd'] is always the pre-execution cwd (the pass's own $WT_PATH per run_member.sh) --
# PR #893's fallback never re-derived cwd from this, so this exact compound shape (the shape the
# PRD's incident theory names) sailed straight through. Only a bare `cd <path>` right after the
# start of the command or a `&&`/`;` separator counts -- PRD non-goal 1: no general shell parser,
# so a variable assigned mid-command, `eval`, or command substitution deciding the path is left
# uncovered and falls back to the existing pre-execution cwd unchanged.
_LEADING_CD_RE = re.compile(r"(?:^|&&|;)\s*cd\s+('[^']*'|\"[^\"]*\"|[^\s;&]+)")
# gh#837: `stash` alone is too coarse -- `stash list`/`stash show` are read-only, `stash pop`
# (and bare `stash`, which git treats as `stash push`) are not. Only `stash` gets this second
# check; every other verb in _MUTATING_SUBCOMMANDS stays decided by the verb alone.
_READONLY_STASH_SUBCOMMANDS = {"list", "show"}
_PATH_TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\S+")

# gh#715: a command that merely QUOTES a mutating verb or a shared-checkout path -- prose in a
# `gh issue comment --body "..."` argument, or a heredoc BODY -- must not be treated as if it
# typed that text as a real shell argument. Both are stripped before every regex/token check
# below; only single-token quoted values ('/repo/file', no internal whitespace) are left alone,
# since a real mutating command can legitimately quote its own path argument and blanket-
# stripping quotes would turn that into a new bypass -- the exact trap gh#715 itself names
# ("the workaround...would work just as well for a genuinely unsafe write").
_SQ_RE = re.compile(r"'([^']*)'")
_DQ_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_HEREDOC_RE = re.compile(r"(<<-?\s*['\"]?)(\w+)(['\"]?)(.*?)(\n[ \t]*\2\b)", re.DOTALL)


def _strip_prose(command: str) -> str:
    def _blank_if_multiword(m: "re.Match[str]") -> str:
        if re.search(r"\s", m.group(1)):
            quote = m.group(0)[0]
            return quote + quote
        return m.group(0)

    command = _HEREDOC_RE.sub(lambda m: m.group(1) + m.group(2) + m.group(3), command)
    command = _SQ_RE.sub(_blank_if_multiword, command)
    command = _DQ_RE.sub(_blank_if_multiword, command)
    return command


def _resolve(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _under(path: str, root: str) -> bool:
    path = path.rstrip("/") + "/"
    root = root.rstrip("/") + "/"
    return path == root or path.startswith(root)


def _effective_cwd(command: str, cwd_real: str | None, verb_start: int) -> str | None:
    """gh#894: walks `cd <path>` segments that occur strictly before `verb_start` (the position
    of the matched mutating verb), tracking cwd left to right the way a shell actually would.
    A `cd` with an unresolvable target (e.g. a literal `$REPO` the hook never expands -- PRD's
    own UNKNOWN, left fail-open on purpose) or one after `verb_start` is ignored and the cwd
    tracked so far is kept."""
    effective = cwd_real
    for m in _LEADING_CD_RE.finditer(command, 0, verb_start):
        target = m.group(1).strip("'\"")
        base = effective or os.getcwd()
        try:
            resolved = target if os.path.isabs(os.path.expanduser(target)) else os.path.join(base, target)
            effective = _resolve(resolved)
        except OSError:
            continue
    return effective


def _bash_targets_repo(command: str, repo_real: str, wt_real: str, cwd_real: str | None) -> bool:
    if not command:
        return False

    command = _strip_prose(command)

    # ALL `git -C <dir> <verb>` occurrences, not just the first -- a chained command like
    # `git -C $REPO log && git -C $REPO add -A && git -C $REPO commit -m wip` has an earlier,
    # innocent `-C $REPO log` before the mutating one; stopping at the first match (the
    # original bug here, caught in review) let the real mutation through.
    for m in _GIT_DASH_C_RE.finditer(command):
        subcmd = m.group(2).strip("'\"")
        try:
            target_dir = _resolve(m.group(1).strip("'\""))
        except OSError:
            target_dir = None
        if not (target_dir and subcmd in _MUTATING_SUBCOMMANDS and _under(target_dir, repo_real)):
            continue
        if subcmd == "stash":
            stash_sub = (m.group(3) or "").strip("'\"")
            if stash_sub in _READONLY_STASH_SUBCOMMANDS:
                continue  # `stash list`/`stash show` are reads, not writes (gh#837)
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

    # gh#834: none of the above requires an explicit path at all -- `cd $REPO && git checkout
    # <branch>` never names $REPO inside the git command itself, so every check above sees only
    # "git checkout <branch>" and finds no path token to test. A mutating git verb with no `-C`
    # override implicitly operates on the process's cwd; if that cwd is the shared checkout
    # (and not this pass's own worktree), the command targets $REPO regardless of what it spells
    # out. `-C` is excluded here because it already redirects git elsewhere, and that case is
    # fully handled by the loop above (including the "allowed" case of `-C $WT_PATH`).
    #
    # gh#894: `cwd_real` itself is always the Bash tool's PRE-EXECUTION cwd (this pass's own
    # $WT_PATH per run_member.sh) -- it is never updated by a `cd` that lives INSIDE the command
    # string. `_effective_cwd` walks any leading/compound `cd <path>` segments before the
    # matched mutating verb so `cd $REPO && git checkout <branch>` (one command, cwd starts at
    # $WT_PATH) is judged by the cwd the git verb actually runs under, not the shell's starting
    # cwd -- PR #893's own comment claimed this closed, and it did not (judge-judy on #893).
    git_verb_match = _MUTATING_GIT_VERB_RE.search(command)
    if git_verb_match and not _GIT_DASH_C_RE.search(command):
        effective_cwd = _effective_cwd(command, cwd_real, git_verb_match.start())
        if effective_cwd and _under(effective_cwd, repo_real) and not _under(effective_cwd, wt_real):
            return True
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
        # gh#834: Claude Code's PreToolUse payload carries the command's own `cwd` -- read it so
        # a bare mutating command with no explicit path (`cd $REPO && git checkout <branch>`)
        # can be caught via cwd, not just via a path token spelled out in the command string.
        cwd = payload.get("cwd") or ""
        try:
            cwd_real = _resolve(cwd) if cwd else None
        except OSError:
            cwd_real = None
        if _bash_targets_repo(command, repo_real, wt_real, cwd_real):
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
