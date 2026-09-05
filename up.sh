#!/usr/bin/env bash
# up.sh — point-and-shoot launcher. One command, any target repo, a running fleet.
#
#   ./up.sh --repo git@github.com:org/other-project.git --name other-project
#   ./up.sh                                        # prompts for both, interactively
#
# WHY THIS EXISTS: everything project-specific (target repo, account creds, deploy secrets,
# analytics source) is already designed to live OUTSIDE the image — env vars and mounts, per
# Dockerfile's own header. This script is the missing last step: turning "point this kit at a
# new repo" from a manual podman-build-then-hand-craft-flags session into one command. Nothing
# here is nonprofit-atlas-specific; every value below is an argument or a prompt.
#
# State lives per-project under ./instances/<name>/ (fleet.env + logs), never inside the image
# — running `up.sh` again for the same --name reuses that config; a new --name is a clean fleet
# for a different repo, side by side, same box.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

REPO_URL=""
NAME=""
CONTAINER_NAME=""
# Space-separated, same shape as FLEET_ACCOUNTS itself -- account_pool.sh already tries these
# in order and fails over; up.sh's job is just mounting creds for EVERY name in the list, not
# only the first. `--account` (singular) still works as an alias for one name.
ACCOUNTS="${FLEET_ACCOUNTS:-primary}"
VIEW_PORT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO_URL="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --container-name) CONTAINER_NAME="$2"; shift 2 ;;
    --account|--accounts) ACCOUNTS="$2"; shift 2 ;;
    --port) VIEW_PORT="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --repo <git-url> --name <project-name> [--container-name <name>] [--accounts \"primary other\"] [--port <n>]"
      exit 0 ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

# --container-name overrides the podman container's own name (default: fleet-kit-$NAME). --name
# still drives instance-state paths, image build, ports -- this ONLY renames the container
# itself, for a project that wants a name matching what the repo is actually called (e.g.
# nonprofit-atlas's fleet running as container "philanthropy", the app's real product name)
# rather than the fleet-kit-prefixed default. Falls back to FLEET_CONTAINER_NAME from the
# instance's own fleet.env (sourced below, once NAME is known) so a re-run on the same box
# remembers the choice instead of reverting to the default -- same persistence pattern as
# FLEET_ACCOUNTS.
if [ -z "$CONTAINER_NAME" ] && [ -f "instances/$NAME/fleet.env" ]; then
  CONTAINER_NAME="$(grep -oE '^FLEET_CONTAINER_NAME=.*' "instances/$NAME/fleet.env" 2>/dev/null | cut -d= -f2-)"
fi
CONTAINER_NAME="${CONTAINER_NAME:-fleet-kit-$NAME}"

if [ -z "$REPO_URL" ] || [ -z "$NAME" ]; then
  echo "Usage: $0 --repo <git-url> --name <project-name> [--container-name <name>] [--accounts \"primary other\"] [--port <n>]" >&2
  exit 1
fi

# Per-instance image tag, same reasoning as VIEW_PORT's per-instance offset below: every
# instance on a box building/running against the identical `fleet-kit:latest` tag meant two
# instances' independent 5-minute auto_deploy.sh builds raced on one shared image underneath
# them -- the confirmed root cause of the 27h outage PR#393 patched the symptom of (gh#395).
# Deriving straight from $NAME (not a hash/offset like VIEW_PORT needs) is enough here: unlike
# a port there's no collision space to avoid, just two instances needing to never say the same
# string.
IMAGE_TAG="fleet-kit:$NAME"

INSTANCE_DIR="$(pwd)/instances/$NAME"
mkdir -p "$INSTANCE_DIR/repo" "$INSTANCE_DIR/logs"
ENV_FILE="$INSTANCE_DIR/fleet.env"
WEBHOOK_SECRET_FILE="$INSTANCE_DIR/webhook_secret"
read -ra ACCOUNT_LIST <<< "$ACCOUNTS"

# 1. Build (or rebuild) the image every run -- podman's own layer cache makes an unchanged
#    build fast, so this costs nothing on a re-run with no source changes. The PREVIOUS shape
#    here (`if ! podman image exists`) only ever built ONCE per box: a `git pull` picking up
#    real fixes (entrypoint.sh, a member's charter, run_member.sh itself) would silently keep
#    running the stale image forever, because the tag already existed. Found live on dino,
#    2026-08-21 -- the running container was 13 commits behind despite `up.sh` having been run
#    after those commits landed in the checkout.
echo "[up] building $IMAGE_TAG (podman reuses cached layers for anything unchanged)..."
podman build -t "$IMAGE_TAG" .

# 2. Generate this project's fleet.env from the template on first run only — never overwrite
#    an operator's existing edits (kill-switch toggles, tuned budgets) on a re-run.
if [ ! -f "$ENV_FILE" ]; then
  echo "[up] writing $ENV_FILE from fleet.env.example (edit this file to configure $NAME)"
  # FLEET_LOG_DIR: the template default (~/Library/Logs/fleet-kit) is a macOS-host path.
  # Inside the container it must be /var/log/fleet-kit — the exact path `-v
  # "$INSTANCE_DIR/logs:/var/log/fleet-kit"` mounts below — or run_member.sh (which sources
  # fleet.env fresh, unlike entrypoint.sh's crontab lines which hardcode $LOG_DIR themselves)
  # writes under root's un-mounted $HOME instead, invisible to the host and to
  # fleet_view_server.py. Found live on dino, 2026-08-21: a real pass's log silently landed at
  # /root/Library/Logs/fleet-kit inside the container, never on the mounted volume.
  # FLEET_IMAGE_NAME: this instance's own derived $IMAGE_TAG, not the template's shared
  # `fleet-kit:latest` default -- every later auto_deploy.sh/deploy.sh cutover for this instance
  # reads it via deploy.sh:94's `${FLEET_IMAGE_NAME:-fleet-kit:latest}` default (gh#395).
  sed -e "s|^FLEET_REPO=.*|FLEET_REPO=/repo|" \
      -e "s|^FLEET_ACCOUNTS=.*|FLEET_ACCOUNTS=\"$ACCOUNTS\"|" \
      -e "s|^FLEET_LOG_DIR=.*|FLEET_LOG_DIR=/var/log/fleet-kit|" \
      -e "s|^FLEET_IMAGE_NAME=.*|FLEET_IMAGE_NAME=$IMAGE_TAG|" \
      fleet.env.example > "$ENV_FILE"
fi

# 3. Verify EVERY named Claude account actually has credentials mounted before handing this to
#    cron for hours — same check entrypoint.sh does at boot, but loud and BEFORE `podman run`
#    instead of buried in container logs a human has to go find. Build the -v mount flags here
#    too, one per account, so account_pool.sh's failover has somewhere real to fail over TO.
CREDS_MOUNT_ARGS=()
for acct in "${ACCOUNT_LIST[@]}"; do
  acct_creds_dir="$HOME/.claude-$acct"
  if [ ! -f "$acct_creds_dir/.credentials.json" ] && [ ! -f "$acct_creds_dir/.claude.json" ]; then
    echo "[up] WARNING: no credentials found at $acct_creds_dir for account '$acct'."
    echo "[up]          copy your whole .credentials.json there before this fleet can build anything:"
    echo "[up]          mkdir -p $acct_creds_dir && cp ~/.claude/.credentials.json $acct_creds_dir/"
  fi
  CREDS_MOUNT_ARGS+=(-v "$acct_creds_dir:/root/.claude-$acct:ro")
done

# 4. Check for a GitHub token the container can read (gh CLI reads GH_TOKEN directly).
GH_TOKEN_VAL="${GH_TOKEN:-$(gh auth token 2>/dev/null || true)}"
if [ -z "$GH_TOKEN_VAL" ]; then
  echo "[up] ERROR: no GH_TOKEN available (set GH_TOKEN, or 'gh auth login' on this box first)." >&2
  exit 1
fi

# 5. Generate this instance's webhook secret on first run only (same never-overwrite reasoning
#    as fleet.env above). the-fixer's event-driven trigger (scripts/webhook_receiver.py) needs
#    this to verify GitHub's HMAC signature; without it entrypoint.sh just skips starting the
#    receiver and the fleet stays poll-only -- a valid, not-broken mode, same as no messenger
#    driver configured. Print it once here so the operator can paste it into GitHub's webhook
#    settings; never re-print an existing one on a re-run (it's already configured or the
#    operator chose not to use it).
if [ ! -f "$WEBHOOK_SECRET_FILE" ]; then
  openssl rand -hex 32 > "$WEBHOOK_SECRET_FILE"
  chmod 600 "$WEBHOOK_SECRET_FILE"
  echo "[up] generated webhook secret at $WEBHOOK_SECRET_FILE -- configure a GitHub repo"
  echo "[up]   webhook (Settings -> Webhooks -> Add webhook) with:"
  echo "[up]     Payload URL: <your public /webhook path>"
  echo "[up]     Content type: application/json"
  echo "[up]     Secret:       $(cat "$WEBHOOK_SECRET_FILE")"
  echo "[up]     Events:       just 'Workflow runs'"
fi

# Port: explicit --port wins; else derive a stable, distinct port per instance name (so N
# projects on one box never collide on fleet_view_server.py's default 8420) via a hash; else
# fall back to the template's FLEET_VIEW_PORT for a single-instance box. Webhook port is
# always dashboard-port + 1 -- deterministic from the same offset, no separate flag needed,
# never collides with another instance's dashboard OR webhook port on the same box.
if [ -z "$VIEW_PORT" ]; then
  DEFAULT_PORT="$(grep -oE '^FLEET_VIEW_PORT=[0-9]+' fleet.env.example | cut -d= -f2)"
  OFFSET=$(( $(cksum <<<"$NAME" | cut -d' ' -f1) % 500 ))
  VIEW_PORT=$(( ${DEFAULT_PORT:-8420} + OFFSET ))
fi
WEBHOOK_PORT=$(( VIEW_PORT + 1 ))

# Reap orphaned entrypoints bound to THIS instance before starting a new one (2026-09-04,
# gh#4340). A blue-green deploy (`philanthropy-green`) left its entrypoint.sh + cron + watchdog
# running for 9 days after podman had already forgotten the container -- still bind-mounted to
# this instance's logs and repo. Under rootless podman pids are host-global, so that dead
# container's watchdog kept `kill -9`-ing the LIVE container's cron every 5 minutes, taking
# in-flight member runs down with it, and its cron kept running git operations against the same
# worktree (which is what corrupted branch.main.merge). `podman run --replace` below only
# replaces the container RECORD -- it does not reap a process tree podman has lost track of, so
# it has to be done here explicitly.
for _pid in $(pgrep -f 'entrypoint\.sh cron-foreground' 2>/dev/null || true); do
  # Match on the mount table: does this process have OUR instance dir bind-mounted?
  if grep -qF " $INSTANCE_DIR/logs /var/log/fleet-kit " "/proc/$_pid/mountinfo" 2>/dev/null; then
    # Skip anything podman still knows about -- only truly orphaned trees get reaped.
    _cid="$(tr '\0' '\n' < "/proc/$_pid/environ" 2>/dev/null | sed -n 's/^HOSTNAME=//p')"
    if [ -n "$_cid" ] && podman container exists "$_cid" 2>/dev/null; then
      continue
    fi
    echo "[up] reaping orphaned entrypoint pid $_pid (container ${_cid:-unknown} gone from podman, still mounted on $INSTANCE_DIR)"
    pkill -9 -P "$_pid" 2>/dev/null || true
    kill -9 "$_pid" 2>/dev/null || true
  fi
done

echo "[up] starting fleet '$NAME' as container '$CONTAINER_NAME' -> $REPO_URL (image $IMAGE_TAG, accounts [${ACCOUNT_LIST[*]}], view port $VIEW_PORT, webhook port $WEBHOOK_PORT)"
podman run -d \
  --name "$CONTAINER_NAME" \
  --replace \
  -e "FLEET_REPO=/repo" \
  -e "FLEET_REPO_URL=$REPO_URL" \
  -e "GH_TOKEN=$GH_TOKEN_VAL" \
  -e "FLEET_ENV_FILE=/fleet-kit/fleet.env" \
  -e "FLEET_VIEW_PORT=$VIEW_PORT" \
  -e "FLEET_WEBHOOK_PORT=$WEBHOOK_PORT" \
  -v "$ENV_FILE:/fleet-kit/fleet.env:ro" \
  -v "$WEBHOOK_SECRET_FILE:/fleet-kit/.webhook_secret:ro" \
  "${CREDS_MOUNT_ARGS[@]}" \
  -v "$INSTANCE_DIR/repo:/repo" \
  -v "$INSTANCE_DIR/logs:/var/log/fleet-kit" \
  -p "$VIEW_PORT:$VIEW_PORT" \
  -p "$WEBHOOK_PORT:$WEBHOOK_PORT" \
  "$IMAGE_TAG"

# 6. Wire this instance into cron: auto-deploy (picks up future merges without a manual
#    deploy.sh run) and account health-check (pages when the credential pool goes bad).
#    Idempotent -- grep before appending, so a re-run of up.sh for an existing instance never
#    duplicates a crontab line. Both scripts are keyed by FLEET_CONTAINER_NAME (see
#    auto_deploy.sh's STATE/LOCKFILE and account_health_check.sh's own state file), which is
#    why this step must pass it explicitly rather than relying on any default.
#
#    WHY THIS EXISTS: found live on dino, 2026-08-29 -- fleet-kit-server-fleet was stood up by
#    hand (a raw `podman run`, bypassing up.sh entirely) specifically because up.sh never did
#    this wiring, so nobody thought to do it after either. Result: an instance 6 commits/13h
#    behind main with no auto-deploy, no health-check, silently. up.sh is the one place that
#    runs for every instance -- closing the gap here means the next instance can't skip it.
KIT_DIR="$(pwd)"
CRON_LOG_DIR="$INSTANCE_DIR/logs"
AUTO_DEPLOY_LINE="*/5 * * * * cd $KIT_DIR && FLEET_INSTANCE_DIR=$INSTANCE_DIR FLEET_CONTAINER_NAME=$CONTAINER_NAME FLEET_LOG_DIR=$CRON_LOG_DIR bash scripts/auto_deploy.sh >> $CRON_LOG_DIR/auto_deploy.cron.log 2>&1"
# NTFY_TOPIC is baked in from up.sh's OWN environment at run time, not left as a cron-time
# expansion -- cron jobs run in a minimal environment that does not inherit the interactive
# shell's exported vars, so `${NTFY_TOPIC:-}` would evaluate empty on every tick and
# account_health_check.sh's `:?` guard would then fail unconditionally, forever. Export
# NTFY_TOPIC before running up.sh (as the printed hint says) for this to take effect.
HEALTH_CHECK_LINE="*/5 * * * * FLEET_LOG_DIR=$CRON_LOG_DIR NTFY_TOPIC=${NTFY_TOPIC:-} FLEET_CONTAINER_NAME=$CONTAINER_NAME bash $KIT_DIR/scripts/account_health_check.sh >> $CRON_LOG_DIR/account_health_check.cron.log 2>&1"
CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
NEW_CRON="$CURRENT_CRON"
# Anchored on a token boundary (end-of-line or whitespace after the value) -- a plain
# substring match (grep -F) would treat FLEET_CONTAINER_NAME=fleet-kit-atlas as already
# present just because FLEET_CONTAINER_NAME=fleet-kit-atlas-staging is, silently skipping
# cron installation for any instance whose name is a prefix of another's.
if ! echo "$CURRENT_CRON" | grep -qF "auto_deploy.sh" || ! echo "$CURRENT_CRON" | grep -F "auto_deploy.sh" | grep -qE "FLEET_CONTAINER_NAME=${CONTAINER_NAME}([[:space:]]|\$)"; then
  NEW_CRON="$NEW_CRON
$AUTO_DEPLOY_LINE"
  echo "[up] added auto-deploy cron for '$CONTAINER_NAME' (every 5 min)"
fi
if ! echo "$CURRENT_CRON" | grep -qF "account_health_check.sh" || ! echo "$CURRENT_CRON" | grep -F "account_health_check.sh" | grep -qE "FLEET_CONTAINER_NAME=${CONTAINER_NAME}([[:space:]]|\$)"; then
  NEW_CRON="$NEW_CRON
$HEALTH_CHECK_LINE"
  echo "[up] added account health-check cron for '$CONTAINER_NAME' (every 5 min, set NTFY_TOPIC in your shell env before running up.sh to page on failure)"
fi
if [ "$NEW_CRON" != "$CURRENT_CRON" ]; then
  echo "$NEW_CRON" | crontab -
fi

echo "[up] NOT automated -- do this yourself: public exposure (Cloudflare tunnel ingress or"
echo "[up]   a Caddy path rule -> localhost:$VIEW_PORT) and its own path_health_check.sh cron"
echo "[up]   line once the URL exists. Topology (one hostname per instance vs. one shared"
echo "[up]   host with Caddy path-routing) is a per-box call up.sh can't safely guess."
