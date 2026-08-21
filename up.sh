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
ACCOUNT="${FLEET_ACCOUNT:-primary}"
IMAGE_TAG="fleet-kit:latest"
VIEW_PORT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO_URL="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --account) ACCOUNT="$2"; shift 2 ;;
    --port) VIEW_PORT="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --repo <git-url> --name <project-name> [--account <claude-account>] [--port <n>]"
      exit 0 ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

if [ -z "$REPO_URL" ] || [ -z "$NAME" ]; then
  echo "Usage: $0 --repo <git-url> --name <project-name> [--account <claude-account>] [--port <n>]" >&2
  exit 1
fi

INSTANCE_DIR="$(pwd)/instances/$NAME"
mkdir -p "$INSTANCE_DIR"
ENV_FILE="$INSTANCE_DIR/fleet.env"
CREDS_DIR="$HOME/.claude-$ACCOUNT"

# 1. Build the image if it doesn't exist yet (idempotent — podman skips unchanged layers).
if ! podman image exists "$IMAGE_TAG"; then
  echo "[up] building $IMAGE_TAG (first run only, subsequent runs reuse cached layers)..."
  podman build -t "$IMAGE_TAG" .
fi

# 2. Generate this project's fleet.env from the template on first run only — never overwrite
#    an operator's existing edits (kill-switch toggles, tuned budgets) on a re-run.
if [ ! -f "$ENV_FILE" ]; then
  echo "[up] writing $ENV_FILE from fleet.env.example (edit this file to configure $NAME)"
  sed -e "s|^FLEET_REPO=.*|FLEET_REPO=/repo|" \
      -e "s|^FLEET_ACCOUNTS=.*|FLEET_ACCOUNTS=\"$ACCOUNT\"|" \
      fleet.env.example > "$ENV_FILE"
fi

# 3. Verify the named Claude account actually has credentials mounted before handing this to
#    cron for hours — same check entrypoint.sh does at boot, but loud and BEFORE `podman run`
#    instead of buried in container logs a human has to go find.
if [ ! -f "$CREDS_DIR/.credentials.json" ] && [ ! -f "$CREDS_DIR/.claude.json" ]; then
  echo "[up] WARNING: no credentials found at $CREDS_DIR for account '$ACCOUNT'."
  echo "[up]          copy your whole .credentials.json there before this fleet can build anything:"
  echo "[up]          mkdir -p $CREDS_DIR && cp ~/.claude/.credentials.json $CREDS_DIR/"
fi

# 4. Check for a GitHub token the container can read (gh CLI reads GH_TOKEN directly).
GH_TOKEN_VAL="${GH_TOKEN:-$(gh auth token 2>/dev/null || true)}"
if [ -z "$GH_TOKEN_VAL" ]; then
  echo "[up] ERROR: no GH_TOKEN available (set GH_TOKEN, or 'gh auth login' on this box first)." >&2
  exit 1
fi

# Port: explicit --port wins; else derive a stable, distinct port per instance name (so N
# projects on one box never collide on fleet_view_server.py's default 8420) via a hash; else
# fall back to the template's FLEET_VIEW_PORT for a single-instance box.
if [ -z "$VIEW_PORT" ]; then
  DEFAULT_PORT="$(grep -oE '^FLEET_VIEW_PORT=[0-9]+' fleet.env.example | cut -d= -f2)"
  OFFSET=$(( $(cksum <<<"$NAME" | cut -d' ' -f1) % 500 ))
  VIEW_PORT=$(( ${DEFAULT_PORT:-8420} + OFFSET ))
fi

echo "[up] starting fleet '$NAME' -> $REPO_URL (image $IMAGE_TAG, account $ACCOUNT, view port $VIEW_PORT)"
exec podman run -d \
  --name "fleet-kit-$NAME" \
  --replace \
  -e "FLEET_REPO=/repo" \
  -e "FLEET_REPO_URL=$REPO_URL" \
  -e "GH_TOKEN=$GH_TOKEN_VAL" \
  -e "FLEET_ENV_FILE=/fleet-kit/fleet.env" \
  -e "FLEET_VIEW_PORT=$VIEW_PORT" \
  -v "$ENV_FILE:/fleet-kit/fleet.env:ro" \
  -v "$CREDS_DIR:/root/.claude-$ACCOUNT:ro" \
  -v "$INSTANCE_DIR/repo:/repo" \
  -v "$INSTANCE_DIR/logs:/var/log/fleet-kit" \
  -p "$VIEW_PORT:$VIEW_PORT" \
  "$IMAGE_TAG"
