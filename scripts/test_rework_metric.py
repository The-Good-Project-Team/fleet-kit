#!/usr/bin/env python3
"""fk#808: the fleet must measure how much of what it merges is rework.

dumbledore is graded on the predictions ledger, and the ledger can only resolve rows against
metrics fleet_metrics.py can compute. Rework was not one, so the fleet's largest observable
failure mode (49.5% of 400 merged PRs on 2026-09-10, churn ratio 14.14) could never resolve a
prediction as a miss. A loop cannot converge on a number it does not count.

Plain-python, no pytest -- matches ci.yml's `python3 scripts/test_*.py`.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fleet_metrics  # noqa: E402
import rework_collect  # noqa: E402


def test_rework_titles_are_recognised() -> None:
    yes = [
        "The header search box stops shrinking to nothing on a narrow laptop",
        'The "Talk to a human" form fits a small phone again (gh#5107)',
        "Feedback intake stops filing duplicate issues",
        "Messenger docs stop claiming the mobile nav layout is fine",
        "Fix the outreach email's price mismatch and unstored opener fact",
    ]
    for t in yes:
        assert rework_collect.classify_title(t), f"missed rework: {t}"
    no = [
        "Write the real Verified-Org outreach email",
        "See every org we have",
        "IRS 990 XML: monthly ingest cron, after-zip cache bust + notices, freshness tile",
    ]
    for t in no:
        assert not rework_collect.classify_title(t), f"false positive: {t}"
    print("ok  rework titles classified")


def test_summarize_is_pure_arithmetic() -> None:
    prs = [
        {"title": "fix the thing", "additions": 10, "deletions": 1},
        {"title": "add a new surface", "additions": 90, "deletions": 9},
    ]
    r = rework_collect.summarize(prs)
    assert r["merged"] == 2, r
    assert r["title_rework"] == 1, r
    assert r["title_rework_pct"] == 50.0, r
    assert r["churn_ratio"] == 10.0, r          # 100 added / 10 deleted
    print("ok  summarize computes rework pct and churn ratio")


def test_no_deletions_gives_no_ratio_not_a_fake_one() -> None:
    r = rework_collect.summarize([{"title": "new", "additions": 5, "deletions": 0}])
    assert r["churn_ratio"] is None, r
    print("ok  a window that deleted nothing has no churn ratio")


def test_empty_window_is_not_zero_percent() -> None:
    r = rework_collect.summarize([])
    assert r["title_rework_pct"] is None, r
    print("ok  an empty window reports None, not 0%")


def _write_cache(d: Path, **over) -> Path:
    row = {"title_rework_pct": 49.5, "churn_ratio": 14.14, "ts": time.time()}
    row.update(over)
    p = d / "rework.json"
    p.write_text(json.dumps(row))
    return p


def test_metric_reads_the_cache() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = _write_cache(Path(d))
        c = fleet_metrics._rework_cache(p)
        assert c and c["title_rework_pct"] == 49.5, c
    print("ok  fleet_metrics reads the collector cache")


def test_stale_cache_is_unavailable_not_zero() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = _write_cache(Path(d), ts=time.time() - (40 * 3600))
        assert fleet_metrics._rework_cache(p) is None, "a 40h-old cache must not resolve a row"
    print("ok  a stale cache is unavailable, not a fabricated number")


def test_missing_cache_is_unavailable() -> None:
    assert fleet_metrics._rework_cache(Path("/nonexistent/rework.json")) is None
    print("ok  a missing cache is unavailable")


def test_metrics_are_in_the_catalog() -> None:
    for m in ("rework_pct", "churn_ratio"):
        base, _ = fleet_metrics.parse(m)
        assert base == m, m
    print("ok  both metrics are addressable by predict.py")


def main() -> int:
    try:
        test_rework_titles_are_recognised()
        test_summarize_is_pure_arithmetic()
        test_no_deletions_gives_no_ratio_not_a_fake_one()
        test_empty_window_is_not_zero_percent()
        test_metric_reads_the_cache()
        test_stale_cache_is_unavailable_not_zero()
        test_missing_cache_is_unavailable()
        test_metrics_are_in_the_catalog()
    except AssertionError as exc:
        print(f"FAIL  {exc}")
        return 1
    print("all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
