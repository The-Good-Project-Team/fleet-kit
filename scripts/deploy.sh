#!/bin/bash
# deploy.sh -- blue-green deploy for one fleet-kit instance's podman container.
#
# Reif, 2026-08-22: "submit a pr, it merges, goes green, restarts server cleanly, to green, if
# stable, go blue." The manual version of this (rebuild image, recreate container) is exactly
# what forced the-fixer's cron-interval fix to sit merged-but-not-live for hours: `podman
# restart`/`podman start` re-runs the EXISTING container's already-baked-in filesystem layers --
# a `podman build` that retags the same image name does not reach a container created before
# the rebuild. The only way to pick up new source is to CREATE a new container from the new
# image, which is exactly the risk blue-green is for: don't take the only running instance down
# to find out the new image is broken.
#
# THE SHAPE: build the new image, start it standalone under its OWN name on ALT ports (never
# touching the live container or ports until health is proven), health-check it for real, then
# cut over by RENAMING (blue -> a timestamped retired name, green -> the real name) rather than
# recreating anything under the live name -- renaming is what keeps blue's container object
# alive and startable by name at every step, so a failure at ANY point has one fixed recovery:
# the retired name is always known, blue is only ever stopped, never removed or recreated.
#
# WHY NOT AN ACTUAL PROXY-FRONTED TRAFFIC SHIFT: this kit exposes two long-lived stdlib HTTP
# servers (fleet_view_server.py, webhook_receiver.py), not a pool of stateless request workers
# -- there is no load balancer in front to shift traffic gradually, and adding one is real
# infra this one pod does not have today. This script gives the two things that actually matter
# for a solo operator's box: a new build is proven healthy BEFORE it takes over the real ports
# (never a blind swap), and the previous good build is one command away if it goes bad after
# cutover (--rollback below), not a rebuild-from-scratch recovery.
#
# Usage: FLEET_INSTANCE_DIR=/home/ubuntu/fleet-kit/instances/nonprofit-atlas \
#          bash scripts/deploy.sh [--rollback]
set -euo pipefail

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTANCE_DIR="${FLEET_INSTANCE_DIR:?set FLEET_INSTANCE_DIR -- e.g. /home/ubuntu/fleet-kit/instances/nonprofit-atlas}"

# Source the instance's fleet.env for FLEET_ACCOUNTS (run_args() below needs it to mount every
# pool account, not just primary -- see run_args()'s own header). auto_deploy.sh, the usual
# caller, never sources fleet.env itself -- confirmed live 2026-08-25: PR #54's mount-every-
# account fix landed in run_args() but the very next real auto_deploy tick still only mounted
# claude-primary, because FLEET_ACCOUNTS was unset at deploy time and the loop fell back to
# its "primary" default. Sourcing it HERE makes deploy.sh self-contained regardless of caller,
# same discipline run_agent_pass.sh/run_member.sh already apply (set -a/+a -- a plain `.` only
# sets local shell vars, invisible to anything this script itself execs).
[ -f "$INSTANCE_DIR/fleet.env" ] && { set -a; . "$INSTANCE_DIR/fleet.env"; set +a; }

CONTAINER="${FLEET_CONTAINER_NAME:-philanthropy}"
RETIRED_MARKER="${CONTAINER}-retired"  # fixed name: the most recent stopped-but-known-good build
IMAGE="${FLEET_IMAGE_NAME:-fleet-kit:latest}"
VIEW_PORT="${FLEET_VIEW_PORT:-8561}"
WEBHOOK_PORT="${FLEET_WEBHOOK_PORT:-8562}"
# Alt ports for the green candidate while it's being proven -- arbitrary but fixed, so a stray
# process from a prior failed deploy is easy to spot (`podman ps` shows these exact numbers).
GREEN_VIEW_PORT="${FLEET_GREEN_VIEW_PORT:-8571}"
GREEN_WEBHOOK_PORT="${FLEET_GREEN_WEBHOOK_PORT:-8572}"
HEALTH_TIMEOUT_S="${FLEET_HEALTH_TIMEOUT_S:-30}"
GH_TOKEN="${GH_TOKEN:-$(gh auth token 2>/dev/null || true)}"

log() { echo "[deploy $(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }

# One place both the green candidate and the real cutover build their `podman run` args from --
# duplicating this list between call sites is exactly how a mount silently drifts between "what
# we tested" and "what we shipped" (the same defect class as the-fixer's own opus/sonnet drift).
#
# CLAUDE ACCOUNT MOUNTS: looped over FLEET_ACCOUNTS (from fleet.env, same list account_pool.sh
# reads) rather than hardcoded to just "primary" -- confirmed live, 2026-08-25: this function
# only ever mounted .claude-primary, so claude-reif's credentials lived in the CONTAINER's
# writable layer only, not a host bind mount. Every deploy (a real podman run under a NEW
# container object, per this file's own header) silently dropped claude-reif back to logged-out,
# while primary survived untouched -- account_pool.sh then correctly saw only one usable
# account and nobody could tell why the pool "lost" claude-reif after every deploy. A host dir
# per account, created here if missing, closes that -- any account in FLEET_ACCOUNTS now
# persists across every future deploy the same way primary always did.
run_args() {
    local name="$1" view_port="$2" webhook_port="$3"
    local account_mounts=()
    for acct in ${FLEET_ACCOUNTS:-primary}; do
        mkdir -p "/home/ubuntu/.claude-$acct"
        account_mounts+=(-v "/home/ubuntu/.claude-$acct:/root/.claude-$acct")
    done
    echo -d --name "$name" \
        -e FLEET_REPO_URL=https://github.com/The-Good-Project-Team/nonprofit-atlas.git \
        -e FLEET_VIEW_PORT="$view_port" \
        -e GH_TOKEN="$GH_TOKEN" \
        -e FLEET_ENV_FILE=/fleet-kit/fleet.env \
        -e FLEET_WEBHOOK_PORT="$webhook_port" \
        -e FLEET_REPO=/repo \
        -v "$INSTANCE_DIR/repo:/repo" \
        -v "$INSTANCE_DIR/logs:/var/log/fleet-kit" \
        -v "$INSTANCE_DIR/fleet.env:/fleet-kit/fleet.env" \
        -v "$INSTANCE_DIR/webhook_secret:/fleet-kit/.webhook_secret" \
        "${account_mounts[@]}" \
        -p "$view_port:$view_port" -p "$webhook_port:$webhook_port" \
        "$IMAGE" cron-foreground
}

health_check() {
    local view_port="$1" deadline=$(( $(date +%s) + HEALTH_TIMEOUT_S ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        # fleet_view_server's / -- 200 means the stdlib HTTP server is actually accepting
        # connections and rendering, not just "the process exists" (a hung import or a crash
        # loop still leaves a PID in `podman ps` for a few seconds).
        if [ "$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://localhost:$view_port/" 2>/dev/null)" = "200" ]; then
            return 0
        fi
        sleep 1
    done
    return 1
}

exists() { podman container exists "$1"; }
running() { [ "$(podman inspect "$1" --format '{{.State.Running}}' 2>/dev/null)" = "true" ]; }

do_rollback() {
    log "ROLLBACK: green (if any) stops, retired build (if any) takes back the live name+ports"
    if exists "${CONTAINER}-green"; then
        podman stop -t 5 "${CONTAINER}-green" >/dev/null 2>&1 || true
        podman rm -f "${CONTAINER}-green" >/dev/null 2>&1 || true
    fi
    if exists "$CONTAINER" && running "$CONTAINER"; then
        log "live container is already up under $CONTAINER -- nothing to restore"
        exit 1
    fi
    if exists "$CONTAINER"; then
        podman start "$CONTAINER"
        log "restarted $CONTAINER (it existed but was stopped)"
        exit 1
    fi
    if exists "$RETIRED_MARKER"; then
        podman rename "$RETIRED_MARKER" "$CONTAINER"
        podman start "$CONTAINER"
        log "restored $RETIRED_MARKER -> $CONTAINER and started it"
        exit 1
    fi
    log "FATAL: no live $CONTAINER and no $RETIRED_MARKER to restore from -- recreate by hand"
    exit 2
}

if [ "${1:-}" = "--rollback" ]; then
    log "manual rollback requested"
    do_rollback
fi

log "building $IMAGE from $KIT_DIR"
podman build -t "$IMAGE" "$KIT_DIR"

log "starting green candidate (${CONTAINER}-green) on alt ports $GREEN_VIEW_PORT/$GREEN_WEBHOOK_PORT"
if exists "${CONTAINER}-green"; then
    podman rm -f "${CONTAINER}-green" >/dev/null 2>&1 || true
fi
# shellcheck disable=SC2046
podman run $(run_args "${CONTAINER}-green" "$GREEN_VIEW_PORT" "$GREEN_WEBHOOK_PORT") >/dev/null

log "health-checking green (up to ${HEALTH_TIMEOUT_S}s)"
if ! health_check "$GREEN_VIEW_PORT"; then
    log "FAILED: green never answered http://localhost:$GREEN_VIEW_PORT/ within ${HEALTH_TIMEOUT_S}s"
    log "leaving ${CONTAINER}-green stopped-not-removed for inspection; blue was never touched"
    podman stop -t 5 "${CONTAINER}-green" >/dev/null 2>&1 || true
    exit 1
fi
log "green is healthy on alt ports"

# Cutover is pure RENAMES from here -- no container is ever recreated under the live name, so
# every step has a container that still exists and can be started if the NEXT step fails.
log "cutover: blue ($CONTAINER) -> $RETIRED_MARKER, green -> $CONTAINER"
if exists "$RETIRED_MARKER"; then
    # An older retired build left over from a previous deploy -- it already lost its shot at
    # being the rollback target the moment THIS deploy's blue took over successfully last time.
    podman rm -f "$RETIRED_MARKER" >/dev/null 2>&1 || true
fi
# FROM HERE UNTIL THE HEALTH CHECK, A FAILURE LEAVES NO LIVE CONTAINER. Confirmed live
# 2026-08-26: `podman rename green -> $CONTAINER` failed after blue had already been renamed
# to $RETIRED_MARKER, and the script simply exited -- prod served nothing on 8420 for ~2min,
# and `--rollback` could not fix it either because do_rollback's first branches look for a
# $CONTAINER that no longer existed. do_rollback ITSELF was always correct (it renames
# $RETIRED_MARKER back); nothing ever called it on a mid-cutover failure. This trap does.
cutover_failed() {
    local rc=$?
    [ "$rc" -eq 0 ] && return 0
    log "FAILED mid-cutover (rc=$rc) -- restoring the previous build rather than leaving prod dark"
    trap - ERR EXIT
    do_rollback
}
trap cutover_failed ERR EXIT
set -e

if exists "$CONTAINER"; then
    podman stop -t 10 "$CONTAINER" >/dev/null 2>&1 || true
    podman rename "$CONTAINER" "$RETIRED_MARKER"
fi
podman stop -t 5 "${CONTAINER}-green" >/dev/null 2>&1 || true
podman rename "${CONTAINER}-green" "$CONTAINER"
# Renaming doesn't change bound ports -- green was created bound to the ALT ports, so it must
# be recreated (not just started) to bind the REAL ports. This is the one unavoidable
# recreate in the whole flow; it happens only after health already passed on the alt ports.
podman rm -f "$CONTAINER" >/dev/null 2>&1
# shellcheck disable=SC2046
podman run $(run_args "$CONTAINER" "$VIEW_PORT" "$WEBHOOK_PORT") >/dev/null

log "health-checking the real cutover (up to ${HEALTH_TIMEOUT_S}s)"
# Past the rename window: the explicit rollback below handles a health failure with more care
# than the generic trap (it preserves the broken build for inspection), so hand off to it.
trap - ERR EXIT
set +e
if ! health_check "$VIEW_PORT"; then
    log "FAILED after cutover -- rolling back to $RETIRED_MARKER"
    podman stop -t 5 "$CONTAINER" >/dev/null 2>&1 || true
    podman rename "$CONTAINER" "${CONTAINER}-broken-$(date +%s)"
    podman rename "$RETIRED_MARKER" "$CONTAINER"
    podman start "$CONTAINER"
    log "ROLLED BACK: $CONTAINER restored to the pre-deploy build and running"
    exit 1
fi

log "DEPLOYED: $CONTAINER live on $VIEW_PORT/$WEBHOOK_PORT, running $(podman exec "$CONTAINER" sh -c 'cd /fleet-kit && git log -1 --oneline' 2>/dev/null)"
log "previous build kept stopped as $RETIRED_MARKER -- roll back any time with: bash $0 --rollback"
