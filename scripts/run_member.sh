#!/bin/bash
# run_member.sh — the ONE runner for every fleet member. Reads members/<name>/<name>.fleet.json,
# applies live overrides, sources that member's charter, and runs it through `claude -p` with
# its own model/turns/tools/budget.
#
# WHY THIS DIDN'T EXIST UNTIL NOW: member_spec.py collapsed the fleet onto one definition store
# (schedule/model/turns/tools/prompt, all in one reviewed file -- see its own header for the
# incident that forced that). But nothing actually executed a member FROM that file: schedulers/
# and run_agent_pass.sh predate member_spec.py and point at a separate legacy `agents/*.md`
# directory (ceo/architect/builder/reviewer, not any of the 7 real members). A `.fleet.json`
# with a `tools.allow/deny` list that no script ever reads is authority written down and
# ignored -- the exact "instruction nobody obeys" failure class the whole kit exists to end.
# This script is what makes prompt_file/model/max_turns/tools the thing that actually runs,
# not just the thing a dashboard displays.
#
# EVERY MEMBER IS AN LLM (Reif, 2026-08-21) -- see member_spec.py's header. There is no
# "mechanical, just exec a script" branch here on purpose: a member with a goal reaches for its
# own helper script (roomba.py, the-fixer.sh) as one Bash-reachable tool among its allowlist,
# same as any other tool, rather than the script BEING the member's whole behavior.
#
# Usage: run_member.sh <member-name> [--dry-run]
#   e.g.  run_member.sh dumbledore
#         run_member.sh roomba --dry-run     # print the resolved command, run nothing
set -uo pipefail

[ -f "${FLEET_ENV_FILE:-./fleet.env}" ] && . "${FLEET_ENV_FILE:-./fleet.env}"

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${FLEET_REPO:?set FLEET_REPO in fleet.env}"
LOG_DIR="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}"
mkdir -p "$LOG_DIR"

MEMBER="${1:?usage: run_member.sh <member-name> [--dry-run]}"
shift || true
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

LOG="$LOG_DIR/${MEMBER}.log"
ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

# --- master kill switch, before anything with a cost --------------------------------------
. "$KIT_DIR/scripts/fleet_enabled.sh"
fleet_enabled_or_exit "$MEMBER"

# --- resolve spec: git baseline + live overrides on top ------------------------------------
SPEC_JSON=$(python3 - "$MEMBER" "$KIT_DIR" <<'PYEOF'
import json, sys
sys.path.insert(0, sys.argv[2] + "/scripts")
import member_spec, overrides
try:
    spec = member_spec.by_name(sys.argv[1])
except member_spec.SpecError as e:
    print(json.dumps({"error": str(e)}))
    sys.exit(1)
eff, applied = overrides.apply(spec)
print(json.dumps({"spec": eff, "applied": applied}))
PYEOF
)
RC=$?
if [ "$RC" -ne 0 ]; then
  log "FATAL: $(echo "$SPEC_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error","spec load failed"))' 2>/dev/null || echo "spec load failed")"
  exit 2
fi

SPEC=$(echo "$SPEC_JSON" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["spec"]))')
APPLIED_COUNT=$(echo "$SPEC_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["applied"]))')
[ "$APPLIED_COUNT" -gt 0 ] && log "$APPLIED_COUNT live override(s) applied on top of the git spec"

jget() { echo "$SPEC" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d$1)"; }

ENABLED=$(jget "['enabled']")
TIMEOUT_S=$(jget "['mandate']['limits'].get('timeout_s', $(jget "['timeout_s']"))")
VISION=$(jget "['report']['vision_link']")
VISION_FLAG=""; [ "$VISION" = "required" ] && VISION_FLAG="--vision-required"

if [ "$ENABLED" != "True" ] && [ "${FLEET_RUN_NOW:-0}" != "1" ]; then
  log "$MEMBER: enabled=false in spec -- exiting without doing anything"
  exit 0
fi

RUN_ID="${MEMBER}-$$-$(date +%s)"

# A member MAY declare its own runner (e.g. judge-judy's judge-judy.sh, which reviews a
# diff as untrusted TEXT with zero tools -- a shape the generic claude -p path below can't
# express safely). Default: none, every other member runs through the generic path.
CUSTOM_RUNNER=$(jget "['llm'].get('runner', '')")
if [ -n "$CUSTOM_RUNNER" ]; then
  RUNNER_PATH="$KIT_DIR/$CUSTOM_RUNNER"
  if [ ! -x "$RUNNER_PATH" ]; then
    log "FATAL: llm.runner=$CUSTOM_RUNNER not found or not executable at $RUNNER_PATH"
    exit 2
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] $RUNNER_PATH (custom runner, timeout ${TIMEOUT_S}s)"
    exit 0
  fi
  log "pass start (custom runner=$CUSTOM_RUNNER)"
  "$RUNNER_PATH"
  RC=$?
  log "pass end rc=$RC (custom runner)"
  exit "$RC"
fi

BEHAVIOR=$(python3 -c "
import sys, json
sys.path.insert(0, '$KIT_DIR/scripts')
import member_spec
spec = json.loads('''$SPEC''')
print(member_spec.behavior_path(spec))
")

cd "$REPO" 2>/dev/null || { log "FATAL: repo missing at $REPO"; exit 1; }
[ -f "$KIT_DIR/scripts/account_pool.sh" ] && . "$KIT_DIR/scripts/account_pool.sh"
command -v account_pool_run >/dev/null 2>&1 || account_pool_run() { "$@"; }

# Source the charter (frontmatter stripped), build --allowedTools/--disallowedTools from the
# spec's own tools.allow/deny, run through the account pool, book real usage.
MODEL=$(jget "['llm']['model']")
MAX_TURNS=$(jget "['llm']['max_turns']")
MAX_BUDGET=$(jget "['mandate']['limits'].get('max_budget_usd', 5)")

PROMPT=$(awk 'BEGIN{d=0} /^---$/{d++; next} d>=2{print}' "$BEHAVIOR")
if [ -z "$PROMPT" ]; then
  log "FATAL: charter empty after frontmatter strip ($BEHAVIOR)"
  exit 2
fi

# Tool flags are the member's AUTHORITY (overrides.py refuses to tune these live, on purpose)
# -- built fresh from the git-reviewed spec every run, never from a cached or hand-edited list.
ALLOWED=$(jget "['llm']['tools'].get('allow', [])" | python3 -c "import ast,sys; print(' '.join(ast.literal_eval(sys.stdin.read())))")
DENIED=$(jget "['llm']['tools'].get('deny', [])" | python3 -c "import ast,sys; print(' '.join(ast.literal_eval(sys.stdin.read())))")
TOOL_ARGS=()
[ -n "$ALLOWED" ] && TOOL_ARGS+=(--allowedTools "$ALLOWED")
[ -n "$DENIED" ] && TOOL_ARGS+=(--disallowedTools "$DENIED")

if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] claude -p <charter:$BEHAVIOR> --model $MODEL --max-turns $MAX_TURNS --max-budget-usd $MAX_BUDGET ${TOOL_ARGS[*]}"
  exit 0
fi

log "pass start (kind=llm charter=$BEHAVIOR model=$MODEL max_turns=$MAX_TURNS budget=\$$MAX_BUDGET)"
# --dangerously-skip-permissions / --setting-sources user: same reasoning as worktree_builder.sh
# -- an unattended pass can't answer an interactive approval prompt, and a target repo's own
# CLAUDE.md/hooks would silently hijack this member's identity otherwise. See that script's
# comment for the measured incident (three full attempts that looked like no-ops).
RAW=$(account_pool_run timeout "$TIMEOUT_S" claude -p "$PROMPT" \
  --model "$MODEL" --dangerously-skip-permissions --setting-sources user \
  --max-turns "$MAX_TURNS" --output-format json \
  --max-budget-usd "$MAX_BUDGET" "${TOOL_ARGS[@]}" 2>>"$LOG")
RC=$?
OUT=$(printf '%s' "$RAW" | python3 "$KIT_DIR/scripts/pass_accounting.py" text)
USAGE_FILE=$(mktemp "${TMPDIR:-/tmp}/fleet_usage.XXXXXX")
printf '%s' "$RAW" | python3 "$KIT_DIR/scripts/pass_accounting.py" usage > "$USAGE_FILE" 2>/dev/null

echo "$OUT" | python3 "$KIT_DIR/scripts/run_report.py" \
  --member "$MEMBER" --run-id "$RUN_ID" --kind llm --exit-code "$RC" \
  --pass-file - --usage-file "$USAGE_FILE" $VISION_FLAG >> "$LOG_DIR/runs.jsonl" 2>>"$LOG"
rm -f "$USAGE_FILE"

SUMMARY=$(tail -c 400 <<<"$OUT" | tr '\n' ' ' | tail -c 300)
if [ "$RC" -eq 0 ]; then
  log "pass end rc=0 (account=${ACCOUNT_POOL_SELECTED:-unknown}) :: $SUMMARY"
else
  log "pass end rc=$RC (account=${ACCOUNT_POOL_SELECTED:-none} reason=${ACCOUNT_POOL_LAST_REASON:-}) :: $SUMMARY"
fi
exit "$RC"
