#!/usr/bin/env python3
"""Live, reversible tuning of a fleet member -- without a PR, without gitpull.

Reif, 2026-08-18: "the important thing is that these jobs are fully editable - so that they can
be adjusted quickly."

The git spec (scripts/fleet/members/*.fleet.json) is the reviewed BASELINE. This is the thin
layer on top of it, so el-jefe -- or a human on admin.philanthropy.org -- can throttle a member
that is burning tokens for nothing and have it apply on that member's NEXT run.

WHY A SECOND LAYER AT ALL, given the whole point of today's work was to collapse two stores
into one: because the one store is delivered by git, and git delivery is exactly what fails
when you most need to intervene. Measured 2026-08-18: the fleet host's gitpull refused 80 consecutive
times on a dirty tree, 6 commits behind, on a box with no agent ssh. During that window NO
committed change could reach the fleet. A throttle that only ships by PR cannot stop a runaway
member during the incident that makes it runaway.

WHAT IS TUNABLE HERE IS DELIBERATELY NARROW -- the dials, never the authority:
    max_turns, model, enabled, schedule      tunable live
    prompt, tools                            PR ONLY
A tool allowlist is a member's AUTHORITY. Letting that change with no diff is precisely how the
old registry drifted from the repo for days with nobody able to see it. Turn budget is a dial;
"may this agent merge PRs" is not.

Every override carries who set it, why, and when it expires. An override with no expiry is a
silent permanent fork of the reviewed spec -- which is the drift this whole change exists to
end -- so `apply()` ignores expired rows and `DEFAULT_TTL_HOURS` bounds the ones that forget.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Only these keys may be tuned live. Everything else is PR-only, by design.
TUNABLE = ("max_turns", "model", "enabled", "schedule")

DEFAULT_TTL_HOURS = 24
STORE = Path(os.environ.get("FLEET_OVERRIDES_PATH",
                            Path.home() / ".claude" / "fleet-overrides.jsonl"))


class OverrideError(ValueError):
    pass


def _now() -> float:
    return time.time()


def set_override(member: str, key: str, value, *, by: str, why: str,
                 ttl_hours: float = DEFAULT_TTL_HOURS, store: Path | None = None) -> dict:
    """Record one override. Append-only: the newest live row for a key wins.

    Append-only rather than update-in-place so the history of who throttled what, and why, is
    readable after the fact -- the same reason routine_prompt_versions existed.
    """
    if key not in TUNABLE:
        raise OverrideError(
            f"{key!r} is not live-tunable. Tunable: {', '.join(TUNABLE)}. "
            f"Prompts and tools are a member's authority and change only through a PR.")
    if not why or not why.strip():
        raise OverrideError("an override needs a reason -- it is what makes it reviewable")
    row = {"member": member, "key": key, "value": value, "by": by, "why": why.strip(),
           "set_at": _now(), "expires_at": _now() + ttl_hours * 3600}
    p = store or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def live_overrides(member: str, *, store: Path | None = None, now: float | None = None) -> dict:
    """The overrides currently in force for one member: {key: row}, newest wins."""
    p = store or STORE
    if not p.exists():
        return {}
    t = _now() if now is None else now
    out: dict[str, dict] = {}
    for line in p.read_text(errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue          # a torn line must never break a member's run
        if row.get("member") != member or row.get("key") not in TUNABLE:
            continue
        # Newest row for a key wins WHETHER OR NOT it is still live: an expired row must be
        # able to retire an older one. Skipping dead rows here instead would make clear() a
        # no-op -- the cleared row would be ignored and the throttle it was meant to lift
        # would stay in force until its own TTL ran out.
        if float(row.get("expires_at", 0)) <= t:
            out.pop(row["key"], None)
            continue
        out[row["key"]] = row
    return out


def apply(spec: dict, *, store: Path | None = None, now: float | None = None) -> tuple[dict, list]:
    """Return (effective_spec, applied_rows). The git spec is never mutated in place."""
    rows = live_overrides(spec["name"], store=store, now=now)
    if not rows:
        return spec, []
    eff = json.loads(json.dumps(spec))
    applied = []
    for key, row in sorted(rows.items()):
        if key == "max_turns" and eff.get("kind") == "llm":
            eff["llm"]["max_turns"] = row["value"]
        elif key == "model" and eff.get("kind") == "llm":
            eff["llm"]["model"] = row["value"]
        elif key == "enabled":
            eff["enabled"] = bool(row["value"])
        elif key == "schedule":
            eff["schedule"] = row["value"]
        else:
            continue
        applied.append(row)
    return eff, applied


def clear(member: str, key: str, *, by: str, store: Path | None = None) -> dict:
    """Expire an override immediately by writing a row that is already past its TTL."""
    row = {"member": member, "key": key, "value": None, "by": by,
           "why": "cleared", "set_at": _now(), "expires_at": 0}
    p = store or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Tune a fleet member live (dials only).")
    ap.add_argument("member")
    ap.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"))
    ap.add_argument("--clear", metavar="KEY")
    ap.add_argument("--by", default="cli")
    ap.add_argument("--why", default="")
    ap.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args(argv)

    if a.show or (not a.set and not a.clear):
        for key, row in sorted(live_overrides(a.member).items()):
            left = (row["expires_at"] - _now()) / 3600
            print(f"{key} = {row['value']}  (by {row['by']}, {left:.1f}h left) -- {row['why']}")
        return 0
    if a.clear:
        clear(a.member, a.clear, by=a.by)
        print(f"cleared {a.clear} for {a.member}")
        return 0
    key, raw = a.set
    try:
        value = json.loads(raw)
    except Exception:
        value = raw
    try:
        row = set_override(a.member, key, value, by=a.by, why=a.why, ttl_hours=a.ttl_hours)
    except OverrideError as exc:
        print(f"refused: {exc}", file=__import__("sys").stderr)
        return 2
    print(f"{a.member}.{key} = {row['value']} for {a.ttl_hours}h ({row['why']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
