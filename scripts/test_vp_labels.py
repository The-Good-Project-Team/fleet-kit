#!/usr/bin/env python3
"""fk#805: vp must collect quality:solid items, not quality:world-class alone.

THE BUG. collect() hardcoded `--label quality:world-class`. On 2026-09-10 the product repo held
quality:ship-it 112, quality:solid 76, quality:world-class 3 -- so the acceptance judge was
reachable by 1.5% of open issues and vp_due.log read "nothing due" every 15 minutes, all day.

RED without the fix: test_collect_gathers_solid_items fails, because the solid tier is never
queried and no item comes back. Same command, GREEN with it.

Plain-python test, no pytest -- matches ci.yml, which runs `python3 scripts/test_*.py`.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load(env: dict | None = None):
    os.environ.pop("FLEET_VP_LABELS", None)
    if env:
        os.environ.update(env)
    import vp_due
    return importlib.reload(vp_due)


def test_collect_gathers_solid_items() -> None:
    vp_due = _load()
    queried: list[str] = []

    def fake_gh(args, cwd):
        if args[0] == "issue":
            label = args[args.index("--label") + 1]
            queried.append(label)
            return [{"number": 5555, "comments": []}] if label == "quality:solid" else []
        return []

    vp_due._gh = fake_gh
    items = vp_due.collect("/repo")

    assert "quality:solid" in queried, f"solid tier never queried; queried {queried}"
    assert [i["number"] for i in items] == [5555], f"solid item not collected: {items}"
    print("ok  collect() gathers quality:solid items")


def test_item_with_two_quality_labels_collected_once() -> None:
    vp_due = _load()

    def fake_gh(args, cwd):
        # same issue carries both labels -- must not be reviewed (or paid for) twice
        return [{"number": 7777, "comments": []}] if args[0] == "issue" else []

    vp_due._gh = fake_gh
    items = vp_due.collect("/repo")
    assert [i["number"] for i in items] == [7777], f"duplicate collection: {items}"
    print("ok  an item in two tiers is collected once")


def test_labels_are_overridable() -> None:
    vp_due = _load({"FLEET_VP_LABELS": "quality:world-class"})
    assert vp_due.VP_LABELS == ("quality:world-class",), vp_due.VP_LABELS
    print("ok  FLEET_VP_LABELS narrows the tiers without a code change")


def main() -> int:
    try:
        test_collect_gathers_solid_items()
        test_item_with_two_quality_labels_collected_once()
        test_labels_are_overridable()
    except AssertionError as exc:
        print(f"FAIL  {exc}")
        return 1
    finally:
        os.environ.pop("FLEET_VP_LABELS", None)
    print("all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
