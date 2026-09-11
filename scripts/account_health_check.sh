#!/bin/bash
# account_health_check.sh -- page a human when EVERY fleet account has been failing for a
# sustained stretch, independent of any LLM member or the accounts themselves.
#
# WHY THIS CAN'T BE AN LLM MEMBER (like dont-shoot-the-messenger): the failure mode this
# watches for IS "every account is dead" -- an LLM pass to report that would itself run
# through the same dead account pool and silently not run. This must be plain bash, host-side
# cron, zero Claude Code dependency -- same reasoning auto_deploy.sh already documents for why
# IT is a host poll and not a webhook.
#
# WHAT IT WATCHES: account-pool.log's "ALL accounts in '...' failed this call" line (written
# by account_pool.sh once per tick where every account failed), measured against the newest
# SUCCESS line in the same log.
#
# The obvious version of this check is broken, and was, live, until 2026-08-25: taking the age
# of the newest FAILURE line can never page. account_pool.sh appends a fresh failure line every
# tick (~5min) for as long as an outage lasts, so that line is always seconds old, `age_minutes`
# is permanently ~0, and `age >= THRESHOLD_MINUTES` is unreachable by construction. The fleet
# sat down for ~2h emitting "failing but only 4m old" every tick and never paged; a human found
# it in a bar chart instead. The age that actually answers "how long has the pool been down"
# is the age of the last SUCCESS, which is what this reads now.
#
# That fix requires account_pool.sh to log successes at all -- it previously returned 0 silently,
# so no success line ever existed to measure from (the original comment here assumed the caller
# logged one; it did not). account_pool_run now writes "account=<a> call succeeded".
#
# CONFIRMED LIVE 2026-08-24/25: both fleet accounts died silently, ticking every ~5min for
# hours, nobody paged until a human noticed a screenshot didn't match the log's story. This
# closes that gap.
#
# STATE: one marker file remembers whether we already paged for the CURRENT outage, so a
# 5-minute cron doesn't re-page every tick -- one page per outage, one recovery page after.
#
# Usage (cron, mirrors auto_deploy.sh's own invocation shape):
#   */5 * * * * FLEET_LOG_DIR=/home/ubuntu/fleet-kit-logs NTFY_TOPIC=<topic> \
#     bash scripts/account_health_check.sh >> .../health_check.cron.log 2>&1
set -uo pipefail

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${FLEET_LOG_DIR:?set FLEET_LOG_DIR -- same dir account_pool.sh writes account-pool.log into}"
POOL_LOG="$LOG_DIR/account-pool.log"
NTFY_TOPIC="${NTFY_TOPIC:-}"   # optional: fleet_alert.sh emails regardless
THRESHOLD_MINUTES="${ACCOUNT_HEALTH_THRESHOLD_MINUTES:-30}"
# gh#266: the 2026-08-30->09-01 outage paged once at 33 minutes then went silent for the
# remaining ~48h45m of a ~2-day outage -- 585 consecutive ticks logged "already paged" and
# pushed nothing. This re-pages on a fixed cadence while the outage stays open, instead of
# once total. 2h is the issue's own starting point; no data in this repo justifies a different
# default (see gh#266's own UNKNOWN note on ntfy rate limits at shorter cadences).
REPAGE_MINUTES="${ACCOUNT_HEALTH_REPAGE_MINUTES:-120}"
STATE_FILE="${ACCOUNT_HEALTH_STATE_FILE:-$LOG_DIR/.account_health_paged.state}"
CONTAINER_NAME="${FLEET_CONTAINER_NAME:-philanthropy}"
RESTART_STATE_FILE="${ACCOUNT_HEALTH_RESTART_STATE_FILE:-$LOG_DIR/.account_health_restarted.state}"

[ -f "$POOL_LOG" ] || { echo "[account_health_check] no pool log at $POOL_LOG yet -- nothing to check"; exit 0; }

last_line=$(tail -1 "$POOL_LOG")
# STATE_FILE's first line is the ORIGINAL page time, written once and never touched again --
# the recovery message below ("outage flagged at $already_paged") and the re-page message's
# "since first page" both need it unchanged. A second line, `last_repage=<timestamp>`, is
# added/updated only once re-pages start; reusing this one file (not a new one) for both is
# gh#266's own non-goal ("not adding a new state store").
already_paged=""
last_repage_at=""
if [ -f "$STATE_FILE" ]; then
  _repage_line=""
  { read -r already_paged; read -r _repage_line; } < "$STATE_FILE" || true
  last_repage_at="${_repage_line#last_repage=}"
fi

# An age this large cannot be a real outage -- it is a synthetic or epoch-0 timestamp. The
# selftest seeds account-pool.log with a 2020-01-01 fixture, and on 2026-09-04 that produced
# a REAL email claiming "No fleet account has succeeded in 3511675+ minutes" (6.7 years).
# fleet_alert.sh runs by absolute path, so the selftest's stubbed `curl` never intercepted it.
# A pager that cries wolf from its own test suite is worse than no pager: it is indistinguish-
# able from the real thing at a glance, and it is the reason to stop reading the alerts.
MAX_PLAUSIBLE_OUTAGE_MINUTES="${ACCOUNT_HEALTH_MAX_PLAUSIBLE_MINUTES:-10080}"  # 7 days

_ntfy() {
  # mode "page" (default) records a NEW critical in alert_store -- only for a genuine problem.
  # mode "resolve" closes the alarm a prior "page" call opened (gh#401: a recovery message that
  # itself went through the "page" branch was recording as a brand-new unresolved critical, so
  # the real alarm it should have closed never closed either -- mirrors budget_read_check.sh's
  # already-correct --resolve pattern). mode "info" is a self-heal notice for an outage that
  # never escalated to a page in the first place (see the DNS-recovery call site below) -- there
  # is nothing to record or resolve, so it goes through fleet_alert.sh's plain positional form,
  # which deliberately never touches alert_store.
  local title="$1" msg="$2" mode="${3:-page}"
  # Under the selftest, send through the stubbed `curl` on PATH instead of the real helper.
  # fleet_alert.sh runs by ABSOLUTE path, so a PATH stub cannot intercept it -- which is
  # exactly how the suite emailed a human on 2026-09-04. The test still observes a real call
  # (it asserts on NTFY_CALLS_FILE), it just cannot escape to Resend.
  if [ -n "${NTFY_CALLS_FILE:-}" ]; then
    # Mirror fleet_alert.sh's own gate: its ntfy leg is conditional on a non-empty
    # NTFY_TOPIC, so with the topic unset NOTHING may reach ntfy. Defaulting the topic here
    # would fire the leg fleet_alert.sh would have skipped, which is the difference between
    # standing in for the helper and quietly routing around it.
    if [ -n "${NTFY_TOPIC:-}" ]; then
      curl -s -H "Title: $title" -d "$msg" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1
    fi
    return 0
  fi
  case "$mode" in
    resolve)
      bash "$KIT_DIR/scripts/fleet_alert.sh" --resolve --check account_health "$title" "$msg" ;;
    info)
      bash "$KIT_DIR/scripts/fleet_alert.sh" "$title" "$msg" ;;
    repage)
      # Deliberately bypasses alert_store's --check/--severity gate (unlike mode "page" below):
      # the FIRST page already recorded this (check, problem) key as an open critical, and
      # alert_store's own dedupe (`already_paged` in alert_store.py's `record()`) suppresses
      # every later call for that same key until it resolves -- calling that path again here
      # would make a re-page permanently silent, exactly the bug gh#266 exists to fix. The
      # plain positional form always sends, same underlying call as mode "info", but kept as
      # its own case so a reader doesn't read a re-page (an outage that already escalated once
      # and is still open) as info's "never escalated" meaning.
      bash "$KIT_DIR/scripts/fleet_alert.sh" "$title" "$msg" ;;
    *)
      bash "$KIT_DIR/scripts/fleet_alert.sh" \
        --check account_health --problem "$title" --severity critical "$title" "$msg" ;;
  esac || echo "[alert] fleet_alert.sh failed" >&2
}

if [[ "$last_line" != *"ALL accounts in"*"failed this call"* ]]; then
  # newest line in the log is not a failure -- pool is healthy (or has never failed).
  if [ -n "$already_paged" ]; then
    _ntfy "fleet-kit[$CONTAINER_NAME]: accounts recovered" \
      "Instance: $CONTAINER_NAME. Its account pool is succeeding again after the outage flagged at $already_paged." \
      "resolve"
    rm -f "$STATE_FILE" "$RESTART_STATE_FILE"
  fi
  echo "[account_health_check] healthy -- newest pool-log line is not a failure"
  exit 0
fi

# Newest line IS a failure. How long since anything SUCCEEDED? Measuring the newest failure
# line is useless -- it is re-appended every tick during an outage and so is always ~0 minutes
# old (see header). Find the newest success line and measure from that instead.
# GNU `date -d` first (the Linux host this cron runs on), BSD `date -j -f` second (a Mac
# running the kit directly). Without the BSD arm this returns nothing on macOS and the check
# exits quietly having measured nothing -- a dead pager that reports itself as fine, which is
# the same class of silent failure this whole script exists to catch.
_line_epoch() {
  local ts
  ts=$(grep -oE '^\[[0-9-]+ [0-9:]+' <<<"$1" | tr -d '[')
  [ -z "$ts" ] && return 1
  date -u -d "$ts" +%s 2>/dev/null \
    || TZ=UTC date -j -f "%Y-%m-%d %H:%M:%S" "$ts" +%s 2>/dev/null
}

# Same GNU/BSD split as _line_epoch above, for STATE_FILE's own "%Y-%m-%d %H:%M UTC" format
# (paged_at/repaged_at below) rather than the pool log's "%Y-%m-%d %H:%M:%S" format.
_state_ts_epoch() {
  local ts="$1"
  [ -z "$ts" ] && return 1
  date -u -d "$ts" +%s 2>/dev/null \
    || TZ=UTC date -j -f "%Y-%m-%d %H:%M %Z" "$ts" +%s 2>/dev/null
}

now_epoch=$(date +%s)
last_ok_line=$(grep "call succeeded" "$POOL_LOG" | tail -1)

if [ -n "$last_ok_line" ]; then
  line_epoch=$(_line_epoch "$last_ok_line")
else
  # No success has EVER been logged (a pool that has never worked, or a log predating success
  # logging). Fall back to the OLDEST failure line -- the outage is at least that old.
  line_epoch=$(_line_epoch "$(grep "failed this call" "$POOL_LOG" | head -1)")
fi

if [ -z "$line_epoch" ]; then
  echo "[account_health_check] WARNING: could not parse a timestamp to measure from"
  exit 0
fi

age_minutes=$(( (now_epoch - line_epoch) / 60 ))

if [ "$age_minutes" -gt "$MAX_PLAUSIBLE_OUTAGE_MINUTES" ]; then
  echo "[account_health_check] implausible outage age ${age_minutes}m (>${MAX_PLAUSIBLE_OUTAGE_MINUTES}m)" \
       "-- treating the log timestamp as synthetic/corrupt rather than paging"
  exit 0
fi

if [ "$age_minutes" -ge "$THRESHOLD_MINUTES" ] && [ -z "$already_paged" ]; then
  paged_at="$(date -u '+%Y-%m-%d %H:%M UTC')"

  # Auto-recovery attempt, host-side, before paging a human -- but ONLY for the network-death
  # failure class, never the auth-flap class. Confirmed live 2026-08-28: a dead slirp4netns
  # process left the philanthropy container with no default route at all -- DNS unreachable,
  # every `claude` call hangs to a timeout, and NOTHING inside the container (or a plain
  # `podman restart`) can fix it; it needed the orphaned host slirp4netns process killed and a
  # full stop+start to force a fresh netns. A real revoked/expired token produces the SAME
  # "every account failing" symptom in account-pool.log, and restarting the pod does nothing
  # for that case -- worse, it would hide a real credential problem behind a green-looking
  # restart. So gate the restart on DNS actually being broken INSIDE the container right now,
  # not on the account-pool symptom alone.
  dns_broken=0
  if ! podman exec "$CONTAINER_NAME" sh -c 'getent hosts api.anthropic.com' >/dev/null 2>&1; then
    dns_broken=1
  fi

  already_restarted=""
  [ -f "$RESTART_STATE_FILE" ] && already_restarted=$(cat "$RESTART_STATE_FILE")

  if [ "$dns_broken" -eq 1 ] && [ -z "$already_restarted" ]; then
    echo "[account_health_check] DNS unreachable inside $CONTAINER_NAME -- attempting auto-recovery"
    # Orphaned slirp4netns processes from a prior crash can squat on a netns and are why a
    # plain `podman restart` alone did not fix this live -- stop, clear anything slirp4netns
    # has open for THIS container's netns, then start fresh. Best-effort: this container's own
    # netns cleanup only, never touches other containers' netns/slirp processes.
    podman stop "$CONTAINER_NAME" >/dev/null 2>&1
    cid=$(podman inspect "$CONTAINER_NAME" --format '{{.Id}}' 2>/dev/null)
    pkill -f "netns/cni-.*$CONTAINER_NAME" 2>/dev/null || true
    podman start "$CONTAINER_NAME" >/dev/null 2>&1
    sleep 5

    if podman exec "$CONTAINER_NAME" sh -c 'getent hosts api.anthropic.com' >/dev/null 2>&1; then
      echo "$paged_at" > "$RESTART_STATE_FILE"
      _ntfy "fleet-kit[$CONTAINER_NAME]: auto-recovered from a dead-network outage" \
        "Instance: $CONTAINER_NAME. No account in its pool had succeeded in ${age_minutes}+ minutes -- DNS inside $CONTAINER_NAME was unreachable (dead slirp4netns), same class as the 2026-08-28 outage. Restarted the container automatically; DNS resolves again. Watching for the next tick to confirm real recovery." \
        "info"
      echo "[account_health_check] auto-recovery restart succeeded -- DNS resolves again"
      exit 0
    else
      echo "$paged_at" > "$RESTART_STATE_FILE"
      _ntfy "🚨 fleet-kit[$CONTAINER_NAME]: auto-recovery FAILED, needs a human" \
        "Instance: $CONTAINER_NAME. DNS inside $CONTAINER_NAME was unreachable; attempted a container restart but DNS is still broken afterward. Last pool-log line: $last_line" \
        "urgent"
      echo "$paged_at" > "$STATE_FILE"
      echo "[account_health_check] auto-recovery restart did NOT fix DNS -- PAGED"
      exit 0
    fi
  fi

  # Not a network problem (or we already tried the restart once this outage) -- this is the
  # auth-flap/exhaustion class, which no restart can fix. Page a human, as before.
  # NAME THE INSTANCE. Three instances (philanthropy, sketchyswap, fleet-kit-server-fleet)
  # run this same script on the same 5-minute cron into the same channel, and this page
  # carried no instance in either the title or the body -- so a sketchyswap quota gate read
  # as a philanthropy outage, and the first thing a human did was go debug the wrong fleet
  # (2026-09-11, live). CONTAINER_NAME defaults to "philanthropy", which makes an
  # unattributed page actively misleading rather than merely vague.
  #
  # DIAGNOSE, DON'T GUESS. "looks like a real account/auth problem" was asserted whenever
  # DNS resolved. But the pool logs its own verdict, and `gated:exhausted_until_<epoch>` is
  # a known weekly-quota gate with a known reset -- nothing is broken, nothing is flapping,
  # and no amount of re-auth helps. Calling that an auth problem sends a human to look for
  # a fault that does not exist. Read the verdict the pool already wrote.
  # The gate verdict is logged PER ACCOUNT, one line per account, and the "ALL accounts
  # failed" summary lands after them -- so $last_line alone never carries it. Scan the tail
  # for the most recent gate instead of only the final line.
  reset_epoch="$(tail -20 "$POOL_LOG" 2>/dev/null \
    | sed -n 's/.*exhausted_until_\([0-9][0-9]*\).*/\1/p' | tail -1)"
  if [ -n "$reset_epoch" ]; then
    reset_human="$(date -u -d "@$reset_epoch" '+%Y-%m-%d %H:%M UTC' 2>/dev/null \
      || date -u -r "$reset_epoch" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || echo "epoch $reset_epoch")"
    diagnosis="every account in the pool is QUOTA-GATED, not broken -- the pool's own verdict is
\`gated:exhausted_until_$reset_epoch\` (resets $reset_human). Re-auth will not help; this fleet is
idle until the reset, or until it is pointed at an account with headroom."
  elif [ "$dns_broken" -eq 1 ]; then
    diagnosis="DNS inside $CONTAINER_NAME is broken (auto-restart already attempted)."
  else
    diagnosis="DNS resolves, and the pool reported no quota gate -- this looks like a real account/auth problem."
  fi

  _ntfy "🚨 fleet-kit[$CONTAINER_NAME]: ALL accounts exhausted" \
    "Instance: $CONTAINER_NAME (pool log: $POOL_LOG). No account in THIS instance's pool has succeeded in ${age_minutes}+ minutes (threshold ${THRESHOLD_MINUTES}m). Other instances are unaffected unless they page separately. Diagnosis: $diagnosis Last pool-log line: $last_line" \
    "urgent"
  echo "$paged_at" > "$STATE_FILE"
  echo "[account_health_check] PAGED -- last success was ${age_minutes}m ago"
elif [ "$age_minutes" -ge "$THRESHOLD_MINUTES" ] && [ -n "$already_paged" ]; then
  # Outage still open and already paged once -- re-page every REPAGE_MINUTES instead of
  # staying silent for the rest of the outage (gh#266). Measured from STATE_FILE's own stored
  # timestamp: the last re-page if one has happened this outage, else the original page.
  reference_at="${last_repage_at:-$already_paged}"
  reference_epoch=$(_state_ts_epoch "$reference_at")
  # An unparseable reference must not wedge the pager silent forever -- fail open (page) the
  # same way the rest of this file treats an unparseable timestamp as reason to act, not stall.
  since_last_page_minutes=999999
  [ -n "$reference_epoch" ] && since_last_page_minutes=$(( (now_epoch - reference_epoch) / 60 ))

  if [ "$since_last_page_minutes" -ge "$REPAGE_MINUTES" ]; then
    repaged_at="$(date -u '+%Y-%m-%d %H:%M UTC')"
    first_paged_epoch=$(_state_ts_epoch "$already_paged")
    since_first_page_minutes="$age_minutes"
    [ -n "$first_paged_epoch" ] && since_first_page_minutes=$(( (now_epoch - first_paged_epoch) / 60 ))
    h=$(( since_first_page_minutes / 60 )); m=$(( since_first_page_minutes % 60 ))
    _ntfy "🚨🚨 fleet-kit[$CONTAINER_NAME]: accounts STILL exhausted (re-page)" \
      "Instance: $CONTAINER_NAME. Still down, ${h}h ${m}m since first page -- no account in ITS pool has succeeded in ${age_minutes}+ minutes. Last pool-log line: $last_line" \
      "repage"
    printf '%s\nlast_repage=%s\n' "$already_paged" "$repaged_at" > "$STATE_FILE"
    echo "[account_health_check] RE-PAGED -- outage still open (${since_first_page_minutes}m since first page), next re-page in ${REPAGE_MINUTES}m"
  else
    echo "[account_health_check] failing but only ${age_minutes}m old (threshold ${THRESHOLD_MINUTES}m), or already paged (next re-page in $(( REPAGE_MINUTES - since_last_page_minutes ))m)"
  fi
else
  echo "[account_health_check] failing but only ${age_minutes}m old (threshold ${THRESHOLD_MINUTES}m)"
fi
