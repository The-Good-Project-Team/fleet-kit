# claude_concurrency.sh -- caps how many `claude -p` passes run at once on this box.
#
# gh#672: on 2026-09-07, ~30 concurrent `claude -p` processes on a 7-core box (183
# run_member.sh processes total) drove load average to 35 and made a 30s deploy health check
# fail against a genuinely-healthy build (fk#671) -- an unbounded fanout (gru's own spawns,
# the-fixer's stale-PR sub-passes, minion dispatch) has no ceiling anywhere today. This gives
# every caller of run_member.sh ONE shared ceiling: a fixed number of "slots", each a file
# guarded by its own flock. Acquiring means holding any one slot's lock; when all N are held,
# the next attempt blocks (kernel wait queue on whichever slot it tries, not a spin) until one
# frees, so a pass QUEUES for a slot rather than spawning immediately (AC4). Deliberately no
# priority/fairness policy here (PRD non-goal #3) -- just a ceiling.
#
# N ~ cores by default (PRD's own "N ~ cores" -- the incident box's binding constraint may
# actually be memory, not cores, so this is overridable). See fleet.env.example.
CLAUDE_CONCURRENCY_DIR="${TMPDIR:-/tmp}/fleet-kit-claude-slots"
CLAUDE_CONCURRENCY_N="${FLEET_CLAUDE_CONCURRENCY:-$(nproc 2>/dev/null || echo 4)}"
mkdir -p "$CLAUDE_CONCURRENCY_DIR" 2>/dev/null || true

# claude_slot_acquire: blocks until one of $CLAUDE_CONCURRENCY_N slot files can be flocked,
# cycling through them so a caller doesn't wait on one specific busy slot while a sibling slot
# sits free. Opens the winning slot on fd 7 for the life of the caller's shell.
claude_slot_acquire() {
  local n="$CLAUDE_CONCURRENCY_N" i
  [ "$n" -ge 1 ] 2>/dev/null || n=4
  while :; do
    for ((i = 0; i < n; i++)); do
      exec 7>"$CLAUDE_CONCURRENCY_DIR/slot-$i.lock"
      if flock -n 7; then
        return 0
      fi
      exec 7>&-
    done
    sleep 1
  done
}

# claude_slot_release: always safe to call, even if acquire never ran.
claude_slot_release() {
  flock -u 7 2>/dev/null || true
  exec 7>&- 2>/dev/null || true
}
