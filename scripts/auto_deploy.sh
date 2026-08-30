#!/bin/bash
# auto_deploy.sh -- host-side poll: main moved -> pull + deploy.sh, no manual redeploy step.
#
# Reif, 2026-08-22/23: "pushing a pr should trigger that deploy pattern we built." Root cause
# this exists to close: the host checkout at /home/ubuntu/fleet-kit was 10 PRs behind origin/main
# (#21-#30 merged but never pulled) for the entire session -- every fix landed in git, several
# never reached the running container at all (jefe.md still had `{{VISION}}` placeholders live
# in prod hours after PR #22 merged, mutation-tested, and reported done). deploy.sh existed but
# nothing ever CALLED it on a merge; this is the caller.
#
# Why a poll, not a webhook: deploy.sh needs `podman build`/`podman run` -- host-level commands
# -- so it must run ON THE HOST, not inside the philanthropy container. The container already
# runs a webhook receiver, but it's inside the container it would need to redeploy (redeploying
# itself from inside itself is the wrong side of that boundary). A host-side webhook receiver
# is more surface (new port, new secret) for marginal latency gain over a tight poll; this kit
# already leans on poll-as-backstop elsewhere (the-fixer's own hourly tick) for the same reason.
#
# Idempotent and cheap on a no-op tick: `git fetch` + compare SHAs, only pulls/deploys/builds
# when main actually moved. A tick that finds nothing new costs one network call, no build, no
# container churn -- safe to run every few minutes from cron.
#
# Usage: FLEET_INSTANCE_DIR=/home/ubuntu/fleet-kit/instances/nonprofit-atlas \
#          bash scripts/auto_deploy.sh
# (run FROM the host checkout, i.e. cwd = /home/ubuntu/fleet-kit)
set -euo pipefail

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}"
LOG="$LOG_DIR/auto_deploy.log"
# Keyed by container name: STATE/LOCKFILE were a single global path shared by every instance
# on the box, so instance A's successful tick wrote LAST_DEPLOYED and instance B's next tick
# read it back and saw "already deployed" even if B's own container was still stale on an
# older SHA (or, worse via the shared LOCKFILE below, one instance's deploy would flock out
# every other instance's tick entirely). Harmless while every instance tracked the same
# fleet-kit main, but that was luck, not a guarantee -- confirmed live 2026-08-29 setting up
# fleet-kit-server-fleet as a second instance alongside nonprofit-atlas/philanthropy.
INSTANCE_KEY="${FLEET_CONTAINER_NAME:-default}"
STATE="$HOME/.cache/fleet-kit/auto_deploy.last_sha.${INSTANCE_KEY}"
mkdir -p "$LOG_DIR" "$(dirname "$STATE")"
log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" >> "$LOG"; }

cd "$KIT_DIR"

# ONE deploy at a time. This poll fires every 5 minutes, and since the drain gate landed
# (deploy.sh, 2026-08-26) a single deploy can legitimately hold for up to FLEET_DRAIN_MAX_S
# (default 1800s) waiting for in-flight agent passes to finish. The early-exit below cannot
# stop the pile-up on its own: the state file is written only on SUCCESS, so while a deploy is
# still draining, LAST_DEPLOYED is stale and every subsequent tick sees "main moved" and starts
# ANOTHER deploy. Observed live within minutes of the drain shipping -- two concurrent
# deploy.sh processes, the second reporting a clear drain and heading for a cutover while the
# first still held. Two deploys renaming the same containers is precisely the mid-cutover race
# #92 had to add automatic rollback for.
#
# flock over a pidfile/mkdir: the kernel releases it when the process dies, so a killed or
# crashed deploy cannot wedge every future tick behind a stale lock. -n = fail immediately
# rather than queueing, because a queued deploy is just a slower duplicate of the one already
# running -- the next 5-minute tick is the retry.
LOCKFILE="$HOME/.cache/fleet-kit/auto_deploy.lock.${INSTANCE_KEY}"
mkdir -p "$(dirname "$LOCKFILE")"
exec 9>"$LOCKFILE"
if command -v flock >/dev/null 2>&1; then
    if ! flock -n 9; then
        # Quiet by default: with a 30-minute drain this is the EXPECTED state for five ticks
        # out of six, and logging each one would bury the real deploy lines in noise.
        #
        # BUT silence is exactly what hid the worst bug this lock has caused. On 2026-08-26 the
        # fd leaked into `podman run`, whose helpers (conmon, slirp4netns) live as long as the
        # CONTAINER -- so the running container held the lock, every tick took this branch, and
        # the deploy pipeline was DEAD for ~2 hours with nothing in the log. deploy.sh now
        # closes the fd (`9>&-`), and this alarm is the backstop: a lock held longer than any
        # legitimate deploy is STALE, and that must be loud even though a held lock normally
        # is not. Same lesson as #93/#102 -- the failure that says nothing is the expensive one.
        held_by="$(lsof -t "$LOCKFILE" 2>/dev/null | head -1)"
        held_age="$(ps -o etimes= -p "${held_by:-0}" 2>/dev/null | tr -d ' ')"
        if [ -n "$held_age" ] && [ "$held_age" -gt "$(( ${FLEET_DRAIN_MAX_S:-1800} + 900 ))" ]; then
            log "STALE LOCK: pid $held_by has held $LOCKFILE for ${held_age}s, longer than any real deploy -- DEPLOYS ARE BLOCKED. If that pid is a container helper (conmon/slirp4netns), the flock fd leaked into podman run; deploy.sh must close it with 9>&-."
        fi
        exit 0
    fi
fi

# Never deploy over a dirty checkout -- a local uncommitted edit (a live-patch hotfix, say)
# silently getting stashed/blown away by a pull is exactly the kind of "healed silently" this
# kit's own persona_law.md warns against. Loud stop, not a guess.
if [ -n "$(git status --porcelain)" ]; then
  log "ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand."
  exit 1
fi

git fetch origin main -q
REMOTE_SHA="$(git rev-parse origin/main)"
LOCAL_SHA="$(git rev-parse HEAD)"
LAST_DEPLOYED="$(cat "$STATE" 2>/dev/null || echo "")"

if [ "$REMOTE_SHA" = "$LOCAL_SHA" ] && [ "$REMOTE_SHA" = "$LAST_DEPLOYED" ]; then
  exit 0  # quiet no-op tick -- nothing moved, nothing to log
fi

log "main moved: local=$LOCAL_SHA remote=$REMOTE_SHA -- pulling + deploying"
# --ff-only, not a plain pull: this host checkout should never have local commits of its own
# (it's a deploy target, not a dev workspace) -- if it ever diverges, that's a "stop and look",
# not something to auto-merge/rebase past. Same "loud stop over a guess" rule as the dirty-tree
# check above.
if ! git merge-base --is-ancestor "$LOCAL_SHA" "$REMOTE_SHA"; then
  log "ABORT: local HEAD is not an ancestor of origin/main -- host checkout has diverged. Resolve by hand, not auto-merged."
  exit 1
fi
git pull --ff-only origin main -q

if FLEET_INSTANCE_DIR="${FLEET_INSTANCE_DIR:?set FLEET_INSTANCE_DIR}" bash "$KIT_DIR/scripts/deploy.sh" >> "$LOG" 2>&1; then
  echo "$REMOTE_SHA" > "$STATE"
  log "deploy OK at $REMOTE_SHA"
else
  log "DEPLOY FAILED at $REMOTE_SHA -- deploy.sh's own rollback already ran (blue untouched); see $LOG above for detail. NOT recording as last-deployed, will retry next tick."
  exit 1
fi
