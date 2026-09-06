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
that persona's own charter (`<name>.md`) -- config never inlines the prompt text, only a path
to it. This is the actual modularity: adding or removing a persona is copying or deleting one
self-contained folder, nothing else in the kit changes.

EVERY MEMBER IS AN LLM. Reif, 2026-08-21: "a persona has a capability, and a goal, its not
about running a few scripts -- granted having those scripts available is nice, as a tool but
not as its complete existence." The kit briefly had a `kind: mechanical` escape hatch (a
member that just execs its own .py/.sh, no claude -p call, no reasoning) for roomba/the-fixer/
messenger -- deterministic-looking work that seemed safer as code. That inverted the actual
design: a script with no goal behind it can't notice when its own premise stops holding (a
safety check that's gone stale, a new failure shape its checklist never anticipated), it can
only execute the branch someone already wrote. The fix is not "run the script" as the member's
whole existence -- it's "the member has a goal, and the script is one tool in its allowlist it
reaches for," same as Bash or Read. roomba/the-fixer/messenger kept their scripts; they gained
a charter and a goal that can call the script, read its output, and decide, rather than being
the script.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

MEMBERS_DIR = Path(__file__).resolve().parent.parent / "members"

# {{TOKEN}} in a charter is either (a) a template slot nobody filled in for this product --
# jefe.md shipped with {{VISION}}/{{NORTH_STAR_METRIC}} unfilled and ran silently as a no-op on
# every hourly tick for its entire life, since "explain your task" reads as a coherent LLM
# response, not an error -- or (b) a runtime slot a member's own `runner` script fills in per
# invocation (judge-judy's {{PR_BODY}}/{{DIFF}}/{{TRUNCATION_NOTE}}, substituted by
# judge-judy.sh before claude -p ever sees the prompt). Only (a) is a defect: a charter with NO
# runner has nothing left to fill the slot, ever, so an unfilled {{TOKEN}} there is permanent,
# not "not yet". A charter WITH a runner is assumed to have its placeholders covered by that
# script -- this check only distinguishes the two cases, it doesn't parse the runner itself.
_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")

# Every member's behavior is a prompt run through claude -p. Tools (including a member's own
# helper scripts, reached via Bash) are how it acts -- never a substitute for having a goal.
_REQUIRED = ("name", "emoji", "mandate", "schedule", "timeout_s", "enabled", "report", "llm")
# max_turns is deliberately NOT required: an absent cap means UNCAPPED, and that is the fleet's
# default posture as of 2026-08-26. Measured on 142 real minion runs, 43 hit the 60-turn wall
# while only 8 came near the budget cap -- every `stop_reason: tool_use` row sat at ~61 turns,
# the CLI cutting a pass mid-tool-call with budget to spare. A truncated pass still spends
# everything it spent before the cut and lands `reported_nothing`, so the cap converted
# expensive-but-finishable work into paid-for nothing. Control moved to SELECTION (gru sizes
# each hour's work to what maxx says the hour affords) and to CHARTER QUALITY (jefe prunes a
# rambling prompt; dumbledore manages jefe). Reif, 2026-08-26: "we must control via
# intelligence vs by force." A spec MAY still set max_turns; if it does, it must be valid.
_LLM_REQUIRED = ("model", "prompt_file", "tools")


class SpecError(ValueError):
    """A member file that cannot be trusted to generate a plist or run a pass."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SpecError(msg)


def validate_schedule(sched, *, where: str = "") -> None:
    """The exactly-one-of shape check for a schedule object.

    Pulled out of validate() (gh#208) so overrides.py can run the SAME check on a live
    dial-edit that this function already runs on a git-committed spec -- a malformed
    schedule is exactly as bad coming from the dashboard as it is coming from a PR, but only
    the PR path called this before.
    """
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


def validate_max_turns(value, *, where: str = "") -> None:
    """Same reasoning as validate_schedule: shared with overrides.py's live dial-edit path."""
    _require(isinstance(value, int) and value > 0,
             f"{where}max_turns must be a positive int when set (omit it for uncapped)")


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

    validate_schedule(spec["schedule"], where=where)

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
    # The checklist is what makes a member reviewable and prunable -- it is NOT a task tracker
    # to reimplement. An llm member runs its checklist through Claude Code's own TodoWrite
    # (already in its tools if granted); this schema only owns the list a human/reviewer reads,
    # never a second progress-tracking mechanism competing with the one already in the harness.
    #
    # `escalation` is the bounded-but-agentic valve: every checklist is finite and every real
    # pass eventually hits something the list didn't anticipate. Without a stated valve a
    # member either silently improvises past its scope (unbounded) or hard-stops on anything
    # unlisted (brittle). Naming the valve up front makes "go outside the checklist" a decision
    # the spec author made on purpose, not a default a model invents mid-run.
    _require(isinstance(mandate.get("escalation"), str) and mandate["escalation"],
             f"{where}mandate.escalation must be a non-empty string (what to do when the "
             f"checklist doesn't cover what this pass hit -- e.g. 'file a backlog item and "
             f"continue' vs 'stop and report, never act blind')")

    llm = spec.get("llm")
    _require(isinstance(llm, dict), f"{where}llm block is required -- every member is an llm")
    for key in _LLM_REQUIRED:
        _require(key in llm, f"{where}llm.{key} is required")
    # Tools are the reason the registry had to die: it had NO column for them, so every
    # scout got one hardcoded allowlist. Requiring an explicit allow list here means a
    # member's authority is reviewable in its own diff. A member's own helper script (roomba.py,
    # the-fixer.sh) is just another entry here, reached through Bash like any other tool.
    tools = llm["tools"]
    _require(isinstance(tools, dict) and isinstance(tools.get("allow"), list) and tools["allow"],
             f"{where}llm.tools.allow must be a non-empty list")
    _require(isinstance(tools.get("deny", []), list), f"{where}llm.tools.deny must be a list")
    if "max_turns" in llm:
        validate_max_turns(llm["max_turns"], where=where)

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
    spec = validate(spec, filename=str(p))
    _check_charter(spec, p.parent)
    return spec


def _check_charter(spec: dict, member_dir: Path) -> None:
    """A charter with no `runner` has no mechanism to ever fill a {{TOKEN}} slot -- catch the
    jefe.md class of bug (a template placeholder that ships live and runs as a silent no-op
    forever) at load time, the same place every other structural mistake here gets caught."""
    if "runner" in spec["llm"]:
        return
    charter = member_dir / spec["llm"]["prompt_file"]
    if not charter.exists():
        return  # behavior_path() raises the "charter not found" error for this case
    hits = sorted(set(_PLACEHOLDER_RE.findall(charter.read_text())))
    _require(not hits,
              f"{spec['name']}: charter {charter.name} has unfilled placeholder(s) {hits} and "
              f"no llm.runner to fill them at runtime -- fill them in or the member silently "
              f"no-ops every pass (see jefe #22 for the real incident)")


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
    """Resolve a member's charter (llm.prompt_file) to an absolute path, relative to ITS OWN
    directory -- never the repo root or cwd. This is what keeps a persona folder
    copy/paste-portable: the spec never needs to know where members/ itself lives."""
    d = Path(members_dir or MEMBERS_DIR) / spec["name"]
    p = d / spec["llm"]["prompt_file"]
    _require(p.exists(), f"{spec['name']}: charter not found at {p}")
    return p
