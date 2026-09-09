#!/bin/bash
# librarian-scrub.sh -- the hourly credential scrub + retention sweep, as a SHELL member.
#
# WHY (fleet-kit#784): `librarian` was an LLM member on sonnet whose whole hourly pass was
# "run librarian.py --execute and read its report" -- 24 model passes a day to invoke one
# script, on a fleet that hit both accounts' weekly limits by Wednesday. roomba already has
# this shape (fleet-kit#514: roomba.sh -> roomba.py, no model). Same pattern here: the script
# does the work, this wrapper files the run record run_report.py parses. The model-shaped
# librarian job (memory tending, intent digest) now runs once a day as `librarian`.
set -uo pipefail
KIT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
LOG_DIR="${FLEET_LOG_DIR:-/var/log/fleet-kit}"
RUN_ID="librarian-scrub-$$-$(date -u +%s)"
LOG="$LOG_DIR/librarian-scrub.log"
mkdir -p "$LOG_DIR"

report() { # <outcome> <evidence> <self-critique> <exit-code>
  printf 'Outcome: %s\nEvidence: %s\nSelf-critique: %s\n' "$1" "$2" "$3" | python3 "$KIT_DIR/scripts/run_report.py" \
    --member librarian-scrub --run-id "$RUN_ID" --kind shell --exit-code "${4:-0}" \
    --pass-file - >> "$LOG_DIR/runs.jsonl" 2>>"$LOG"
}

# 840s: under the member's 900s timeout so a long cold scan is cut here, with its checkpoint
# already banked (librarian.py checkpoints every 25 files), rather than SIGKILLed by the wrapper.
OUT=$(timeout 840 python3 "$KIT_DIR/members/librarian/librarian.py" --execute 2>&1); RC=$?
printf '%s\n' "$OUT" | tail -n 40 >> "$LOG"

if [ "$RC" -ne 0 ] && [ "$RC" -ne 124 ]; then
  report "QUIET — scrub could not run (librarian.py rc=$RC); nothing changed" \
         "$(printf '%s' "$OUT" | tail -n 3 | tr '\n' ' ' | cut -c1-300)" \
         "a scrub that cannot run must not claim the store is clean -- reported the failure" "$RC"
  echo "QUIET rc=$RC"
  exit 0
fi

SCANNED=$(printf '%s' "$OUT" | grep -oE '[0-9]+ file\(s\) scanned' | tail -n 1 | grep -oE '^[0-9]+' || echo 0)
REDACTED=$(printf '%s' "$OUT" | grep -oE '[0-9]+ redacted' | tail -n 1 | grep -oE '^[0-9]+' || echo 0)
COMPRESSED=$(printf '%s' "$OUT" | grep -oE '[0-9]+ compressed' | tail -n 1 | grep -oE '^[0-9]+' || echo 0)
DROPPED=$(printf '%s' "$OUT" | grep -oE '[0-9]+ dropped' | tail -n 1 | grep -oE '^[0-9]+' || echo 0)
# The credential classes found this run, for a HUMAN to rotate -- redaction is never rotation.
CLASSES=$(printf '%s' "$OUT" | grep -E '^\s+[A-Za-z_-]+.*: [0-9]+ occurrence' | tr -s ' ' | tr '\n' ';' | cut -c1-240)
PARTIAL=""
[ "$RC" -eq 124 ] && PARTIAL=" (cut at 840s, watermark checkpointed; next tick resumes)"

if [ "${REDACTED:-0}" -eq 0 ] && [ "${COMPRESSED:-0}" -eq 0 ] && [ "${DROPPED:-0}" -eq 0 ]; then
  report "QUIET — scrub ${SCANNED:-0} scanned, 0 redacted, 0 compressed, 0 dropped${PARTIAL}" \
         "\`members/librarian/librarian.py --execute\` incremental since the last watermark; no secret-shaped string in the changed transcripts" \
         "none -- deterministic scan, nothing to change" 0
  echo "QUIET ${SCANNED:-0} scanned"
  exit 0
fi

report "scrub ${SCANNED:-0} scanned, ${REDACTED:-0} redacted, ${COMPRESSED:-0} compressed, ${DROPPED:-0} dropped${PARTIAL}; classes to ROTATE: ${CLASSES:-none named}" \
       "\`members/librarian/librarian.py --execute\` report in \`$LOG\` (last 40 lines of this run)" \
       "classes found are for a human to rotate; this run only redacted the strings" 0
echo "scrub ${SCANNED:-0} scanned, ${REDACTED:-0} redacted"
exit 0
