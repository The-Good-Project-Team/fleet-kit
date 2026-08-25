#!/usr/bin/env python3
"""Prove a fresh clone of this kit actually works, before you schedule anything.

Run: python3 scripts/selftest.py

Checks only what the kit itself owns -- no network, no gh, no claude. It answers one question:
"did I copy this correctly", not "is my fleet configured". That second question is what step 5
of the README (one hand-run builder pass) is for.

This exists because the kit's own port caught a real break: run_report.py imported a scoring
module that was never copied, so a fresh clone crashed on import. Nothing noticed until someone
ran it.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent

ok, fail = [], []


def check(name, fn):
    try:
        fn()
        ok.append(name)
    except Exception as exc:  # noqa: BLE001 -- a selftest reports, it does not raise
        fail.append((name, f"{type(exc).__name__}: {exc}"))


def _members():
    import member_spec
    specs = member_spec.load_all(ROOT / "members")
    assert specs, "no member specs found in members/"
    return specs


def _members_dir_default_is_right():
    # Regression check: MEMBERS_DIR once pointed at scripts/members (a directory that has
    # never existed -- members/ lives at repo root, a sibling of scripts/), so
    # member_spec.load_all() with NO explicit path silently returned []. Every OTHER check in
    # this file passes ROOT / "members" explicitly and would never have caught that -- this is
    # the one check that exercises the module's own default.
    import member_spec
    assert member_spec.MEMBERS_DIR == ROOT / "members", (
        f"member_spec.MEMBERS_DIR = {member_spec.MEMBERS_DIR}, expected {ROOT / 'members'}")
    assert member_spec.load_all(), "member_spec.load_all() with its OWN default path found nothing"


def _member_specs_validate():
    for s in _members():
        assert s["name"] and s["llm"]["prompt_file"]


def _report_contract():
    import run_report
    text = ("FLEET-REPORT\nOutcome: filed #12 for the broken hook\n"
            "Evidence: app.py:41\nVision-link: a user can finish the task\n")
    rec = run_report.build_record(member="t", run_id="r", kind="llm", exit_code=0,
                                  pass_text=text, usage={"num_turns": 3, "weighted_input": 9},
                                  vision_required=True)
    assert rec["status"] == "ok", rec["status"]
    # The property that matters most: silence is RECORDED, not ignored.
    quiet = run_report.build_record(member="t", run_id="r", kind="llm", exit_code=0,
                                    pass_text="had a look around", usage=None,
                                    vision_required=False)
    assert quiet["status"] == "reported_nothing", quiet["status"]
    # A budget decline or a timeout never gets to write a FLEET-REPORT block -- that empty
    # outcome must not collapse into reported_nothing (#3015, #3083).
    declined = run_report.build_record(member="t", run_id="r", kind="llm", exit_code=3,
                                       pass_text="", usage=None, vision_required=False)
    assert declined["status"] == "budget_declined", declined["status"]
    timed_out = run_report.build_record(member="t", run_id="r", kind="llm", exit_code=124,
                                        pass_text="", usage=None, vision_required=False)
    assert timed_out["status"] == "timed_out", timed_out["status"]


def _overrides_are_narrow():
    import overrides
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "ov.jsonl"
        spec = _members()[0]
        overrides.set_override(spec["name"], "max_turns", 7, by="selftest",
                               why="proving the dial works", store=store)
        eff, applied = overrides.apply(spec, store=store)
        assert eff["llm"]["max_turns"] == 7 and applied
        assert spec["llm"]["max_turns"] != 7, "the git spec must not be mutated"
        # Authority is never live-tunable.
        try:
            overrides.set_override(spec["name"], "tools", ["x"], by="selftest",
                                   why="should refuse", store=store)
        except overrides.OverrideError:
            return
        raise AssertionError("tools must NOT be live-tunable")


def _env_example_exists():
    assert (ROOT / "fleet.env.example").is_file()
    assert not (ROOT / "fleet.env").exists(), "fleet.env is yours to create and must stay untracked"


def _schedulers_for_both_platforms():
    assert list((ROOT / "schedulers" / "launchd").glob("*.plist")), "no launchd templates"
    assert list((ROOT / "schedulers" / "systemd").glob("*.timer")), "no systemd timers"


def _bash_eval(setup: str, expr: str) -> str:
    """Source account_pool.sh in a scratch HOME and echo one expression's result."""
    import subprocess
    pool = ROOT / "scripts" / "account_pool.sh"
    with tempfile.TemporaryDirectory() as tmp:
        script = f'set -uo pipefail\nexport FLEET_LOG_DIR="{tmp}"\n{setup}\nsource "{pool}"\n{expr}\n'
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
        return proc.stdout.strip()


def _classifier_ignores_incidental_rate_limit_text():
    """A fleet member that merely PRINTS the words "rate limit" must not gate its account.

    Live incident 2026-08-25: gru runs `gh api rate_limit` every pass to check GitHub quota.
    account_pool.sh tees the command's full stdout, so that phrase landed in the captured
    output, classified as `exhausted`, and gated both Anthropic accounts for an hour at a
    time -- roughly 2h of total fleet downtime, caused entirely by the fleet reading its own
    log text back as an outage.
    """
    gh_output = 'rate limit: {"limit":5000,"remaining":4969,"used":31}'
    got = _bash_eval("", f'_account_pool_classify_failure {json.dumps(gh_output)}')
    assert got != "exhausted", (
        f"incidental 'rate limit' text classified as {got!r} -- this gates a healthy account"
    )


def _classifier_still_catches_a_real_limit():
    """The narrowed regex must not go blind to an actual Anthropic limit."""
    for real in [
        "You've hit your weekly limit. Your limit resets 1pm (UTC).",
        "You have reached your usage limit",
        "API error 429: too many requests",
    ]:
        got = _bash_eval("", f'_account_pool_classify_failure {json.dumps(real)}')
        assert got == "exhausted", f"real limit {real!r} classified as {got!r}, not exhausted"


def _unparseable_exhaustion_gates_briefly_not_for_an_hour():
    """Exhaustion with no stated reset time is suspect -- back off minutes, not an hour.

    Every gate written during the 2026-08-25 outage came from this fallback, because the
    output was misclassified and so carried no reset time to parse. A 1h gate turned each
    false positive into twelve lost ticks, during which the fleet could not fix anything --
    including this bug.
    """
    out = _bash_eval(
        "", '_account_pool_mark_exhausted acct "exhausted, no reset time stated" >/dev/null; '
            'now=$(date +%s); epoch=$(awk \'{print $2}\' "$ACCOUNT_POOL_STATE_FILE"); '
            'echo $(( epoch - now ))'
    )
    gap = int(out)
    assert gap <= 600, f"unparseable exhaustion gated for {gap}s -- too long to stay dark"


def _a_real_reset_time_is_still_honored():
    """A stated reset must win over the short fallback, so we don't hammer a genuine limit."""
    out = _bash_eval(
        "", '_account_pool_mark_exhausted acct "hit your weekly limit, resets 1pm (UTC)" >/dev/null; '
            'now=$(date +%s); epoch=$(awk \'{print $2}\' "$ACCOUNT_POOL_STATE_FILE"); '
            'echo $(( epoch - now ))'
    )
    assert int(out) > 600, "a stated reset time collapsed to the short fallback"


def _pool_logs_successes_so_downtime_is_measurable():
    """account_health_check.sh measures outage age from the last SUCCESS line.

    Without a success line the log holds only failures, and since a failure is re-appended
    every tick, "age of newest line" is permanently ~0 and the pager can never fire. That is
    exactly why ~2h of downtime on 2026-08-25 paged nobody.
    """
    src = (ROOT / "scripts" / "account_pool.sh").read_text()
    assert "call succeeded" in src, "account_pool_run logs no success line to measure from"

    check_src = (ROOT / "scripts" / "account_health_check.sh").read_text()
    assert "call succeeded" in check_src, "health check does not measure from the last success"
    assert 'last_line=$(tail -1 "$POOL_LOG")\nage' not in check_src


if __name__ == "__main__":
    check("member specs load and validate", _member_specs_validate)
    check("member_spec's OWN default MEMBERS_DIR resolves (not just an explicit path)", _members_dir_default_is_right)
    check("report contract: ok + silence is recorded", _report_contract)
    check("overrides tune dials, refuse authority", _overrides_are_narrow)
    check("fleet.env.example present, fleet.env untracked", _env_example_exists)
    check("schedulers ship for macOS and Linux", _schedulers_for_both_platforms)
    check("incidental 'rate limit' text does not gate an account", _classifier_ignores_incidental_rate_limit_text)
    check("a real usage limit is still classified exhausted", _classifier_still_catches_a_real_limit)
    check("exhaustion with no stated reset backs off minutes, not an hour", _unparseable_exhaustion_gates_briefly_not_for_an_hour)
    check("a stated reset time is honored over the fallback", _a_real_reset_time_is_still_honored)
    check("pool logs successes so outage length is measurable", _pool_logs_successes_so_downtime_is_measurable)

    for n in ok:
        print(f"  ok    {n}")
    for n, why in fail:
        print(f"  FAIL  {n}\n        {why}")
    print(f"\n{len(ok)} passed, {len(fail)} failed")
    raise SystemExit(1 if fail else 0)
