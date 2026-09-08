#!/bin/bash
# philanthropy_deploy.sh -- the philanthropy.org incident-response deploy driver (gh#728).
#
# Wire its path into FLEET_DEPLOY_DRIVER. Implements the three functions fixer_fire_path.py's
# roll_back_and_verify() calls (see scripts/deploy_driver.md's "optional: rollback + status"
# addendum) against the already-provisioned, already-verified forced-command deploy key to
# `atlas-serve` (gh#728's issue body: `ssh atlas-serve checksum-report` prints hashes,
# `ssh atlas-serve id` is refused -- a shell is never available, only these named verbs).
#
# current_sha  -> `checksum-report`'s raw output, trimmed. Not a git SHA (the key grants no way
#                 to read one) -- an opaque identity string is all roll_back_and_verify() needs:
#                 it only ever compares two reads of THIS function for equality, never parses
#                 the value as a real commit.
# rollback     -> `rollback-blue-green`. Idempotent per deploy_driver.md's own `deploy`
#                 requirement -- atlas-serve's own verb owns that property, this driver does not
#                 re-implement it.
# status       -> re-reads checksum-report and reports only whether the box answered at all
#                 (exit code), independent of whether current_sha's value changed -- "is the
#                 deploy key still working" is a different question than "did the sha move."
set -uo pipefail

cmd="${1:?usage: $0 <current_sha|rollback|status>}"

case "$cmd" in
  current_sha)
    ssh atlas-serve checksum-report
    ;;
  rollback)
    ssh atlas-serve rollback-blue-green
    ;;
  status)
    if ssh atlas-serve checksum-report >/dev/null; then
      echo "ok: atlas-serve deploy key answering"
    else
      rc=$?
      echo "FAILED: atlas-serve deploy key did not answer (exit $rc)" >&2
      exit "$rc"
    fi
    ;;
  *)
    echo "philanthropy_deploy.sh: unknown command '$cmd' (want current_sha|rollback|status)" >&2
    exit 2
    ;;
esac
