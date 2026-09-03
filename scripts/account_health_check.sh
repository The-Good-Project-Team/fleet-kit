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
STATE_FILE="${ACCOUNT_HEALTH_STATE_FILE:-$LOG_DIR/.account_health_paged.state}"
CONTAINER_NAME="${FLEET_CONTAINER_NAME:-philanthropy}"
RESTART_STATE_FILE="${ACCOUNT_HEALTH_RESTART_STATE_FILE:-$LOG_DIR/.account_health_restarted.state}"

[ -f "$POOL_LOG" ] || { echo "[account_health_check] no pool log at $POOL_LOG yet -- nothing to check"; exit 0; }

last_line=$(tail -1 "$POOL_LOG")
already_paged=""
[ -f "$STATE_FILE" ] && already_paged=$(cat "$STATE_FILE")

_ntfy() {
  local title="$1" msg="$2" priority="$3"
  bash "$KIT_DIR/scripts/fleet_alert.sh" "$title" "$msg" \
    || echo "[alert] fleet_alert.sh failed" >&2
}

if [[ "$last_line" != *"ALL accounts in"*"failed this call"* ]]; then
  # newest line in the log is not a failure -- pool is healthy (or has never failed).
  if [ -n "$already_paged" ]; then
    _ntfy "fleet-kit: accounts recovered" \
      "Fleet account pool is succeeding again after an outage flagged at $already_paged." \
      "default"
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
      _ntfy "fleet-kit: auto-recovered from a dead-network outage" \
        "No fleet account had succeeded in ${age_minutes}+ minutes -- DNS inside $CONTAINER_NAME was unreachable (dead slirp4netns), same class as the 2026-08-28 outage. Restarted the container automatically; DNS resolves again. Watching for the next tick to confirm real recovery." \
        "default"
      echo "[account_health_check] auto-recovery restart succeeded -- DNS resolves again"
      exit 0
    else
      echo "$paged_at" > "$RESTART_STATE_FILE"
      _ntfy "🚨 fleet-kit: auto-recovery FAILED, needs a human" \
        "DNS inside $CONTAINER_NAME was unreachable; attempted a container restart but DNS is still broken afterward. Last pool-log line: $last_line" \
        "urgent"
      echo "$paged_at" > "$STATE_FILE"
      echo "[account_health_check] auto-recovery restart did NOT fix DNS -- PAGED"
      exit 0
    fi
  fi

  # Not a network problem (or we already tried the restart once this outage) -- this is the
  # auth-flap/exhaustion class, which no restart can fix. Page a human, as before.
  _ntfy "🚨 fleet-kit: ALL accounts exhausted" \
    "No fleet account has succeeded in ${age_minutes}+ minutes (threshold ${THRESHOLD_MINUTES}m). DNS check: $([ "$dns_broken" -eq 1 ] && echo 'broken (already attempted auto-restart)' || echo 'ok -- looks like a real account/auth problem, not network'). Last pool-log line: $last_line" \
    "urgent"
  echo "$paged_at" > "$STATE_FILE"
  echo "[account_health_check] PAGED -- last success was ${age_minutes}m ago"
else
  echo "[account_health_check] failing but only ${age_minutes}m old (threshold ${THRESHOLD_MINUTES}m), or already paged"
fi
