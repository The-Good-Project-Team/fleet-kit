#!/usr/bin/env bash
# merge_arm.sh — one arm command that works on both a merge-queue repo and a plain one.
#
# gh#524 (parent: #523 deliverable 2). auto_update_branch.sh and worktree_builder.sh each
# hardcoded ONE merge-arm shape, and each shape is wrong for the other repo:
#
#   * bare `gh pr merge --auto`       -- REQUIRED on a merge-queue-controlled repo. An explicit
#     strategy there is an invalid combination: gh ERRORS ("The merge strategy for main is set
#     by the merge queue") instead of enqueueing -- confirmed live on nonprofit-atlas#3307/#3108.
#   * `gh pr merge --auto --squash`   -- REQUIRED on a plain (non-queue) repo: gh refuses to
#     guess a strategy non-interactively there ("--merge, --rebase, or --squash required when
#     not running interactively") -- confirmed live on fleet-kit's own repo (PRs
#     #406/#407/#413/#414/#416/#417 in one day).
#
# Neither caller knows its target repo's shape ahead of time (and #523 deliverable 1 can flip
# it by hand), so this tries the bare form first -- it is the only form that's EVER valid, since
# a queue repo rejects the other outright -- and falls back to --squash only on the specific
# "required when not running interactively" string. Any other failure (permissions, already
# merged, a judge-judy BLOCK) is never retried into an unrelated error; it is reported as-is.
set -u

# arm_pr_auto_merge PR_NUMBER
#
# On success: prints nothing, returns 0.
# On failure: prints the failure message, returns non-zero -- a drop-in replacement for the
# `arm_err="$(gh pr merge "$pr" --auto 2>&1 >/dev/null)"` shape both callers already used.
arm_pr_auto_merge() {
  local pr="$1" err
  if err="$(gh pr merge "$pr" --auto 2>&1 >/dev/null)"; then
    return 0
  fi
  if [[ "$err" == *"required when not running interactively"* ]]; then
    if err="$(gh pr merge "$pr" --auto --squash 2>&1 >/dev/null)"; then
      return 0
    fi
  fi
  printf '%s' "$err"
  return 1
}
