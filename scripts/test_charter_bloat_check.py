#!/usr/bin/env python3
"""Unit tests for charter_bloat_check.analyze -- pure function, no gh/network calls.

fk#753: this replaces "reconstruct each member's consolidation history from memory" with a
mechanical check. These tests pin the two behaviors that check depends on: a net-reductive PR
resets the counter, and `gh pr list --search "X in:files"`-style false positives (a PR that
never actually touched the file) must not count at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import charter_bloat_check as cbc  # noqa: E402


def _pr(number, merged_at, path, additions, deletions):
    return {
        "number": number,
        "mergedAt": merged_at,
        "files": [{"path": path, "additions": additions, "deletions": deletions}],
    }


def test_all_additive_since_start_counts_every_pr():
    prs = [
        _pr(1, "2026-09-01T00:00:00Z", "members/x/x.md", 10, 0),
        _pr(2, "2026-09-02T00:00:00Z", "members/x/x.md", 8, 1),
        _pr(3, "2026-09-03T00:00:00Z", "members/x/x.md", 5, 0),
    ]
    r = cbc.analyze(["members/x/x.md"], prs)["members/x/x.md"]
    assert r["count_since_consolidation"] == 3
    assert r["last_consolidation_pr"] is None
    assert r["needs_consolidation"] is False  # below threshold of 5


def test_net_reductive_pr_resets_the_counter():
    prs = [
        _pr(1, "2026-09-01T00:00:00Z", "members/x/x.md", 10, 0),
        _pr(2, "2026-09-02T00:00:00Z", "members/x/x.md", 20, 0),
        _pr(3, "2026-09-03T00:00:00Z", "members/x/x.md", 5, 40),  # consolidation
        _pr(4, "2026-09-04T00:00:00Z", "members/x/x.md", 3, 0),
    ]
    r = cbc.analyze(["members/x/x.md"], prs)["members/x/x.md"]
    assert r["count_since_consolidation"] == 1  # only PR#4, after the consolidation
    assert r["last_consolidation_pr"] == 3
    assert r["last_consolidation_date"] == "2026-09-03T00:00:00Z"


def test_needs_consolidation_flag_trips_at_five():
    prs = [_pr(n, f"2026-09-0{n}T00:00:00Z", "members/x/x.md", 4, 0) for n in range(1, 6)]
    r = cbc.analyze(["members/x/x.md"], prs)["members/x/x.md"]
    assert r["count_since_consolidation"] == 5
    assert r["needs_consolidation"] is True


def test_pr_that_never_touched_the_file_is_not_counted():
    # The bug fk#753 flags in `gh pr list --search "X in:files"`: a PR whose body/title merely
    # MENTIONS members/x/x.md must not count just because search text-matched it.
    prs = [
        _pr(1, "2026-09-01T00:00:00Z", "members/y/y.md", 10, 0),  # different file entirely
    ]
    r = cbc.analyze(["members/x/x.md"], prs)["members/x/x.md"]
    assert r["count_since_consolidation"] == 0
    assert r["last_consolidation_pr"] is None
    assert r["needs_consolidation"] is False


def test_zero_diff_pr_never_counts_as_consolidation():
    # additions == deletions == 0 (e.g. a rename with no content change) must not
    # spuriously reset the counter via the `delete >= add` check.
    prs = [
        _pr(1, "2026-09-01T00:00:00Z", "members/x/x.md", 0, 0),
        _pr(2, "2026-09-02T00:00:00Z", "members/x/x.md", 3, 0),
    ]
    r = cbc.analyze(["members/x/x.md"], prs)["members/x/x.md"]
    assert r["count_since_consolidation"] == 2
    assert r["last_consolidation_pr"] is None


def test_tied_mergedat_mixing_int_and_str_number_does_not_raise():
    # A squash-merged PR (`number` is int) and a direct push (`number` is `sha[:9]`, a str) can
    # land within the same calendar second. analyze() must not compare int < str while sorting.
    prs = [
        _pr(1, "2026-09-01T00:00:00Z", "members/x/x.md", 10, 0),
        _pr("abc123def", "2026-09-01T00:00:00Z", "members/x/x.md", 5, 0),
    ]
    r = cbc.analyze(["members/x/x.md"], prs)["members/x/x.md"]
    assert r["count_since_consolidation"] == 2
    assert r["last_consolidation_pr"] is None


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
