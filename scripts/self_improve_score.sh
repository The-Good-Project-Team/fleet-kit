#!/bin/bash
# self_improve_score.sh -- once a day, an LLM reads a digest of the fleet's own recent activity
# and scores how much the fleet is actually SELF-IMPROVING, on a 1-100 scale anchored by Reif's
# own reference points: 100 = Jarvis (autonomously diagnoses itself, rewrites its own rules,
# gets measurably better without being told), 1 = a Windows update notification (nags, repeats
# the same notice, changes nothing on its own).
#
# WHY THIS IS THE ONE PLACE AN LLM JUDGES THE FLEET, NOT PLAIN BASH: every other watcher in
# this kit (account_health_check.sh, tunnel_health_check.sh) is deliberately plain bash on host
# cron, because THEIR job is "is the thing that runs LLM calls broken" -- diagnosing that with
# an LLM call would depend on the very thing being diagnosed. This script's job is different:
# "is the fleet's judgment actually improving," which is inherently a judgment call, not a
# threshold check. A regex can't tell "fixed the root cause" from "patched the symptom again."
#
# WHAT IT READS (last 7 days, all already-existing files/gh calls, nothing new to persist):
#   - Self-evolution PRs (jefe/dumbledore branches) -- charter/rule changes the fleet made to
#     itself, same query poll_gh_state already runs.
#   - runs.jsonl outcome counts -- is signal rate trending up, is the fleet still hitting the
#     same walls.
#   - dumbledore's own memory file (.claude/dumbledore-memory.md via the merged PR diffs) --
#     dumbledore's whole charter IS "fix the charter/instruction that caused the symptom," so
#     its recent activity is the most direct signal of self-correction actually happening.
#
# OUTPUT: one line appended to $FLEET_LOG_DIR/self_improve_score.jsonl per run:
#   {"date": "2026-08-25", "score": 61, "reasoning": "one sentence, plain text"}
# fleet_view_server.py reads this file directly (same append-only jsonl pattern as runs.jsonl)
# -- no new endpoint machinery, no DB.
#
# Run: cron, once daily. Needs FLEET_REPO, FLEET_LOG_DIR (same env every other script here uses).

set -uo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# set -a/+a around the source: a plain `.` only sets these as local shell variables, which
# `claude -p` (a separate exec) never sees. Same fix run_member.sh already had to learn the
# hard way (see its own comment on this exact line) -- ACCOUNT_POOL_ORDER and every account's
# token live in fleet.env, not the calling environment, so without this every account_pool_run
# call here fails "unauthenticated" for every account and this script silently no-ops.
[ -f "${FLEET_ENV_FILE:-$KIT_DIR/fleet.env}" ] && { set -a; . "${FLEET_ENV_FILE:-$KIT_DIR/fleet.env}"; set +a; }
# Never hand the fleet-view write key to an LLM pass -- it authorizes POST /api/run_now,
# which spawns agent runs on this box. See run_member.sh's fuller note. Least privilege.
unset FLEET_API_KEY


FLEET_REPO="${FLEET_REPO:?FLEET_REPO required}"
FLEET_LOG_DIR="${FLEET_LOG_DIR:?FLEET_LOG_DIR required}"
OUT_FILE="$FLEET_LOG_DIR/self_improve_score.jsonl"
# Scored every 3h (Reif, 2026-08-25), so the stamp is a timestamp, not a bare date -- a
# date-only key would let the first run of the day satisfy the guard and silently no-op the
# other seven. `date` stays a full UTC timestamp for the same reason: the chart plots one
# point per SCORE, and seven points sharing "2026-08-25" would collapse on the x-axis.
NOW_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# The 3h slot this run belongs to (00,03,...,21) -- the idempotency key. Re-running inside the
# same slot is still a no-op, same "once per condition" shape as tunnel_health_check.sh; the
# condition is just narrower now.
SLOT="$(date -u +%Y-%m-%d)T$(printf '%02d' $(( 10#$(date -u +%H) / 3 * 3 )))"

if [ -f "$OUT_FILE" ] && grep -q "\"slot\": \"$SLOT\"" "$OUT_FILE" 2>/dev/null; then
  exit 0
fi

source "$KIT_DIR/scripts/account_pool.sh" 2>/dev/null || true
command -v account_pool_run >/dev/null 2>&1 || account_pool_run() { "$@"; }

cd "$FLEET_REPO" || exit 1

SELF_EVO_JEFE=$(gh pr list --state merged --search "head:jefe/" --json number,title,mergedAt --limit 15 2>/dev/null)
SELF_EVO_DUMBLEDORE=$(gh pr list --state merged --search "head:dumbledore/" --json number,title,mergedAt --limit 15 2>/dev/null)

# Per-DAY outcome counts, not one 7-day aggregate -- the score has to be able to see whether
# signal rate actually moved after a specific jefe/dumbledore PR's merge date, not just that
# self-correction happened SOMETIME this week. Reif, 2026-08-25: "the way to see if things are
# improving is if jefe makes a charter modification, and that modification leads to
# improvements in tracking, which leads to faster and better modifications" -- a flat/aggregate
# count can't show that causal chain; a day-by-day series can.
RUNS_FILE="$FLEET_LOG_DIR/runs.jsonl"
DAILY_OUTCOMES="unavailable"
if [ -f "$RUNS_FILE" ]; then
  DAILY_OUTCOMES=$(tail -n 5000 "$RUNS_FILE" | python3 -c "
import json, sys, datetime
cutoff = datetime.datetime.now(datetime.timezone.utc).timestamp() - 7*86400
by_day = {}
for line in sys.stdin:
    try:
        r = json.loads(line)
    except Exception:
        continue
    ts = r.get('ts') or 0
    if ts < cutoff:
        continue
    day = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()
    s = r.get('status', 'unknown')
    d = by_day.setdefault(day, {})
    d[s] = d.get(s, 0) + 1
print(json.dumps(by_day, indent=None, sort_keys=True))
" 2>/dev/null || echo "unavailable")
fi

PROMPT="You are scoring whether an autonomous agent fleet (fleet-kit, running on nonprofit-atlas) is genuinely SELF-IMPROVING, not just running.

THE ACTUAL TEST (Reif's own definition, use this exactly -- not general 'did it self-correct sometimes'):
Self-improvement is a COMPOUNDING LOOP: jefe or dumbledore makes a charter/rule change -> that
change leads to a MEASURABLE improvement in tracking/outcomes AFTER its merge date -> that
better tracking leads to the NEXT charter change being faster and/or better than the last one.
One isolated fix is not this loop. A fix that never shows up as a real shift in the daily
outcome numbers after it landed is not this loop, no matter how good the fix reads in isolation.
A high score REQUIRES showing at least one instance of this actual chain in the evidence below --
name the specific PR, the specific date, and the specific before/after shift in the daily
numbers it produced. If you cannot point to that chain concretely, the score must be low
regardless of how much self-evolution PR *volume* exists.

Scale, anchored exactly:
- 100 = Jarvis from Iron Man: the compounding loop above is clearly running -- fixes visibly
  cause better numbers, and the fixes themselves are getting faster/sharper over time.
- 50 = self-correction happens, but it's isolated fixes with no visible compounding -- the
  fleet fixes things without getting better AT fixing things.
- 1 = a Windows update notification: nags about the same thing repeatedly, takes no corrective
  action itself, a human has to intervene every time.
- Score honestly. Volume of self-evolution PRs alone does NOT justify a high score -- lots of
  jefe/dumbledore activity with no visible before/after improvement in the daily numbers is
  still a low score, because the loop Reif is asking about isn't there.

Evidence, last 7 days:

Self-evolution PRs sourced by jefe (the fleet's own orchestrator correcting itself), with merge dates -- check whether the daily numbers below shifted after each date:
${SELF_EVO_JEFE:-none}

Self-evolution PRs sourced by dumbledore (whose entire charter is 'fix the charter/instruction that caused the symptom'), with merge dates:
${SELF_EVO_DUMBLEDORE:-none}

Run outcome counts BY DAY, last 7 days (ok = did real work, quiet/reported_nothing = ran but found nothing, budget_declined = didn't run at all) -- look for the shift a PR's merge date should have caused:
${DAILY_OUTCOMES}

Score 1-100. Reasoning must either (a) name a specific PR, its merge date, and the specific before/after shift in the daily numbers that followed it, or (b) explicitly say no such shift is visible in the evidence and that's why the score is capped low. Output ONLY this JSON, nothing else, no markdown fences:
{\"score\": <int 1-100>, \"reasoning\": \"<one or two sentences>\"}"

export IS_SANDBOX=1
RAW=$(account_pool_run timeout 90 claude -p "$PROMPT" \
  --model claude-sonnet-5 --dangerously-skip-permissions --setting-sources user \
  --max-turns 1 --output-format text 2>>"$FLEET_LOG_DIR/self_improve_score.log")
RC=$?

SCORE_JSON=$(printf '%s' "$RAW" | python3 -c "
import json, sys, re
raw = sys.stdin.read().strip()
m = re.search(r'\{.*\}', raw, re.S)
if not m:
    print(''); sys.exit(0)
try:
    d = json.loads(m.group(0))
    score = max(1, min(100, int(d.get('score', 0))))
    reasoning = str(d.get('reasoning', ''))[:600]
    print(json.dumps({'score': score, 'reasoning': reasoning}))
except Exception:
    print('')
" 2>/dev/null)

if [ -z "$SCORE_JSON" ]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) rc=$RC :: LLM did not return parseable score, skipping" >> "$FLEET_LOG_DIR/self_improve_score.log"
  exit 1
fi

python3 -c "
import json
d = json.loads('''$SCORE_JSON''')
d['date'] = '$NOW_TS'
d['slot'] = '$SLOT'   # idempotency key: one score per 3h slot, re-runs inside it are no-ops
print(json.dumps(d))
" >> "$OUT_FILE"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) scored slot=$SLOT :: $SCORE_JSON" >> "$FLEET_LOG_DIR/self_improve_score.log"
