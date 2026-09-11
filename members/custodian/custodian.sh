#!/bin/bash
# custodian.sh -- one surface per job, driven down (fk#807). A plain script, not a model pass.
#
# WHY A SCRIPT: the debt is arithmetic over the route table and the work item is generated from
# it. There is no judgment for a model to add, so this follows roomba's shape (fleet-kit#514):
# run_member.sh dispatches here via the spec's llm.runner, and the pass is recorded through
# run_report.py so fleet.db, /status and fleet_kpi see it exactly like any other member.
#
# THE RECURSION is machinery that already exists, not anything this script re-decides:
#   surface_debt names the worst job -> one retirement filed -> a minion folds the extra
#   surface into the survivor and reruns ui_surfaces.py --baseline in that same PR ->
#   test_ui_surfaces_ratchet.py freezes the lower number -> the next pass reads a smaller debt.
# Terminates at debt 0, which is every job serving exactly one page.
#
# ONE AT A TIME on purpose: if a previous surface-debt item is still open, this files nothing.
# A 13-item cleanup epic is exactly the garbage nobody picks up.
set -uo pipefail

KIT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
LOG_DIR="${FLEET_LOG_DIR:-/var/log/fleet-kit}"
REPO="${FLEET_REPO:?set FLEET_REPO -- the product repo whose surfaces this sweeps}"
RUN_ID="custodian-$$-$(date -u +%s)"
LOG="$LOG_DIR/custodian.log"
MARKER="surface-debt"
mkdir -p "$LOG_DIR"

report() { # <outcome> <evidence> <self-critique> <exit-code>
  printf 'Outcome: %s\nEvidence: %s\nSelf-critique: %s\n' "$1" "$2" "$3" | python3 "$KIT_DIR/scripts/run_report.py" \
    --member custodian --run-id "$RUN_ID" --kind shell --exit-code "${4:-0}" \
    --pass-file - >> "$LOG_DIR/runs.jsonl" 2>>"$LOG"
}

# --- the rework numbers, first: they must refresh even when there is no surface to retire,
# --- because dumbledore's predictions ledger resolves rework_pct/churn_ratio off this cache
# --- and a stale cache reads as "unavailable" rather than resolving a row either way.
REPO_SLUG="${KIT_REPO_SLUG:-}"
if [ -z "$REPO_SLUG" ]; then
  REPO_SLUG=$(cd "$REPO" && git config --get remote.origin.url 2>/dev/null \
    | sed -E 's#(git@|https://)github.com[:/]##; s#\.git$##')
fi
REWORK_LINE="rework cache not refreshed (no repo slug resolved)"
if [ -n "$REPO_SLUG" ]; then
  if RW=$(python3 "$KIT_DIR/scripts/rework_collect.py" --repo "$REPO_SLUG" --limit 400 --print 2>&1); then
    REWORK_LINE=$(printf '%s' "$RW" | head -n 2 | tr '\n' ' ' | cut -c1-200)
  else
    REWORK_LINE="rework_collect failed: $(printf '%s' "$RW" | tail -n 1 | cut -c1-140)"
  fi
fi

# --- surface debt ---------------------------------------------------------------------------
DEBT_JSON=$(cd "$REPO" && python3 scripts/qa/surface_debt.py --json 2>&1); RC=$?
if [ "$RC" -ne 0 ]; then
  # A debt this member cannot compute is never guessed at. A fabricated 0 would read as
  # "no duplication left" and silently retire the whole loop.
  report "QUIET — surface debt could not be computed (surface_debt.py rc=$RC); nothing filed" \
         "$(printf '%s' "$DEBT_JSON" | tail -n 2 | tr '\n' ' ' | cut -c1-260) | $REWORK_LINE" \
         "escalation path: the route walker or app deps are broken, not the surfaces" "$RC"
  echo "QUIET surface_debt rc=$RC"
  exit 0
fi

DEBT=$(printf '%s' "$DEBT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("debt", 0))' 2>/dev/null || echo 0)
WORST=$(printf '%s' "$DEBT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("worst") or "")' 2>/dev/null || echo "")
UNCLASS=$(printf '%s' "$DEBT_JSON" | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin).get("unclassified") or []))' 2>/dev/null || echo "")

if [ "${DEBT:-0}" = "0" ] || [ -z "$WORST" ]; then
  report "QUIET — surface debt 0; every job serves exactly one page" \
         "unclassified jobs: ${UNCLASS:-none} | $REWORK_LINE" \
         "none -- deterministic count over the route table" 0
  echo "QUIET debt=0"
  exit 0
fi

# One retirement in flight at a time.
OPEN=$(cd "$REPO" && timeout 25s gh issue list --state open --search "in:title $MARKER" --limit 5 \
  --json number --jq '.[0].number' 2>/dev/null || true)
if [ -n "$OPEN" ] && [ "$OPEN" != "null" ]; then
  report "QUIET — surface debt $DEBT but #$OPEN is still open; one retirement at a time" \
         "worst job: $WORST | unclassified: ${UNCLASS:-none} | $REWORK_LINE" \
         "none -- filing a second item would build the backlog this member exists to prevent" 0
  echo "QUIET debt=$DEBT open=#$OPEN"
  exit 0
fi

BODY=$(mktemp); trap 'rm -f "$BODY"' EXIT
if ! (cd "$REPO" && python3 scripts/qa/surface_debt.py --next) > "$BODY" 2>>"$LOG"; then
  report "QUIET — surface debt $DEBT but the work item could not be generated; nothing filed" \
         "worst job: $WORST | $REWORK_LINE" "surface_debt.py --next failed; see $LOG" 1
  echo "QUIET --next failed"
  exit 0
fi
{
  echo
  echo "---"
  echo "Filed by the \`custodian\` member. Surface debt is **$DEBT** extra surfaces right now;"
  echo "this item retires one of them. The next pass files the next worst job only after this"
  echo "one lands, so the queue never grows a backlog nobody picks up."
} >> "$BODY"

TITLE="$MARKER: $WORST serves more than one surface, retire the extras"
NEW=$(cd "$REPO" && timeout 40s gh issue create --title "$TITLE" --body-file "$BODY" \
  --label fleet:backlog --label quality:solid 2>&1 | tail -n 1)
case "$NEW" in
  http*)
    report "filed one surface retirement: $WORST (surface debt $DEBT)" \
           "$NEW | unclassified: ${UNCLASS:-none} | $REWORK_LINE" \
           "none -- the ratchet freezes the lower count once this lands, so the next pass starts smaller" 0
    echo "OK filed $NEW"
    ;;
  *)
    report "QUIET — surface debt $DEBT but filing failed; nothing was created" \
           "gh issue create said: $(printf '%s' "$NEW" | cut -c1-200) | $REWORK_LINE" \
           "the next tick retries; no partial state is left behind" 1
    echo "QUIET file failed"
    ;;
esac
exit 0
