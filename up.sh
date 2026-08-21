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
# Space-separated, same shape as FLEET_ACCOUNTS itself -- account_pool.sh already tries these
# in order and fails over; up.sh's job is just mounting creds for EVERY name in the list, not
# only the first. `--account` (singular) still works as an alias for one name.
ACCOUNTS="${FLEET_ACCOUNTS:-primary}"
IMAGE_TAG="fleet-kit:latest"
VIEW_PORT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO_URL="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --account|--accounts) ACCOUNTS="$2"; shift 2 ;;
    --port) VIEW_PORT="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --repo <git-url> --name <project-name> [--accounts \"primary other\"] [--port <n>]"
      exit 0 ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

if [ -z "$REPO_URL" ] || [ -z "$NAME" ]; then
  echo "Usage: $0 --repo <git-url> --name <project-name> [--accounts \"primary other\"] [--port <n>]" >&2
  exit 1
fi

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
  sed -e "s|^FLEET_REPO=.*|FLEET_REPO=/repo|" \
      -e "s|^FLEET_ACCOUNTS=.*|FLEET_ACCOUNTS=\"$ACCOUNTS\"|" \
      -e "s|^FLEET_LOG_DIR=.*|FLEET_LOG_DIR=/var/log/fleet-kit|" \
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

echo "[up] starting fleet '$NAME' -> $REPO_URL (image $IMAGE_TAG, accounts [${ACCOUNT_LIST[*]}], view port $VIEW_PORT, webhook port $WEBHOOK_PORT)"
exec podman run -d \
  --name "fleet-kit-$NAME" \
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
