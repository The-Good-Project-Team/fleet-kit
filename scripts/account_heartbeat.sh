#!/bin/bash
# account_heartbeat.sh -- prove each pool account is alive on a schedule, instead of waiting
# for real fleet work to reveal it.
#
# WHY THIS EXISTS (real incident, 2026-09-02/03): reif_tgp's maxx anchor went 216,715s --
# 60.2 HOURS -- stale with nothing paging. Every existing monitor is DOWNSTREAM of work that
# was not happening, so none of them could see it:
#
#   account_health_check.sh  reads account-pool.log for failure lines. A walled account runs
#                            no passes, so it writes no lines, so this reports
#                            "healthy -- newest pool-log line is not a failure". Silence
#                            reads identically to success.
#   anchor_staleness_check   watched MAXX_HANDLE=reif only; reif_tgp had no watcher at all.
#   account_status.sh --live the probe existed, but only ever ran by hand.
#
# The failure is self-perpetuating, which is why it never recovered on its own: maxx anchors
# are PUSHED by sessions (maxx_emit). Account gets walled -> stops running sessions -> stops
# emitting anchors -> its budget row freezes in the walled state -> the pool keeps skipping it
# -> still no sessions. Nothing inside that loop can break it. The stale row is what wrote the
# gate epoch (week_reset 1788480000) that made the pool skip a HEALTHY account for a day,
# leaving one account carrying all 12 members and the week 38% over pace.
#
# So this GENERATES the signal rather than waiting for it: one cheap `claude -p` per account
# per tick, so a dead credential surfaces on a schedule instead of when a minion happens to
# need it.
#
# WHAT IT CANNOT TELL YOU -- both limits measured, not assumed:
#
#   1. QUOTA. account_status.sh:16 documents it: a weekly-quota-exhausted account still answers
#      `claude -p` fine; only real load fails. A PASS here means "auth is live", never
#      "there is quota".
#   2. ANCHOR FRESHNESS. Tested 2026-09-03 against the 60h-stale reif_tgp row: a passing
#      heartbeat did NOT refresh it (216874s -> 216921s, it kept aging). maxx anchors come
#      from the EMITTER, which lives on the laptop (~/.maxx/emit.log, emit-cursor.json) --
#      dino has no ~/.maxx at all, because an anchor needs a session that can read /usage and
#      a headless fleet pass cannot. So a frozen budget row can only be unfrozen from the
#      machine that runs interactive sessions on that account. This script cannot do it, and
#      anchor_staleness_check.sh is what tells you it needs doing.
#
# Usage: FLEET_INSTANCE_DIR=... FLEET_CONTAINER_NAME=... bash scripts/account_heartbeat.sh
set -uo pipefail

INSTANCE_DIR="${FLEET_INSTANCE_DIR:?set FLEET_INSTANCE_DIR}"

# Save the caller's HOST-scoped values across the source. fleet.env's own FLEET_LOG_DIR is
# CONTAINER-scoped (/var/log/fleet-kit) and would clobber the host path this cron writes to --
# the identical gh#196 incident auto_deploy.sh's header documents, hit again writing this.
CALLER_LOG_DIR="${FLEET_LOG_DIR:-}"
CALLER_CONTAINER="${FLEET_CONTAINER_NAME:-}"
[ -f "$INSTANCE_DIR/fleet.env" ] && { set -a; . "$INSTANCE_DIR/fleet.env"; set +a; }
[ -n "$CALLER_LOG_DIR" ] && FLEET_LOG_DIR="$CALLER_LOG_DIR"
[ -n "$CALLER_CONTAINER" ] && FLEET_CONTAINER_NAME="$CALLER_CONTAINER"

LOG_DIR="${FLEET_LOG_DIR:-/home/ubuntu/fleet-kit-logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/account_heartbeat.log"
log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" >> "$LOG"; }

CONTAINER="${FLEET_CONTAINER_NAME:?set FLEET_CONTAINER_NAME}"
ACCOUNTS="${FLEET_ACCOUNTS:-}"
[ -z "$ACCOUNTS" ] && { log "no FLEET_ACCOUNTS configured -- nothing to probe"; exit 0; }

failed=""
for acct in $ACCOUNTS; do
  # Same credential-dir convention account_pool.sh uses: /root/.claude-<acct> in-container.
  out=$(podman exec "$CONTAINER" sh -c \
        "CLAUDE_CONFIG_DIR=/root/.claude-$acct timeout 90 claude -p 'say ok'" 2>&1)
  rc=$?
  if [ $rc -eq 0 ] && printf '%s' "$out" | grep -qi "ok"; then
    log "account=$acct heartbeat OK -- auth live (says nothing about quota or anchor age)"
  else
    # Truncated: a failure body can carry a token or a stack trace, and this log is not a
    # secrets-grade file.
    log "account=$acct heartbeat FAILED rc=$rc -- $(printf '%s' "$out" | head -c 120 | tr '\n' ' ')"
    failed="$failed $acct"
  fi
done

if [ -n "$failed" ] && [ -n "${NTFY_TOPIC:-}" ]; then
  curl -sf -o /dev/null -H "Title: fleet account heartbeat failed" \
    -d "Heartbeat could not authenticate:$failed (container $CONTAINER). Auth is broken for these accounts -- this is NOT a quota gate, which fails differently. Check set_account_token.sh." \
    "https://ntfy.sh/$NTFY_TOPIC" || log "WARNING: ntfy page failed"
fi
exit 0
