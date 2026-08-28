#!/bin/bash
# postflight_dirty_check.sh — shared by run_member.sh and worktree_builder.sh.
#
# fleet-kit#78 (tracked at nonprofit-atlas#3113, 15+ recurrences): worktree isolation is a
# `cd`, not a sandbox. It stops nothing when a tool call names the shared checkout by its
# absolute path ($REPO/...) instead of the worktree it was actually placed in -- that write
# lands in $REPO for real, uncommitted, where every other concurrently-running fleet member
# reads and writes. Every prior occurrence was found by a human or another pass noticing
# $REPO was dirty, sometimes hours later. This is the automated check #3113's own writeup
# asked for: after an isolated pass exits, confirm $REPO -- the ONE shared checkout every
# worktree is cloned FROM -- is still clean, and if not, say so loudly and attribute it.
#
# Cheap option, not a sandbox: a mount namespace / fully separate clone per pass would close
# this for good but is a much bigger infra change. This can't stop the leak, only make sure
# nobody has to discover it by accident again.
#
# Callers must already have: $REPO set, a `log` function defined, and $LOG_DIR set -- same
# preconditions run_member.sh and worktree_builder.sh both already establish before creating
# their worktree.
#
# Usage: check_repo_clean_postflight <run-or-item-label>
check_repo_clean_postflight() {
  local label="${1:-unknown}"
  local dirty
  dirty=$(git -C "$REPO" status --short 2>/dev/null) || return 0
  [ -z "$dirty" ] && return 0
  log "ALERT: \$REPO ($REPO) is dirty after this pass exited (run=$label) -- worktree isolation was bypassed by an absolute-path write outside the worktree (fleet-kit#78). Leaked paths:"
  log "$dirty"
  # A second, dedicated file: the per-member log above is where a human already looks for
  # THIS member, but this failure mode is cross-member by nature (it dirties the ONE shared
  # checkout every member's worktree comes from) -- one file any pass can tail/grep across
  # every occurrence, instead of hunting through each member's own log in turn.
  {
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] run=$label repo=$REPO"
    echo "$dirty"
  } >> "$LOG_DIR/repo_dirty_alerts.log"
}
