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
# KNOWN LIMITATION, not fixed here: with two members racing $REPO concurrently, whichever
# pass's cleanup happens to run first after the dirt appears is the one that gets blamed --
# there is no per-pass "was $REPO clean when I started" baseline, only "is it clean now".
# Closing that needs each pass to snapshot $REPO's state on entry, a bigger change than this
# alert. What this DOES fix: the same leaked file no longer re-alerts (and re-blames) every
# single later pass forever -- see the state-file dedup below.
#
# gh#4542: detection alone left a leak sitting in $REPO for hours -- the gitpull cron's
# `git merge --ff-only` (git_pull_guard.sh) has no way to proceed over a dirty tree, so it just
# refuses on the same dirt every 10 minutes forever until a human notices and rescues it by
# hand (`git stash -u`, per jefe's existing manual playbook). Below, ALERT now auto-stashes the
# leak the same way, immediately -- reversible (nothing is dropped, `git stash list` still
# holds it), so the very next gitpull tick can fast-forward again instead of waiting on a human
# to notice a crashlooping cron. Locks on the same $REPO/.git/fleet_pull.lock git_pull_guard.sh
# already uses, so an auto-stash here and a concurrent gitpull tick never race the same index.
#
# What this does NOT do, on purpose: silently expire or drop old stash entries. Reif's
# writeup on gh#4542 found 17+ prior manual rescue stashes already sitting unreviewed for
# weeks -- auto-stashing without ever surfacing that pile just moves the same rot from a dirty
# working tree to a dirty stash list nobody looks at either. STASH_PILE_WARN_THRESHOLD below is
# the stated policy: once the pile crosses it, log a POLICY line naming the count so a human
# decides what to land or drop -- the fleet has no mechanism that judges whether stashed WIP is
# still wanted, so it does not delete any of it itself.
#
# Callers must already have: $REPO set, a `log` function defined, and $LOG_DIR set -- same
# preconditions run_member.sh and worktree_builder.sh both already establish before creating
# their worktree.
#
# Usage: check_repo_clean_postflight <run-or-item-label>
STASH_PILE_WARN_THRESHOLD="${STASH_PILE_WARN_THRESHOLD:-5}"

check_repo_clean_postflight() {
  local label="${1:-unknown}"
  local state_file="$LOG_DIR/.repo_dirty_state"
  local dirty rc

  dirty=$(git -C "$REPO" status --short 2>&1)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # A failed `git status` is NOT "clean" -- it means we genuinely don't know, which is a
    # louder problem than ordinary dirt (index.lock contention, a corrupt index, $REPO gone
    # mid-check). Alert on the uncertainty itself rather than silently reporting all-clear.
    log "ALERT: could not verify \$REPO ($REPO) is clean after this pass exited (run=$label) -- \`git status\` itself failed (rc=$rc): $dirty"
    {
      echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] run=$label repo=$REPO git-status-failed rc=$rc"
      echo "$dirty"
    } >> "$LOG_DIR/repo_dirty_alerts.log"
    return 0
  fi

  if [ -z "$dirty" ]; then
    rm -f "$state_file"   # confirmed clean again -- any prior leak this pass is checking for is gone
    return 0
  fi

  # Dedup against the last-alerted content: without this, one leaked file re-triggers a fresh
  # "ALERT, blame run=X" on every single later pass's own cleanup forever, mis-attributing the
  # SAME stale dirt to every innocent pass that happens to check next.
  local sig
  sig=$(printf '%s' "$dirty" | (command -v sha256sum >/dev/null 2>&1 && sha256sum || shasum -a 256) | awk '{print $1}')
  if [ -f "$state_file" ] && [ "$(sed -n '1p' "$state_file")" = "$sig" ]; then
    log "NOTE: \$REPO ($REPO) is still dirty from an earlier leak (first flagged as run=$(sed -n '2p' "$state_file")) -- not caused by this run (run=$label); auto-stash of it must have failed, see repo_dirty_alerts.log, needs a human rescue"
    return 0
  fi
  printf '%s\n%s\n' "$sig" "$label" > "$state_file"

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

  # gh#4542: remediate immediately rather than leave the leak for gitpull to keep refusing on.
  # Locked against git_pull_guard.sh's own lockfile so an in-flight `git merge --ff-only` and
  # this stash can never touch the index at the same time.
  local lockfile="$REPO/.git/fleet_pull.lock"
  local stash_msg
  stash_msg="postflight-dirty-check auto-stash run=$label $(date '+%Y-%m-%dT%H:%M:%SZ')"
  local stash_out stash_rc
  if command -v flock >/dev/null 2>&1; then
    stash_out=$(flock "$lockfile" git -C "$REPO" stash push -u -m "$stash_msg" 2>&1)
  else
    stash_out=$(git -C "$REPO" stash push -u -m "$stash_msg" 2>&1)
  fi
  stash_rc=$?

  if [ "$stash_rc" -eq 0 ]; then
    rm -f "$state_file"   # remediated -- $REPO is clean again, nothing left for a later pass to blame
    local pile_count
    pile_count=$(git -C "$REPO" stash list 2>/dev/null | wc -l | tr -d ' ')
    log "REMEDIATED: auto-stashed the leak from run=$label so gitpull can proceed on its next tick ($stash_out); \$REPO stash pile now has $pile_count entries, nothing dropped"
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] REMEDIATED run=$label repo=$REPO auto-stashed pile=$pile_count" >> "$LOG_DIR/repo_dirty_alerts.log"
    if [ "$pile_count" -ge "$STASH_PILE_WARN_THRESHOLD" ]; then
      log "POLICY: \$REPO auto-stash pile has grown to $pile_count entries (threshold $STASH_PILE_WARN_THRESHOLD) -- these are reversible rescues, never auto-expired; a human needs to run \`git -C $REPO stash list\` and decide what to land or drop"
      echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] POLICY repo=$REPO stash-pile=$pile_count threshold=$STASH_PILE_WARN_THRESHOLD needs-human-review" >> "$LOG_DIR/repo_dirty_alerts.log"
    fi
  else
    log "REMEDIATION FAILED: could not auto-stash \$REPO ($REPO) after run=$label -- leak left in place, gitpull will keep refusing until a human intervenes: $stash_out"
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] REMEDIATION-FAILED run=$label repo=$REPO: $stash_out" >> "$LOG_DIR/repo_dirty_alerts.log"
  fi
}
