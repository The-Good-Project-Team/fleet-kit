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

ONE PERSONA = ONE DIRECTORY, not one file. `members/<name>/<name>.fleet.json` sits alongside
that persona's own behavior file (`prompt.md` for kind=llm, a runnable script for
kind=mechanical) -- config never inlines the prompt text or the command logic, only a path to
it. This is the actual modularity: adding or removing a persona is copying or deleting one
self-contained folder, nothing else in the kit changes. A prompt or a script is independently
readable, diffable, and (for mechanical members) independently testable without touching JSON.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MEMBERS_DIR = Path(__file__).resolve().parent.parent / "members"

KINDS = ("llm", "mechanical")

# A mechanical member runs its own script; an LLM member runs its own prompt through claude -p.
# Both get the same lifecycle and the same report contract -- that is the whole point of one
# schema. Both point OUT to a file in their own directory, never inline text/commands in the
# JSON, so the actual behavior stays independently readable/diffable/testable.
_REQUIRED = ("name", "emoji", "kind", "mandate", "schedule", "timeout_s", "enabled", "report")
_LLM_REQUIRED = ("model", "max_turns", "prompt_file", "tools")
_MECHANICAL_REQUIRED = ("run_file",)


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
    # "🪖 gru" for a dashboard tile or a log line -- same reasoning as nonprofit-atlas's own
    # roster.py: a named crew is legible at a glance, a bare launchd/JSON slug is not.
    _require(isinstance(spec.get("emoji"), str) and spec["emoji"],
              f"{where}emoji must be a non-empty string")
    if filename not in ("<dict>",):
        stem = Path(filename).name.replace(".fleet.json", "")
        _require(stem == name, f"{where}filename must match name {name!r}, got {stem!r}")
        # One persona = one directory: members/<name>/<name>.fleet.json. Catches the copy-paste
        # mistake of a folder renamed but its .fleet.json left with the old name (or vice
        # versa) before it becomes a silent "which spec actually loaded" bug at runtime.
        parent = Path(filename).parent.name
        _require(parent == name,
                 f"{where}parent directory must match name {name!r}, got {parent!r} "
                 f"(expected members/{name}/{name}.fleet.json)")

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

    # A persona described only in prose ("try harder", "do better") gives a human nothing to
    # PULL when it underperforms -- no lever, just a vague hope the next pass does better.
    # `mandate` makes the levers structural: what it's judged against (target), the concrete
    # steps it runs through (checklist -- a list, not a paragraph a model can skim past), and
    # its own limits restated in one place a reviewer reads without hunting through prompt
    # prose. Every field here should map to something you could actually dial down in a bad
    # week: fewer checklist items, a tighter target, a lower turn/timeout ceiling.
    mandate = spec["mandate"]
    _require(isinstance(mandate, dict), f"{where}mandate must be an object")
    _require(isinstance(mandate.get("target"), str) and mandate["target"],
             f"{where}mandate.target must be a non-empty string (what this member is judged against)")
    checklist = mandate.get("checklist")
    _require(isinstance(checklist, list) and checklist,
             f"{where}mandate.checklist must be a non-empty list of concrete steps")
    _require(all(isinstance(c, str) and c for c in checklist),
             f"{where}mandate.checklist items must all be non-empty strings")
    _require(isinstance(mandate.get("limits"), dict),
             f"{where}mandate.limits must be an object (turns/timeout/budget, restated for review)")

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
        for key in _MECHANICAL_REQUIRED:
            _require(key in spec, f"{where}{key} is required for kind=mechanical")
        _require(isinstance(spec["run_file"], str) and spec["run_file"],
                 f"{where}run_file must be a non-empty relative path (e.g. 'run.sh')")

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
    return [load(f) for f in sorted(d.glob("*/*.fleet.json"))]


def by_name(name: str, members_dir: str | os.PathLike | None = None) -> dict:
    d = Path(members_dir or MEMBERS_DIR)
    p = d / name / f"{name}.fleet.json"
    if not p.exists():
        raise SpecError(f"no such fleet member: {name} (looked for {p})")
    return load(p)


def behavior_path(spec: dict, members_dir: str | os.PathLike | None = None) -> Path:
    """Resolve a member's prompt_file (llm) or run_file (mechanical) to an absolute path,
    relative to ITS OWN directory -- never the repo root or cwd. This is what keeps a persona
    folder copy/paste-portable: the spec never needs to know where members/ itself lives."""
    d = Path(members_dir or MEMBERS_DIR) / spec["name"]
    rel = spec["llm"]["prompt_file"] if spec["kind"] == "llm" else spec["run_file"]
    p = d / rel
    _require(p.exists(), f"{spec['name']}: behavior file not found at {p}")
    return p
