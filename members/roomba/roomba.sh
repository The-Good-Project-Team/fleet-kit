#!/bin/bash
# roomba.sh -- the worktree sweep as a plain script, not a 30-turn LLM pass (fleet-kit#514).
#
# WHY: measured on the last 1,000 runs per container, roomba burned 25-30 turns of window per
# tick to run roomba.py twice and write a report, and 43 of 93 passes on the fleet-kit instance
# were quiet. Every safety check that matters (merge-state, clean tree, protect list, min-age,
# dangling PID) already lives in roomba.py -- the model added nothing but a re-read of the
# dry-run output it then re-ran. Same output, zero window: run_member.sh dispatches here via
# the spec's llm.runner (the judge-judy shape), and this records the pass through
# run_report.py so fleet.db, /status and fleet_kpi see it exactly as before.
#
# WHAT CHANGED IN SCOPE: the "crew health" half of the old charter (NOT_LOADED / STALE /
# CRASHLOOP ghosts) is not here. A ghost is a job that stopped doing work, and that is now
# what member_liveness_check.sh (fleet-kit#512) pages on from OUTSIDE the container -- a
# sweep inside the container that has to notice its own cron is dead was the wrong place for
# it anyway (the Sep 3-5 outage: roomba was one of the members that went dark).
#
# Ambiguity rule is unchanged and now mechanical: roomba.py only lists a candidate once every
# check passes; anything it could not decide it does not list. This script never adds a
# candidate of its own, so "always KEEP, never guess toward removal" holds by construction.
set -uo pipefail

KIT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
LOG_DIR="${FLEET_LOG_DIR:-/var/log/fleet-kit}"
REPO="${FLEET_REPO:?set FLEET_REPO -- the repo whose worktrees roomba sweeps}"
RUN_ID="roomba-$$-$(date -u +%s)"
LOG="$LOG_DIR/roomba.log"
mkdir -p "$LOG_DIR"

report() { # <outcome> <evidence> <self-critique> <exit-code>
  printf 'Outcome: %s\nEvidence: %s\nSelf-critique: %s\n' "$1" "$2" "$3" | python3 "$KIT_DIR/scripts/run_report.py" \
    --member roomba --run-id "$RUN_ID" --kind shell --exit-code "${4:-0}" \
    --pass-file - >> "$LOG_DIR/runs.jsonl" 2>>"$LOG"
}

# Dry-run first, same as the charter always did. Its stdout is the evidence.
DRY=$(cd "$REPO" && python3 "$KIT_DIR/members/roomba/roomba.py" --repo "$REPO" 2>&1); RC=$?
if [ "$RC" -ne 0 ]; then
  report "QUIET — worktree sweep could not run (roomba.py rc=$RC); nothing removed" \
         "$(printf '%s' "$DRY" | tail -n 3 | tr '\n' ' ' | cut -c1-300)" \
         "a sweep that cannot evaluate must not remove -- kept everything, reported the failure" "$RC"
  echo "QUIET rc=$RC"
  exit 0
fi
# roomba.py's summary line is "N worktree(s) evaluated, M would be removed (dry-run)". The
# Outcome below is phrased "(N evaluated, M removed)" -- the exact shape the old model pass
# wrote and the one scripts/fleet_kpi.py anchors on (gh#343), so the KPI keeps counting.
EVALUATED=$(printf '%s' "$DRY" | grep -oE '[0-9]+ worktree\(s\) evaluated' | tail -n 1 | grep -oE '^[0-9]+' || echo 0)
CANDIDATES=$(printf '%s' "$DRY" | grep -cE '^\s*would-remove' || true)

if [ "${CANDIDATES:-0}" -eq 0 ]; then
  report "QUIET — worktree sweep (${EVALUATED:-0} evaluated, 0 removed); nothing to remove" \
         "dry-run listed no candidate: every worktree is merged-and-in-use, unmerged, dirty, protected, or younger than min-age" \
         "none -- deterministic sweep, no candidates" 0
  echo "QUIET ${EVALUATED:-0} evaluated, 0 removed"
  exit 0
fi

# Candidates exist and every one passed roomba.py's own checks: execute, record what went.
EXEC=$(cd "$REPO" && python3 "$KIT_DIR/members/roomba/roomba.py" --repo "$REPO" --execute 2>&1); RC=$?
REMOVED=$(printf '%s' "$EXEC" | grep -cE '^\s*removed' || true)
if [ "$RC" -ne 0 ]; then
  report "worktree sweep (${EVALUATED:-0} evaluated, ${REMOVED:-0} removed) before roomba.py failed rc=$RC" \
         "$(printf '%s' "$EXEC" | tail -n 4 | tr '\n' ' ' | cut -c1-300)" \
         "partial execute -- the next tick re-evaluates from scratch, nothing is lost" "$RC"
  echo "PARTIAL rc=$RC"
  exit 0
fi
report "worktree sweep (${EVALUATED:-0} evaluated, ${REMOVED:-0} removed)" \
       "$(printf '%s' "$EXEC" | grep -E '^\s*removed' | head -n 5 | tr '\n' ' ' | cut -c1-300)" \
       "none -- every removal passed roomba.py's merge/clean/protect/min-age/dead-PID checks" 0
echo "OK ${EVALUATED:-0} evaluated, ${REMOVED:-0} removed"
exit 0
