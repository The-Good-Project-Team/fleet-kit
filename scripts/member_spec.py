#!/usr/bin/env python3
"""One fleet member = one file in scripts/fleet/members/. This module reads and validates them.

WHY THIS EXISTS. The fleet ran on TWO definition stores for a historical reason that stopped
being true on 2026-08-06, when the claude.ai cloud-routine ring was retired: the Postgres
`routine_registry` was that ring's store and never got collapsed into the repo. The half that
lives in git has a reconciler (plist_reconcile.py, every gitpull tick) so a merged change is
live in ~2 minutes. The half in Postgres has none -- its only writer for the five scout-*
slugs was scripts/register_lane_scouts.py, run BY HAND. Measured consequences on 2026-08-18:
  * 6 of 7 lanes had never logged a pass -- the lane_pass_log instruction lived in the manual
    registrar, so the running scouts were never given it. They were not disobeying.
  * routine-poll claimed scout-datadog with max_runtime_secs=900 while the repo had said 1800
    for days.
  * tokens_used was NULL for every routine run ever: routine_poll.sh never passed
    --output-format json.
  * tools could not be scoped per member at all -- the allowlist was hardcoded in the shell
    and routine_registry had no column for it.
So: git is the one store, and a member's schedule, prompt, tools, model and turn budget are
ordinary reviewed fields in one file.

FORMAT IS JSON, NOT YAML, AND THAT IS DELIBERATE. the fleet host -- the only machine the fleet runs on
-- has no `yaml` module for /usr/bin/python3 (verified 2026-08-18: ModuleNotFoundError). A
member file must be readable by the runner ON THAT BOX with the stdlib alone. This is the same
defect class as the prmerged plist pinning an interpreter that did not exist (#2750) and the
scout prompts telling agents to run bare `python` when the fleet host has none: an assumed dependency
that is absent exactly where it has to work. `.fleet.json` keeps comments out but costs
nothing a reviewer needs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MEMBERS_DIR = Path(__file__).resolve().parent / "members"

KINDS = ("llm", "mechanical")

# A mechanical member runs a command; an LLM member runs a prompt through claude -p. Both get
# the same lifecycle and the same report contract -- that is the whole point of one schema.
_REQUIRED = ("name", "kind", "schedule", "timeout_s", "enabled", "report")
_LLM_REQUIRED = ("model", "max_turns", "prompt_file", "tools")


class SpecError(ValueError):
    """A member file that cannot be trusted to generate a plist or run a pass."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SpecError(msg)


def validate(spec: dict, *, filename: str = "<dict>") -> dict:
    """Full validation. Raises SpecError with the file named, never returns a partial spec."""
    where = f"{filename}: "
    for key in _REQUIRED:
        _require(key in spec, f"{where}missing required key {key!r}")

    name = spec["name"]
    _require(isinstance(name, str) and name, f"{where}name must be a non-empty string")
    if filename not in ("<dict>",):
        stem = Path(filename).name.replace(".fleet.json", "")
        _require(stem == name, f"{where}filename must match name {name!r}, got {stem!r}")

    kind = spec["kind"]
    _require(kind in KINDS, f"{where}kind must be one of {KINDS}, got {kind!r}")

    sched = spec["schedule"]
    _require(isinstance(sched, dict), f"{where}schedule must be an object")
    # Three shapes, and the distinction between the last two is load-bearing: launchd's
    # StartCalendarInterval with a Minute but NO Hour means "every hour at :MM", while adding
    # an Hour makes it once a DAY. Collapsing them into one "calendar" field silently turned
    # magikarp/mbuild/mpm from hourly into daily when this schema was first drafted -- a 24x
    # cadence loss that renders as a perfectly valid plist.
    keys = [k for k in ("interval_s", "hourly_at_minute", "daily_at") if k in sched]
    _require(len(keys) == 1,
             f"{where}schedule needs exactly one of interval_s, hourly_at_minute, daily_at")
    if "interval_s" in sched:
        _require(isinstance(sched["interval_s"], int) and sched["interval_s"] > 0,
                 f"{where}schedule.interval_s must be a positive int")
    elif "hourly_at_minute" in sched:
        m = sched["hourly_at_minute"]
        _require(isinstance(m, int) and 0 <= m <= 59,
                 f"{where}schedule.hourly_at_minute must be an int 0-59")
    else:
        _require(isinstance(sched["daily_at"], str) and ":" in sched["daily_at"],
                 f"{where}schedule.daily_at must look like 'HH:MM'")

    _require(isinstance(spec["timeout_s"], int) and spec["timeout_s"] > 0,
             f"{where}timeout_s must be a positive int")
    _require(isinstance(spec["enabled"], bool), f"{where}enabled must be a bool")

    if kind == "llm":
        llm = spec.get("llm")
        _require(isinstance(llm, dict), f"{where}kind=llm needs an llm block")
        for key in _LLM_REQUIRED:
            _require(key in llm, f"{where}llm.{key} is required for kind=llm")
        # Tools are the reason the registry had to die: it had NO column for them, so every
        # scout got one hardcoded allowlist. Requiring an explicit allow list here means a
        # member's authority is reviewable in its own diff.
        tools = llm["tools"]
        _require(isinstance(tools, dict) and isinstance(tools.get("allow"), list) and tools["allow"],
                 f"{where}llm.tools.allow must be a non-empty list")
        _require(isinstance(tools.get("deny", []), list), f"{where}llm.tools.deny must be a list")
        _require(isinstance(llm["max_turns"], int) and llm["max_turns"] > 0,
                 f"{where}llm.max_turns must be a positive int")
    else:
        _require(isinstance(spec.get("command"), str) and spec["command"],
                 f"{where}kind=mechanical needs a command string")

    report = spec["report"]
    _require(isinstance(report, dict), f"{where}report must be an object")
    _require(report.get("vision_link") in ("required", "optional"),
             f"{where}report.vision_link must be 'required' or 'optional'")
    return spec


def load(path: str | os.PathLike) -> dict:
    p = Path(path)
    try:
        spec = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise SpecError(f"{p.name}: not valid JSON ({exc})") from exc
    return validate(spec, filename=str(p))


def load_all(members_dir: str | os.PathLike | None = None) -> list[dict]:
    """Every member, sorted by name so any generated output is deterministic.

    Determinism is load-bearing, not tidiness: plist_reconcile_core.classify() decides
    REINSTALL by comparing repo bytes to installed bytes, so a generator whose output ordering
    wobbles would make every gitpull tick see spurious drift and bootout/bootstrap the whole
    fleet every two minutes.
    """
    d = Path(members_dir or MEMBERS_DIR)
    return [load(f) for f in sorted(d.glob("*.fleet.json"))]


def by_name(name: str, members_dir: str | os.PathLike | None = None) -> dict:
    d = Path(members_dir or MEMBERS_DIR)
    p = d / f"{name}.fleet.json"
    if not p.exists():
        raise SpecError(f"no such fleet member: {name} (looked for {p})")
    return load(p)
