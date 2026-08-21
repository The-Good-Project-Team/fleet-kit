#!/bin/bash
# fleet-recycle-retired.sh — a deliberate no-op stub, kept for roster fidelity only.
#
# nonprofit-atlas's real crew roster (scripts/fleet/roster.py) lists fleet-recycle as one of
# the jobs roomba absorbs. Its actual implementation (fleet_recycle.sh) was retired 2026-08-09:
# it watched a long-running tmux `claude` session's context, and that interactive-pane
# architecture has no live callers anywhere in a headless per-pass fleet like this one. This
# stub exists only so a session grepping for "fleet-recycle" finds an explanation, not a
# silent gap in the file tree.
set -uo pipefail
echo "fleet-recycle: retired -- the tmux/interactive-pane architecture it watched no longer" \
     "exists in a headless per-pass fleet. See roomba.py for the live worktree/ghost sweep" \
     "that replaced its job. Nothing to unload here; this stub is not scheduled by any" \
     "member.fleet.json." >&2
exit 0
