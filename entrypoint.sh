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

# gh#592: register the worktree-isolation PreToolUse guard into every account's own
# settings.json -- $CLAUDE_CONFIG_DIR is account-specific (see account_pool.sh), so this must
# land in each /root/.claude-<account> dir above, not just the bare default; see
# worktree_guard_hook_install.py's own header for why. Idempotent (safe every boot), merges
# rather than overwrites, and never blocks boot on failure -- a missing guard is worse than a
# silent one only if nobody is told, so this warns loudly instead of `set -e` killing the
# container over it.
guard_targets=("/root/.claude")
for acct in ${FLEET_ACCOUNTS:-primary}; do
  guard_targets+=("/root/.claude-$acct")
done
if ! python3 /fleet-kit/scripts/worktree_guard_hook_install.py "${guard_targets[@]}"; then
  echo "[entrypoint] WARNING: worktree_guard_hook_install.py failed -- gh#592's mechanical worktree guard is NOT registered this boot"
fi

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

    # Publish this instance's own FLEET_SHARE_FRACTION for sibling containers to read (gh#293)
    # -- see scripts/publish_share.sh for why. A no-op when FLEET_SHARE_DIR is unset. Runs here
    # too (not just from inside check_share_sum.sh itself) so a sibling has something to read
    # even before this instance's first jefe pass.
    bash /fleet-kit/scripts/publish_share.sh || true

    # FLEET_CRON_MEMBERS (gh#138): every member with its own `run_member.sh <name>` line below
    # used to be installed unconditionally, regardless of what fleet.env declared this instance
    # to be -- a "judge-judy only" box still ran the full 9-script crew. Unset (the default)
    # keeps today's behavior byte-for-identical: every known member goes on cron, same as
    # before this existed. Set it to a space/comma-separated subset (e.g.
    # `FLEET_CRON_MEMBERS=judge-judy`) to schedule only those. dont-shoot-the-messenger is
    # excluded from ALL_CRON_MEMBERS because its own cron line is already commented out
    # (archived 2026-09-04, see below) -- re-enabling it is a separate step from this mechanism.
    ALL_CRON_MEMBERS=(the-fixer judge-judy gru jefe roomba marie datta dumbledore sentry librarian)
    if [ -n "${FLEET_CRON_MEMBERS:-}" ]; then
      IFS=', ' read -ra RESOLVED_CRON_MEMBERS <<< "$FLEET_CRON_MEMBERS"
      for m in "${RESOLVED_CRON_MEMBERS[@]}"; do
        known=0
        for candidate in "${ALL_CRON_MEMBERS[@]}"; do
          [ "$m" = "$candidate" ] && known=1 && break
        done
        if [ "$known" -ne 1 ]; then
          echo "[entrypoint] FATAL: FLEET_CRON_MEMBERS names unknown member '$m' -- known members: ${ALL_CRON_MEMBERS[*]}" >&2
          exit 1
        fi
      done
    else
      RESOLVED_CRON_MEMBERS=("${ALL_CRON_MEMBERS[@]}")
    fi

    cron_member_enabled() {
      local name="$1" m
      for m in "${RESOLVED_CRON_MEMBERS[@]}"; do
        [ "$m" = "$name" ] && return 0
      done
      return 1
    }

    CRONTAB=/etc/cron.d/fleet-kit
    {
      echo "FLEET_ENV_FILE=/fleet-kit/fleet.env"
      echo "PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      echo "HOME=/root"
      # gh#569: deploy.sh's `docker run -e FLEET_SHARE_DIR=...` only reaches PID 1 and its
      # direct children -- every cron-triggered job starts from this block instead, which never
      # forwarded it, so check_share_sum.sh (and any other cron-triggered reader) silently saw
      # it unset and reported "ok: shares total 0" while the shared account was really
      # oversubscribed. Forward PID 1's own value (empty/unset falls through to
      # check_share_sum.sh's pre-gh#293 host-side scan unchanged, same as today).
      echo "FLEET_SHARE_DIR=${FLEET_SHARE_DIR:-}"
      # gh#579: same env-forwarding gap as gh#569 above, sibling variable. maxx_lease.py's
      # ledger is SHARED ACROSS INSTANCES only when FLEET_LEASE_DIR is set -- unforwarded here,
      # a cron-triggered pass falls back to a per-instance/empty ledger and silently reports
      # reserved_pct: 0 even when the real shared bind-mount is populated.
      echo "FLEET_LEASE_DIR=${FLEET_LEASE_DIR:-}"
      # gh#581: same env-forwarding gap as gh#569/gh#579 above, third sibling variable.
      # publish_share.sh resolves this container's identity as
      # `${FLEET_INSTANCE_NAME:-default}` -- unforwarded here, every cron-triggered process
      # (including check_share_sum.sh) falls back to the literal string "default" and
      # multiple real instances all publish their fraction under the same shared
      # `default.json` key, each overwriting whichever instance's cron tick ran last.
      echo "FLEET_INSTANCE_NAME=${FLEET_INSTANCE_NAME:-}"
      echo
      # Canary must record that cron FIRED, independent of whether git had anything to say
      # (2026-09-04, gh#4340): the old form only touched gitpull.log when git printed output,
      # so a pull that died early -- e.g. `fatal: Cannot fast-forward to multiple branches.`
      # after a member left branch.main.merge pointing at a deleted member/* branch -- left the
      # canary untouched for hours. The watchdog below read that as "cron not firing" and
      # kill -9'd cron every 5 minutes, killing in-flight member runs with it. `date -u` first,
      # unconditionally, so the canary means "cron fired".
      #
      # git_pull_guard.sh (gh#68), not a bare `git pull --ff-only`: fetches `origin main`
      # directly (same reasoning as the old explicit `origin main` above -- a dirty
      # branch.main.merge cannot wedge it), and when $FLEET_REPO's checked-out branch can never
      # fast-forward onto it (nonprofit-atlas#3130: a squash-merged branch deleted upstream),
      # self-heals onto main instead of spinning on the same dead ref every 10 minutes. Still
      # writes to gitpull.log either way, so the canary above stays meaningful.
      echo "*/10 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE); date -u >> $LOG_DIR/gitpull.log 2>&1; cd $FLEET_REPO && bash /fleet-kit/scripts/git_pull_guard.sh >> $LOG_DIR/gitpull.log 2>&1"
      # Backstop poll widened */2 -> hourly (2026-08-22, Reif: "don't want to see it crying so
      # much, costs 20 cents a run") -- every tick spawns a real claude -p turn even on green
      # (check.sh gates the reasoning depth, not the LLM spin-up cost itself), and the webhook
      # above already covers the fast CI/deploy-red path in near-real-time. This tick only needs
      # to catch prod-down-with-no-failing-workflow-run, which doesn't need sub-hour latency.
      if cron_member_enabled the-fixer; then
        echo "47 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh the-fixer >> $LOG_DIR/the-fixer.log 2>&1"
      fi
      # Widened */5 -> hourly (2026-08-23, Reif): 215/215 runs at */5 had failed since it was
      # enabled (own config set max_budget_usd=0 -- claude -p died before any work, fixed
      # alongside this), so */5 was pure churn, not signal. Now that it does real work (the
      # local messenger driver verifies transcripts are actually findable), hourly is plenty --
      # nothing about "is a transcript readable" needs sub-hour latency.
      # ARCHIVED 2026-09-04 (Reif): this instance has no FLEET_MESSENGER_DRIVER set, so
      # dont-shoot-the-messenger.sh exits at its first guard ("no driver configured -- logs
      # stay local-only, transcript relay unavailable. This is a valid mode, not an error.")
      # having done nothing. Every tick still paid for a full `claude -p` spawn to run a
      # script that returns immediately. The comment above describes a local driver doing
      # real work -- that is NOT true on this instance: `grep FLEET_MESSENGER_DRIVER
      # fleet.env` returns nothing. Re-enable by setting FLEET_MESSENGER_DRIVER to an
      # executable driver, then uncommenting the line below.
      # echo "51 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh dont-shoot-the-messenger >> $LOG_DIR/dont-shoot-the-messenger.log 2>&1"
      if cron_member_enabled judge-judy; then
        echo "*/15 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh judge-judy >> $LOG_DIR/judge-judy.log 2>&1"
      fi
      # auto_update_branch.sh (gh#172): nothing else in this repo keeps an open PR's branch
      # current with main, so one merge pushes every other open PR BEHIND/BLOCKED forever --
      # confirmed live 2026-08-29/30 at "full saturation" (8/8 open PRs stuck simultaneously,
      # the only remedy a human/jefe hand-running update-branch per PR). Not a member (no
      # members/*/*.fleet.json, same shape as self_improve_score.sh below) so it needs its own
      # line here -- a script existing and being documented does not mean anything schedules
      # it (that exact gap already bit datta gh#3321 and self_improve_score.sh/account_health
      # below). Minutes 5/20/35/50 run just ahead of judge-judy's :00/15/30/45 tick above, so a
      # branch synced here has fresh checks ready in time for judge-judy's next pick instead of
      # both mechanisms serializing their own separate waits.
      echo "5,20,35,50 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/auto_update_branch.sh >> $LOG_DIR/auto_update_branch.log 2>&1"
      # Hourly/daily anchors nudged OFF the-fixer's even-minute grid (*/2) and gitpull's
      # ten-minute grid (*/10) -- :00/:20/:30/:40 all landed exactly on both, so every one of
      # these fired shoulder-to-shoulder with a poll every single time instead of getting a
      # clear tick to itself. Minutes below are arbitrary but deliberately off both grids.
      # gru's cron field is instance-tunable via FLEET_GRU_CADENCE (default "*", i.e.
      # hourly at :03) -- 2026-08-28, Reif: instances doing "small build mode" set this to
      # "*/2" in their own fleet.env without forking this file. Default is unchanged from
      # the original hourly schedule.
      if cron_member_enabled gru; then
        echo "3 ${FLEET_GRU_CADENCE:-*} * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_gru_fanout.sh >> $LOG_DIR/gru.log 2>&1"
      fi
      if cron_member_enabled jefe; then
        echo "21 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh jefe >> $LOG_DIR/jefe.log 2>&1"
      fi
      if cron_member_enabled roomba; then
        echo "41 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh roomba >> $LOG_DIR/roomba.log 2>&1"
      fi
      # librarian (philanthropy#4439, nonprofit-atlas#4410 seq:1): scrubs credential-shaped
      # strings out of session transcripts and enforces the compress/drop retention window.
      # Hourly like roomba/marie/datta -- a live credential leak on disk does not get a slower
      # cadence than hygiene work does. :55 is unclaimed on the minute map above.
      if cron_member_enabled librarian; then
        echo "55 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh librarian >> $LOG_DIR/librarian.log 2>&1"
      fi
      if cron_member_enabled marie; then
        echo "33 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh marie >> $LOG_DIR/marie.log 2>&1"
      fi
      # datta (:12, analysis) -- the coverage dispatcher. It spawns nerds itself, so ONLY datta
      # gets a cron line; nerd ships enabled:false and never self-fires, exactly like minion
      # under gru. Added 2026-08-26 after roomba filed nonprofit-atlas#3321: datta had been
      # enabled+scheduled in its own spec since 11:39 that day and had run ZERO times, because
      # a member's spec does not put it on cron -- THIS hand-maintained list does, and nobody
      # remembered. The dashboard read "never run" and nothing else complained.
      # datta's hour field is instance-tunable via FLEET_DATTA_CADENCE (default "*", hourly at
      # :12), the same splice shape as FLEET_GRU_CADENCE above and validated by the same
      # _CRON_HOUR_FIELDS family (fleet-kit#514). On the fleet-kit instance datta+nerd were
      # 47% of the window analysing the fleet itself while the score they feed sat flat for
      # 11 days; "9" makes that a daily 09:12 pass instead of 24 hourly ones.
      if cron_member_enabled datta; then
        echo "12 ${FLEET_DATTA_CADENCE:-*} * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh datta >> $LOG_DIR/datta.log 2>&1"
      fi
      # dumbledore: daily -> every 7h (2026-08-25, Reif), now that it OWNS the Magikarp score
      # rather than treating it as one rot-hunt item among five. A once-daily owner gets 1
      # feedback tick per day against a score sampled every 3h; at 7h it gets 3-4, which is
      # what makes its Prediction/Last-verdict loop mean anything.
      #
      # Explicit hours, NOT `13 */7 * * *`: cron's step operator restarts the pattern each day,
      # so */7 fires at 00,07,14,21 and then again at 00 -- a 3h gap across midnight, not 7h.
      # 01/08/15/22 keeps 15:13-ish (its long-standing slot) in the rotation and stays off the
      # :03/:21/:33/:41/:47/:51 minutes the other members already own.
      if cron_member_enabled dumbledore; then
        echo "13 1,8,15,22 * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh dumbledore >> $LOG_DIR/dumbledore.log 2>&1"
      fi
      # sentry: every 3h, the USER-FACING surfaces (990 search/report, superadmin, this
      # dashboard). Explicit hours for the same reason dumbledore uses them -- `*/3` restarts
      # its pattern each day. :17 is unclaimed (:03/:12/:13/:21/:33/:41 are taken).
      if cron_member_enabled sentry; then
        echo "17 0,3,6,9,12,15,18,21 * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/run_member.sh sentry >> $LOG_DIR/sentry.log 2>&1"
      fi
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
      # number_read.py --fetch (fleet-kit#513): pulls the venture's number from FLEET_NUMBER_URL
      # into $LOG_DIR/number.json so run_member.sh can put it above every charter. Every 6h at
      # :29 (unclaimed on the minute map above); the endpoint caches 6h itself. Sources
      # fleet.env at tick time (FLEET_ENV_FILE is in the crontab env) so a URL added after boot
      # takes effect on the next tick, same reasoning as gh#279. Not a member: a member must
      # never compute its own number (KPI doctrine rule 1).
      echo "29 */6 * * * root set -a; . \$FLEET_ENV_FILE; set +a; FLEET_LOG_DIR=$LOG_DIR python3 /fleet-kit/scripts/number_read.py --fetch >> $LOG_DIR/number_read.log 2>&1"
      # deploy_staleness_check.sh (gh#201): independent of deploy.sh/auto_deploy.sh, so it can
      # catch the case where NEITHER ran in a window -- both delivery paths (#140 poll, #189
      # push) were found down simultaneously with nothing noticing until a human-triggered
      # nerd pass stumbled onto it. Hourly at :57 is unclaimed on the minute map above and
      # comfortably inside the default 4h staleness budget (FLEET_DEPLOY_STALENESS_BUDGET_S).
      echo "57 * * * * root export GH_TOKEN=\$(cat $TOKEN_FILE) && bash /fleet-kit/scripts/deploy_staleness_check.sh >> $LOG_DIR/deploy_staleness_check.log 2>&1"
      # auto_deploy_race_check.sh (gh#255): auto_deploy.sh's own guarded fetch/pull cannot
      # produce a multi-branch fast-forward error or a ref-lock race -- when auto_deploy.cron.log
      # (the HOST crontab's raw stdout/stderr capture, same bind-mounted $FLEET_LOG_DIR as this
      # container reads) shows one anyway, some OTHER unidentified process is running unscoped
      # git ops against the same checkout, and today that is silent until a human happens to
      # tail a raw cron log. Runs inside the container (like deploy_staleness_check.sh above),
      # not on the host: it only reads/appends plain log files, no podman needed.
      #
      # gh#464 (2026-09-05): was hourly at :44 -- but gh#275's own sanctioned-ABORT escalation
      # (SANCTIONED_ABORT_THRESHOLD, 3 consecutive ticks / ~15min at auto_deploy.sh's 5-minute
      # poll cadence) can only ever page as often as THIS cron fires, not as often as it
      # detects. A live diverged-HEAD stall crossed the 3-tick threshold at ~21:05 UTC but the
      # hourly checker didn't evaluate it until :44 -- a ~54min silent window, most of an
      # auto_deploy_race_check.sh cron period, on the fleet's only escalation path for exactly
      # this failure shape. The scan itself is a cheap cursor-based read of append-only log
      # files (no git/podman work), so running it at the same 5-minute cadence as the thing it
      # watches costs nothing and closes that window to ~1 tick (~5min) instead of ~1 hour.
      echo "*/5 * * * * root bash /fleet-kit/scripts/auto_deploy_race_check.sh >> $LOG_DIR/auto_deploy_race_check.log 2>&1"
      # lane_kpi.py (gh#324): independent devops-lane KPI job -- deploy_success_rate/
      # deploy_count_7d were previously only ad-hoc greps a nerd pass ran by hand against
      # auto_deploy.log, i.e. the lane computing its own number (kpi-doctrine.md rule 1
      # violation). Runs inside the container like deploy_staleness_check.sh/
      # auto_deploy_race_check.sh above, for the same reason: it only reads/appends plain
      # files under $LOG_DIR (auto_deploy.log, fleet.db), both already reachable over the same
      # bind mount those two scripts use -- no podman needed. Hourly at :14 (unclaimed on the
      # minute map above); its own read_latest() flags a reading stale past 2x this interval.
      #
      # A plain python3 script, unlike deploy_staleness_check.sh, has no shell preamble to
      # source fleet.env itself -- so this cron line sources it inline (same shape
      # account_health_check.sh's line above uses, for the same reason: FLEET_LOG_DIR must
      # resolve to the container-scoped /var/log/fleet-kit fleet.env sets, not lane_kpi.py's
      # own $HOME-based fallback) before invoking it. `export FLEET_LOG_DIR=$LOG_DIR` first,
      # same as account_health_check.sh's line -- if fleet.env is ever missing/unreadable at
      # tick time the `[ -f ... ]` guard below short-circuits and never sources it, and without
      # this export lane_kpi.py would silently fall back to fleet_db.py's own $HOME-based
      # default and read/write a completely different, wrong fleet.db with no error at all.
      echo "14 * * * * root export FLEET_LOG_DIR=$LOG_DIR && [ -f \"\${FLEET_ENV_FILE:-/fleet-kit/fleet.env}\" ] && { set -a; . \"\${FLEET_ENV_FILE:-/fleet-kit/fleet.env}\"; set +a; }; python3 /fleet-kit/scripts/lane_kpi.py record >> $LOG_DIR/lane_kpi.log 2>&1"
      # account_health_check.sh + tunnel_health_check.sh (gh#171): the fleet's only outage
      # pagers per README step 6. PR#169 wired both into schedulers/systemd + schedulers/launchd
      # -- the bare-host path -- but never into THIS heredoc, the container-native path, so
      # every container deployment has run with zero outage paging since #154 was closed. Same
      # failure class as self_improve_score.sh above: a script existing and being documented as
      # required does not mean anything schedules it here.
      #
      # Neither script sources fleet.env (see each script's own header -- both must stay plain
      # bash, zero Claude Code dependency, so the watcher never depends on the thing it's
      # watching), so their env vars are exported explicitly on the cron line itself, same shape
      # as GH_TOKEN above. PUBLIC_URL is read from fleet.env here (already sourced into
      # entrypoint's own shell above) and baked into the generated crontab text at boot -- out
      # of scope for gh#279 below, see that issue's non-goals.
      #
      # NTFY_TOPIC is deliberately NOT baked in here (gh#279): entrypoint.sh only runs once, at
      # container boot, so a value interpolated at generation time is frozen until the next
      # restart -- a human editing fleet.env's NTFY_TOPIC= later (e.g. during THIS incident,
      # gh#269) would have no way to make the pager pick it up short of a full container
      # restart. Instead, each cron LINE re-sources fleet.env itself, immediately before
      # `exec`ing the script, the same `[ -f ... ] && { set -a; . ...; set +a; }` shape
      # entrypoint.sh itself uses above (and run_member.sh per-job) -- so the value is re-read
      # at every tick, not just at boot. FLEET_ENV_FILE is already exported crontab-wide (see
      # the heredoc's own first line below), so this reuses that instead of hard-coding the
      # path again. The scripts themselves still never source fleet.env directly (per gh#171's
      # non-goal, restated in gh#279's) -- this sourcing happens in the cron line, one shell
      # hop before the script starts, not inside it. If NTFY_TOPIC is still unset in fleet.env,
      # the freshly-sourced value is still unset/empty and each script's own `:?` guard fires
      # exactly as before -- this changes when the value is read, not what happens when it's
      # genuinely absent.
      echo "27 * * * * root export FLEET_LOG_DIR=$LOG_DIR && [ -f \"\${FLEET_ENV_FILE:-/fleet-kit/fleet.env}\" ] && { set -a; . \"\${FLEET_ENV_FILE:-/fleet-kit/fleet.env}\"; set +a; }; bash /fleet-kit/scripts/account_health_check.sh >> $LOG_DIR/account_health_check.log 2>&1"
      echo "37 * * * * root export PUBLIC_URL=${PUBLIC_URL:-} FLEET_VIEW_PORT=${FLEET_VIEW_PORT:-8420} && [ -f \"\${FLEET_ENV_FILE:-/fleet-kit/fleet.env}\" ] && { set -a; . \"\${FLEET_ENV_FILE:-/fleet-kit/fleet.env}\"; set +a; }; bash /fleet-kit/scripts/tunnel_health_check.sh >> $LOG_DIR/tunnel_health_check.log 2>&1"
      # path_health_check.sh (gh#249): the fleet's THIRD outage pager -- tunnel-health above
      # only checks the tunnel's ROOT hostname, which falls through Caddy's default route and
      # never touches either instance's real path-routed dashboard (/fleet/<name>). Same
      # failure class as account/tunnel-health (gh#171) and self_improve_score.sh/
      # deploy_staleness_check.sh above: PR#238 shipped the script and it worked when invoked
      # by hand, but nothing here scheduled it, so it never survived a redeploy.
      #
      # PUBLIC_PATH_URL is per-instance (each box's own /fleet/<name> path, e.g.
      # https://dino.luckymachines.co/fleet/fleet-kit) -- unlike PUBLIC_URL/NTFY_TOPIC above,
      # there is no fleet-wide value, so it must be set in THIS box's own fleet.env for the
      # page to fire at all. Like NTFY_TOPIC, it is deliberately left unset by default: the
      # script's own `:?` guard fails loudly and logs why until an operator sets one, rather
      # than silently checking nothing. NTFY_TOPIC is re-sourced at tick-time here too (gh#279,
      # see account_health_check.sh's cron line above for the full reasoning).
      echo "24 * * * * root export PUBLIC_PATH_URL=${PUBLIC_PATH_URL:-} STATE_FILE=$LOG_DIR/.path_health_paged.state && [ -f \"\${FLEET_ENV_FILE:-/fleet-kit/fleet.env}\" ] && { set -a; . \"\${FLEET_ENV_FILE:-/fleet-kit/fleet.env}\"; set +a; }; bash /fleet-kit/scripts/path_health_check.sh >> $LOG_DIR/path_health_check.log 2>&1"
      # sync_health_check.sh (gh#273): the fleet's FOURTH outage pager -- fleet.db's own
      # sync_state.offset against runs.jsonl's byte size, the ONLY watchdog on whether
      # fleet_view_server.py's tail_runs_forever thread (which every nerd/gru/dumbledore read
      # of fleet.db depends on) is still alive. Unlike account/tunnel/path-health above, this
      # runs every 5 minutes rather than hourly: the sync loop itself ticks every 2 seconds, so
      # a dead thread is a much faster-onset failure than a dead account pool or tunnel, and an
      # hourly sample could sit on a stalled fleet.db for up to an hour before even taking its
      # first reading. NTFY_TOPIC re-sourced at tick-time, same reasoning as the cron lines
      # above (gh#279).
      echo "*/5 * * * * root export FLEET_LOG_DIR=$LOG_DIR && [ -f \"\${FLEET_ENV_FILE:-/fleet-kit/fleet.env}\" ] && { set -a; . \"\${FLEET_ENV_FILE:-/fleet-kit/fleet.env}\"; set +a; }; bash /fleet-kit/scripts/sync_health_check.sh >> $LOG_DIR/sync_health_check.log 2>&1"
    } > "$CRONTAB"
    chmod 0644 "$CRONTAB"
    # Validate BEFORE cron ever reads this file (2026-09-05, gh#4340). Vixie cron rejects the
    # ENTIRE file when one time field is out of range -- it does not skip the bad line. On
    # 2026-09-03 `FLEET_GRU_CADENCE=0,30` (meant as "every 30 minutes", but this dial feeds the
    # HOUR field) rendered `3 0,30 * * *`; hour 30 is out of range, so all 18 fleet jobs went
    # silent at once for ~40h while `cron -f` sat there looking perfectly healthy and the
    # watchdog restarted it 500+ times against a file cron was never going to load.
    # --fix comments out only the offending line, so one bad dial costs one job, not the fleet.
    if ! python3 /fleet-kit/scripts/validate_crontab.py "$CRONTAB" --fix; then
      echo "[entrypoint] CRITICAL: crontab failed validation and could not be repaired" >&2
    fi
    echo "[entrypoint] resolved cron members (FLEET_CRON_MEMBERS=${FLEET_CRON_MEMBERS:-<unset, full list>}): ${RESOLVED_CRON_MEMBERS[*]}"
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
    # Every watchdog line is tagged with this container's own hostname (= container id short
    # form) (2026-09-04, gh#4340): a blue-green deploy left `philanthropy-green`'s entrypoint
    # alive for 9 days after podman had forgotten the container, still bind-mounted to the LIVE
    # instance's logs + repo. Because pids are host-global under rootless podman, its watchdog's
    # `kill -9 $CRON_PID` killed the RUNNING container's cron. Untagged log lines made that
    # look like one flapping watchdog instead of two fighting.
    WATCHDOG_TAG="${HOSTNAME:-unknown}"
    cron -f &
    CRON_PID=$!
    echo "[entrypoint] cron started (pid $CRON_PID, watchdog $WATCHDOG_TAG)"
    CANARY="$LOG_DIR/gitpull.log"
    # Seed the canary at boot (2026-09-04, gh#4340). $LOG_DIR is bind-mounted from the host and
    # SURVIVES the container, so a fresh container inherits the previous one's gitpull.log --
    # already minutes or hours stale. auto_deploy.sh blue-green-replaces this container roughly
    # every 30 minutes, so without this the watchdog reads that inherited mtime, concludes "cron
    # is not firing" within one 5-minute tick of every single deploy, and kill -9's a cron that
    # has simply not reached its first */10 tick yet. Touching it here means the age measured
    # below is always age-since-THIS-container-started, which is what the check actually means.
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] [$WATCHDOG_TAG] entrypoint start -- seeding canary" >> "$CANARY"
    STALL_THRESHOLD_S="${FLEET_CRON_STALL_THRESHOLD_S:-1800}"
    WATCHDOG_LOG="$LOG_DIR/cron_watchdog.log"
    while true; do
      sleep 300
      if ! kill -0 "$CRON_PID" 2>/dev/null; then
        echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] [$WATCHDOG_TAG] cron pid $CRON_PID gone -- restarting" \
          | tee -a "$WATCHDOG_LOG"
        cron -f &
        CRON_PID=$!
        continue
      fi
      if [ -f "$CANARY" ]; then
        age=$(( $(date +%s) - $(stat -c %Y "$CANARY") ))
        if [ "$age" -gt "$STALL_THRESHOLD_S" ]; then
          echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] [$WATCHDOG_TAG] CRITICAL: $CANARY stale ${age}s (> ${STALL_THRESHOLD_S}s) -- cron pid $CRON_PID alive but not firing jobs, restarting it" \
            | tee -a "$WATCHDOG_LOG"
          # Only ever kill a cron that is genuinely our own child. Guards against the
          # orphaned-watchdog case above, where $CRON_PID may name a pid belonging to a
          # different (live) container after the original exited and the pid was reused.
          if [ "$(ps -o ppid= -p "$CRON_PID" 2>/dev/null | tr -d ' ')" = "$$" ]; then
            kill -9 "$CRON_PID" 2>/dev/null || true
            wait "$CRON_PID" 2>/dev/null || true
          else
            echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] [$WATCHDOG_TAG] refusing to kill pid $CRON_PID -- not our child (orphan watchdog?); exiting" \
              | tee -a "$WATCHDOG_LOG"
            exit 0
          fi
          cron -f &
          CRON_PID=$!
          echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] [$WATCHDOG_TAG] cron restarted (pid $CRON_PID)" \
            | tee -a "$WATCHDOG_LOG"
        fi
      fi
    done
    ;;
  *)
    exec "$@"
    ;;
esac
