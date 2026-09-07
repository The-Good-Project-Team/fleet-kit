#!/usr/bin/env python3
"""worktree_guard_hook_install.py -- gh#592.

Idempotently registers worktree_guard_hook.py as a PreToolUse hook (matcher "Edit|Write|Bash")
inside one or more Claude Code user settings.json files -- merges into whatever is already
there, never overwrites an operator's own hooks/permissions, and is a no-op on a second run.

WHY MULTIPLE FILES, NOT JUST THE BARE $HOME/.claude/settings.json GH#592'S AC1 NAMES: the
member runner's own `claude -p ... --setting-sources user` (run_member.sh) loads settings from
$CLAUDE_CONFIG_DIR/settings.json, and CLAUDE_CONFIG_DIR is account-specific
($HOME/.claude-<account>) for EVERY real pass -- account_pool.sh's account_pool_run sets this
even for the single-account "primary" case, and entrypoint.sh's own boot-time credential check
already enumerates the exact same per-account dirs. A settings.json written only at the bare
path would build clean, pass a spec-compliance read of AC1, and still never actually run
against a real scheduled pass -- the identical "fix looks complete, does nothing live" shape
this whole issue exists to end. So callers (entrypoint.sh) pass BOTH the bare default and every
account dir it already knows about.

Usage: worktree_guard_hook_install.py <settings.json-path> [<settings.json-path> ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MATCHER = "Edit|Write|Bash"


def _hook_command() -> str:
    kit_dir = Path(__file__).resolve().parent.parent
    return f"python3 {kit_dir / 'scripts' / 'worktree_guard_hook.py'}"


def merge_one(path: Path, hook_cmd: str) -> bool:
    """Registers hook_cmd in path's settings.json. Returns True iff the file changed."""
    if path.exists():
        try:
            settings = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            print(f"worktree_guard_hook_install: {path} is not valid JSON ({exc}), leaving untouched",
                  file=sys.stderr)
            return False
    else:
        settings = {}

    pre_list = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])

    for entry in pre_list:
        for h in entry.get("hooks", []):
            if h.get("command") == hook_cmd:
                return False  # already registered, nothing to do

    pre_list.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": hook_cmd}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return True


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: worktree_guard_hook_install.py <settings.json-path> [...]", file=sys.stderr)
        return 2
    hook_cmd = _hook_command()
    for raw in argv:
        p = Path(raw)
        if merge_one(p, hook_cmd):
            print(f"worktree_guard_hook_install: registered gh#592 guard in {p}")
        else:
            print(f"worktree_guard_hook_install: {p} already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
