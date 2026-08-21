#!/bin/bash
# dont-shoot-the-messenger.sh — ships logs, relays transcripts, via a pluggable driver.
#
# Provenance: genericized from nonprofit-atlas's logship.py + transcript_relay.py +
# gitpull_stall_alert.py -- three separate scripts pushing to the same dashboard by the same
# pattern, folded into one persona. See ../../scripts/messenger_driver.md for the contract.
#
# WHY BEST-EFFORT: this persona reports on other personas' behalf. A failure here must never
# change the exit code or behavior of whatever it's shipping logs FOR -- it is a side channel.
set -uo pipefail

LOG_DIR="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}"
SELF_LOG="$LOG_DIR/dont-shoot-the-messenger.log"
DRIVER="${FLEET_MESSENGER_DRIVER:-}"

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { echo "[$(ts)] $*" >> "$SELF_LOG"; }

if [ -z "$DRIVER" ]; then
  # Log once, not every tick -- a state file remembers we already said this.
  MARK="$HOME/.cache/fleet-kit/messenger-nodriver.marked"
  if [ ! -f "$MARK" ]; then
    mkdir -p "$(dirname "$MARK")"
    log "no FLEET_MESSENGER_DRIVER configured -- logs stay local-only, transcript relay unavailable. This is a valid mode, not an error."
    touch "$MARK"
  fi
  exit 0
fi

if [ ! -x "$DRIVER" ]; then
  log "CONFIG ERROR: FLEET_MESSENGER_DRIVER=$DRIVER is not executable"
  exit 0  # best-effort: never fail the caller over our own config problem
fi

"$DRIVER" ship_logs "$LOG_DIR" >>"$SELF_LOG" 2>&1
SHIP_RC=$?
case "$SHIP_RC" in
  0) log "ship_logs: ok" ;;
  1) log "ship_logs: destination unreachable, retry next tick" ;;
  2) log "ship_logs: CONFIG ERROR reported by driver" ;;
  *) log "ship_logs: unexpected exit $SHIP_RC" ;;
esac

# Pending-transcript requests are the driver's own state to enumerate -- this script only
# knows to ask "is anything pending", never reaches into the driver's storage directly.
PENDING=$("$DRIVER" list_pending 2>>"$SELF_LOG" || true)
for run_id in $PENDING; do
  "$DRIVER" relay_transcript "$run_id" >>"$SELF_LOG" 2>&1 \
    && log "relay_transcript $run_id: delivered" \
    || log "relay_transcript $run_id: failed, retry next tick"
done

exit 0
