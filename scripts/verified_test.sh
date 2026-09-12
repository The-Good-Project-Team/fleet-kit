#!/usr/bin/env bash
# verified_test.sh -- run the suite and leave a receipt the push hook can check.
#
# The receipt is the whole point: pretest_push_hook.py refuses a `git push` out of a worktree
# whose current content has no green run behind it. Writing the receipt by hand is not a
# shortcut worth taking -- the hash comes from the hook's own --content-hash, so a
# hand-written one is wrong the moment the tree moves.
#
#   bash /fleet-kit/scripts/verified_test.sh                 # whole suite
#   bash /fleet-kit/scripts/verified_test.sh tests/test_x.py # narrower, recorded as such
#
# Width defaults to the box's core count, not 4: the pool machines have 6-7 cores and the
# hosted runners this replaces had 4, so the local run should not inherit their ceiling.
set -uo pipefail

WT="${WT_PATH:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
cd "$WT" || { echo "verified_test: cannot cd to $WT" >&2; exit 2; }

HOOK="$(dirname "$(readlink -f "$0")")/pretest_push_hook.py"
RECEIPT="$(python3 "$HOOK" --receipt-path "$WT")"
WORKERS="${PYTEST_WORKERS:-auto}"

# Receipt is written AFTER the run, against the content as it stands THEN -- computing it up
# front would certify a tree the suite never saw if a test writes into the worktree.
rm -f "$RECEIPT"

echo "verified_test: pytest -n $WORKERS ${*:-<full suite>} in $WT"
python3 -m pytest -q -n "$WORKERS" "$@"
code=$?

HASH="$(python3 "$HOOK" --content-hash "$WT")"
if [ "$code" -eq 0 ]; then STATUS=pass; else STATUS=fail; fi
printf '{"status":"%s","content":"%s","args":"%s","exit":%d,"ts":%d}\n' \
  "$STATUS" "$HASH" "${*:-full}" "$code" "$(date +%s)" > "$RECEIPT"

if [ "$code" -ne 0 ]; then
  echo "verified_test: suite FAILED (exit $code) -- the push hook will block until it is green" >&2
fi
exit "$code"
