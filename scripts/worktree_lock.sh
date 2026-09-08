# worktree_lock.sh -- shared fair lock for `git worktree add`/`remove`/`prune` against $REPO's
# .git/worktrees admin dir. Sourced by run_member.sh and worktree_builder.sh, which contend for
# the SAME lock (gh#4727: an unguarded `remove`/`prune` races a sibling's concurrent `add`).
#
# gh#672: replaces the old `mkdir "$LOCK"` + `sleep 2` spin. That spin has no memory of arrival
# order -- a lock release and a brand-new contender's very FIRST `mkdir` attempt can land in the
# same instant and win exactly as often as a process that has been polling for minutes, because
# a sleeping poller isn't even attempting the `mkdir` at the moment the lock frees. Measured
# 2026-09-07: under ~30 concurrent run_member.sh processes (load 35 on 7 cores), this starved
# the messenger inbox pass (fk#669/#670 -- the fleet's highest-priority input, a real reply from
# Reif) for 6 minutes straight before it hit the 120s per-attempt timeout and died with FATAL.
#
# flock(2) fixes the mechanism, not just the symptom: once a waiter calls it, that waiter is a
# real blocked kernel task for the ENTIRE wait, not a process that stops contending between
# polls -- so a late arrival can no longer win purely by having better timing against someone
# else's sleep. Stale-lock recovery (a sibling killed mid-add wedging every later attempt) is
# also the kernel's problem now: flock releases automatically when the holding process dies, so
# the old "steal a lock dir older than 5 minutes" workaround is no longer needed.
WORKTREE_LOCK_FILE="${TMPDIR:-/tmp}/fleet-kit-worktree-add.flock"

# worktree_lock_acquire [timeout_s=120]: blocks on the kernel's wait queue (not a poll loop)
# until the lock is free, or returns 1 after timeout_s. Opens the lock on fd 8 for the life of
# the caller's shell -- release explicitly with worktree_lock_release before anything that must
# not inherit it (podman run, a backgrounded/detached child), the same discipline auto_deploy.sh
# already follows for its own flock fd 9.
worktree_lock_acquire() {
  local timeout="${1:-120}"
  exec 8>"$WORKTREE_LOCK_FILE"
  flock -w "$timeout" 8
}

# worktree_lock_release: always safe to call, even if acquire never ran or already timed out.
worktree_lock_release() {
  flock -u 8 2>/dev/null || true
  exec 8>&- 2>/dev/null || true
}
