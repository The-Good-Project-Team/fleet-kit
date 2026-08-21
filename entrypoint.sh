#!/bin/bash
# entrypoint.sh — container-native scheduler. Same cadences as schedulers/{launchd,systemd}
# but expressed as cron, since a container has no launchd/systemd of its own to hand jobs to.
#
# Modes (CMD arg):
#   cron-foreground (default) — install the crontab below, run cron in the foreground so the
#                                container has a PID 1 that actually keeps running.
#   once:<script>              — run exactly one fleet-kit script and exit. For the "run one
#                                pass by hand before scheduling anything" step in the README,
#                                and for CI/smoke-testing the image itself:
#                                `docker run fleet-kit once:worktree_builder.sh`
#   shell                       — drop into bash. Debugging only.
set -euo pipefail

: "${FLEET_REPO:?set FLEET_REPO (env or fleet.env) -- the repo this fleet builds against}"
: "${GH_TOKEN:?set GH_TOKEN -- gh CLI reads this directly, no separate auth step}"

# Clone the target repo on first boot if it isn't already there (bind-mounted or a prior
# container layer). A fresh container with only FLEET_REPO=/repo and no mount clones for you --
# matches the "point this at a repo" pitch without a manual clone step per box.
if [ ! -d "$FLEET_REPO/.git" ]; then
  FLEET_REPO_URL="${FLEET_REPO_URL:?FLEET_REPO ($FLEET_REPO) doesn't exist yet -- set FLEET_REPO_URL to clone it}"
  echo "[entrypoint] cloning $FLEET_REPO_URL -> $FLEET_REPO"
  gh repo clone "$FLEET_REPO_URL" "$FLEET_REPO"
fi

# One CLAUDE_CONFIG_DIR per account named in FLEET_ACCOUNTS must already be mounted at
# /root/.claude-<account> (see account_pool.sh) -- verify at boot rather than failing silently
# three hours into the first cron tick.
for acct in ${FLEET_ACCOUNTS:-primary}; do
  dir="/root/.claude-$acct"
  if [ ! -f "$dir/.credentials.json" ] && [ ! -f "$dir/.claude.json" ]; then
    echo "[entrypoint] WARNING: no credentials mounted at $dir for account '$acct' -- account_pool_run will report unauthenticated for it. Mount a real Claude Code credentials dir there."
  fi
done

case "${1:-cron-foreground}" in
  once:*)
    script="${1#once:}"
    cd "$FLEET_REPO"
    exec bash "/fleet-kit/scripts/$script"
    ;;
  shell)
    exec bash
    ;;
  cron-foreground)
    LOG_DIR="${FLEET_LOG_DIR:-/var/log/fleet-kit}"
    mkdir -p "$LOG_DIR"

    # Start the live dashboard in the background -- this is the whole point of exposing a
    # port from the container. Without this, cron-foreground runs the loop with nothing
    # observable from outside except raw log files inside the container.
    ( cd /fleet-kit && FLEET_ENV_FILE=/fleet-kit/fleet.env exec python3 scripts/fleet_view_server.py \
        >> "$LOG_DIR/fleet_view.log" 2>&1 ) &
    echo "[entrypoint] fleet_view_server started on :${FLEET_VIEW_PORT:-8420} (pid $!)"

    CRONTAB=/etc/cron.d/fleet-kit
    {
      echo "FLEET_ENV_FILE=/fleet-kit/fleet.env"
      echo "PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      echo "GH_TOKEN=${GH_TOKEN}"
      echo "HOME=/root"
      echo
      # Cadences match schedulers/README.md's table. gitpull only matters for a persistent
      # container across restarts within one run -- harmless no-op on a fresh clone.
      echo "*/10 * * * * root cd $FLEET_REPO && git pull --ff-only >> $LOG_DIR/gitpull.log 2>&1"
      echo "0 * * * * root bash /fleet-kit/scripts/worktree_builder.sh >> $LOG_DIR/builder.log 2>&1"
      echo "*/15 * * * * root bash /fleet-kit/scripts/code_review_local.sh >> $LOG_DIR/review.log 2>&1"
    } > "$CRONTAB"
    chmod 0644 "$CRONTAB"
    echo "[entrypoint] installed crontab:"
    cat "$CRONTAB"
    cron -f
    ;;
  *)
    exec "$@"
    ;;
esac
