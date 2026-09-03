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
# Usage: fleet_alert.sh "<title>" "<body>"
# Config: /home/ubuntu/.config/maxx/alert.env  (RESEND_API_KEY, MAIL_FROM, FLEET_ALERT_EMAIL)
#         NTFY_TOPIC from the caller's env or anchor.env.
set -uo pipefail

TITLE="${1:?usage: fleet_alert.sh <title> <body>}"
BODY="${2:-}"
LOG=/home/ubuntu/fleet-kit-logs/fleet_alert.log
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
