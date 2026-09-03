#!/bin/bash
# budget_read_check.sh -- page when the budget meter is UNREADABLE for the account the fleet
# is actually spending from.
#
# WHY THIS EXISTS (incident 2026-09-02/03, cost: ~2.5 days of a whole account unused):
# the budget chain has THIRTEEN fail-open paths and, before this script, ZERO notifications.
# maxx_reader returns (None, "<label>") on a bad read; maxx_share_ceiling prints ""; and
# gru_allowance's own comment says it plainly -- `return ""  # fail open: no ceiling => caller
# keeps its own fallback`. Each is individually CORRECT: a bad reading may only ever be used
# to conserve, never to invent headroom. But "conserve" silently is indistinguishable from
# "working", and that is the whole failure:
#
#   reif_tgp's anchor froze 2026-08-31 (nothing on dino ever emitted for it).
#   -> maxx served verdict=stale for 60.2h
#   -> maxx_reader refused it (correctly), returning label=maxx_verdict_stale
#   -> gru fell back to a hardcoded constant and paced the fleet at ~1/8 of real headroom
#   -> the pool skipped a HEALTHY account for a day on a stale gate epoch from that same row
#   -> ONE account carried all 12 members while the other sat at 3% used, for days.
#
# Nothing anywhere said a word. A human found it by looking at a usage screenshot.
#
# WHAT IT CHECKS: the handle resolve_maxx_handle.sh picks -- i.e. the account this pass WOULD
# spend from, not a hardcoded one. That matters: the same incident had FLEET_MAXX_HANDLE
# pointing at a different account than FLEET_ACCOUNTS was spending, so a check against the
# static handle would have reported a healthy meter for an account doing no work.
#
# Pages when: the meter is unreadable (any non-ok label), OR readable but the anchor behind it
# is older than MAX_ANCHOR_AGE (a fresh-looking verdict computed from frozen data).
set -uo pipefail

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTANCE_DIR="${FLEET_INSTANCE_DIR:?set FLEET_INSTANCE_DIR}"
CALLER_LOG_DIR="${FLEET_LOG_DIR:-}"
[ -f "$INSTANCE_DIR/fleet.env" ] && { set -a; . "$INSTANCE_DIR/fleet.env"; set +a; }
[ -n "$CALLER_LOG_DIR" ] && FLEET_LOG_DIR="$CALLER_LOG_DIR"

LOG_DIR="${FLEET_LOG_DIR:-/home/ubuntu/fleet-kit-logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/budget_read_check.log"
STATE="${BUDGET_READ_STATE_FILE:-$LOG_DIR/.budget_read_paged.state}"
MAX_ANCHOR_AGE="${BUDGET_MAX_ANCHOR_AGE:-3600}"
log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" >> "$LOG"; }

CONTAINER="${FLEET_CONTAINER_NAME:?set FLEET_CONTAINER_NAME}"

# Resolve handle+key the same way run_member.sh does, so we check the SPENDING account.
read -r HANDLE KEY < <(podman exec "$CONTAINER" bash -c \
  'cd /fleet-kit && set -a; . /fleet-kit/fleet.env; set +a; bash scripts/resolve_maxx_handle.sh' 2>/dev/null)
HANDLE="${HANDLE:-${FLEET_MAXX_HANDLE:-}}"
[ -z "$HANDLE" ] && { log "no handle resolved and no FLEET_MAXX_HANDLE -- cannot check"; exit 0; }

READ=$(podman exec "$CONTAINER" bash -c \
  "cd /fleet-kit && set -a; . /fleet-kit/fleet.env; set +a; FLEET_MAXX_HANDLE='$HANDLE' FLEET_MAXX_KEY='$KEY' python3 scripts/maxx_reader.py" 2>/dev/null)

LABEL=$(printf '%s' "$READ" | python3 -c "import json,sys;print(json.load(sys.stdin).get('label','parse_fail'))" 2>/dev/null || echo parse_fail)
# Pull the whole budget row once: the anchor age AND the fields that say WHICH limit
# is binding. Without the session terms this check cannot tell a self-resolving 5h
# block wall apart from a broken meter, and it reported the former as the latter.
BUDGET_TSV=$(podman exec "$CONTAINER" bash -c '
cd /fleet-kit; set -a; . fleet.env; set +a
read -r H K < <(bash scripts/resolve_maxx_handle.sh)
curl -s --max-time 25 -X POST "$FLEET_MAXX_URL/mcp?handle=$H&k=$K" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"maxx_budget\",\"arguments\":{}}}" > /tmp/mx_age.json 2>/dev/null
python3 -c "
import json
try:
    d=json.load(open(\"/tmp/mx_age.json\"))
    t=json.loads(d[\"result\"][\"content\"][0][\"text\"])
except Exception:
    print(-1); print(-1); print(-1); print(-1); print(-1); raise SystemExit
def g(k, d2=-1):
    v = t.get(k)
    return d2 if v is None else v
print(int(g(\"anchor_age_sec\")))
print(g(\"session_used_pct\"))
print(g(\"session_advised_pct\"))
print(int(g(\"five_reset_in_sec\")))
print(g(\"week_bank_pct\"))
"' 2>/dev/null)
AGE=$(printf '%s' "$BUDGET_TSV" | sed -n 1p | tr -cd '0-9-')
SESS_USED=$(printf '%s' "$BUDGET_TSV" | sed -n 2p | tr -cd '0-9.-')
SESS_ADV=$(printf '%s' "$BUDGET_TSV" | sed -n 3p | tr -cd '0-9.-')
FIVE_RESET=$(printf '%s' "$BUDGET_TSV" | sed -n 4p | tr -cd '0-9-')
WEEK_BANK=$(printf '%s' "$BUDGET_TSV" | sed -n 5p | tr -cd '0-9.-')
[ -z "$AGE" ] && AGE=-1

# WHICH LIMIT IS BINDING -- three different situations, only two are problems.
#
# A 5h session-block wall is NOT a fault and must never page. maxx returns verdict=over
# (and maxx_share_ceiling hard-stops to 0.0) when session_used_pct passes
# session_advised_pct, which reads identically to a broken meter at the label level --
# this check reported exactly that and emailed "meter unreadable" for a perfectly
# readable meter on a healthy account (2026-09-03).
#
# It is usually the system working. An account whose anchor went stale for days comes
# back with most of its week unspent and only hours of runway left; the fleet then
# compresses that budget into what remains, and hitting the block wall is the EXPECTED
# consequence of spending budget that would otherwise expire unused at week reset.
# Leaving tokens on the table is the real loss, not walling a block that refills on a
# timer. So: log it, never page it -- unless the WEEK is also over pace, which is the
# case that actually needs a human.
problem=""
SESSION_WALL=0
if [ "$LABEL" = "over" ] || [ "$LABEL" = "maxx_verdict_over" ]; then
  if awk "BEGIN{exit !(${SESS_USED:--1} >= ${SESS_ADV:-101})}" 2>/dev/null; then
    SESSION_WALL=1
  fi
fi
if [ "$SESSION_WALL" = "1" ]; then
  mins=$(( ${FIVE_RESET:-0} / 60 ))
  if awk "BEGIN{exit !(${WEEK_BANK:-0} < 0)}" 2>/dev/null; then
    # Block walled AND the week is behind pace -- this one is real.
    problem="5h session block spent (${SESS_USED}% of advised ${SESS_ADV}%) AND the week is ${WEEK_BANK}% over pace"
  elif [ "$AGE" -gt 0 ] 2>/dev/null && [ "$AGE" -ge "$MAX_ANCHOR_AGE" ]; then
    # A stale anchor is a real fault and must NOT be masked by a session wall: the
    # numbers the wall is judged from come from that same frozen row, so "walled" may
    # itself be an artifact of data that stopped moving. Staleness wins.
    problem="anchor STALE (${AGE}s old, max ${MAX_ANCHOR_AGE}s) while the 5h block reads walled -- the wall may be an artifact of frozen data"
  else
    log "session-block wall handle=$HANDLE used=${SESS_USED}% advised=${SESS_ADV}% resets_in=${mins}m week_bank=${WEEK_BANK}% -- expected, not paging"
    [ -f "$STATE" ] && rm -f "$STATE"
    exit 0
  fi
fi
[ -z "$problem" ] && [ "$LABEL" != "ok" ] && [ "$SESSION_WALL" = "0" ] && problem="meter UNREADABLE (label=$LABEL)"
if [ -z "$problem" ] && [ "$AGE" -gt 0 ] 2>/dev/null && [ "$AGE" -ge "$MAX_ANCHOR_AGE" ]; then
  problem="anchor STALE (${AGE}s old, max ${MAX_ANCHOR_AGE}s) -- verdict looks fine but is computed from frozen data"
fi

if [ -n "$problem" ]; then
  already=""; [ -f "$STATE" ] && already=$(cat "$STATE" 2>/dev/null)
  log "ALARM handle=$HANDLE $problem"
  if [ "$already" != "$LABEL/$problem" ]; then
    # Both channels, via the shared helper: an alarm that only reaches ntfy reaches nobody
    # who does not have the app (fleet_alert.sh's header documents the incident).
    bash "$KIT_DIR/scripts/fleet_alert.sh" \
      "fleet budget meter unreadable ($HANDLE)" \
      "The fleet is spending from @$HANDLE and its budget meter is not usable: $problem

This FAILS OPEN -- gru keeps running on a fallback constant instead of real headroom, and nothing else reports it. Last time this went unseen for 60h and left a whole account unused.

Check which account:  bash scripts/resolve_maxx_handle.sh
Then read its meter:  FLEET_MAXX_HANDLE=<handle> FLEET_MAXX_KEY=<key> python3 scripts/maxx_reader.py
If the anchor is stale, the probe token may need re-minting:
  bash /home/ubuntu/Classified/dino/maxx/register_probe.sh <acct> <handle>"
    printf '%s' "$LABEL/$problem" > "$STATE"
  fi
  exit 0
fi

[ -f "$STATE" ] && { rm -f "$STATE"; log "RECOVERED handle=$HANDLE label=ok anchor=${AGE}s"; }
log "ok handle=$HANDLE label=ok anchor=${AGE}s"
exit 0
