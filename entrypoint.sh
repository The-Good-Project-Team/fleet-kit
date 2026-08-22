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
  FLEET_REPO_URL="${FLEET_REPO_URL:?FLEET_REPO ($FLEET_REPO) does not exist yet -- set FLEET_REPO_URL to clone it}"
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
    ( cd /fleet-kit && FLEET_ENV_FILE=/fleet-kit/fleet.env FLEET_LOG_DIR="$LOG_DIR" \
        exec python3 scripts/fleet_view_server.py \
        >> "$LOG_DIR/fleet_view.log" 2>&1 ) &
    echo "[entrypoint] fleet_view_server started on :${FLEET_VIEW_PORT:-8420} (pid $!)"

    # Webhook receiver, same pattern -- event-driven the-fixer trigger (a red CI/deploy run
    # fires it directly instead of waiting on its own poll interval). Only starts if a secret
    # is actually provisioned; a container with none just doesn't offer this path, same as any
    # other optional driver in this kit (messenger, prod-diag).
    if [ -s /fleet-kit/.webhook_secret ]; then
      ( cd /fleet-kit && FLEET_WEBHOOK_SECRET="$(cat /fleet-kit/.webhook_secret)" \
          FLEET_REPO="$FLEET_REPO" FLEET_LOG_DIR="$LOG_DIR" \
          exec python3 scripts/webhook_receiver.py --port "${FLEET_WEBHOOK_PORT:-8562}" \
          >> "$LOG_DIR/webhook_receiver.log" 2>&1 ) &
      echo "[entrypoint] webhook_receiver started on :${FLEET_WEBHOOK_PORT:-8562} (pid $!)"
    else
      echo "[entrypoint] no .webhook_secret found -- webhook_receiver not started (poll-only mode)"
    fi

    # GH_TOKEN lives in its own root-only file, never inline in the crontab -- /etc/cron.d
    # entries are world-readable by design (0644, so cron itself and any exec'd user can read
    # them) and `cat`/`podman logs`/a debugging session dumping the crontab for cadence review
    # would otherwise reprint the live token in plaintext every time. Each cron job sources
    # this file itself instead.
    TOKEN_FILE=/root/.gh_token
    umask 077
    printf '%s' "$GH_TOKEN" > "$TOKEN_FILE"

    # Every real member runs through run_member.sh now (2026-08-21 -- this crontab previously
    # only ever ran worktree_builder.sh + judge-judy.sh directly, predating run_member.sh and
    # missing gru/jefe/roomba/dumbledore/messenger entirely; a container built from this image
    # would have silently run 2 of 7 members forever). Cadences match each member's own
    # schedule in members/*/*.fleet.json -- see schedulers/README.md for the human-readable
    # table. the-fixer keeps a coarse poll here as the prod-down backstop (no GitHub event
    # exists for "the site is dark with no failing workflow run") even with the webhook wired.
    CRONTAB=/etc/cron.d/fleet-kit
    {
      echo "FLEET_ENV_FILE=/fleet-kit/fleet.env"
      echo "PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      echo "HOME=/root"
      echo
      echo "*/10 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && cd $FLEET_REPO && git pull --ff-only >> $LOG_DIR/gitpull.log 2>&1"
      echo "*/2 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh the-fixer >> $LOG_DIR/the-fixer.log 2>&1"
      echo "*/5 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh dont-shoot-the-messenger >> $LOG_DIR/dont-shoot-the-messenger.log 2>&1"
      echo "*/15 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh judge-judy >> $LOG_DIR/judge-judy.log 2>&1"
      # Hourly/daily anchors nudged OFF the-fixer's even-minute grid (*/2) and gitpull's
      # ten-minute grid (*/10) -- :00/:20/:30/:40 all landed exactly on both, so every one of
      # these fired shoulder-to-shoulder with a poll every single time instead of getting a
      # clear tick to itself. Minutes below are arbitrary but deliberately off both grids.
      echo "3 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_gru_fanout.sh"
      echo "21 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh jefe >> $LOG_DIR/jefe.log 2>&1"
      echo "41 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh roomba >> $LOG_DIR/roomba.log 2>&1"
      echo "33 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh marie >> $LOG_DIR/marie.log 2>&1"
      echo "13 15 * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh dumbledore >> $LOG_DIR/dumbledore.log 2>&1"
    } > "$CRONTAB"
    chmod 0644 "$CRONTAB"
    echo "[entrypoint] installed crontab (token redacted, stored separately at $TOKEN_FILE, mode 600):"
    cat "$CRONTAB"
    cron -f
    ;;
  *)
    exec "$@"
    ;;
esac
