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

    # GH_TOKEN in a cron job's shell only authenticates the `gh` CLI -- a bare `git pull`
    # (the */10 canary above) has no credential path of its own and fails outright
    # ("could not read Username for 'https://github.com'") even with a valid token exported
    # right next to it (#3095: /repo sat 8 commits behind origin/main for ~24h before this
    # was caught). Wiring the credential helper here, once at boot, covers every future
    # `git` invocation under this HOME -- `gh auth git-credential` itself reads GH_TOKEN
    # from the environment at call time, so it doesn't need a value baked in now.
    git config --global credential.helper '!gh auth git-credential'

    # Every real member runs through run_member.sh now (2026-08-21 -- this crontab previously
    # only ever ran worktree_builder.sh + judge-judy.sh directly, predating run_member.sh and
    # missing gru/jefe/roomba/dumbledore/messenger entirely; a container built from this image
    # would have silently run 2 of 7 members forever). Cadences match each member's own
    # schedule in members/*/*.fleet.json -- see schedulers/README.md for the human-readable
    # table. the-fixer keeps a coarse poll here as the prod-down backstop (no GitHub event
    # exists for "the site is dark with no failing workflow run") even with the webhook wired.
    # Source fleet.env here too -- FLEET_GRU_CADENCE and any future crontab-shape dial must be
    # visible to THIS shell (the heredoc below runs in entrypoint's own process) to affect the
    # generated crontab at all; run_member.sh sourcing it per-job is a separate, later read that
    # cannot retroactively change minutes already baked into the crontab file. set -a/+a per the same
    # reasoning as run_member.sh's own sourcing (2026-08-22 GH_TOKEN incident writeup there).
    [ -f "${FLEET_ENV_FILE:-/fleet-kit/fleet.env}" ] && { set -a; . "${FLEET_ENV_FILE:-/fleet-kit/fleet.env}"; set +a; }

    CRONTAB=/etc/cron.d/fleet-kit
    {
      echo "FLEET_ENV_FILE=/fleet-kit/fleet.env"
      echo "PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      echo "HOME=/root"
      echo
      echo "*/10 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && cd $FLEET_REPO && git pull --ff-only >> $LOG_DIR/gitpull.log 2>&1"
      # Backstop poll widened */2 -> hourly (2026-08-22, Reif: "don't want to see it crying so
      # much, costs 20 cents a run") -- every tick spawns a real claude -p turn even on green
      # (check.sh gates the reasoning depth, not the LLM spin-up cost itself), and the webhook
      # above already covers the fast CI/deploy-red path in near-real-time. This tick only needs
      # to catch prod-down-with-no-failing-workflow-run, which doesn't need sub-hour latency.
      echo "47 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh the-fixer >> $LOG_DIR/the-fixer.log 2>&1"
      # Widened */5 -> hourly (2026-08-23, Reif): 215/215 runs at */5 had failed since it was
      # enabled (own config set max_budget_usd=0 -- claude -p died before any work, fixed
      # alongside this), so */5 was pure churn, not signal. Now that it does real work (the
      # local messenger driver verifies transcripts are actually findable), hourly is plenty --
      # nothing about "is a transcript readable" needs sub-hour latency.
      echo "51 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh dont-shoot-the-messenger >> $LOG_DIR/dont-shoot-the-messenger.log 2>&1"
      echo "*/15 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh judge-judy >> $LOG_DIR/judge-judy.log 2>&1"
      # Hourly/daily anchors nudged OFF the-fixer's even-minute grid (*/2) and gitpull's
      # ten-minute grid (*/10) -- :00/:20/:30/:40 all landed exactly on both, so every one of
      # these fired shoulder-to-shoulder with a poll every single time instead of getting a
      # clear tick to itself. Minutes below are arbitrary but deliberately off both grids.
      # gru's cron field is instance-tunable via FLEET_GRU_CADENCE (default "*", i.e.
      # hourly at :03) -- 2026-08-28, Reif: instances doing "small build mode" set this to
      # "*/2" in their own fleet.env without forking this file. Default is unchanged from
      # the original hourly schedule.
      echo "3 ${FLEET_GRU_CADENCE:-*} * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_gru_fanout.sh"
      echo "21 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh jefe >> $LOG_DIR/jefe.log 2>&1"
      echo "41 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh roomba >> $LOG_DIR/roomba.log 2>&1"
      echo "33 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh marie >> $LOG_DIR/marie.log 2>&1"
      # datta (:12, analysis) -- the coverage dispatcher. It spawns nerds itself, so ONLY datta
      # gets a cron line; nerd ships enabled:false and never self-fires, exactly like minion
      # under gru. Added 2026-08-26 after roomba filed nonprofit-atlas#3321: datta had been
      # enabled+scheduled in its own spec since 11:39 that day and had run ZERO times, because
      # a member's spec does not put it on cron -- THIS hand-maintained list does, and nobody
      # remembered. The dashboard read "never run" and nothing else complained.
      echo "12 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh datta >> $LOG_DIR/datta.log 2>&1"
      # dumbledore: daily -> every 7h (2026-08-25, Reif), now that it OWNS the Magikarp score
      # rather than treating it as one rot-hunt item among five. A once-daily owner gets 1
      # feedback tick per day against a score sampled every 3h; at 7h it gets 3-4, which is
      # what makes its Prediction/Last-verdict loop mean anything.
      #
      # Explicit hours, NOT `13 */7 * * *`: cron's step operator restarts the pattern each day,
      # so */7 fires at 00,07,14,21 and then again at 00 -- a 3h gap across midnight, not 7h.
      # 01/08/15/22 keeps 15:13-ish (its long-standing slot) in the rotation and stays off the
      # :03/:21/:33/:41/:47/:51 minutes the other members already own.
      echo "13 1,8,15,22 * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh dumbledore >> $LOG_DIR/dumbledore.log 2>&1"
      # sentry: every 3h, the USER-FACING surfaces (990 search/report, superadmin, this
      # dashboard). Explicit hours for the same reason dumbledore uses them -- `*/3` restarts
      # its pattern each day. :17 is unclaimed (:03/:12/:13/:21/:33/:41 are taken).
      echo "17 0,3,6,9,12,15,18,21 * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh sentry >> $LOG_DIR/sentry.log 2>&1"
      # self_improve_score.sh: NOT a member (no members/*/*.fleet.json), so it was invisible
      # to selftest's "every scheduled member is actually on cron" check (#114) and had no
      # line here at all -- the exact same missing-cron-line failure class that bit datta
      # (nonprofit-atlas#3321), recurring in the one place that check cannot see because it
      # only walks members/*/*.fleet.json. Found by dumbledore 2026-08-28: self_improve_score.jsonl
      # did not exist anywhere under $LOG_DIR, so the Magikarp score dumbledore and jefe both
      # read every pass had never been computed on this box, ever. Hourly at :07 (unclaimed --
      # see the minute map in the comments above) is frequent enough to catch each 3h slot
      # (00/03/06...) within an hour of it opening; the script's own SLOT idempotency guard
      # makes every other tick inside the same window a fast, cheap no-op.
      echo "7 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/self_improve_score.sh >> $LOG_DIR/self_improve_score_cron.log 2>&1"
    } > "$CRONTAB"
    chmod 0644 "$CRONTAB"
    echo "[entrypoint] installed crontab (token redacted, stored separately at $TOKEN_FILE, mode 600):"
    cat "$CRONTAB"

    # Watchdog around cron -f, not a bare foreground exec (2026-08-23, issue #3093): every
    # scheduled member AND the account-independent */10 git-pull canary went silent
    # fleet-wide for ~4h11m while `cron -f` stayed up the whole time (same pid, no crash, no
    # restart) -- then resumed on its own with zero log trace explaining the gap. This image
    # has no syslog daemon, so cron's own job-dispatch log (normally syslog's cron facility)
    # goes nowhere either way; the canary's log file is the only externally-visible signal
    # that cron is actually firing. This loop is that external signal's consumer: if the
    # canary goes stale well past its own 10-minute cadence, restart cron rather than trust a
    # human to notice the whole fleet went quiet.
    cron -f &
    CRON_PID=$!
    echo "[entrypoint] cron started (pid $CRON_PID)"
    CANARY="$LOG_DIR/gitpull.log"
    STALL_THRESHOLD_S="${FLEET_CRON_STALL_THRESHOLD_S:-1800}"
    WATCHDOG_LOG="$LOG_DIR/cron_watchdog.log"
    while true; do
      sleep 300
      if ! kill -0 "$CRON_PID" 2>/dev/null; then
        echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] cron pid $CRON_PID gone -- restarting" \
          | tee -a "$WATCHDOG_LOG"
        cron -f &
        CRON_PID=$!
        continue
      fi
      if [ -f "$CANARY" ]; then
        age=$(( $(date +%s) - $(stat -c %Y "$CANARY") ))
        if [ "$age" -gt "$STALL_THRESHOLD_S" ]; then
          echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] CRITICAL: $CANARY stale ${age}s (> ${STALL_THRESHOLD_S}s) -- cron pid $CRON_PID alive but not firing jobs, restarting it" \
            | tee -a "$WATCHDOG_LOG"
          kill -9 "$CRON_PID" 2>/dev/null || true
          wait "$CRON_PID" 2>/dev/null || true
          cron -f &
          CRON_PID=$!
          echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] cron restarted (pid $CRON_PID)" \
            | tee -a "$WATCHDOG_LOG"
        fi
      fi
    done
    ;;
  *)
    exec "$@"
    ;;
esac
