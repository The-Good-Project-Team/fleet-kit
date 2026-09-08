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
# run_member.sh writes its "started" row (run_report.py --started) BEFORE calling
# claude_slot_acquire, specifically so a pass killed before claude -p even starts still leaves
# a trace (gh#145). fleet_stats.lost_passes() then flags any "started" row with no completion
# row as lost after grace_minutes (default 90). A queue wait with no ceiling of its own could
# exceed that grace window under exactly the burst this ceiling exists to survive, making a
# healthy, merely-queued pass misreport as lost. Default comfortably under 90 minutes; a caller
# that times out here still gets its slot (see claude_slot_acquire) -- this bounds how long a
# pass looks "possibly lost" while queued, it does not turn the ceiling into a hard failure.
CLAUDE_CONCURRENCY_SLOT_TIMEOUT_S="${FLEET_CLAUDE_SLOT_TIMEOUT_S:-3000}"
mkdir -p "$CLAUDE_CONCURRENCY_DIR" 2>/dev/null || true

# claude_slot_acquire: blocks until one of $CLAUDE_CONCURRENCY_N slot files can be flocked,
# cycling through them so a caller doesn't wait on one specific busy slot while a sibling slot
# sits free. Opens the winning slot on fd 7 for the life of the caller's shell. Each slot is
# tried with a short BLOCKING flock (not `flock -n` + a fixed sleep): a waiter that is
# genuinely registered on the kernel's wait queue for real stretches of its wait cannot be
# outrun by a fresh contender purely on poll timing, the same reasoning worktree_lock.sh
# already applies to the single worktree-add lock. Past CLAUDE_CONCURRENCY_SLOT_TIMEOUT_S this
# still returns 0 (see the timeout var's own comment above) -- the ceiling is best-effort, not
# a hard cap a pass can be starved behind forever. gh#694: that best-effort wait keeps cycling
# through EVERY slot even past the deadline -- it must never bind to one specific slot number
# (slot 0 was tried once; a long-lived holder of slot 0 then parked every timed-out caller
# behind it while other slots cycled freely, reintroducing the false "lost pass" alarm #692
# was written to remove). The only change past the deadline is a one-time log line so an
# operator can see a pass is still fair-queueing, not stuck.
claude_slot_acquire() {
  local n="$CLAUDE_CONCURRENCY_N" i deadline warned=
  [ "$n" -ge 1 ] 2>/dev/null || n=4
  deadline=$(( $(date +%s) + CLAUDE_CONCURRENCY_SLOT_TIMEOUT_S ))
  while :; do
    for ((i = 0; i < n; i++)); do
      exec 7>"$CLAUDE_CONCURRENCY_DIR/slot-$i.lock"
      if flock -w 1 7; then
        return 0
      fi
      exec 7>&-
    done
    if [ -z "$warned" ] && [ "$(date +%s)" -ge "$deadline" ]; then
      warned=1
      echo "claude_slot_acquire: past ${CLAUDE_CONCURRENCY_SLOT_TIMEOUT_S}s, still waiting for any of $n slots" >&2
    fi
  done
}

# claude_slot_release: always safe to call, even if acquire never ran.
claude_slot_release() {
  flock -u 7 2>/dev/null || true
  exec 7>&- 2>/dev/null || true
}
