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

# gh#278: source the instance's fleet.env so host-side dials (FLEET_AUTO_DEPLOY_SELF_HEAL below)
# can be tuned there like every other instance-scoped setting, instead of needing a crontab-line
# edit. Same save/source/restore discipline deploy.sh already uses (scripts/deploy.sh:58) for the
# identical reason: fleet.env's own FLEET_LOG_DIR is CONTAINER-scoped (/var/log/fleet-kit) and
# would silently clobber the HOST-scoped value the cron caller already exported -- the exact
# gh#196 incident deploy.sh's own header documents. $LOG/$LOG_DIR/$INSTANCE_KEY/$STATE/$LOCKFILE
# above are already resolved from the caller's values, so this can't move where THIS tick reads
# or writes its own state; it only protects what auto_deploy.sh hands to deploy.sh as a child
# process below. FLEET_CONTAINER_NAME is saved/restored for the same reason as FLEET_LOG_DIR: if
# it ever diverged from the cron-exported value (e.g. an instance's fleet.env hand-edited without
# re-running up.sh), the deploy.sh child would target a different container than the one
# INSTANCE_KEY/STATE/LOCKFILE above were computed against.
CALLER_LOG_ENV="${FLEET_LOG_DIR:-}"
CALLER_CONTAINER_NAME="${FLEET_CONTAINER_NAME:-}"
[ -n "${FLEET_INSTANCE_DIR:-}" ] && [ -f "$FLEET_INSTANCE_DIR/fleet.env" ] && { set -a; . "$FLEET_INSTANCE_DIR/fleet.env"; set +a; } || true
FLEET_LOG_DIR="$CALLER_LOG_ENV"
FLEET_CONTAINER_NAME="$CALLER_CONTAINER_NAME"

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
#
# gh#278 (2026-09-03, 60+ ticks / 5h+ stall): every prior incident on this thread hit this
# exact ABORT but none could say WHAT was dirty, because the log line never captured it --
# and every pass diagnosing it lacked host access to run `git status` itself. That made this
# guard's own self-heal question ("is a reset safe here?") unanswerable from any automated
# pass, unlike the diverged-HEAD guard below which already has FLEET_AUTO_DEPLOY_SELF_HEAL.
# This does not add auto-recovery -- the persona_law warning above still applies, a real
# hotfix must never be auto-discarded -- it only makes the NEXT occurrence diagnosable without
# needing a human to SSH in first.
PORCELAIN="$(git status --porcelain)"
if [ -n "$PORCELAIN" ]; then
  log "ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand. Dirty entries: $(echo "$PORCELAIN" | tr '\n' '|')"
  exit 1
fi

# gh#68/gh#255: this exact .git directory can also be touched by git_pull_guard.sh, an
# unrelated unlocked cron tick running INSIDE the container against the same checkout when this
# box self-hosts fleet-kit (i.e. $FLEET_REPO's bind-mount happens to BE $KIT_DIR) -- confirmed
# live as matching SHA pairs across gitpull.log and this script's own race errors. Locking on
# the .git dir itself, not a fleet-kit-specific path, means this serializes against that guard
# automatically whenever they really do share a directory, and costs nothing (an uncontended,
# instantly-released lock) when they don't. Released right after the pull below -- no reason to
# hold it through the build/podman steps that follow.
GIT_LOCKFILE="$KIT_DIR/.git/fleet_pull.lock"
exec 8>"$GIT_LOCKFILE"
if command -v flock >/dev/null 2>&1; then
    flock 8
fi

git fetch origin main -q
REMOTE_SHA="$(git rev-parse origin/main)"
LOCAL_SHA="$(git rev-parse HEAD)"
LAST_DEPLOYED="$(cat "$STATE" 2>/dev/null || echo "")"

if [ "$REMOTE_SHA" = "$LOCAL_SHA" ] && [ "$REMOTE_SHA" = "$LAST_DEPLOYED" ]; then
  exit 0  # quiet no-op tick -- nothing moved, nothing to log
fi

# gh#619: coalesce deploys. Every deploy cordons the fleet (FLEET_ENABLED=false) while in-flight
# passes drain -- median 270s, p90 990s, ~18 times a day -- so main moving every few minutes cost
# the fleet 4-17% of every day with no new passes starting. A runtime move that lands inside
# FLEET_DEPLOY_MIN_INTERVAL_S of the last SUCCESSFUL deploy is deferred (logged once), and the
# first tick after the window deploys everything that landed meanwhile in one drain. A failed
# deploy does not stamp the window, so its retry is as immediate as before. Manual deploy.sh is
# untouched. Set FLEET_DEPLOY_MIN_INTERVAL_S=0 in fleet.env to get the old deploy-every-move.
DEPLOY_MIN_INTERVAL_S="${FLEET_DEPLOY_MIN_INTERVAL_S:-7200}"
DEPLOYED_AT_FILE="$STATE.deployed_at"
DEFER_FLAG="$STATE.deferring"
LAST_DEPLOYED_AT="$(cat "$DEPLOYED_AT_FILE" 2>/dev/null || echo "")"
NOW_S="$(date +%s)"
if [[ "$LAST_DEPLOYED_AT" =~ ^[0-9]+$ ]] && [ $((NOW_S - LAST_DEPLOYED_AT)) -lt "$DEPLOY_MIN_INTERVAL_S" ]; then
  if [ ! -f "$DEFER_FLAG" ]; then
    log "main moved: remote=$REMOTE_SHA only $((NOW_S - LAST_DEPLOYED_AT))s after the last deploy -- COALESCING: deferring until FLEET_DEPLOY_MIN_INTERVAL_S=${DEPLOY_MIN_INTERVAL_S}s has elapsed; anything else that lands meanwhile rides the same deploy (gh#619)"
    : > "$DEFER_FLAG"
  fi
  exit 0
fi
rm -f "$DEFER_FLAG"
log "main moved: local=$LOCAL_SHA remote=$REMOTE_SHA -- pulling + deploying"
# --ff-only, not a plain pull: this host checkout should never have local commits of its own
# (it's a deploy target, not a dev workspace) -- if it ever diverges, that's a "stop and look",
# not something to auto-merge/rebase past. Same "loud stop over a guess" rule as the dirty-tree
# check above.
if ! git merge-base --is-ancestor "$LOCAL_SHA" "$REMOTE_SHA"; then
  # gh#278: 3 confirmed occurrences (gh#245, gh#275, gh#278 itself) where the actual cause was a
  # squash-merged/rebased branch tip whose TREE already matched origin/main byte-for-byte -- not
  # a real divergence, just a stale ref (this repo squash-merges every PR, so a stranded branch
  # tip can never become an ancestor of main through any future merge; see README's "Why the box
  # silently falls behind"). The manual recovery for exactly this case is already documented
  # there: confirm content-identical, `git checkout main`, pull.
  #
  # Self-heal is opt-in and OFF by default: gh#278's own PRD flagged "is an automated
  # `git reset --hard` on the deploy host acceptable at all" as an explicit UNKNOWN needing a
  # human sign-off this script can't give itself -- flipping FLEET_AUTO_DEPLOY_SELF_HEAL on in
  # fleet.env IS that sign-off, not a default this pass should guess at. When on, it still only
  # fires if the working tree is content-identical to origin/main; a genuine divergence always
  # falls through to the unchanged ABORT below, byte-for-byte.
  if [ "${FLEET_AUTO_DEPLOY_SELF_HEAL:-false}" = "true" ] && git diff --quiet origin/main; then
    log "SELF-HEAL: local HEAD diverged but tree matches origin/main -- resetting onto origin/main"
    git checkout main -q
    git reset --hard origin/main -q
    log "SELF-HEAL: reset complete, proceeding into normal deploy"
  else
    # gh#372 (Part C4): the line above told an operator nothing about what to resolve -- no
    # branch, no SHA, no merge-base -- so #278 had to close with "why does the checkout keep
    # ending up on a feature-branch tip" explicitly UNKNOWN, unanswerable from a log line that
    # never recorded which tip. This appends that detail rather than replacing the sentence, so
    # every prior ABORT-matching check (selftest, auto_deploy_race_check.sh) still matches.
    DIVERGED_BRANCH="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo "(detached HEAD)")"
    DIVERGED_MERGE_BASE="$(git merge-base "$LOCAL_SHA" "$REMOTE_SHA" 2>/dev/null || echo "")"
    if [ -n "$DIVERGED_MERGE_BASE" ]; then
      DIVERGED_AHEAD="$(git rev-list --count "$DIVERGED_MERGE_BASE".."$LOCAL_SHA" 2>/dev/null || echo "?")"
      DIVERGED_BEHIND="$(git rev-list --count "$DIVERGED_MERGE_BASE".."$REMOTE_SHA" 2>/dev/null || echo "?")"
    else
      DIVERGED_AHEAD="?"
      DIVERGED_BEHIND="?"
    fi
    log "ABORT: local HEAD is not an ancestor of origin/main -- host checkout has diverged. Resolve by hand, not auto-merged. branch=$DIVERGED_BRANCH local=${LOCAL_SHA:0:7} remote=${REMOTE_SHA:0:7} ahead=$DIVERGED_AHEAD behind=$DIVERGED_BEHIND"
    exit 1
  fi
fi
git pull --ff-only origin main -q
exec 8>&-  # release the shared git lock before the (potentially half-hour) deploy below

if FLEET_INSTANCE_DIR="${FLEET_INSTANCE_DIR:?set FLEET_INSTANCE_DIR}" bash "$KIT_DIR/scripts/deploy.sh" >> "$LOG" 2>&1; then
  echo "$REMOTE_SHA" > "$STATE"
  date +%s > "$DEPLOYED_AT_FILE"
  log "deploy OK at $REMOTE_SHA"
else
  log "DEPLOY FAILED at $REMOTE_SHA -- deploy.sh's own rollback already ran (blue untouched); see $LOG above for detail. NOT recording as last-deployed, will retry next tick."
  exit 1
fi
