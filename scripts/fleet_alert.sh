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
# Usage: fleet_alert.sh "<title>" "<body>" ["<ntfy priority>"]
#        fleet_alert.sh --check <name> --problem <key> --severity transient|degraded|critical \
#                       [--handle <h>] "<title>" "<body>"
#        fleet_alert.sh --resolve --check <name> [--problem <key>] "<title>" "<body>"
# Config: /home/ubuntu/.config/maxx/alert.env  (RESEND_API_KEY, MAIL_FROM, FLEET_ALERT_EMAIL)
#         NTFY_TOPIC from the caller's env or anchor.env.
# Priority is optional and ntfy-only (gh#815): omit it and the ntfy leg sends with no Priority
# header at all, same as every caller before gh#815 -- adding this could not change any of
# their behavior since none of them pass a 3rd positional arg.
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
PRIORITY="${3:-}"
LOG="${FLEET_ALERT_LOG:-${FLEET_LOG_DIR:-/home/ubuntu/fleet-kit-logs}/fleet_alert.log}"
# Alarms neither channel could deliver wait here and are retried at the front of the NEXT
# call (fleet-kit#512). See the drain block below for why.
QUEUE="${FLEET_ALERT_QUEUE:-$(dirname "$LOG")/alerts_undelivered.jsonl}"
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

# NTFY_TOPIC fallback (fleet-kit#512): on 2026-09-04 the "ALL accounts exhausted" alarm's email
# leg failed (Resend, http=) and NO ntfy leg ran, because the caller's environment carried no
# NTFY_TOPIC -- the leg below is gated on it. The log said ALARM UNDELIVERED and nobody heard.
# anchor.env already holds the topic for the maxx checks; read only that one key from it so a
# caller that forgot the topic still reaches the second channel. A caller that sets it wins.
if [ -z "${NTFY_TOPIC:-}" ] && [ -f /home/ubuntu/.config/maxx/anchor.env ]; then
  NTFY_TOPIC=$(grep -E '^NTFY_TOPIC=' /home/ubuntu/.config/maxx/anchor.env | head -1 | cut -d= -f2- | tr -d '"'"'"'')
fi

_send_email() {  # <title> <body> -> 0 on delivered
  [ -n "${RESEND_API_KEY:-}" ] && [ -n "${FLEET_ALERT_EMAIL:-}" ] || { log "email skipped: no RESEND_API_KEY/FLEET_ALERT_EMAIL in alert.env"; return 1; }
  local payload code
  payload=$(TITLE="$1" BODY="$2" FROM="${MAIL_FROM:-990 Scout <hello@philanthropy.org>}" \
            TO="$FLEET_ALERT_EMAIL" python3 -c '
import json, os
print(json.dumps({
    "from": os.environ["FROM"],
    "to": [os.environ["TO"]],
    "subject": os.environ["TITLE"],
    "text": os.environ["BODY"] + "\n\n-- dino fleet alarm",
}))')
  code=$(curl -s -o /tmp/fa_resp.json -w '%{http_code}' --max-time 25 \
    -X POST https://api.resend.com/emails \
    -H "Authorization: Bearer $RESEND_API_KEY" \
    -H "Content-Type: application/json" -d "$payload")
  if [ "$code" = "200" ]; then
    log "email OK to $FLEET_ALERT_EMAIL -- $1"; rm -f /tmp/fa_resp.json; return 0
  fi
  log "email FAILED http=$code -- $(head -c 150 /tmp/fa_resp.json 2>/dev/null)"; rm -f /tmp/fa_resp.json; return 1
}

_send_ntfy() {  # <title> <body> [priority] -> 0 on delivered
  [ -n "${NTFY_TOPIC:-}" ] || return 1
  local prio_hdr=()
  [ -n "${3:-}" ] && prio_hdr=(-H "Priority: $3")
  if curl -sf -o /dev/null --max-time 15 -H "Title: $1" "${prio_hdr[@]}" -d "$2" "https://ntfy.sh/$NTFY_TOPIC"; then
    return 0
  fi
  log "ntfy FAILED -- $1"; return 1
}

_deliver() {  # <title> <body> [priority] -> 0 if ANY channel took it
  local any=1
  _send_email "$1" "$2" && any=0
  _send_ntfy "$1" "$2" "${3:-}" && any=0
  return $any
}

# Drain the undelivered queue FIRST (fleet-kit#512). An alarm that failed both channels is not
# gone -- it waits here, and the next alarm (or the next healthy tick of any check that pages
# through this helper) retries it before sending its own. Bounded to 20 per call so a long
# outage of the mail provider cannot turn one call into a flood; the rest wait their turn.
if [ -s "$QUEUE" ]; then
  _keep=$(mktemp)
  while IFS= read -r _line; do
    [ -n "$_line" ] || continue
    _t=$(printf '%s' "$_line" | python3 -c 'import json,sys; print(json.load(sys.stdin)["title"])' 2>/dev/null) || continue
    _b=$(printf '%s' "$_line" | python3 -c 'import json,sys; print(json.load(sys.stdin)["body"])' 2>/dev/null) || continue
    if _deliver "[retry] $_t" "$_b"; then
      log "retry delivered -- $_t"
    else
      printf '%s\n' "$_line" >> "$_keep"
    fi
  done < <(head -n 20 "$QUEUE")
  tail -n +21 "$QUEUE" >> "$_keep" 2>/dev/null
  mv "$_keep" "$QUEUE"
fi

if _deliver "$TITLE" "$BODY" "$PRIORITY"; then
  exit 0
fi
log "ALARM UNDELIVERED (queued for retry) -- $TITLE :: $BODY"
TITLE="$TITLE" BODY="$BODY" python3 -c '
import json, os, time
print(json.dumps({"ts": int(time.time()), "title": os.environ["TITLE"], "body": os.environ["BODY"]}))' >> "$QUEUE"
exit 0
