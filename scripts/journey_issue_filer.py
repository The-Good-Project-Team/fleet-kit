#!/usr/bin/env python3
"""journey_issue_filer -- turns a failed sentry journey step into a filed, deduped,
self-closing GitHub issue (gh#660).

THE GAP THIS CLOSES. #656 gave sentry a catalog of journeys (`members/sentry/journeys.yaml`);
#657 (still unbuilt as of this PR -- see PART OF, below) will give it a walker that runs them
and records pass/fail per step. Neither one turns a failure into something a builder can act
on: today that would mean a human reading sentry's own run output line by line. This is the
seam between "a step failed" and "there is exactly one open, evidence-carrying issue for it,
and it closes itself the moment the step passes again."

RESULTS FILE CONTRACT (the interface #657's walker must produce; this is the schema this
file was written against, since #657 does not exist yet -- see PART OF):

  {
    "run": "<run id, e.g. a timestamp or qa-out/<run> directory name>",
    "deploy_sha": "<sha under test this run, or \"\" if unknown>",
    "journeys": [
      {
        "id": "sign-in",                 # matches journeys.yaml's id, kebab-case
        "name": "Sign in",               # matches journeys.yaml's name
        "steps": [
          {
            "index": 0,                  # 0-based position in the journey
            "action": "Navigate to https://philanthropy.org/login",
            "observable_result": "The page renders a sign-in form ...",
            "status": "pass" | "fail",
            "screenshot": "qa-out/<run>/journeys/sign-in/desktop/0.png",  # optional
            "request": {...} | null,     # optional, HTTP request that broke, if observable
            "response": {...} | null     # optional, HTTP response that broke, if observable
          },
          ...
        ]
      },
      ...
    ]
  }

Conventionally written by the walker to `qa-out/<run>/journeys/results.json`, sibling to the
per-step screenshot directories `qa-out/<run>/journeys/<id>/<viewport>/` journeys.yaml already
documents. This file does not read journeys.yaml itself -- the walker already parsed the
catalog to run it, so the walker is what carries action/observable_result into the results
file. That keeps this module decoupled from the catalog's YAML format (no PyYAML dependency
here) and gives it one, already-validated source of truth per run.

LABEL: `fleet:sentry-journey` (UNKNOWN #1 in #660's PRD) -- a new label, not a reuse of
`fleet:visual-change` (#659's), because #659's label is about a visual diff crossing a
threshold and this is about a functional step failing outright; #661's KPI tile and any future
dedup need to be able to tell those apart by label alone.

LAST-PASSING SHA: (UNKNOWN #2) tracked in a small per-journey-step state file (default
`~/.cache/fleet-kit/journey_last_pass.json`), not derived from `runs.jsonl` after the fact --
runs.jsonl records one row per MEMBER PASS, not per journey step, so recovering "which sha last
passed this exact step" from it would mean re-parsing every historical sentry run's own qa-out
tree. A small state file keyed by `<journey_id>::step<index>` is the same shape auto_deploy.sh
already uses for its own last-sha tracking (`STATE=...auto_deploy.last_sha.*`).

DEDUPE: match on a hidden marker in the issue body (`<!-- fleet:sentry-journey key=... -->`),
not on title text -- a title can be edited or reworded by a human without breaking the match,
same reasoning closes_gate.py's own comment-parsing takes for machine-vs-human text.

gh#849: the marker match only ever finds an issue THIS filer wrote. A hand-filed duplicate
(no marker, sometimes not even this label -- #724) falls back to a second, widened search
across every open issue for the journey id and exact step index in its own title/body text --
see `find_open_issue()` and `key_matches_text()`.

VIEWPORT COLLAPSING (gh#660 follow-up, live proof #690/#691): journey_walker.py's own results.json
id convention suffixes every non-desktop viewport onto the journey id (`<id>--<viewport>`), so a
naive `<journey_id>::step<N>` key is viewport-specific by construction -- a step-0 navigation
failure (wrong URL, HTTP error, DNS) fires once per viewport even though it cannot possibly
depend on screen width. `step_key()` collapses ONLY step index 0 (the narrow, defensible rule
marie's PRD asks for -- results.json carries no reliable HTTP-status signal to widen this
further without guessing) back to the bare journey id, so both viewports land on the same key.
Every other step index keeps today's per-viewport key, since rendering/layout failures at later
steps genuinely are per-viewport. `process()` groups same-run entries by their (possibly
collapsed) key before deciding fail/pass, so one run's two-viewport step-0 failure files or
closes exactly once, and a partial recovery (one viewport passing, the other still failing)
never reads as a full one.

PART OF #660, not Closes: acceptance criterion 1 requires a failed step to result in an issue
"within the same [sentry] pass" -- that pass does not exist yet, since #657 (the walker that
would actually run journeys.yaml and emit a results.json) is still unbuilt on main as of this
PR (confirmed: `gh issue view 657` shows it open, unclaimed, no merged PR). This module is
built and demonstrated standalone against the results-file contract above (see
`test_journey_issue_filer.py`, which exercises file -> repeat -> recover end to end against a
mocked `gh`) so it needs zero changes once #657 lands and starts writing that file for real.

Pure command-BUILDERS are separated from `gh`-executing calls so tests assert exact argv and
exact title/body text without a network call -- same split board_github.py already uses.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PREFIX = os.environ.get("FLEET_LABEL_PREFIX", "fleet:")
LABEL_JOURNEY = f"{PREFIX}sentry-journey"
LABEL_COLOR = "5319e7"
LABEL_DESC = "sentry: a journey step (gh#657/gh#660) failed and was filed by journey_issue_filer.py"

# fleet-kit#785: red reuses this whole filer (dedup, self-close, state) under its own label,
# marker and wording. A profile is the only thing that varies; everything below defaults to
# SENTRY so existing callers and tests are byte-for-byte unchanged.
class Profile:
    def __init__(self, key, label, color, desc, marker_tag, lead, title_suffix):
        self.key = key; self.label = label; self.color = color; self.desc = desc
        self.marker_tag = marker_tag; self.lead = lead; self.title_suffix = title_suffix

SENTRY = Profile("sentry", LABEL_JOURNEY, LABEL_COLOR, LABEL_DESC, "sentry-journey",
                 "Sentry drove the **{name}** journey as a real person would, and this step stopped doing its job.",
                 "isn't working")
RED = Profile("red", f"{PREFIX}red-team", "b60205",
              "red: an adversarial attack (gh#785) landed against our product; filed by journey_issue_filer.py",
              "red-team",
              "Red drove the **{name}** attack against our own product, and it LANDED -- the product did the unsafe thing.",
              "can be broken")
PROFILES = {"sentry": SENTRY, "red": RED}

DEFAULT_STATE_PATH = Path(
    os.environ.get(
        "FLEET_JOURNEY_STATE",
        str(Path.home() / ".cache" / "fleet-kit" / "journey_last_pass.json"),
    )
)

# gh#770: journeys.yaml's own URLs are all philanthropy.org, so an explicit target repo (not
# whatever `gh` defaults to for the sandbox's ambient checkout, gh#151) is the correct default.
DEFAULT_REPO = "The-Good-Project-Team/philanthropy"

def _marker_re(tag):
    return re.compile(r"<!--\s*fleet:" + re.escape(tag) + r"\s+key=([^\s]+?)\s*-->")


MARKER_RE = _marker_re("sentry-journey")  # back-compat: sentry's own marker


def is_viewport_independent(step_index: int) -> bool:
    """Only step 0 (navigating into a journey) is treated as viewport-independent -- see
    VIEWPORT COLLAPSING above for why this is deliberately narrow."""
    return step_index == 0


def base_journey_id(journey_id: str) -> str:
    """Strips journey_walker.py's `--<viewport>` suffix, if present, back to the catalog id."""
    idx = journey_id.find("--")
    return journey_id[:idx] if idx != -1 else journey_id


def viewport_of(journey_id: str) -> str:
    """Inverse of journey_walker.py's id convention: no `--<viewport>` suffix means desktop."""
    idx = journey_id.find("--")
    return journey_id[idx + 2:] if idx != -1 else "desktop"


def step_key(journey_id: str, step_index: int) -> str:
    if is_viewport_independent(step_index):
        journey_id = base_journey_id(journey_id)
    return f"{journey_id}::step{step_index}"


def marker_for(key: str, tag: str = "sentry-journey") -> str:
    return f"<!-- fleet:{tag} key={key} -->"


def key_from_body(body: str, tag: str = "sentry-journey") -> str | None:
    m = _marker_re(tag).search(body or "")
    return m.group(1) if m else None


# gh#849: the marker match above only ever finds an issue THIS filer wrote itself. A human or
# another agent who hand-files the same defect (a real, recurring case -- #814/#724 were both
# hand-filed dupes of what the filer would otherwise re-file) carries no marker, so `process()`
# needs a second, textual way to recognise "this is already tracked" before it files again.
_KEY_RE = re.compile(r"^(.*)::step(\d+)$")


def parse_key(key: str) -> "tuple[str, int] | None":
    """Inverse of step_key(): the bare (viewport-stripped) journey id and step index a human
    would actually write about, e.g. "search-and-open-org--mobile_390::step1" -> ("search-and-
    open-org", 1). Returns None for a key that doesn't match the filer's own `::step<N>` shape."""
    m = _KEY_RE.match(key)
    if not m:
        return None
    return base_journey_id(m.group(1)), int(m.group(2))


def key_matches_text(key: str, text: str) -> bool:
    """True when `text` (an issue's title + body) names both the journey and the exact failing
    step -- the narrow signal #849's PRD asks for: journey id alone is not enough (AC5, a
    different step in the same journey must not match), and neither is "step N" alone."""
    parsed = parse_key(key)
    if parsed is None:
        return False
    journey_id, step_index = parsed
    if journey_id not in text:
        return False
    return re.search(r"\bstep\s*" + re.escape(str(step_index)) + r"\b", text, re.IGNORECASE) is not None


# --- pure content builders (unit-tested; never executed by tests) ------------------------------

def build_issue_title(journey_name: str, step_action: str, suffix: str = "isn't working") -> str:
    action = (step_action or "").strip().rstrip(".")
    if len(action) > 70:
        action = action[:67] + "..."
    return f'{journey_name}: "{action}" {suffix}'


def _repro_steps(steps: list[dict], up_to_index: int) -> str:
    lines = []
    for s in steps:
        if s.get("index", 0) > up_to_index:
            break
        lines.append(f"{s.get('index', 0) + 1}. {s.get('action', '').strip()}")
    return "\n".join(lines)


def build_issue_body(
    journey: dict,
    step: dict,
    run: str,
    deploy_sha: str,
    last_pass_sha: str | None,
    key: str,
    viewports: list[str] | None = None,
    profile: "Profile" = SENTRY,
) -> str:
    lines = [
        profile.lead.format(name=journey.get("name", journey.get("id"))),
        "",
        f"**Failed step:** {step.get('action', '').strip()}",
        f"**Expected:** {step.get('observable_result', '').strip()}",
    ]
    if viewports:
        lines.append(f"**Affected viewports:** {', '.join(viewports)}")
    lines += [
        "",
        "**Repro steps (run these in order):**",
        _repro_steps(journey.get("steps", [step]), step.get("index", 0)),
        "",
    ]
    screenshot = step.get("screenshot")
    if screenshot:
        lines += [f"**Screenshot:** {screenshot}", ""]
    request, response = step.get("request"), step.get("response")
    if request or response:
        lines += ["**What broke, on the wire:**"]
        if request:
            lines.append(f"- Request: `{json.dumps(request)}`")
        if response:
            lines.append(f"- Response: `{json.dumps(response)}`")
        lines.append("")
    lines += [
        "**Last deploy sha known to pass this step:** "
        + (last_pass_sha or "unknown -- this is the first observed failure"),
        f"**This run:** {run} (sha {deploy_sha or 'unknown'})",
        "",
        marker_for(key, profile.marker_tag),
    ]
    return "\n".join(lines)


def build_file_cmd(title: str, body: str, label: str = LABEL_JOURNEY, repo: str | None = None) -> list[str]:
    cmd = ["gh", "issue", "create", "--title", title, "--body", body, "--label", label]
    if repo:
        cmd += ["--repo", repo]
    return cmd


def build_list_cmd(label: "str | None" = LABEL_JOURNEY, repo: str | None = None) -> list[str]:
    """gh#849: `label=None` drops the `--label` filter entirely -- the widened fallback search
    `find_open_issue()` uses once the marker match misses, since a hand-filed duplicate (#724)
    may carry neither the marker nor even this label. `--json` always includes `title` too,
    for that same fallback's title-or-body text match; the marker fast path ignores it."""
    cmd = ["gh", "issue", "list", "--state", "open", "--limit", "200", "--json", "number,title,body"]
    if label:
        cmd += ["--label", label]
    if repo:
        cmd += ["--repo", repo]
    return cmd


def build_comment_cmd(number: int, body: str, repo: str | None = None) -> list[str]:
    cmd = ["gh", "issue", "comment", str(number), "--body", body]
    if repo:
        cmd += ["--repo", repo]
    return cmd


def build_close_cmd(number: int, body: str, repo: str | None = None) -> list[str]:
    cmd = ["gh", "issue", "close", str(number), "--comment", body]
    if repo:
        cmd += ["--repo", repo]
    return cmd


# --- state (last-passing sha per journey+step) ---------------------------------------------

def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


# --- execution -----------------------------------------------------------------------------

def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)


def ensure_label(runner=_run, profile: "Profile" = SENTRY, repo: str | None = None) -> "str | None":
    """Idempotent: `gh` errors on a duplicate create; that failure is expected and ignored.
    Any other create failure (e.g. a 422 on a too-long description) is NOT a duplicate -- it
    means the label never got made, so every later `gh issue create --label ...` will fail
    with 'not found', silently, on a zero-finding run that never reaches that call (gh#892).
    Returned (and printed) so the caller can fold it into the run's error summary instead of
    swallowing it the same way the duplicate case is swallowed.

    gh#922: `repo` must be forwarded like every sibling builder -- without it this call falls
    back to the ambient checkout's repo, which recreates the label in the wrong place on a
    fresh target repo and makes every later `--label` issue create fail "not found"."""
    cmd = ["gh", "label", "create", profile.label, "--color", profile.color, "--description", profile.desc]
    if repo:
        cmd += ["--repo", repo]
    rc, out = runner(cmd)
    if rc == 0 or "already exists" in out:
        return None
    msg = f"ensure_label({profile.label}) failed: {out[:300]}"
    print(f"journey_issue_filer: {msg}", file=sys.stderr)
    return msg


# gh#914: a `gh issue list` call that fails (rate limit, network blip) is NOT the same fact as
# "the board genuinely has no open issue for this key" -- the caller must be able to tell them
# apart. Confirmed live 2026-09-11: a transient rate-limit error made every lookup this call
# made return the same bare None a real no-match returns, and the caller (process(), which
# branches on truthiness alone) filed #911/#912 as brand-new duplicates of #814/#884.
LOOKUP_FAILED = object()


def _list_issues(cmd: list[str], runner):
    """Shared `gh issue list` execution/parsing for both find_open_issue() passes -- returns
    a list of issue dicts, or LOOKUP_FAILED (never None-as-empty, per gh#914) on any failure."""
    rc, out = runner(cmd)
    if rc != 0:
        print(f"journey_issue_filer: list FAILED: {out[:300]}", file=sys.stderr)
        return LOOKUP_FAILED
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        print(f"journey_issue_filer: list returned unparsable output: {out[:300]}", file=sys.stderr)
        return LOOKUP_FAILED


def find_open_issue(key: str, runner=_run, profile: "Profile" = SENTRY, repo: str | None = None):
    """Returns an issue number on a match, None if the lookup ran cleanly and found none, or
    LOOKUP_FAILED if the lookup itself could not be trusted -- see gh#914 note above.

    Two passes: the marker match (AC3's fast path, unchanged) against this profile's own
    labelled issues; then, only on a clean miss, gh#849's widened fallback across EVERY open
    issue (no label filter -- #724 proved a hand-filed dup can lack even the label) matching
    journey id + step index in the issue's own title/body text. A hand-filed issue this filer
    never wrote gets a comment through this path instead of a second issue."""
    issues = _list_issues(build_list_cmd(profile.label, repo), runner)
    if issues is LOOKUP_FAILED:
        return LOOKUP_FAILED
    for issue in issues:
        if key_from_body(issue.get("body") or "", profile.marker_tag) == key:
            return issue.get("number")

    all_issues = _list_issues(build_list_cmd(None, repo), runner)
    if all_issues is LOOKUP_FAILED:
        return LOOKUP_FAILED
    for issue in all_issues:
        text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
        if key_matches_text(key, text):
            return issue.get("number")
    return None


def load_results(path: Path) -> dict:
    return json.loads(path.read_text())


def iter_steps(results: dict):
    for journey in results.get("journeys", []):
        for step in journey.get("steps", []):
            yield journey, step


def group_by_key(results: dict) -> "dict[str, list[tuple[dict, dict]]]":
    """Groups this run's (journey, step) pairs by their dedupe key, so a step-0 failure that
    ran at two viewports (same key, per VIEWPORT COLLAPSING above) is decided ONCE -- filed
    once, commented once, and only closed when every viewport mapped to that key passed. A
    key that never collapses (every other step index) still ends up with one entry per group,
    which is exactly today's per-viewport behaviour."""
    groups: "dict[str, list[tuple[dict, dict]]]" = {}
    for journey, step in iter_steps(results):
        key = step_key(journey["id"], step.get("index", 0))
        groups.setdefault(key, []).append((journey, step))
    return groups


def process(results_path: Path, state_path: Path = DEFAULT_STATE_PATH, runner=_run, dry_run: bool = False, profile: "Profile" = SENTRY, repo: str | None = None) -> dict:
    """Walks one results.json, files/comments/closes as needed. Returns a summary dict of
    what happened -- never raises on a `gh` failure, since one bad call must not stop the rest
    of the run from being processed (same non-crashing-on-a-single-failure shape #657's own
    walker AC3 requires of itself)."""
    results = load_results(results_path)
    run = results.get("run", "")
    deploy_sha = results.get("deploy_sha", "")
    state = load_state(state_path)
    summary = {"filed": [], "commented": [], "closed": [], "skipped": [], "errors": []}

    if not dry_run:
        label_error = ensure_label(runner, profile, repo)
        if label_error:
            summary["errors"].append(label_error)

    for key, entries in group_by_key(results).items():
        failing = [(j, s) for j, s in entries if s.get("status") == "fail"]

        if failing:
            # Only the viewports that actually failed are "affected" -- a viewport that
            # happened to pass in the same collapsed group must not be reported as broken.
            failing_viewports = sorted({viewport_of(j["id"]) for j, _ in failing})
            journey, step = failing[0]
            existing = None if dry_run else find_open_issue(key, runner, profile, repo)
            if existing is LOOKUP_FAILED:
                # gh#914: the dedup lookup could not run -- filing now risks a duplicate of an
                # issue we simply couldn't see. Skip this key, don't guess, and say so in the
                # summary rather than under "filed" or lumped into unrelated "errors".
                summary["skipped"].append({"key": key, "reason": "lookup_failed"})
                continue
            if existing:
                note = f"Recurred again on run `{run}` (sha `{deploy_sha or 'unknown'}`)."
                if not dry_run:
                    rc, out = runner(build_comment_cmd(existing, note, repo))
                    if rc != 0:
                        summary["errors"].append(f"comment #{existing} failed: {out[:200]}")
                        continue
                summary["commented"].append({"issue": existing, "key": key})
            else:
                title = build_issue_title(journey.get("name", journey["id"]), step.get("action", ""), profile.title_suffix)
                collapsed_viewports = failing_viewports if len(failing_viewports) > 1 else None
                body = build_issue_body(
                    journey, step, run, deploy_sha, state.get(key, {}).get("last_pass_sha"), key,
                    collapsed_viewports, profile,
                )
                cmd = build_file_cmd(title, body, profile.label, repo)
                if dry_run:
                    print(f"[dry-run] would run: {' '.join(cmd)}")
                    summary["filed"].append({"issue": None, "key": key, "title": title})
                    continue
                rc, out = runner(cmd)
                if rc != 0:
                    summary["errors"].append(f"file {key} failed: {out[:300]}")
                    continue
                print(out)  # the issue URL -- callers log it as the durable id
                m = re.search(r"/issues/(\d+)\s*$", out)
                summary["filed"].append({"issue": int(m.group(1)) if m else None, "key": key, "title": title})

        elif entries and all(s.get("status") == "pass" for _, s in entries):
            # every entry mapped to this key passed -- a partial recovery (see AC6), or a
            # step whose status is neither "pass" nor "fail" (a walker crash, an unrecognized
            # value), never reaches this branch: it is left untouched this run, same as the
            # original per-step `elif status == "pass":` guard did.
            state[key] = {"last_pass_sha": deploy_sha, "last_pass_run": run}
            existing = None if dry_run else find_open_issue(key, runner, profile, repo)
            if existing is LOOKUP_FAILED:
                summary["skipped"].append({"key": key, "reason": "lookup_failed"})
                continue
            if existing:
                note = f"Passing again as of run `{run}` (sha `{deploy_sha or 'unknown'}`)."
                if not dry_run:
                    rc, out = runner(build_close_cmd(existing, note, repo))
                    if rc != 0:
                        summary["errors"].append(f"close #{existing} failed: {out[:200]}")
                        continue
                summary["closed"].append({"issue": existing, "key": key})

    if not dry_run:
        save_state(state_path, state)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", required=True, type=Path, help="path to a walker results.json")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="per-journey last-pass state file")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen, touch nothing")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="sentry", help="sentry (journeys) or red (adversarial), fleet-kit#785")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="owner/name to file/list/comment/close against (gh#770); not the sandbox's ambient gh default")
    args = ap.parse_args()

    summary = process(args.results, args.state, dry_run=args.dry_run, profile=PROFILES[args.profile], repo=args.repo)
    print(json.dumps(summary, indent=2))
    if summary.get("skipped"):
        # gh#914 AC7: a reader of the pass report must be able to see the run was degraded
        # (the board couldn't be read for some keys), not just a clean-looking summary.
        print(
            f"journey_issue_filer: {len(summary['skipped'])} key(s) skipped -- "
            "dedup lookup failed, board could not be read",
            file=sys.stderr,
        )
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
