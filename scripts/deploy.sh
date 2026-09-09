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
# `|| true` for the same set -e reason as the drain guard below: this is at TOP LEVEL, so a
# false test (no fleet.env yet -- a first deploy, or an instance that keeps config elsewhere)
# returns non-zero and aborts the whole script before it has logged a single line.
#
# FLEET_LOG_DIR is saved/restored around this source on purpose: every instance's fleet.env
# sets it to a CONTAINER-internal path (/var/log/fleet-kit -- what run_member.sh and every
# member script see once they're running inside), but this script runs on the HOST and needs
# its own, different, host-filesystem log dir (see LOG_DIR below). Sourcing fleet.env
# unguarded overwrites whatever the caller (auto_deploy.sh, a human, cron) already exported
# with that container path -- confirmed live 2026-08-29: `mkdir -p "$LOG_DIR"` then tries to
# create /var/log/fleet-kit ON THE HOST, which is root:syslog-owned and not writable by the
# operator account, so deploy.sh (and every downstream auto_deploy.sh tick) failed at its very
# first line for both instances simultaneously the moment fleet.env's own FLEET_LOG_DIR value
# was ever allowed to reach here.
#
# FLEET_CONTAINER_NAME is restored the same way, but only when the caller actually had one set
# (gh#278): auto_deploy.sh, the usual caller, computes its own INSTANCE_KEY/STATE/LOCKFILE from
# the cron-exported FLEET_CONTAINER_NAME before ever calling this script, and restores that same
# value around its own fleet.env source for exactly this reason (auto_deploy.sh:52-60). If this
# script's unguarded source were allowed to overwrite it with fleet.env's own value in that path,
# an instance whose fleet.env was hand-edited (e.g. container renamed) without re-running up.sh
# would compute CONTAINER below from the NEW name while auto_deploy.sh's state/lock bookkeeping
# stayed keyed to the OLD one -- the tick would rename/restart a container under a different name
# than the one it believed it was managing.
# Unlike FLEET_LOG_DIR, this restore is conditional: rename_account.sh documents running this
# script directly with only FLEET_INSTANCE_DIR set (`FLEET_INSTANCE_DIR=... bash scripts/deploy.sh`,
# rename_account.sh:119), relying on fleet.env to supply FLEET_CONTAINER_NAME. An unconditional
# restore would blank that out and silently fall back to the "philanthropy" default below.
CALLER_LOG_ENV="${FLEET_LOG_DIR:-}"
CALLER_CONTAINER_NAME="${FLEET_CONTAINER_NAME:-}"
[ -f "$INSTANCE_DIR/fleet.env" ] && { set -a; . "$INSTANCE_DIR/fleet.env"; set +a; } || true
FLEET_LOG_DIR="$CALLER_LOG_ENV"
[ -n "$CALLER_CONTAINER_NAME" ] && FLEET_CONTAINER_NAME="$CALLER_CONTAINER_NAME"

# The target repo this instance's agents work. Baked in as a literal until 2026-08-26, which
# made deploy.sh the one file that could not serve a second instance: up.sh correctly passes
# the operator's own FLEET_REPO_URL on first launch, but EVERY subsequent deploy re-created the
# container with `-e FLEET_REPO_URL=...nonprofit-atlas.git` regardless. A second fleet would
# have come up pointed at the right repo, worked fine, and then silently switched to
# nonprofit-atlas on its first redeploy -- agents filing issues and PRs against someone else's
# repo, with nothing in the logs saying the target had changed. Required, not defaulted: a
# wrong repo is not something to guess at.
FLEET_REPO_URL="${FLEET_REPO_URL:?set FLEET_REPO_URL (the repo this fleet works) in fleet.env}"
# Host dir holding the per-account .claude-<acct> credential mounts. Was /home/ubuntu, which is
# only correct on this one box; a Mac or any non-ubuntu host silently created the dirs under a
# path nobody looks at and every account came up logged out (the same class of failure the
# account-mount comment below describes). $HOME is the right default for the user running the
# deploy, and stays overridable for an operator who keeps credentials elsewhere.
FLEET_CREDS_DIR="${FLEET_CREDS_DIR:-$HOME}"
CONTAINER="${FLEET_CONTAINER_NAME:-philanthropy}"
RETIRED_MARKER="${CONTAINER}-retired"  # fixed name: the most recent stopped-but-known-good build
IMAGE="${FLEET_IMAGE_NAME:-fleet-kit:latest}"
VIEW_PORT="${FLEET_VIEW_PORT:-8561}"
WEBHOOK_PORT="${FLEET_WEBHOOK_PORT:-8562}"
# Alt ports for the green candidate while it's being proven -- arbitrary but fixed, so a stray
# process from a prior failed deploy is easy to spot (`podman ps` shows these exact numbers).
GREEN_VIEW_PORT="${FLEET_GREEN_VIEW_PORT:-8571}"
GREEN_WEBHOOK_PORT="${FLEET_GREEN_WEBHOOK_PORT:-8572}"
# gh#672: 30s false-negatived a genuinely-healthy green build under real load (2026-09-07,
# load average 35 on 7 cores -- `serving http://0.0.0.0:8420` was already logged when the
# check gave up), which then tore down a container that was still serving in-flight passes
# (fk#671). 120s is the new default; still overridable, but a caller now has to opt INTO a
# shorter window rather than getting one that has already failed once under real load.
HEALTH_TIMEOUT_S="${FLEET_HEALTH_TIMEOUT_S:-120}"
GH_TOKEN="${GH_TOKEN:-$(gh auth token 2>/dev/null || true)}"

# Durable receipt regardless of caller. auto_deploy.sh only captures this script's stdout into
# auto_deploy.log when IT is the one invoking deploy.sh -- a human running deploy.sh directly,
# up.sh, or a future push-based trigger (#189) previously left zero durable record (gh#196).
# Same FLEET_LOG_DIR convention auto_deploy.sh already uses (auto_deploy.sh:28), own filename
# so a direct run doesn't interleave with auto_deploy's own poll-tick log.
LOG_DIR="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}"

# ONE lease ledger for every instance on this box. Each instance keeps its own $INSTANCE_DIR
# for logs/repo/env, but they all draw from a SINGLE maxx account pool, so a reservation
# ledger only one instance can see coordinates nothing (live 2026-09-02: philanthropy and
# fleet-kit-server-fleet each kept a private maxx-leases.json, both sat at `[]` while both
# fleets ran, and maxx_share_ceiling.py's "already-coordinated" reserved_pct never contained
# the other instance's spend). Deliberately OUTSIDE $INSTANCE_DIR -- that is the whole point.
SHARED_LEASE_DIR="${FLEET_LEASE_DIR:-$HOME/.cache/fleet-kit/leases}"
mkdir -p "$SHARED_LEASE_DIR"

# Same shape, one number: every instance publishes just its own FLEET_SHARE_FRACTION here
# (publish_share.sh) so check_share_sum.sh can sum every sibling's share from inside any one
# container (gh#293) -- without bind-mounting the `instances/` parent tree wholesale, which
# would leak every instance's fleet.env (live CLAUDE_CODE_OAUTH_TOKEN/FLEET_MAXX_KEY) into
# every other instance's container. Deliberately OUTSIDE $INSTANCE_DIR, same as the lease dir.
SHARED_SHARE_DIR="${FLEET_SHARE_DIR:-$HOME/.cache/fleet-kit/shares}"
mkdir -p "$SHARED_SHARE_DIR"
mkdir -p "$LOG_DIR"
DEPLOY_LOG="$LOG_DIR/deploy.log"

log() {
    local line="[deploy $(date '+%Y-%m-%d %H:%M:%S %Z')] $*"
    echo "$line"
    echo "$line" >> "$DEPLOY_LOG"
}

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
        mkdir -p "$FLEET_CREDS_DIR/.claude-$acct"
        account_mounts+=(-v "$FLEET_CREDS_DIR/.claude-$acct:/root/.claude-$acct")
    done
    # Analysis credentials (datta/nerd): a read-only mount of the directory holding the
    # service-account JSON that GSC/GA4 need. Optional and skipped when unset, so an instance
    # that does not analyse those lanes is unaffected -- but WITHOUT it a growth or datadog
    # nerd cannot measure its own KPI and files "no credential" forever. :ro because a pass
    # only ever reads these; nothing in the fleet should be able to rewrite a key.
    local analytics_mounts=()
    if [ -n "${FLEET_ANALYTICS_CREDS_DIR:-}" ] && [ -d "$FLEET_ANALYTICS_CREDS_DIR" ]; then
        analytics_mounts+=(-v "$FLEET_ANALYTICS_CREDS_DIR:/fleet-kit/.analytics:ro")
    fi
    # Resend credentials for the messenger (fk#558): the same file fleet_alert.sh reads on the
    # host, mounted read-only at the same path so in-container senders find it. Optional --
    # without it the messenger logs no-credentials and the fleet runs exactly as before.
    local alert_mounts=()
    local alert_env="${FLEET_ALERT_ENV:-/home/ubuntu/.config/maxx/alert.env}"
    if [ -f "$alert_env" ]; then
        alert_mounts+=(-v "$alert_env:/home/ubuntu/.config/maxx/alert.env:ro")
    fi
    # Shared secret store (control_plane.py secrets): one host file every instance reads
    # through the include line at the top of its fleet.env. Mounted read-only at the SAME
    # absolute path it has on the host, so the include line resolves inside and outside the
    # container alike (the alert.env precedent above). Absent file: no mount, no error -- an
    # instance still carrying its own keys in fleet.env keeps working.
    local secret_mounts=()
    local secrets_env="${FLEET_SECRETS_FILE:-$HOME/.config/fleet-kit/secrets.env}"
    if [ -f "$secrets_env" ]; then
        secret_mounts+=(-v "$secrets_env:$secrets_env:ro")
    fi
    echo -d --name "$name" \
        -e FLEET_REPO_URL="$FLEET_REPO_URL" \
        -e FLEET_VIEW_PORT="$view_port" \
        -e GH_TOKEN="$GH_TOKEN" \
        -e FLEET_ENV_FILE=/fleet-kit/fleet.env \
        -e FLEET_WEBHOOK_PORT="$webhook_port" \
        -e FLEET_REPO=/repo \
        -e FLEET_LEASE_DIR=/fleet-kit/leases \
        -e FLEET_SHARE_DIR=/fleet-kit/shares \
        -e FLEET_INSTANCE_NAME="$name" \
        -v "$SHARED_LEASE_DIR:/fleet-kit/leases" \
        -v "$SHARED_SHARE_DIR:/fleet-kit/shares" \
        -v "$INSTANCE_DIR/repo:/repo" \
        -v "$INSTANCE_DIR/logs:/var/log/fleet-kit" \
        -v "$INSTANCE_DIR/fleet.env:/fleet-kit/fleet.env" \
        -v "$INSTANCE_DIR/webhook_secret:/fleet-kit/.webhook_secret" \
        "${account_mounts[@]}" \
        "${analytics_mounts[@]}" \
        "${alert_mounts[@]}" \
        "${secret_mounts[@]}" \
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
    log "FATAL ERROR: no live $CONTAINER and no $RETIRED_MARKER to restore from -- recreate by hand"
    exit 2
}

# --- proxy-fronted rolling cutover (gh#625) ----------------------------------------------
# Reif, 2026-09-07: "our job is to always be deploying." The drain gate below exists because
# the legacy cutover STOPS blue to free the live ports, which kills every pass mid-flight --
# so it cordons the fleet and waits (median 270s, p90 990s, ~18x/day = 4-17% of every day with
# no new passes starting). On a box where caddy already fronts the instance (dino: caddy :9000
# reverse_proxy -> localhost:<view>/<webhook>), none of that is necessary: the new build comes
# up on the OTHER port pair, caddy's upstream is swapped and reloaded (zero downtime), and the
# old container keeps RUNNING with its cron removed until its in-flight passes finish, then a
# detached reaper stops it. No cordon, no drain, no killed pass. Two builds run passes side by
# side for a while -- the same concurrency the fleet already has inside one container, on the
# same lock-serialized fleet.db, lease ledger and shared /repo checkout.
#
# Ports alternate between the A pair (FLEET_VIEW_PORT/FLEET_WEBHOOK_PORT) and the B pair
# (FLEET_GREEN_VIEW_PORT/FLEET_GREEN_WEBHOOK_PORT); $LIVE_PORTS_FILE records which pair is live
# so the next deploy (and any host check that wants the live port) can read it.
#
# Falls back to the legacy stop-and-recreate path when there is no Caddyfile naming this
# instance's live port (a first deploy on a fresh box, a Mac host, the selftest fixture).
PROXY_CADDYFILE="${FLEET_CADDYFILE:-$HOME/Caddyfile}"
LIVE_PORTS_FILE="$INSTANCE_DIR/live_ports"
RETIRED_PORTS_FILE="$INSTANCE_DIR/retired_ports"
RETIRE_MAX_S="${FLEET_RETIRE_MAX_S:-7200}"

live_ports() {
    # "<view> <webhook>" of the container that is live NOW. Before the first proxy-mode deploy
    # there is no record, and the legacy path always left the A pair live.
    local v w
    read -r v w < "$LIVE_PORTS_FILE" 2>/dev/null || true
    [[ "${v:-}" =~ ^[0-9]+$ && "${w:-}" =~ ^[0-9]+$ ]] && { echo "$v $w"; return; }
    echo "$VIEW_PORT $WEBHOOK_PORT"
}

proxy_mode() {
    [ "${FLEET_PROXY_CUTOVER:-1}" = "1" ] || return 1
    [ -f "$PROXY_CADDYFILE" ] || return 1
    local v w; read -r v w < <(live_ports)
    grep -qE "localhost:${v}([^0-9]|$)" "$PROXY_CADDYFILE" || return 1
    command -v caddy >/dev/null 2>&1
}

# caddy_swap <file> <old_view> <old_webhook> <new_view> <new_webhook>: rewrite ONLY this
# instance's two upstreams. Exact-port match ([^0-9]|$) so 8420 never touches 84200 and a
# sibling instance's upstream on the same Caddyfile is left alone. Truncate-and-rewrite (same
# inode) so an external watcher on the file keeps working.
caddy_swap() {
    local file="$1" ov="$2" ow="$3" nv="$4" nw="$5" tmp
    tmp="$(mktemp)"
    sed -E "s/localhost:${ov}([^0-9]|$)/localhost:${nv}\1/g; s/localhost:${ow}([^0-9]|$)/localhost:${nw}\1/g" "$file" > "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

caddy_reload() {
    caddy reload --config "$PROXY_CADDYFILE" --adapter caddyfile >/dev/null 2>&1
}

# Stop scheduling NEW passes in a container without touching the ones in flight: cron ignores
# any file in /etc/cron.d whose name contains a dot, so a rename disables the crontab and keeps
# it restorable for --rollback.
disable_cron_in() { podman exec "$1" sh -c 'mv -f /etc/cron.d/fleet-kit /etc/cron.d/fleet-kit.retired 2>/dev/null || true' 9>&- 2>/dev/null || true; }
enable_cron_in()  { podman exec "$1" sh -c 'mv -f /etc/cron.d/fleet-kit.retired /etc/cron.d/fleet-kit 2>/dev/null || true' 9>&- 2>/dev/null || true; }

# Detached reaper: stop the retired container once its passes are done (or after RETIRE_MAX_S).
# setsid + nohup so it outlives deploy.sh AND auto_deploy.sh's cron tick; 9>&- so it never
# inherits the deploy flock. Exits quietly if the retired container is renamed away (rollback)
# or already stopped.
spawn_reaper() {
    local name="$1"
    setsid nohup bash -c '
        name="$1"; max="$2"; log="$3"; waited=0
        while :; do
            state="$(podman inspect "$name" --format "{{.State.Running}}" 2>/dev/null || echo gone)"
            [ "$state" = "true" ] || exit 0
            n="$(podman exec "$name" pgrep -c -f "bash .*run_member[.]sh" 2>/dev/null | head -1 | tr -cd "0-9")"
            [ -z "$n" ] && n=0
            if [ "$n" -eq 0 ] || [ "$waited" -ge "$max" ]; then
                podman stop -t 30 "$name" >/dev/null 2>&1 || true
                echo "[deploy $(date "+%Y-%m-%d %H:%M:%S %Z")] reaper: stopped $name after ${waited}s with $n pass(es) left (gh#625)" >> "$log"
                exit 0
            fi
            sleep 60; waited=$((waited + 60))
        done' _ "$name" "$RETIRE_MAX_S" "$DEPLOY_LOG" >/dev/null 2>&1 9>&- &
}

proxy_deploy() {
    local ov ow nv nw
    read -r ov ow < <(live_ports)
    if [ "$ov" = "$VIEW_PORT" ]; then nv="$GREEN_VIEW_PORT"; nw="$GREEN_WEBHOOK_PORT"; else nv="$VIEW_PORT"; nw="$WEBHOOK_PORT"; fi
    log "rolling deploy (gh#625): live $CONTAINER on $ov/$ow, new build goes to $nv/$nw, no cordon"

    # gh#672 AC6: only one retired generation is kept, and its NAME must be free before this
    # deploy's own cutover can rename $CONTAINER -> $RETIRED_MARKER later -- but its in-flight
    # passes must not be killed on the SPECULATION that this deploy even succeeds. If it is
    # still running, its ports (nv/nw -- ports alternate every deploy, so a still-running
    # previous-retired build sits on exactly the pair this build needs) are genuinely blocked;
    # previously this stopped it unconditionally, right here, before green had even been built,
    # let alone health-checked -- killing its passes for a deploy that might go on to fail its
    # OWN health check for unrelated reasons, and destroying the one known-good fallback
    # do_rollback depends on in the same stroke. Abandon this tick instead: leave it running,
    # log why, and let the next deploy tick (or its own reaper, RETIRE_MAX_S) retry once it has
    # actually freed the ports on its own.
    if exists "$RETIRED_MARKER" && running "$RETIRED_MARKER"; then
        log "ABANDONED: previous retired build ($RETIRED_MARKER) still running on $nv/$nw -- not stopping its in-flight passes to force this deploy through; will retry next tick"
        return 1
    fi
    exists "${CONTAINER}-green" && podman rm -f "${CONTAINER}-green" >/dev/null 2>&1 || true

    log "building $IMAGE from $KIT_DIR"
    local sha; sha="$(git -C "$KIT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
    podman build --build-arg DEPLOY_SHA="$sha" -t "$IMAGE" "$KIT_DIR"

    log "starting ${CONTAINER}-green on $nv/$nw"
    # shellcheck disable=SC2046
    podman run $(run_args "${CONTAINER}-green" "$nv" "$nw") >/dev/null 9>&-
    log "health-checking green (up to ${HEALTH_TIMEOUT_S}s)"
    if ! health_check "$nv"; then
        log "FAILED: green never answered http://localhost:$nv/ within ${HEALTH_TIMEOUT_S}s -- live build untouched, no cordon happened"
        podman stop -t 5 "${CONTAINER}-green" >/dev/null 2>&1 || true
        return 1
    fi

    cp -f "$PROXY_CADDYFILE" "$PROXY_CADDYFILE.pre-deploy"
    caddy_swap "$PROXY_CADDYFILE" "$ov" "$ow" "$nv" "$nw"
    if ! caddy_reload; then
        log "FAILED: caddy reload rejected the swapped Caddyfile -- restoring it, live build untouched"
        cat "$PROXY_CADDYFILE.pre-deploy" > "$PROXY_CADDYFILE"; caddy_reload || true
        podman stop -t 5 "${CONTAINER}-green" >/dev/null 2>&1 || true
        return 1
    fi
    log "cutover: caddy upstream $ov/$ow -> $nv/$nw reloaded (zero downtime); $CONTAINER -> $RETIRED_MARKER keeps running until its passes finish"

    # Free the $RETIRED_MARKER name for the rename below -- safe now: green is proven healthy,
    # and this deploy only ever got here because that container was already stopped (not
    # running), so no in-flight pass is lost by removing it.
    exists "$RETIRED_MARKER" && podman rm -f "$RETIRED_MARKER" >/dev/null 2>&1 || true

    if exists "$CONTAINER"; then
        disable_cron_in "$CONTAINER"
        podman rename "$CONTAINER" "$RETIRED_MARKER"
        echo "$ov $ow" > "$RETIRED_PORTS_FILE"
    fi
    podman rename "${CONTAINER}-green" "$CONTAINER"
    echo "$nv $nw" > "$LIVE_PORTS_FILE"
    VIEW_PORT="$nv"; WEBHOOK_PORT="$nw"
    exists "$RETIRED_MARKER" && spawn_reaper "$RETIRED_MARKER"
    return 0
}

proxy_rollback() {
    # Undo the last proxy-mode cutover: caddy back to the retired pair, retired build resumes
    # scheduling, the current build is stopped and kept for inspection.
    local rv rw cv cw
    exists "$RETIRED_MARKER" || { log "ROLLBACK: no $RETIRED_MARKER to restore"; return 1; }
    read -r rv rw < "$RETIRED_PORTS_FILE" 2>/dev/null || { log "ROLLBACK: no $RETIRED_PORTS_FILE -- cannot tell which ports the retired build holds"; return 1; }
    read -r cv cw < <(live_ports)
    running "$RETIRED_MARKER" || podman start "$RETIRED_MARKER" >/dev/null
    health_check "$rv" || { log "ROLLBACK: retired build not healthy on $rv"; return 1; }
    caddy_swap "$PROXY_CADDYFILE" "$cv" "$cw" "$rv" "$rw" && caddy_reload
    enable_cron_in "$RETIRED_MARKER"
    if exists "$CONTAINER"; then
        disable_cron_in "$CONTAINER"
        podman stop -t 30 "$CONTAINER" >/dev/null 2>&1 || true
        podman rename "$CONTAINER" "${CONTAINER}-broken-$(date +%s)"
    fi
    podman rename "$RETIRED_MARKER" "$CONTAINER"
    echo "$rv $rw" > "$LIVE_PORTS_FILE"; rm -f "$RETIRED_PORTS_FILE"
    log "ROLLED BACK: caddy -> $rv/$rw, $CONTAINER is the previous build and scheduling again"
    return 0
}

if [ "${1:-}" = "--rollback" ]; then
    log "manual rollback requested"
    if [ -f "$RETIRED_PORTS_FILE" ] && [ -f "$PROXY_CADDYFILE" ]; then
        proxy_rollback && exit 0
        exit 1
    fi
    do_rollback
fi

# --- pre-build freshness guard ----------------------------------------------------------
# gh#248: this is the manual/direct invocation path (auto_deploy.sh's poll already pulls
# before calling this script -- see its own guard, auto_deploy.sh:100-106). A human running
# `bash scripts/deploy.sh` directly builds whatever KIT_DIR's working tree happens to hold,
# with no pull and no staleness check -- confirmed live 2026-08-30: a merged the-fixer fix
# sat invisible for 3+ hours because the host checkout was never pulled before a manual
# deploy, and the-fixer kept firing on a 9-hour-old ghost failure the whole time. Same "loud
# stop, not a guess" rule as auto_deploy.sh's dirty-check: never silently ship stale code,
# never silently blow away an uncommitted local edit either.
if [ -n "$(git -C "$KIT_DIR" status --porcelain)" ]; then
    log "ABORT: $KIT_DIR is dirty -- refusing to pull over local changes. Resolve by hand, or invoke without a pull-guard bypass."
    exit 1
fi
git -C "$KIT_DIR" fetch origin main -q
REMOTE_SHA="$(git -C "$KIT_DIR" rev-parse origin/main)"
LOCAL_SHA="$(git -C "$KIT_DIR" rev-parse HEAD)"
if [ "$REMOTE_SHA" != "$LOCAL_SHA" ]; then
    if ! git -C "$KIT_DIR" merge-base --is-ancestor "$LOCAL_SHA" "$REMOTE_SHA"; then
        log "ABORT: $KIT_DIR has diverged from origin/main (local=$LOCAL_SHA remote=$REMOTE_SHA) -- not a simple behind-by-N, resolve by hand."
        exit 1
    fi
    log "$KIT_DIR was behind origin/main (local=$LOCAL_SHA remote=$REMOTE_SHA) -- pulling before build"
    git -C "$KIT_DIR" merge --ff-only origin/main
fi

# --- drain gate -------------------------------------------------------------------------
# Blue-green protects SERVING (no request hits a half-started container). It does nothing for
# WORK: agent passes run as children of the blue container's cron, so the `podman stop -t 10`
# in the cutover below SIGTERMs then SIGKILLs whatever is mid-pass. Found live 2026-08-26:
# auto_deploy landed #92 while an ad-hoc marie pass was scoring issue complexity; the pass died
# at 17 of 79 issues, ~$3 spent, and (before the companion fix in run_member.sh) left no
# runs.jsonl row at all. A pass can run for an hour (timeout_s: 3600) and auto_deploy ticks
# every few minutes, so on an active merge day this is not an edge case -- it is a coin flip.
#
# This is connection draining, the same thing a load balancer does before pulling a backend:
# stop sending work, let in-flight work finish, THEN replace. The difference is we cannot
# gracefully finish an agent pass in seconds, so we DEFER the whole deploy instead. auto_deploy
# is a cron poll -- a deferred deploy simply happens on the next tick, and its state file is
# only written on success, so nothing is lost by waiting.
#
# Bounded on purpose: a genuinely stuck pass must not block deploys forever. Past the max, we
# deploy anyway and say so loudly -- run_member.sh's SIGTERM trap then records the interrupted
# pass as status=killed instead of losing it silently. Belt and braces: drain avoids the kill,
# the trap makes an unavoidable kill visible.
#
# Skipped entirely when there is no live blue (first deploy, or recovering from a dark prod):
# there is no work to drain, and blocking recovery on a missing container would be backwards.
DRAIN_MAX_S="${FLEET_DRAIN_MAX_S:-1800}"
CORDONED=0
# TRUNCATE-AND-REWRITE, never `sed -i` / replace-then-rename. fleet.env is BIND-MOUNTED into
# the container ($INSTANCE_DIR/fleet.env -> /fleet-kit/fleet.env), and a bind mount follows the
# INODE, not the path: any edit that writes a new file and renames it over the old one leaves
# the container reading the ORIGINAL inode forever. refresh_container.sh's header documents
# this exact trap for this exact file; the cordon walked straight into it.
#
# Caught live 2026-08-26, and it made the cordon a silent NO-OP in production: the host file
# read FLEET_ENABLED=false while `podman exec ... grep FLEET_ENABLED /fleet-kit/fleet.env`
# still read true, so members kept starting mid-drain -- in-flight went 3 -> 7 WHILE cordoned,
# and a marie pass began 1m41s into it. The worst possible shape: the deploy log honestly
# announced a cordon that was never in effect.
#
# `cat tmp > file` truncates the EXISTING inode in place, so the container sees the change on
# its very next read -- the property fleet_enabled_or_exit depends on.
cordon_write() {
    local want="$1" tmp
    tmp="$(mktemp)"
    awk -v v="$want" '
        /^[[:space:]]*FLEET_ENABLED[[:space:]]*=/ { print "FLEET_ENABLED=" v; found=1; next }
        { print }
        END { if (!found) print "FLEET_ENABLED=" v }
    ' "$INSTANCE_DIR/fleet.env" > "$tmp"
    cat "$tmp" > "$INSTANCE_DIR/fleet.env"
    rm -f "$tmp"
}
# Idempotent on purpose: the EXIT trap and an explicit call after the drain can both reach it,
# and a second run must not re-enable a fleet a human turned off in the meantime.
uncordon_fleet() {
    [ "$CORDONED" = "1" ] || return 0
    CORDONED=0
    cordon_write true
    log "uncordon: FLEET_ENABLED=true -- members resume on the next tick"
}

# `bash .*run_member[.]sh` and not a plain `run_member.sh`: pgrep matches against full command
# lines, so a bare pattern also matches the very shell podman spawns to RUN the check, and the
# gate would report a pass running forever (verified live -- `pgrep -af run_member.sh` returns
# its own wrapper). The [.] keeps the pattern from matching itself in any context.
drain_inflight_passes() {
    exists "$CONTAINER" || { log "drain: no live $CONTAINER -- nothing to drain"; return 0; }
    podman inspect "$CONTAINER" --format '{{.State.Running}}' 2>/dev/null | grep -q true \
        || { log "drain: $CONTAINER is not running -- nothing to drain"; return 0; }

    # CORDON before draining. Without this the drain waits for a fleet that never idles:
    # gru fires hourly and BLOCKS until every minion it spawned finishes, marie/jefe/roomba
    # each hold their own slot, and gru keeps SPAWNING new minions the whole time -- measured
    # live 2026-08-26, in-flight went 2 -> 5 DURING a drain, so the count was rising, not
    # falling, and the deploy was heading for its 1800s bound to force-kill exactly the work
    # the gate exists to protect. Waiting for zero only terminates if nothing new starts.
    #
    # This is cordon-then-drain, the standard node-rollout shape: stop scheduling NEW work,
    # let existing work finish, then replace. FLEET_ENABLED=false is the kit's own kill switch
    # and is read fresh by fleet_enabled_or_exit at the top of EVERY pass, so it takes effect
    # on the very next cron tick with nothing to restart -- and it gates only the automatic
    # loop, so a human's "run now" still works while a deploy is quiescing.
    #
    # Restored by trap on EVERY exit path, including the failure paths that `exit 1` out of
    # this script: leaving the fleet cordoned after a failed deploy would silently stop every
    # member indefinitely, which is a far worse outcome than the deploy we were trying to do.
    if [ "${FLEET_QUIESCE:-1}" = "1" ] && [ -f "$INSTANCE_DIR/fleet.env" ]; then
        if grep -qE '^[[:space:]]*FLEET_ENABLED[[:space:]]*=[[:space:]]*true' "$INSTANCE_DIR/fleet.env"; then
            CORDONED=1
            trap uncordon_fleet EXIT INT TERM
            cordon_write false
            log "cordon: FLEET_ENABLED=false -- no NEW passes will start while this deploy drains"
        fi
    fi

    local waited=0 inflight
    while :; do
        # CORDON BREACH CHECK (gh#552): fleet.env's FLEET_ENABLED has a writer this drain does
        # not control -- fleet_view_server.py's `/api/fleet_toggle` (the dashboard's kill-switch
        # toggle) calls write_env_flag() directly with zero awareness of an in-progress drain.
        # Root-caused live 2026-09-06: a gru pass started 173s into a 7-in-flight drain that,
        # per deploy.log's own account, did not legitimately clear or time out for another 31
        # minutes -- none of this loop's three logged exit paths (clear / timeout / no-live-
        # container) had fired, so something OUTSIDE this script reset the flag. The original
        # grep for writers (`grep -rn FLEET_ENABLED scripts/*.sh`) missed it because it is a
        # `.py` file. Re-assert every poll (bounds exposure to one 15s tick instead of the rest
        # of the drain) and log it loudly, so the previously-silent path is now visible here
        # instead of only reconstructible from gru.log/fleet.env mtime.
        if [ "$CORDONED" = "1" ] \
           && ! grep -qE '^[[:space:]]*FLEET_ENABLED[[:space:]]*=[[:space:]]*false' "$INSTANCE_DIR/fleet.env"; then
            log "CORDON BREACHED: FLEET_ENABLED was reset outside cordon_write()/uncordon_fleet() while this drain was still in progress -- re-cordoning. Check for a fleet_toggle/dashboard write racing this deploy."
            cordon_write false
        fi
        # `|| echo 0` on its own is NOT enough: pgrep -c PRINTS "0" and THEN exits 1 when it
        # matches nothing, so the fallback appends a second line and $inflight becomes "0\n0" --
        # which is non-empty, fails -eq, and made the gate report a defer with nothing running.
        # Seen live 2026-08-26 ("drain: 0 / 0 agent pass(es) in flight"). Take the first line
        # and keep only digits, so any pgrep quirk still yields a number.
        # `|| true` INSIDE the substitution, and it is the whole ballgame. This script runs
        # under `set -o pipefail`, and `pgrep` exits 1 when it matches NOTHING -- so with an
        # idle fleet the pipeline fails, the substitution fails, and `set -e` kills the deploy
        # right here. The healthiest possible state (no passes in flight) was the ONE state
        # that aborted every deploy, and it did so silently: the log showed a clean
        # cordon/uncordon pair and then nothing, while the same build ran fine by hand.
        # Measured 2026-08-26 across three consecutive DEPLOY FAILED at 328c1ac.
        inflight="$(podman exec "$CONTAINER" pgrep -c -f 'bash .*run_member[.]sh' 2>/dev/null | head -1 | tr -cd '0-9' || true)"
        [ -z "$inflight" ] && inflight=0
        if [ "$inflight" -eq 0 ] 2>/dev/null; then
            # `|| true`: a `[ cond ] && cmd` guard that evaluates FALSE returns non-zero, and
            # under `set -e` that status propagates -- it killed the deploy on the exact path
            # this branch exists for. When waited=0 (nothing in flight, the drain clears
            # instantly), the guard is false, so the FASTEST, HEALTHIEST drain was the one that
            # aborted the deploy, right after logging "uncordon" and looking like a clean pass.
            # Measured 2026-08-26: three consecutive DEPLOY FAILED at 328c1ac, each dying at
            # this line with no error, while the same build ran fine by hand.
            [ "$waited" -gt 0 ] && log "drain: clear after ${waited}s -- proceeding with deploy" || true
            uncordon_fleet
            return 0
        fi
        if [ "$waited" -ge "$DRAIN_MAX_S" ]; then
            log "drain: STILL $inflight pass(es) in flight after ${DRAIN_MAX_S}s -- deploying ANYWAY."
            log "drain: those passes will be killed; run_member.sh records them as status=killed (safe to re-run)."
            uncordon_fleet
            return 0
        fi
        [ "$waited" -eq 0 ] && log "drain: $inflight agent pass(es) in flight -- deferring cutover (max ${DRAIN_MAX_S}s)"
        sleep 15
        waited=$((waited + 15))
    done
}
finish_deploy() {
    log "DEPLOYED: $CONTAINER live on $VIEW_PORT/$WEBHOOK_PORT, running $(podman exec "$CONTAINER" sh -c 'cd /fleet-kit && git log -1 --oneline' 2>/dev/null)"
    log "previous build kept stopped as $RETIRED_MARKER -- roll back any time with: bash $0 --rollback"

    # gh#622: a fix is tested the instant it lands. The cordon above skipped every hourly member
    # tick that fell inside the drain (gru 13:03Z and 14:03Z on 2026-09-07 both exited on
    # FLEET_ENABLED=false), so after cutover nothing exercised the new build until the next tick,
    # up to an hour later. Kick one gru pass in the live container now -- detached, with the same
    # env the crontab line hands it -- so the build runs its real path within seconds of landing
    # and the fanout the cordon ate is replaced. Best-effort: a failed kick is logged, never fatal.
    if podman exec -d "$CONTAINER" bash -c 'set -a; eval "$(grep -hE "^[A-Z_]+=" /etc/cron.d/* 2>/dev/null)"; set +a; export GH_TOKEN=$(cat /root/.gh_token 2>/dev/null); cd /fleet-kit && bash scripts/run_gru_fanout.sh >> /var/log/fleet-kit/gru.log 2>&1' 9>&- 2>/dev/null; then
        log "post-deploy: kicked one gru pass in $CONTAINER so the new build runs its real path now, not at the next cron tick (gh#622)"
    else
        log "post-deploy: could not kick a gru pass in $CONTAINER -- the next cron tick will run it (gh#622)"
    fi
}

if proxy_mode; then
    proxy_deploy || exit 1
    finish_deploy
    exit 0
fi
log "no caddy upstream for this instance's live port in $PROXY_CADDYFILE -- legacy stop-and-recreate cutover (cordon + drain)"
drain_inflight_passes

log "building $IMAGE from $KIT_DIR"
# Baked into the image as --build-arg, not written into KIT_DIR itself: KIT_DIR is a live git
# checkout (the build context), and dropping a file into it would leave an untracked artifact
# in the working tree on every deploy. gh#201: the running container's /fleet-kit is `COPY .
# /fleet-kit` with .git excluded (.dockerignore) -- never a real checkout on any instance built
# from this Dockerfile -- so this SHA baked at build time is the only way a later staleness
# check can know what was actually shipped, without needing podman/host access itself.
DEPLOY_SHA="$(git -C "$KIT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
podman build --build-arg DEPLOY_SHA="$DEPLOY_SHA" -t "$IMAGE" "$KIT_DIR"

log "starting green candidate (${CONTAINER}-green) on alt ports $GREEN_VIEW_PORT/$GREEN_WEBHOOK_PORT"
if exists "${CONTAINER}-green"; then
    podman rm -f "${CONTAINER}-green" >/dev/null 2>&1 || true
fi
# shellcheck disable=SC2046
# `9>&-` closes auto_deploy.sh's flock fd for THIS call only. podman's helper processes
# (conmon, slirp4netns) live as long as the CONTAINER, so anything they inherit is held for
# hours -- and on 2026-08-26 that was the deploy lock itself: `lsof` showed conmon holding
# fd 9 with an elapsed time exactly matching the container's StartedAt, every subsequent
# auto_deploy tick found the lock taken, exited 0 through its quiet-no-op path, and the
# pipeline was dead for ~2 hours with NOTHING in the log. Harmless when the fd does not
# exist (a hand-run deploy), which is why it is unconditional.
podman run $(run_args "${CONTAINER}-green" "$GREEN_VIEW_PORT" "$GREEN_WEBHOOK_PORT") >/dev/null 9>&-

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
#
# NOTE: this REPLACES the drain's uncordon EXIT trap (bash keeps one handler per signal). That
# is safe only because the drain always calls uncordon_fleet explicitly on both of its return
# paths, so the fleet is already uncordoned by the time execution reaches here -- verified by
# selftest. If you ever add a third way out of the drain, uncordon on it too.
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
# Same fd-close as the green candidate above -- this is the container that LIVES, so a leaked
# fd here is the one that wedges every future deploy.
podman run $(run_args "$CONTAINER" "$VIEW_PORT" "$WEBHOOK_PORT") >/dev/null 9>&-

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

finish_deploy
