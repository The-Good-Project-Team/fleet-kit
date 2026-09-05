#!/bin/bash
# fleet_alert.sh -- one place every fleet alarm sends a page, so an alarm cannot fire into a
# channel nobody reads.
#
# WHY: every alarm on this box paged ONLY ntfy, which requires the ntfy app installed and
# subscribed to a topic. Reif does not have it, so the alarms were firing into a void --
# functionally the same as the silent fail-open they were built to catch. Found 2026-09-03
# while wiring budget_read_check: the meter had been unreadable for 60h, and even once an
# alarm existed for it, nothing would have reached a human.
#
# ntfy.sh's own X-Email header is NOT an option: anonymous email sending is rejected
# ({"code":40053,"error":"anonymous email sending is not allowed"}), tested live. So mail
# goes through Resend, the same sender philanthropy.org already uses (mailer.py).
#
# Sends BOTH: email (durable, reaches a human who is not watching a terminal) and ntfy
# (instant, if the app is ever installed). Either failing is logged, never fatal -- an alarm
# helper that exits non-zero would take down the check that called it.
#
# SEVERITY (added 2026-09-05): callers now pass --severity/--check/--problem so that
# alert_store.py can decide whether this condition should reach a human at all. 27 alarms
# fired in 48h and 3 were real: a container restarting for 6 minutes paged "meter UNREADABLE"
# (naming the wrong account), and one maxx outage paged 11 times because two per-handle
# latches in two scripts could not see each other. Noise is not a cosmetic problem -- an inbox
# with 24 false pages trains the reader to archive on sight, which is how the NEXT real 60h
# outage gets missed.
#
# The old positional form still works and still pages immediately -- any caller that has not
# been taught severity keeps its previous behavior exactly, so this change cannot mute an
# alarm that has not been deliberately reclassified.
#
# Usage: fleet_alert.sh "<title>" "<body>"
#        fleet_alert.sh --check <name> --problem <key> --severity transient|degraded|critical \
#                       [--handle <h>] "<title>" "<body>"
#        fleet_alert.sh --resolve --check <name> [--problem <key>] "<title>" "<body>"
# Config: /home/ubuntu/.config/maxx/alert.env  (RESEND_API_KEY, MAIL_FROM, FLEET_ALERT_EMAIL)
#         NTFY_TOPIC from the caller's env or anchor.env.
set -uo pipefail

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CHECK=""; PROBLEM=""; SEVERITY=""; HANDLE=""; RESOLVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check)    CHECK="${2:-}"; shift 2 ;;
    --problem)  PROBLEM="${2:-}"; shift 2 ;;
    --severity) SEVERITY="${2:-}"; shift 2 ;;
    --handle)   HANDLE="${2:-}"; shift 2 ;;
    --resolve)  RESOLVE=1; shift ;;
    --) shift; break ;;
    *) break ;;
  esac
done

TITLE="${1:?usage: fleet_alert.sh [--check X --problem Y --severity Z] <title> <body>}"
BODY="${2:-}"
LOG=/home/ubuntu/fleet-kit-logs/fleet_alert.log
_slog() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" >> "$LOG"; }

# Recovery path: only tell a human it recovered if a human was told it broke.
if [ "$RESOLVE" = "1" ] && [ -n "$CHECK" ]; then
  WAS_PAGED=$(python3 - "$CHECK" "$PROBLEM" <<'PY' 2>/dev/null || echo unknown
import sys, pathlib
sys.path.insert(0, "/home/ubuntu/fleet-kit/scripts")
try:
    import alert_store
    check, problem = sys.argv[1], sys.argv[2]
    r = alert_store.resolve(check, problem) if problem else {"was_paged": any(
        x["was_paged"] for x in alert_store.resolve_check(check)) }
    print("yes" if r.get("was_paged") else "no")
except Exception:
    print("unknown")
PY
)
  if [ "$WAS_PAGED" = "no" ]; then
    _slog "recovery suppressed (never paged) -- $TITLE"
    exit 0
  fi
fi

# Severity gate: ask the store whether this condition should page now.
if [ -n "$CHECK" ] && [ -n "$SEVERITY" ] && [ "$RESOLVE" = "0" ]; then
  VERDICT=$(FLEET_LOG_DIR="${FLEET_LOG_DIR:-/home/ubuntu/fleet-kit-logs}" \
    python3 "$KIT_DIR/scripts/alert_store.py" record \
      --check "$CHECK" --problem "${PROBLEM:-$TITLE}" --severity "$SEVERITY" \
      --handle "$HANDLE" --detail "$BODY" 2>&1)
  RC=$?
  if [ "$RC" = "10" ]; then
    _slog "SUPPRESSED [$SEVERITY] $CHECK/$PROBLEM -- $VERDICT"
    exit 0
  fi
  _slog "PAGING [$SEVERITY] $CHECK/$PROBLEM -- $VERDICT"
fi
log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" >> "$LOG"; }

[ -f /home/ubuntu/.config/maxx/alert.env ] && { set -a; . /home/ubuntu/.config/maxx/alert.env; set +a; }

sent_any=0

if [ -n "${RESEND_API_KEY:-}" ] && [ -n "${FLEET_ALERT_EMAIL:-}" ]; then
  PAYLOAD=$(TITLE="$TITLE" BODY="$BODY" FROM="${MAIL_FROM:-990 Scout <hello@philanthropy.org>}" \
            TO="$FLEET_ALERT_EMAIL" python3 -c '
import json, os
print(json.dumps({
    "from": os.environ["FROM"],
    "to": [os.environ["TO"]],
    "subject": os.environ["TITLE"],
    "text": os.environ["BODY"] + "\n\n-- dino fleet alarm",
}))')
  CODE=$(curl -s -o /tmp/fa_resp.json -w '%{http_code}' --max-time 25 \
    -X POST https://api.resend.com/emails \
    -H "Authorization: Bearer $RESEND_API_KEY" \
    -H "Content-Type: application/json" -d "$PAYLOAD")
  if [ "$CODE" = "200" ]; then
    log "email OK to $FLEET_ALERT_EMAIL -- $TITLE"; sent_any=1
  else
    log "email FAILED http=$CODE -- $(head -c 150 /tmp/fa_resp.json 2>/dev/null)"
  fi
  rm -f /tmp/fa_resp.json
else
  log "email skipped: no RESEND_API_KEY/FLEET_ALERT_EMAIL in alert.env"
fi

if [ -n "${NTFY_TOPIC:-}" ]; then
  if curl -sf -o /dev/null --max-time 15 -H "Title: $TITLE" -d "$BODY" "https://ntfy.sh/$NTFY_TOPIC"; then
    sent_any=1
  else
    log "ntfy FAILED -- $TITLE"
  fi
fi

[ "$sent_any" -eq 0 ] && log "ALARM UNDELIVERED -- $TITLE :: $BODY"
exit 0
