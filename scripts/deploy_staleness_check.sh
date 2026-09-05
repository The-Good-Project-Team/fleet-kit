#!/bin/bash
# deploy_staleness_check.sh -- independent gate: is the LIVE /fleet-kit tree actually the code
# deploy.sh thinks it shipped, or has delivery gone silently dark?
#
# gh#201: deploy.sh/auto_deploy.sh only ever push forward blind -- nothing compared the live
# tree against the SHA it should reflect, and nothing alerted when they diverged. Confirmed
# live 2026-08-29: /fleet-kit sat unsynced for 5h50m while 8 commits merged to main, and the
# only reason anyone noticed was a human-triggered nerd pass doing manual `stat`/`diff`
# forensics -- the exact "fleet that looks busy" failure the README calls out. This is that
# check, on its own schedule, so it catches the case where deploy.sh never ran at all.
#
# WHY THIS RUNS INSIDE THE CONTAINER (entrypoint.sh's crontab), NOT ON THE HOST like
# auto_deploy.sh: auto_deploy.sh needs `podman build`/`podman run`, host-only capabilities.
# This check only needs to compare two SHAs -- one baked into this very image at build time
# (Dockerfile's DEPLOY_SHA arg -> /fleet-kit/.deploy_sha, see deploy.sh), one read from
# GitHub's API for main's current HEAD -- both reachable from inside the container over the
# network it already has. No podman/docker-in-docker needed.
#
# WHY NOT A MANIFEST OF INDIVIDUAL FILE HASHES (the issue's other suggested strategy): the
# issue named that as the fallback for when "/fleet-kit is not always a git checkout on every
# instance". It never is, on ANY instance built from this Dockerfile -- COPY . /fleet-kit with
# .dockerignore excluding .git/ is unconditional, not box-specific. A single baked SHA is exact
# (the image content is a deterministic function of the git tree it was built from) and far
# simpler than tracking a per-file hash set that has to be kept in sync with what actually
# ships.
#
# UNKNOWN (per marie's PRD comment on gh#201, left for a human, not guessed here): the exact
# staleness budget. FLEET_DEPLOY_STALENESS_BUDGET_S defaults to 4h (14400s), the midpoint of
# the issue's own suggested 2.5-5h range -- override in fleet.env once a human picks a number.
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "${FLEET_ENV_FILE:-$KIT_DIR/fleet.env}" ] && { set -a; . "${FLEET_ENV_FILE:-$KIT_DIR/fleet.env}"; set +a; } || true

LOG_DIR="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}"
mkdir -p "$LOG_DIR"
# Same durable file gh#196 established for deploy.sh itself -- one place a human or a future
# fleet_view tile greps for anything deploy-shaped, not a new log to remember to check.
DEPLOY_LOG="$LOG_DIR/deploy.log"

# gh#381: healthy-silent (in-sync no-op tick) and dead-silent (process never got this far) were
# byte-identical -- nothing distinguished "checked, all fine" from "never ran at all". Write this
# unconditionally, before any exit 0 branch below, so every invocation leaves a positive trace
# regardless of which path it takes afterward (including a `gh api` failure/timeout further down).
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$LOG_DIR/deploy_staleness_check.lastrun"

log() {
    local line="[deploy-staleness $(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"
    echo "$line"
    echo "$line" >> "$DEPLOY_LOG"
}

STALENESS_BUDGET_S="${FLEET_DEPLOY_STALENESS_BUDGET_S:-14400}"

# Same derivation self_improve_score.sh already uses for fleet-kit's own repo slug (gh#178) --
# KIT_DIR in production is /fleet-kit, a vendored copy with no .git, so KIT_REPO_SLUG must come
# from fleet.env there. Deliberately not factored into a shared lib: every script in this repo
# that needs this derives it inline (self_improve_score.sh is the only other caller), so a new
# shared file for one more caller would be the new subsystem this issue's PRD explicitly says
# not to add.
_repo_slug() {
    local url
    url="$(git -C "$1" remote get-url origin 2>/dev/null)"
    if [ -z "$url" ]; then
        local first_remote
        first_remote="$(git -C "$1" remote 2>/dev/null | head -1)"
        [ -n "$first_remote" ] && url="$(git -C "$1" remote get-url "$first_remote" 2>/dev/null)"
    fi
    printf '%s' "$url" | sed -E 's#^git@github\.com:##; s#^https://github\.com/##; s#\.git$##'
}
KIT_REPO_SLUG="${KIT_REPO_SLUG:-$(_repo_slug "$KIT_DIR")}"
if [ -z "$KIT_REPO_SLUG" ]; then
    log "cannot determine fleet-kit's own repo slug (no .git at $KIT_DIR and KIT_REPO_SLUG not set in fleet.env) -- staleness check skipped this tick"
    exit 0
fi

DEPLOYED_SHA="unknown"
[ -f "$KIT_DIR/.deploy_sha" ] && DEPLOYED_SHA="$(cat "$KIT_DIR/.deploy_sha")"
if [ "$DEPLOYED_SHA" = "unknown" ] || [ -z "$DEPLOYED_SHA" ]; then
    log "no .deploy_sha baked into this image -- built before gh#201, or DEPLOY_SHA resolution failed at build time. Cannot compute staleness until the next deploy bakes one in."
    exit 0
fi

# timeout wrapper (2026-09-04, dumbledore rot hunt): confirmed live via deploy.log/
# deploy_staleness_check.log both going silent for 6 consecutive hourly ticks
# (2026-09-03 16:57-21:57 UTC) during the exact gh#278/#275 host stall this check exists to
# catch -- cron kept firing (other members' logs updated normally the whole window) but this
# script produced zero output, including none of its own early-exit log() lines, which only
# happens if the process itself never returned. Neither `gh api` call here had a timeout, so a
# slow/hung network response (plausible under the same host-level disruption gh#278 documents)
# can wedge the ONE independent watchdog for this class of failure for hours with no trace.
MAIN_SHA="$(timeout 25s gh api "repos/$KIT_REPO_SLUG/commits/main" --jq .sha 2>/dev/null || echo "")"
if [ -z "$MAIN_SHA" ]; then
    log "could not reach GitHub API for $KIT_REPO_SLUG's main HEAD -- staleness check skipped this tick"
    exit 0
fi

if [ "$DEPLOYED_SHA" = "$MAIN_SHA" ]; then
    # In sync: stay quiet, same "no news is good news" convention auto_deploy.sh uses for a
    # no-op tick -- logging every in-sync tick would bury the STALE lines this check exists to
    # surface.
    exit 0
fi

# Diverged. The AGE of the oldest commit merged into main after DEPLOYED_SHA is how long
# delivery has actually been dark -- the same measure the issue's own evidence used by hand
# (oldest un-synced commit's merge time vs wall clock). The compare API returns commits
# strictly after base, oldest first, so [0] is exactly that commit.
OLDEST_UNDEPLOYED_DATE="$(timeout 25s gh api "repos/$KIT_REPO_SLUG/compare/$DEPLOYED_SHA...$MAIN_SHA" --jq '.commits[0].commit.committer.date' 2>/dev/null || echo "")"
if [ -z "$OLDEST_UNDEPLOYED_DATE" ]; then
    log "DRIFT: live sha=$DEPLOYED_SHA differs from main=$MAIN_SHA but the compare could not be resolved (force-push/rebase on main?) -- duration unknown, cannot confirm against the ${STALENESS_BUDGET_S}s budget"
    exit 0
fi

OLDEST_TS="$(date -u -d "$OLDEST_UNDEPLOYED_DATE" +%s 2>/dev/null || echo 0)"
NOW_TS="$(date -u +%s)"
STALE_S=$(( NOW_TS - OLDEST_TS ))

if [ "$STALE_S" -gt "$STALENESS_BUDGET_S" ]; then
    log "STALE: live /fleet-kit at $DEPLOYED_SHA, main at $MAIN_SHA -- oldest undeployed commit merged $((STALE_S / 3600))h$(((STALE_S % 3600) / 60))m ago, past the ${STALENESS_BUDGET_S}s budget. deploy.sh has not landed a build in this window -- check auto_deploy.sh/gh#140's scheduler and gh#189's push path."
fi
