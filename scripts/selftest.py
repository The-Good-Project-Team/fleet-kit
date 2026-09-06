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

import datetime
import json
import re
import pathlib
import sys
import time
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


def _incomplete_fanout_is_not_reported_nothing():
    """gh#252: the-fixer's own live break, 2026-08-30 04:49 UTC -- it correctly diagnosed 4
    stuck PRs, dispatched one `run_member.sh the-fixer --item <PR>` sub-pass per PR in the
    background, then ended its turn on "I'll wait for all 4 sub-passes..." without ever
    writing Outcome:/Evidence:. exit_code was 0 (a normal end_turn, not a kill/timeout/decline),
    so classify() fell through to `reported_nothing` -- identical to a pass that genuinely did
    nothing, even though $0.52 of real diagnosis work happened and 4 sub-passes were then
    orphan-killed with no link back to this parent.
    """
    import run_report
    text = ("I found 4 stuck PRs and dispatched sub-passes:\n"
            "bash scripts/run_member.sh the-fixer --item 224\n"
            "bash scripts/run_member.sh the-fixer --item 229\n"
            "bash scripts/run_member.sh the-fixer --item 239\n"
            "bash scripts/run_member.sh the-fixer --item 247\n"
            "I'll wait for all 4 sub-passes to finish before filing the final report.\n")
    rec = run_report.build_record(member="the-fixer", run_id="r", kind="llm", exit_code=0,
                                  pass_text=text, usage=None, vision_required=False)
    assert rec["status"] == "incomplete_fanout", rec["status"]
    assert rec["orphaned_items"] == ["224", "229", "239", "247"], rec["orphaned_items"]

    # AC4: a fan-out parent that DOES wait and report normally is unaffected -- same dispatch
    # lines in the prose, but a real Outcome:/Evidence: means this was never an orphan.
    reported = run_report.build_record(
        member="the-fixer", run_id="r2", kind="llm", exit_code=0,
        pass_text=text + "Outcome: fixed and merged all 4 (#224 #229 #239 #247)\n"
                          "Evidence: gh pr list --state merged\n",
        usage=None, vision_required=False)
    assert reported["status"] == "ok", reported["status"]
    assert reported["orphaned_items"] is None, reported["orphaned_items"]

    # A genuinely empty pass (no dispatch evidence at all) must still read as reported_nothing,
    # not incomplete_fanout -- the new branch only fires on the specific empty-outcome-plus-
    # dispatch-evidence combination.
    plain_quiet = run_report.build_record(member="t", run_id="r3", kind="llm", exit_code=0,
                                          pass_text="had a look around", usage=None,
                                          vision_required=False)
    assert plain_quiet["status"] == "reported_nothing", plain_quiet["status"]
    assert plain_quiet["orphaned_items"] is None, plain_quiet["orphaned_items"]


def _incomplete_fanout_matches_task_dispatch_shape():
    """gh#257 AC1: datta's real dispatch invocation (datta.md:109) has no `--item <N>` at all --
    it's `run_member.sh nerd --task "lane=<lane> — ..."`, since nerd is lane-dispatched, not
    item-dispatched. Confirmed live 2026-08-30 07:15:59 UTC (run_id datta-8636-1788073921):
    datta spawned two real nerd sub-passes this way, then ran out of budget mid-poll before ever
    writing Outcome:/Evidence:. Before this fix, `_DISPATCH_RE` only recognized gh#252's `--item`
    shape, so `dispatched_items` was always `[]` for this pass and classify() fell through to
    `reported_nothing` instead of `incomplete_fanout` -- identical real-work loss, missed by the
    original pattern.
    """
    import run_report
    text = ('Spawning nerd sub-passes for the qualifying lanes:\n'
            'FLEET_RUN_NOW=1 bash scripts/run_member.sh nerd --task "lane=datadog — KPI '
            'stale 9h" (PID 9834)\n'
            'FLEET_RUN_NOW=1 bash scripts/run_member.sh nerd --task "lane=ui — guardrail '
            'breached" (PID 9835)\n'
            'Waiting for both nerd passes (datadog, ui) to finish -- the background poll will '
            'notify me when PIDs 9834/9835 exit.\n')
    rec = run_report.build_record(member="datta", run_id="r", kind="llm", exit_code=0,
                                  pass_text=text, usage=None, vision_required=False)
    assert rec["status"] == "incomplete_fanout", rec["status"]
    assert rec["orphaned_items"] == ["datadog", "ui"], rec["orphaned_items"]

    # AC4: a fan-out parent that DOES wait and report normally is unaffected.
    reported = run_report.build_record(
        member="datta", run_id="r2", kind="llm", exit_code=0,
        pass_text=text + "Outcome: both nerd lanes came back clean (#4301)\n"
                          "Evidence: fleet.db runs for datadog/ui\n",
        usage=None, vision_required=False)
    assert reported["status"] == "ok", reported["status"]
    assert reported["orphaned_items"] is None, reported["orphaned_items"]


def _report_lost_is_not_reported_nothing():
    """gh#257 AC2/AC3, unit level: dont-shoot-the-messenger's Case 1 -- a real Report:/Outcome:/
    Evidence: block composed correctly, then overwritten by one more trailing turn (gh#167's
    shape). stream_log.py's own `_detect_trailing_loss` already recognizes this from the raw
    event stream and prints a WARNING, but run_report.py's classify() never consulted it, so
    even a detected loss still landed `reported_nothing` -- no different from a pass that
    genuinely found nothing. This pins classify()'s new `trailing_loss` param: with the
    wrapper's signal present, an empty-outcome run reads as the new `report_lost` status
    (AC2); with the identical input and no signal -- exactly today's pre-fix code path -- it
    still reads `reported_nothing` (AC3), proving the fix changes real behavior.
    """
    import run_report
    # The trailing wrap-up text itself carries no Outcome:/Evidence: lines -- the real report
    # was in the turn before it and is gone by the time run_report.py ever sees `pass_text`.
    lost_wrapup_text = "**Status:** QUIET (no-op) -- nothing to improve, driver absence is by design"

    # AC3: identical input, trailing_loss not signalled -- today's pre-fix behavior, unchanged.
    pre_fix = run_report.build_record(member="dont-shoot-the-messenger", run_id="r1", kind="llm",
                                      exit_code=0, pass_text=lost_wrapup_text, usage=None,
                                      vision_required=False)
    assert pre_fix["status"] == "reported_nothing", pre_fix["status"]

    # AC2: identical input, trailing_loss signalled by the wrapper -- new, distinct status.
    post_fix = run_report.build_record(member="dont-shoot-the-messenger", run_id="r2", kind="llm",
                                       exit_code=0, pass_text=lost_wrapup_text, usage=None,
                                       vision_required=False, trailing_loss=True)
    assert post_fix["status"] == "report_lost", post_fix["status"]
    assert post_fix["status"] not in ("ok", "reported_nothing"), post_fix["status"]

    # AC4-style: neither fix writes/infers the lost Outcome:/Evidence: text anywhere -- only
    # `status` differs between the two records above.
    assert post_fix["outcome"] == pre_fix["outcome"] is None, (post_fix["outcome"], pre_fix["outcome"])
    assert post_fix["evidence"] == pre_fix["evidence"] is None, (post_fix["evidence"], pre_fix["evidence"])

    # A pass that DOES have a real, present outcome is unaffected by the flag even if it were
    # (wrongly) set -- trailing_loss only ever matters on the already-empty-outcome branch.
    reported = run_report.build_record(member="t", run_id="r3", kind="llm", exit_code=0,
                                       pass_text="Outcome: did a thing #12\nEvidence: ran it\n",
                                       usage=None, vision_required=False, trailing_loss=True)
    assert reported["status"] == "ok", reported["status"]


def _incomplete_fanout_outranks_trailing_loss_on_overlap():
    """gh#461 AC2/AC3: a run can satisfy BOTH `_detect_trailing_loss` (a real report existed
    one turn earlier and was overwritten) AND `dispatched_items` (the same pass fanned out a
    background sub-pass it never waited on) -- the-fixer/datta's exact shape of report, then
    keep working, then dispatch. Before this fix classify() checked `trailing_loss` first, so
    this overlap silently read as `report_lost` and gh#252's `orphaned_items` tracking (the
    actionable list of what's still out there unresolved) vanished for that case. Red before
    the reorder in classify(), green after.
    """
    import run_report
    text = ("Report:\nBOTTOM LINE: found the regression, dispatching parallel fixes.\n\n"
            "bash scripts/run_member.sh the-fixer --item 224\n"
            "bash scripts/run_member.sh the-fixer --item 229\n"
            "Waiting for both sub-passes before the final wrap-up.\n")
    # No Outcome:/Evidence: lines -- empty outcome is the precondition for either branch.
    overlap = run_report.build_record(member="the-fixer", run_id="r1", kind="llm", exit_code=0,
                                      pass_text=text, usage=None, vision_required=False,
                                      trailing_loss=True)
    assert overlap["status"] == "incomplete_fanout", overlap["status"]
    assert overlap["status"] != "report_lost", overlap["status"]
    assert overlap["orphaned_items"] == ["224", "229"], overlap["orphaned_items"]

    # AC3-style control: identical dispatch text, trailing_loss NOT signalled -- unaffected,
    # already incomplete_fanout before this fix and must stay so.
    no_overlap = run_report.build_record(member="the-fixer", run_id="r2", kind="llm",
                                         exit_code=0, pass_text=text, usage=None,
                                         vision_required=False, trailing_loss=False)
    assert no_overlap["status"] == "incomplete_fanout", no_overlap["status"]
    assert no_overlap["orphaned_items"] == ["224", "229"], no_overlap["orphaned_items"]

    # Control: trailing_loss signalled with NO dispatch evidence at all -- still report_lost,
    # proving the reorder didn't just delete the trailing_loss branch outright.
    plain_loss = run_report.build_record(member="t", run_id="r3", kind="llm", exit_code=0,
                                         pass_text="a short non-report wrap-up", usage=None,
                                         vision_required=False, trailing_loss=True)
    assert plain_loss["status"] == "report_lost", plain_loss["status"]
    assert plain_loss["orphaned_items"] is None, plain_loss["orphaned_items"]


def _gh167_trailing_loss_flows_end_to_end_through_stream_log_and_run_report():
    """gh#257 AC2/AC3, sourced end-to-end through stream_log.py -> run_report.py (the same two
    scripts run_member.sh chains, minus the `claude -p`/timeout wrapper around them): a synthetic
    stream-json fixture reproducing dont-shoot-the-messenger's real 2026-08-30 06:52:04 UTC shape
    (a report-shaped assistant text block, one intervening tool round-trip, then a short
    non-report wrap-up that becomes the actual `result`) run through stream_log.py's REAL
    `main()` -- not a hand-built dict -- must (AC2) detect the loss and, once run_member.sh's
    `--trailing-loss` flag carries that signal to run_report.py, classify as neither `ok` nor
    `reported_nothing`; and (AC3) the identical fixture, run without that flag -- today's
    pre-fix code path -- must still classify `reported_nothing`, proving the fix changes real
    behavior rather than adding a dead branch nothing exercises.
    """
    import subprocess

    events = [
        {"type": "system", "subtype": "init", "model": "claude-x", "tools": [], "cwd": "/tmp"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text":
            "Report:\nBOTTOM LINE: driver absence is by design, 58 consecutive clean runs.\n\n"
            "Outcome: confirmed FLEET_MESSENGER_DRIVER absence is intentional, no action needed\n"
            "Evidence: `grep FLEET_MESSENGER_DRIVER entrypoint.sh` shows it unset on purpose\n"
        }]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "TaskUpdate",
            "input": {"description": "closing out checklist"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": False,
            "content": "ok"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text":
            "**Status:** QUIET (no-op) -- nothing to improve, driver absence is by design"}]}},
        {"type": "result", "subtype": "success", "num_turns": 12, "total_cost_usd": 0.31,
         "stop_reason": "end_turn",
         "result": "**Status:** QUIET (no-op) -- nothing to improve, driver absence is by design"},
    ]
    stdin_text = "\n".join(json.dumps(e) for e in events) + "\n"

    with tempfile.TemporaryDirectory() as d:
        result_file = Path(d) / "result.json"
        loss_file = Path(d) / "loss.txt"
        p = subprocess.run(
            [sys.executable, str(HERE / "stream_log.py"),
             "--result-out", str(result_file), "--trailing-loss-out", str(loss_file)],
            input=stdin_text, capture_output=True, text=True, timeout=30)
        assert p.returncode == 0, p.stderr
        assert "WARNING: gh#167 trailing-turn report loss detected" in p.stdout, p.stdout
        # AC2 precondition: the detector actually fired for this fixture -- a non-empty file,
        # mirroring how run_member.sh itself tests it (`[ -s "$TRAILING_LOSS_FILE" ]`).
        assert loss_file.read_text().strip(), "expected a non-empty trailing-loss-out file"

        raw = result_file.read_text()
        out = subprocess.run([sys.executable, str(HERE / "pass_accounting.py"), "text"],
                             input=raw, capture_output=True, text=True, timeout=30).stdout

        def run_report_status(*extra_args):
            p = subprocess.run(
                [sys.executable, str(HERE / "run_report.py"),
                 "--member", "dont-shoot-the-messenger", "--run-id", "r", "--kind", "llm",
                 "--exit-code", "0", "--pass-file", "-", *extra_args],
                input=out, capture_output=True, text=True, timeout=30)
            assert p.returncode == 0, p.stderr
            return json.loads(p.stdout)["status"]

        # AC3: pre-fix path (no --trailing-loss) -- unchanged, still reported_nothing.
        assert run_report_status() == "reported_nothing"
        # AC2: post-fix path -- the wrapper's signal flips this to a new, distinct status.
        post_fix_status = run_report_status("--trailing-loss")
        assert post_fix_status not in ("ok", "reported_nothing"), post_fix_status
        assert post_fix_status == "report_lost", post_fix_status


def _run_member_wires_trailing_loss_flag():
    """gh#257 AC2/AC3: the two pieces above are useless to a live pass unless run_member.sh
    actually chains them -- source-checked the same way `_run_member_writes_a_started_row_...`
    checks its own wiring, so a future edit that drops either flag (rather than the logic behind
    it) fails a test instead of silently going quiet in prod.

    gh#461 AC1: the old version of this check (`"--trailing-loss" in re.sub(...)`) was a
    repo-wide substring search -- it passed as long as the string `--trailing-loss` appeared
    ANYWHERE after stripping `--trailing-loss-out`, including in the
    `TRAILING_LOSS_FLAG="--trailing-loss"` assignment line itself. A future edit that dropped
    `$TRAILING_LOSS_FLAG` from the real run_report.py invocation line (run_member.sh's only
    normal-exit call, the one carrying --pass-file -/--usage-file) while leaving that
    assignment untouched still satisfied the old assert -- false confidence on exactly the
    regression this test's docstring claims to catch. Now it isolates that one real call
    (joining its backslash-continued lines into a single logical line first) and checks
    `$TRAILING_LOSS_FLAG` is actually referenced there.
    """
    src = (ROOT / "scripts" / "run_member.sh").read_text()
    assert "--trailing-loss-out" in src, "stream_log.py is never told where to write the loss signal"
    assert "TRAILING_LOSS_FILE" in src, "no side-channel file variable for the loss signal"
    assert '[ -s "$TRAILING_LOSS_FILE" ]' in src, \
        "run_member.sh never tests the loss file for non-emptiness before building the flag"

    logical_lines = re.sub(r"\\\n", " ", src).splitlines()
    invocation = next(
        (ln for ln in logical_lines
         if "run_report.py" in ln and "--pass-file -" in ln and "--usage-file" in ln),
        None)
    assert invocation is not None, \
        "no real run_report.py invocation line found (expected --pass-file -/--usage-file)"
    assert "$TRAILING_LOSS_FLAG" in invocation, \
        "run_member.sh's real run_report.py call never forwards $TRAILING_LOSS_FLAG: " + invocation


def _artifact_regex_accepts_backtick_spans():
    """gh#251: roomba/the-fixer's real evidence is a path, PID, or SHA -- none of which has a
    GitHub-artifact shape (`#123`, a URL, `file.ext:123`), so classify() folded genuinely
    evidenced passes into `reported_nothing`. This fleet's own convention for "this is the
    concrete thing" is a backtick span, so _ARTIFACT now accepts a non-empty one too.
    """
    import run_report

    def status(outcome, evidence="ran it"):
        rec = {"outcome": outcome, "evidence": evidence}
        return run_report.classify(rec, vision_required=False, exit_code=0)

    # AC1: backtick-wrapped path.
    assert status("Swept `/tmp/fleet-run-nerd-43503`") == "ok"
    # AC2: backtick-wrapped PID reference, in evidence rather than outcome.
    assert status("swept a stale worktree", "confirmed via `ps -p 43503`") == "ok"
    # AC3: backtick-wrapped git SHA.
    assert status("already-fighting `91eb91a9`") == "ok"
    # AC4: the pre-existing shapes are unchanged -- no regression.
    assert status("did a thing #12") == "ok"
    assert status("did a thing https://github.com/x/y/issues/12") == "ok"
    assert status("did a thing app.py:41") == "ok"
    # AC5: no artifact of any shape, no backtick span -- still reported_nothing. The widening
    # must not make the check vacuous.
    assert status("did stuff, all good") == "reported_nothing"


def _rsi_lines_survive_to_the_next_pass():
    """#83: the compounding chain needs a data plane, not a log grep.

    The job this proves is dumbledore's, not a function's: pass N writes `Prediction:`, and
    pass N+1 must be able to READ THAT LITERAL LINE BACK to say whether it came true. Before
    this, the three RSI lines were parsed nowhere and survived only in stream_log.py's
    truncated `thinking:` output -- intermediate reasoning, not the final answer -- so
    `Last-verdict:` had nothing to check and the score's own reasoning cited the broken chain.
    Exercised through runs.jsonl -> fleet.db, the same path run_member.sh uses, because a
    build_record() assertion alone would still pass with no column to land in.
    """
    import run_report, fleet_db, sqlite3
    text = ("FLEET-REPORT\nScore-now: 18 (down from 22)\n"
            "Prediction: capturing these lines closes the loop\n"
            "Last-verdict: my last prediction did not come true -- #83 still open\n"
            "Outcome: filed #83\nEvidence: scripts/run_report.py:60\n")
    rec = run_report.build_record(member="dumbledore", run_id="rsi-1", kind="llm", exit_code=0,
                                  pass_text=text, usage=None, vision_required=False)
    assert rec["prediction"] == "capturing these lines closes the loop", rec.get("prediction")
    assert rec["score_now"] == "18 (down from 22)", rec.get("score_now")
    assert rec["last_verdict"].startswith("my last prediction did not come true")
    # Absence stays NULL and never changes status -- same contract as self_critique.
    bare = run_report.build_record(member="t", run_id="rsi-2", kind="llm", exit_code=0,
                                   pass_text="Outcome: did a thing\nEvidence: app.py:1\n",
                                   usage=None, vision_required=False)
    assert bare["prediction"] is None and bare["status"] == "ok", bare["status"]

    with tempfile.TemporaryDirectory() as d:
        runs = Path(d) / "runs.jsonl"
        runs.write_text(json.dumps(rec) + "\n")
        conn = fleet_db.connect(Path(d) / "fleet.db")
        fleet_db.sync(conn, runs_file=runs)
        got = conn.execute("SELECT prediction, score_now, last_verdict FROM runs "
                           "WHERE member='dumbledore' ORDER BY recorded_at DESC LIMIT 1").fetchone()
        assert got and got[0] == "capturing these lines closes the loop", got
        assert got[1] == "18 (down from 22)" and got[2], got

        # An ALREADY-EXISTING fleet.db is the case that actually ships: CREATE TABLE IF NOT
        # EXISTS is a no-op there, so without the ADD COLUMN migration prod keeps the old
        # shape and every insert fails on column count while a fresh box looks fine.
        # The legacy table is today's SCHEMA minus the three new columns -- built by stripping
        # them out of the real thing, so this fixture can't drift into a shape prod never had.
        old = Path(d) / "old.db"
        legacy_schema = "\n".join(
            ln for ln in fleet_db.SCHEMA.splitlines()
            if not any(c in ln.split()[:1] for c in ("prediction", "score_now", "last_verdict"))
        )
        legacy = sqlite3.connect(str(old))
        legacy.executescript(legacy_schema)
        legacy.execute("INSERT INTO runs (run_id, member, outcome, recorded_at) "
                       "VALUES ('legacy-1','marie','a row written before the migration', 1.0)")
        legacy.commit(); legacy.close()

        conn2 = fleet_db.connect(old)
        cols = {r[1] for r in conn2.execute("PRAGMA table_info(runs)")}
        assert {"prediction", "score_now", "last_verdict"} <= cols, cols
        # The pre-existing row must survive untouched, reading NULL for what wasn't captured.
        row = conn2.execute("SELECT member, outcome, prediction FROM runs "
                            "WHERE run_id='legacy-1'").fetchone()
        assert row == ("marie", "a row written before the migration", None), row
        # ...and a NEW record must insert into the migrated table without a column-count error.
        newruns = Path(d) / "more.jsonl"
        newruns.write_text(json.dumps(rec) + "\n")
        fleet_db.sync(conn2, runs_file=newruns)
        assert conn2.execute("SELECT prediction FROM runs WHERE run_id='rsi-1'").fetchone()[0] \
            == "capturing these lines closes the loop"


def _fleet_db_run_id_collisions_dont_lose_a_verdict():
    """#212: judge-judy's run_id is `review-<pr>-<sha>`, not per-invocation, so two genuinely
    different concurrent reviews of the same PR head used to collide on fleet.db's bare
    run_id PRIMARY KEY -- INSERT OR REPLACE silently kept only one verdict. Live-quantified:
    286 runs.jsonl lines / 280 distinct run_ids vs. 280 rows in fleet.db, 3 of the 6 colliding
    pairs holding outright contradictory verdicts. AC1-3 of the PRD, exercised the same way
    #83's RSI test is: through runs.jsonl -> fleet.db, not a unit assertion on a helper alone.
    """
    import io
    import sqlite3
    from contextlib import redirect_stderr
    import fleet_db

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        # AC1: two distinct runs sharing judge-judy's collision-prone run_id, different
        # recorded_at, both persist as separate rows.
        runs = d / "runs.jsonl"
        blocked = {"run_id": "review-175-817e99187df8", "member": "judge-judy",
                   "outcome": "blocked PR #175", "_recorded_at": 100.0}
        approved = {"run_id": "review-175-817e99187df8", "member": "judge-judy",
                    "outcome": "approved PR #175", "_recorded_at": 200.0}
        runs.write_text(json.dumps(blocked) + "\n" + json.dumps(approved) + "\n")
        conn = fleet_db.connect(d / "fleet.db")
        n = fleet_db.sync(conn, runs_file=runs)
        assert n == 2, n
        rows = conn.execute(
            "SELECT outcome FROM runs WHERE run_id = ? ORDER BY recorded_at",
            ("review-175-817e99187df8",)).fetchall()
        assert rows == [("blocked PR #175",), ("approved PR #175",)], rows

        # AC2: re-syncing the same lines (e.g. after an offset reset) must not duplicate --
        # idempotency is keyed on (run_id, recorded_at), which a literal re-read reproduces
        # exactly, not on run_id alone.
        conn.execute("UPDATE sync_state SET offset = 0"); conn.commit()
        fleet_db.sync(conn, runs_file=runs)
        rows2 = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id = ?", ("review-175-817e99187df8",)
        ).fetchone()[0]
        assert rows2 == 2, f"re-sync duplicated rows: {rows2}"

        # AC3: a genuine collision -- same run_id AND same recorded_at, different content --
        # must be LOGGED, not silently discarded. This is the failure mode the PRD's Non-goal
        # section says is worse than a missing row: a confident, complete-looking wrong one.
        collide = d / "collide.jsonl"
        first = {"run_id": "dup-1", "member": "m", "outcome": "first", "_recorded_at": 5.0}
        second = {"run_id": "dup-1", "member": "m", "outcome": "second", "_recorded_at": 5.0}
        collide.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n")
        buf = io.StringIO()
        with redirect_stderr(buf):
            fleet_db.sync(conn, runs_file=collide)
        assert "COLLISION" in buf.getvalue() and "dup-1" in buf.getvalue(), buf.getvalue()

        # A live fleet.db predates this fix and still has run_id as a bare PRIMARY KEY --
        # CREATE TABLE IF NOT EXISTS is a no-op against it (same reason _ADD_COLUMNS exists),
        # so without an in-place migration the fix would reach only a freshly rebuilt db. The
        # legacy shape here is today's base SCHEMA (every column _ADD_COLUMNS doesn't own)
        # with the old bare run_id PRIMARY KEY, matching what a real pre-#212 fleet.db has.
        old = d / "legacy.db"
        legacy = sqlite3.connect(str(old))
        legacy.executescript("""
            CREATE TABLE runs (
              run_id TEXT PRIMARY KEY, member TEXT NOT NULL, kind TEXT, item_id TEXT, pr TEXT,
              status TEXT, exit_code INTEGER, outcome TEXT, evidence TEXT, vision_link TEXT,
              self_critique TEXT, cost_usd REAL, num_turns INTEGER, input_tokens INTEGER,
              output_tokens INTEGER, cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
              duration_ms INTEGER, stop_reason TEXT, recorded_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runs_member_time ON runs(member, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
            CREATE INDEX IF NOT EXISTS idx_runs_item ON runs(item_id);
        """)
        legacy.execute("INSERT INTO runs (run_id, member, outcome, recorded_at) "
                       "VALUES ('legacy-1','marie','a row written before the migration', 1.0)")
        legacy.commit(); legacy.close()

        conn2 = fleet_db.connect(old)
        pk_cols = [r[1] for r in conn2.execute("PRAGMA table_info(runs)") if r[5]]
        assert pk_cols == ["run_id", "recorded_at"], pk_cols
        row = conn2.execute("SELECT member, outcome FROM runs WHERE run_id = 'legacy-1'").fetchone()
        assert row == ("marie", "a row written before the migration"), row
        fleet_db.sync(conn2, runs_file=runs)
        migrated_rows = conn2.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id = ?", ("review-175-817e99187df8",)
        ).fetchone()[0]
        assert migrated_rows == 2, migrated_rows

        # `ALTER TABLE RENAME` carries indexes over onto the renamed table by table, not by
        # name, so `CREATE INDEX IF NOT EXISTS` in SCHEMA name-matches the ones still attached
        # to the dropped runs_legacy_pk and no-ops -- the migration used to silently leave the
        # rebuilt `runs` table with zero of its three indexes. member/time, status and item
        # lookups this file exists to make fast (its own module docstring) would fall back to
        # a full table scan with no error and no log line.
        idx_names = {r[1] for r in conn2.execute("PRAGMA index_list(runs)")}
        expected = {"idx_runs_member_time", "idx_runs_status", "idx_runs_item"}
        assert expected <= idx_names, idx_names


def _fleet_db_query_runs_item_id_matches_free_text_mentions():
    """gh#405 AC1/AC4: `query_runs(item_id=...)` used to exact-match the `item_id` column
    alone -- a column only ever written by a `--item N` build-claim pass, 3.9% of rows
    fleet-wide. A pass that only DISCUSSED an issue in free text (marie/nerd/dumbledore/...)
    was invisible to it, so "what has the fleet said about #143" came back a confident,
    wrong `[]`. AC1: exact match is now an OR with a `#N` mention in outcome/evidence/
    self_critique. AC4: a free-text-only row (no item_id set at all) is still returned.
    """
    import fleet_db

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        runs = d / "runs.jsonl"
        claimed = {"run_id": "minion-item143-1", "member": "minion", "item_id": "143",
                   "outcome": "built the fix", "_recorded_at": 300.0}
        # AC4: free-text-only mention, item_id column never set on this row.
        discussed = {"run_id": "marie-1", "member": "marie",
                     "outcome": "reconfirmed #143 still open", "_recorded_at": 200.0}
        evidence_only = {"run_id": "nerd-1", "member": "nerd", "outcome": "filed a finding",
                          "evidence": "live-checked against #143's own AC", "_recorded_at": 100.0}
        # A DIFFERENT issue whose number merely contains "143" as a substring must not
        # false-positive -- the PRD's own open question about #143 vs #1430 boundary-anchoring.
        decoy = {"run_id": "marie-2", "member": "marie", "outcome": "closed #1430",
                 "_recorded_at": 50.0}
        runs.write_text("\n".join(json.dumps(r) for r in
                                   (claimed, discussed, evidence_only, decoy)) + "\n")
        conn = fleet_db.connect(d / "fleet.db")
        fleet_db.sync(conn, runs_file=runs)

        rows = fleet_db.query_runs(conn, item_id="143")
        ids = {r["run_id"] for r in rows}
        assert ids == {"minion-item143-1", "marie-1", "nerd-1"}, ids

        # Old exact-match behavior stays a strict subset (AC4's "not replaced").
        exact_only = [r for r in rows if r["item_id"] == "143"]
        assert {r["run_id"] for r in exact_only} == {"minion-item143-1"}, exact_only

        # #1430's mention must not leak into a #143 lookup.
        assert "marie-2" not in ids, ids

        # limit is honored AFTER the free-text narrowing, not applied to the raw LIKE superset.
        limited = fleet_db.query_runs(conn, item_id="143", limit=2)
        assert len(limited) == 2, limited


def _fleet_db_query_runs_empty_item_id_is_treated_like_none():
    """gh#484: three truthiness checks against `item_id` in query_runs() used to disagree for
    `item_id=""` -- `if item_id:` (falsy) vs `if item_id is None:` (False, since "" is not
    None) -- so an empty string skipped the LIMIT clause entirely and returned the whole
    table. AC1: `item_id=""` must return the same rows as `item_id=None`. AC2: `limit` must
    still be honored for `item_id=""` against a table with more rows than the limit.
    """
    import fleet_db

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        runs = d / "runs.jsonl"
        rows_in = [{"run_id": f"r{i}", "member": "minion", "outcome": "did stuff",
                    "_recorded_at": float(i)} for i in range(5)]
        runs.write_text("\n".join(json.dumps(r) for r in rows_in) + "\n")
        conn = fleet_db.connect(d / "fleet.db")
        fleet_db.sync(conn, runs_file=runs)

        none_rows = fleet_db.query_runs(conn, item_id=None, limit=3)
        empty_rows = fleet_db.query_runs(conn, item_id="", limit=3)
        assert len(none_rows) == len(empty_rows) == 3, (none_rows, empty_rows)
        assert {r["run_id"] for r in none_rows} == {r["run_id"] for r in empty_rows}


def _fleet_db_composite_pk_migration_is_lock_serialized():
    """#212: fleet_view_server.py calls `fleet_db.connect()` from several independent
    threads -- the background tail thread and per-request handlers -- and
    `_migrate_composite_pk` is a rename/rebuild/drop of `runs`, not an idempotent ADD COLUMN.
    Without serializing it, two threads racing `connect()` against the same not-yet-migrated
    legacy db could both see the old schema and both try to rename the same table, raising a
    raw sqlite3.OperationalError and (for the background thread) silently killing the live
    run feed. Reproduces the race directly: N threads call connect() against one legacy db at
    once; none may raise, and the migration must still run exactly once."""
    import sqlite3
    import threading

    import fleet_db

    with tempfile.TemporaryDirectory() as d:
        old = Path(d) / "legacy.db"
        legacy = sqlite3.connect(str(old))
        legacy.executescript("""
            CREATE TABLE runs (
              run_id TEXT PRIMARY KEY, member TEXT NOT NULL, kind TEXT, item_id TEXT, pr TEXT,
              status TEXT, exit_code INTEGER, outcome TEXT, evidence TEXT, vision_link TEXT,
              self_critique TEXT, cost_usd REAL, num_turns INTEGER, input_tokens INTEGER,
              output_tokens INTEGER, cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
              duration_ms INTEGER, stop_reason TEXT, recorded_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runs_member_time ON runs(member, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
            CREATE INDEX IF NOT EXISTS idx_runs_item ON runs(item_id);
        """)
        legacy.execute("INSERT INTO runs (run_id, member, outcome, recorded_at) "
                       "VALUES ('legacy-1','marie','pre-migration row', 1.0)")
        legacy.commit(); legacy.close()

        errors = []
        lock = threading.Lock()

        def worker():
            try:
                # sqlite3 connections are thread-affine (check_same_thread defaults True) --
                # connect and close within the same worker thread; only pass/fail crosses back.
                c = fleet_db.connect(old)
                c.close()
            except Exception as e:  # noqa: BLE001 -- the race under test raises sqlite3 errors
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"concurrent connect() raised: {errors!r}"
        check_conn = fleet_db.connect(old)
        pk_cols = [r[1] for r in check_conn.execute("PRAGMA table_info(runs)") if r[5]]
        assert pk_cols == ["run_id", "recorded_at"], pk_cols
        row = check_conn.execute(
            "SELECT member, outcome FROM runs WHERE run_id = 'legacy-1'").fetchone()
        assert row == ("marie", "pre-migration row"), row
        idx_names = {r[1] for r in check_conn.execute("PRAGMA index_list(runs)")}
        expected = {"idx_runs_member_time", "idx_runs_status", "idx_runs_item"}
        assert expected <= idx_names, idx_names


def _fanout_packs_the_hour_by_complexity():
    """gru fills an hour's allowance with WORK; N is an output of that, never an input.

    Reif, 2026-08-26: "its more about choosing jobs to run (that max out the availability)
    than it is about choosing minions to spawn." The failure this guards is a regression to
    counting minions as if every item were the same size -- two complexity-3s may fit an hour
    that one complexity-9 would blow.

    Everything here is PERCENT OF WEEK. The fleet runs on a subscription, so dollars are not
    the constraint; maxx already applies both buffers (weekly_max, per_diem_use) before a
    caller sees per_diem_hourly_pct.
    """
    import fanout
    # The ladder is exponential and anchored at 5 -- must match marie.md's Part C2 table.
    assert fanout.complexity_multiplier(5) == 1.0
    assert fanout.complexity_multiplier(1) < fanout.complexity_multiplier(5) < fanout.complexity_multiplier(10)
    span = fanout.complexity_multiplier(10) / fanout.complexity_multiplier(1)
    assert 10 < span < 25, f"ladder span {span:.1f}x is not the calibrated ~15x"
    # An unlabelled item is median, never free -- otherwise it packs infinitely many.
    assert fanout.complexity_multiplier(None) == fanout.complexity_multiplier(5)

    items = [{"number": 1, "complexity": 9}, {"number": 2, "complexity": 1},
             {"number": 3, "complexity": 2}]
    # Priority order is marie's and is never reordered by size: a too-big item is SKIPPED and
    # the cheaper items behind it still land.
    r = fanout.pack(items, allowance_pct=0.10, unit_pct=0.05)
    assert [c["number"] for c in r["chosen"]] == [2, 3], r["chosen"]
    assert [c["number"] for c in r["skipped"]] == [1], r["skipped"]
    assert not r["over_allowance"] and r["binding"] == "allowance"
    assert r["est_spend_pct"] <= r["allowance_pct"]

    # Size drives packing: a bigger allowance takes the expensive item too.
    assert fanout.pack(items, 1.0, 0.05)["n"] == 3

    # Nothing claimable is not an error, and never spawns.
    assert fanout.pack([], 0.4, 0.05)["binding"] == "nothing_claimable"

    # A floor may exceed the allowance, but must SAY so -- silent overspend is the one
    # outcome this must never produce.
    tight = fanout.pack(items, 0.001, 0.05, min_items=1)
    assert tight["n"] == 1 and tight["over_allowance"] and tight["forced_over_floor"] == 1

    # The unit cost is DERIVED from real passes, normalised by each pass's own complexity so
    # a week of easy items doesn't make everything look cheap.
    unit = fanout.calibrate([{"pct": 0.05, "complexity": 5},
                             {"pct": 0.05 * fanout.complexity_multiplier(8), "complexity": 8}])
    assert abs(unit - 0.05) < 1e-6, unit
    # Refuses to invent one when there is nothing usable.
    assert fanout.calibrate([]) is None
    assert fanout.calibrate([{"pct": 0, "complexity": 5}]) is None
    try:
        fanout.pack(items, 0.4, 0)
    except ValueError:
        pass
    else:
        raise AssertionError("unit_pct=0 silently accepted")


def _cost_bridge_converts_real_spend_into_fanouts_observed_shape():
    """gh#4020 / fleet-kit#260: fanout.py's own docstring promises `unit_pct` is derived from
    what passes ACTUALLY spent, but nothing ever built that derivation -- every gru pass since
    2026-08-30 called it with a hand-typed `--unit-pct 0.05` guess. cost_bridge.to_observed()
    is the missing function: it distributes one already-known allowance_pct across real
    cost_usd, proportional to each run's share of the group's total spend, into exactly the
    shape fanout.py --observed expects.
    """
    import time
    import cost_bridge
    import fanout

    runs = [{"item_id": "10", "cost_usd": 2.0}, {"item_id": "20", "cost_usd": 1.0}]
    observed = cost_bridge.to_observed(runs, allowance_pct=0.03,
                                       complexity_by_item={"10": 8, "20": 3})
    assert len(observed) == 2, observed
    by_item = {o["complexity"]: o["pct"] for o in observed}
    # $2 : $1 spend must split the 0.03 allowance 2:1, not evenly and not by complexity.
    assert abs(by_item[8] - 0.02) < 1e-9, observed
    assert abs(by_item[3] - 0.01) < 1e-9, observed
    assert abs(sum(o["pct"] for o in observed) - 0.03) < 1e-9, "must exhaust the allowance"

    # An item missing from complexity_by_item is median, never free -- same convention as an
    # unlabelled item everywhere else in this file.
    unlabelled = cost_bridge.to_observed([{"item_id": "30", "cost_usd": 1.0}], allowance_pct=0.01)
    assert unlabelled[0]["complexity"] == fanout.DEFAULT_COMPLEXITY, unlabelled

    # Refuses to invent a split when there's nothing usable -- same discipline as
    # fanout.calibrate(), never a fabricated number.
    assert cost_bridge.to_observed([], 0.03) == []
    assert cost_bridge.to_observed(runs, 0.0) == []
    assert cost_bridge.to_observed([{"item_id": "1", "cost_usd": 0}], 0.03) == []
    assert cost_bridge.to_observed([{"item_id": "1", "cost_usd": None}], 0.03) == []

    # The output feeds fanout.calibrate() directly -- that's the whole point of matching its
    # --observed shape exactly.
    unit = fanout.calibrate(observed)
    assert unit is not None and unit > 0, unit

    # recent_minion_costs() is the thin DB seam -- exercised through fleet.db like the other
    # fleet_db-backed checks in this file, not mocked.
    import fleet_db
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        runs_file = d / "runs.jsonl"
        recs = [
            {"run_id": "r1", "member": "minion", "item_id": "10", "_recorded_at": time.time(),
             "tokens": {"cost_usd": 2.0}},
            {"run_id": "r2", "member": "minion", "item_id": "20", "_recorded_at": time.time(),
             "tokens": {"cost_usd": 1.0}},
            # a different member's spend must not leak into minion's calibration.
            {"run_id": "r3", "member": "gru", "item_id": "99", "_recorded_at": time.time(),
             "tokens": {"cost_usd": 99.0}},
            # stale (outside the lookback window) must not count either.
            {"run_id": "r4", "member": "minion", "item_id": "40", "_recorded_at": time.time() - 999999,
             "tokens": {"cost_usd": 5.0}},
        ]
        runs_file.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
        conn = fleet_db.connect(d / "fleet.db")
        fleet_db.sync(conn, runs_file=runs_file)
        real = cost_bridge.recent_minion_costs(conn, member="minion", hours=2.0)
        assert {r["item_id"] for r in real} == {"10", "20"}, real


def _claim_history_blocks_an_item_that_keeps_dead_ending():
    """gh#64: gru.md step 2 filtered `fleet:claimed` and (later) `fleet:needs-human-op`, but
    nothing distinguished "never tried" from "tried and dead-ended N times" -- the same
    chronically-blocked item got reclaimed and respawned every hour, burning a full
    claim/spawn/clear cycle each time on doomed work (nonprofit-atlas#3104/#3088).

    AC4's own fixture: an item with N prior dead-end claims (still open, so none of those
    claims resulted in a merge -- see claim_history.py's module docstring for why that's
    inferable without a separate `gh pr view` per candidate) must be excluded once N reaches
    the threshold, and must NOT be excluded before it.
    """
    import time
    import claim_history

    # Pure core: run_id shape matching, independent of any DB.
    run_ids = ["minion-item64-111-1", "minion-item64-222-2", "minion-item99-333-3"]
    assert claim_history.dead_end_claim_count(run_ids, 64) == 2
    assert claim_history.dead_end_claim_count(run_ids, 99) == 1
    assert claim_history.dead_end_claim_count(run_ids, 12345) == 0
    assert not claim_history.is_dead_end_blocked(run_ids, 64, threshold=3)
    assert claim_history.is_dead_end_blocked(
        run_ids + ["minion-item64-444-4"], 64, threshold=3)

    # Real integration, through fleet.db like the other fleet_db-backed checks in this file --
    # a run's own reported `status` must NOT matter (a prior "ok" minion run against an item
    # that is STILL in gru's open-candidate list is still a dead end; only the issue closing
    # would prove otherwise, and a closed issue would never reach this check at all).
    import fleet_db
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        runs_file = d / "runs.jsonl"
        now = time.time()
        recs = [
            {"run_id": "minion-item64-100-1", "member": "minion", "item_id": "64",
             "status": "ok", "_recorded_at": now - 3 * 86400},
            {"run_id": "minion-item64-100-2", "member": "minion", "item_id": "64",
             "status": "reported_nothing", "_recorded_at": now - 2 * 86400},
            # a different item's claim must not count against #64.
            {"run_id": "minion-item99-100-3", "member": "minion", "item_id": "99",
             "status": "ok", "_recorded_at": now - 1 * 86400},
            # outside the 14-day window: must not count.
            {"run_id": "minion-item64-100-4", "member": "minion", "item_id": "64",
             "status": "ok", "_recorded_at": now - 20 * 86400},
        ]
        runs_file.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
        conn = fleet_db.connect(d / "fleet.db")
        fleet_db.sync(conn, runs_file=runs_file)

        run_ids_64 = claim_history.minion_runs_for_item(conn, 64, window_days=14.0)
        assert len(run_ids_64) == 2, run_ids_64  # the 20-day-old one is excluded
        assert not claim_history.is_dead_end_blocked(run_ids_64, 64, threshold=3)

        # A third dead end inside the window tips it over the default threshold.
        runs_file.write_text(runs_file.read_text() + json.dumps(
            {"run_id": "minion-item64-100-5", "member": "minion", "item_id": "64",
             "status": "reported_nothing", "_recorded_at": now}) + "\n")
        fleet_db.sync(conn, runs_file=runs_file)
        run_ids_64 = claim_history.minion_runs_for_item(conn, 64, window_days=14.0)
        assert len(run_ids_64) == 3, run_ids_64
        assert claim_history.is_dead_end_blocked(
            run_ids_64, 64, threshold=claim_history.DEFAULT_DEAD_END_THRESHOLD)

        # The CLI surfaces the same verdict via exit code -- what gru.md's step 2c actually runs.
        import subprocess
        out = subprocess.run(
            [sys.executable, str(HERE / "claim_history.py"), "--item", "64",
             "--db-path", str(d / "fleet.db")],
            capture_output=True, text=True)
        assert out.returncode == 1, (out.returncode, out.stdout, out.stderr)
        assert "BLOCKED" in out.stdout, out.stdout

        out_clean = subprocess.run(
            [sys.executable, str(HERE / "claim_history.py"), "--item", "99",
             "--db-path", str(d / "fleet.db")],
            capture_output=True, text=True)
        assert out_clean.returncode == 0, (out_clean.returncode, out_clean.stdout, out_clean.stderr)
        assert "ok" in out_clean.stdout, out_clean.stdout


def _gru_md_checks_claim_history_before_claiming():
    """Doc-consistency guard, same shape as `_gru_md_clamps_allowance_to_share_ceiling`: proves
    the gh#64 dead-end check is actually wired into gru.md's step order (between step 2's
    candidate read and step 4's claim), not just implemented and never called."""
    text = (HERE.parent / "members" / "gru" / "gru.md").read_text()
    assert "claim_history.py" in text, \
        "gru.md never calls claim_history.py -- gh#64's dead-end check is unreachable"
    step2c = text.index("2c.")
    step4 = text.index("4. **Claim your chosen items")
    assert step2c < step4, "step 2c must run before step 4's claim, not after"


def _maxx_reader_reports_the_fleets_hourly_slice_not_a_laptops_pacing():
    """The meter gru spends against is the FLEET's per-diem hour, never a session's pacing.

    Real outage, 2026-08-26: the build half of the fleet sat idle for hours reporting
    `headroom_fraction=0.0, label=ok` -- a real signal, read live, and completely wrong.
    0.0 came from `1 - session_used_pct/session_advised_pct` where those two fields described
    an interactive LAPTOP session (22% used against a 7.4% advised share) that had nothing to
    do with the fleet. The fleet's own allowance at that exact moment was healthy:
    per_diem_hourly_pct=0.356, reserved_pct=0, week_bank_pct=1.9, verdict=ok.

    A human's hot session must never read as a fleet-wide wall. This is the same
    silence-reads-as-health failure the deploy pipeline kept producing: gru dutifully reported
    QUIET every pass, so the outage looked like an honest empty backlog.

    Second half: gru.md and fanout.py both document spending against `per_diem_hourly_pct`
    minus `reserved_pct`. The reader never emitted either field, so gru had no source for the
    only number its charter tells it to use.
    """
    import maxx_reader

    live = {
        "verdict": "ok",
        "session_advised_pct": 7.4, "session_used_pct": 22,   # a laptop mid-burn
        "per_diem_hourly_pct": 0.356, "reserved_pct": 0,      # the fleet's real slice
        "week_bank_pct": 1.9,
    }
    fraction, label, budget = maxx_reader.get_headroom("https://example.invalid", "h", "k",
                                                    fetcher=lambda *a, **k: live)
    assert label == "ok", label
    # The fields gru's charter actually spends against must reach it.
    assert budget["per_diem_hourly_pct"] == 0.356, budget
    assert budget["reserved_pct"] == 0, budget
    # A laptop over its advised share must NOT zero the fleet.
    assert fraction > 0, f"a laptop's overburn zeroed the fleet: {fraction}"

    # A genuinely empty week bank still reads as no headroom -- fail open must not mean
    # fail blind.
    dry, _, _ = maxx_reader.get_headroom(
        "https://example.invalid", "h", "k", fetcher=lambda *a, **k: {**live, "week_bank_pct": 0})
    assert dry == 0.0, dry

    # Unreadable meter -> None, never a budget-shaped stand-in for a failure.
    for bad in ("maxx_unreachable", "maxx_auth_rejected"):
        f, lbl, b = maxx_reader.get_headroom(
            "https://example.invalid", "h", "k", fetcher=lambda *a, **k: bad)
        assert f is None and lbl == bad and b == {}, (f, lbl, b)
    # A REAL over-budget verdict is a spendable 0.0, never None -- None means "no trustworthy
    # reading" and would send the caller back to a stale fallback instead of the fresh "spend
    # ~0" signal maxx just gave it (contract fixed by PR #131).
    f, lbl, b = maxx_reader.get_headroom(
        "https://example.invalid", "h", "k", fetcher=lambda *a, **k: {**live, "verdict": "over"})
    assert f == 0.0 and lbl == "over", (f, lbl)
    assert b["verdict"] == "over", b

    # A genuinely unrecognized verdict is still unreadable -> None, not a budget-shaped stand-in.
    f, lbl, b = maxx_reader.get_headroom(
        "https://example.invalid", "h", "k", fetcher=lambda *a, **k: {**live, "verdict": "sideways"})
    assert f is None and lbl == "maxx_verdict_sideways" and b, (f, lbl, b)

    # The CLI prints the allowance fields, so a human (and gru) can see the real slice.
    import json as _json, subprocess
    out = subprocess.run([sys.executable, str(HERE / "maxx_reader.py"), "--selftest"],
                         capture_output=True, text=True)
    payload = _json.loads(out.stdout)
    assert "per_diem_hourly_pct" in payload and "headroom_fraction" in payload, payload


def _gru_md_clamps_allowance_to_share_ceiling():
    """2026-09-01 audit (Reif): FLEET_SHARE_FRACTION (this instance's slice of fleet-wide
    hourly headroom, run_member.sh -> maxx_share_ceiling.py -> FLEET_SHARE_CEILING_PCT) and
    FLEET_GRU_ALLOWANCE_FRACTION (gru's own slice of ITS instance's allowance, gru.md step 1)
    are two INDEPENDENT fractions -- gru.md computed allowance_pct straight off raw hourly
    headroom and never looked at FLEET_SHARE_CEILING_PCT, so on a multi-instance box (e.g.
    philanthropy + fleet-kit-server-fleet sharing one maxx account pool) gru could reserve
    more than its own instance's ceiling, double-spending headroom another instance already
    counted on. This proves the clamp text is actually in gru.md, not just fixed once and
    silently droppable on a future edit -- a doc-consistency check, same shape as the other
    gru.md-derived checks in this file (grep for `per_diem_hourly_pct` above)."""
    text = (HERE.parent / "members" / "gru" / "gru.md").read_text()
    assert "FLEET_SHARE_CEILING_PCT" in text, \
        "gru.md lost its FLEET_SHARE_CEILING_PCT reference -- gru can double-spend headroom " \
        "another fleet-kit instance already reserved"
    # The clamp became a MULTIPLY (2026-09-02): allowance = CEILING * GRU_ALLOWANCE_FRACTION.
    # That is strictly stronger than the old min() -- the result is always <= the ceiling for
    # any fraction in [0,1], AND it stops gru taking 100% of the instance's slice, which min()
    # allowed (and in practice always produced, making the dial dead config). What this test
    # guards is the INTENT -- gru's number is derived FROM the instance ceiling, never from raw
    # local headroom -- so it accepts either composition rather than pinning one spelling.
    assert ("min(allowance_pct, FLEET_SHARE_CEILING_PCT)" in text
            or "FLEET_SHARE_CEILING_PCT * FLEET_GRU_ALLOWANCE_FRACTION" in text), \
        "gru.md no longer derives allowance_pct from FLEET_SHARE_CEILING_PCT -- gru can " \
        "double-spend headroom another fleet-kit instance already reserved"
    # The fanout.py call site must hand it the ALREADY-clamped value, not re-derive the raw
    # unclamped formula a second time (that would silently bypass the clamp above it).
    assert "${FLEET_GRU_ALLOWANCE_FRACTION:-0.70}>" not in text, \
        "fanout.py --allowance-pct call site still inlines the raw unclamped formula"


def _maxx_lease_reserves_releases_and_self_expires():
    """gh#161 part 2: reserve/release were documented in gru.md since forever but never
    implemented -- reserved_pct stayed permanently 0 no matter how many leases should
    logically be live. This proves the local ledger actually holds, drops, and self-expires
    a lease, and that maxx_reader.py's CLI surfaces the total on top of the remote reading."""
    import maxx_lease

    with tempfile.TemporaryDirectory() as d:
        state_file = Path(d) / "maxx-leases.json"

        assert maxx_lease.total_reserved_pct(state_file) == 0.0

        lease_id = maxx_lease.maxx_reserve(pct=0.05, label="gru-test", ttl_sec=3600,
                                           state_file=state_file)
        assert lease_id
        assert abs(maxx_lease.total_reserved_pct(state_file) - 0.05) < 1e-9

        # A second, concurrent lease adds on top -- this is the whole point (back-to-back gru
        # passes must see each other's in-flight spend).
        lease_id2 = maxx_lease.maxx_reserve(pct=0.03, label="gru-test2", ttl_sec=3600,
                                            state_file=state_file)
        assert abs(maxx_lease.total_reserved_pct(state_file) - 0.08) < 1e-9

        maxx_lease.maxx_release(lease_id, state_file=state_file)
        assert abs(maxx_lease.total_reserved_pct(state_file) - 0.03) < 1e-9
        # Releasing an already-released (or never-existent) lease is a no-op, never an error --
        # gru.md step 6 calls this unconditionally, even on a failure path.
        maxx_lease.maxx_release(lease_id, state_file=state_file)

        # A lease past its own TTL self-expires WITHOUT an explicit release -- gru.md's
        # documented backstop ("a lease that outlives its own hour self-expires instead of
        # choking every later pass forever").
        maxx_lease.maxx_release(lease_id2, state_file=state_file)
        expired_id = maxx_lease.maxx_reserve(pct=0.5, label="gru-expired", ttl_sec=-1,
                                             state_file=state_file)
        assert expired_id
        assert maxx_lease.total_reserved_pct(state_file) == 0.0


def _maxx_lease_concurrent_reserves_dont_clobber_each_other():
    """fleet-code-review BLOCK on PR #163: unlocked read-modify-write meant two overlapping
    gru passes calling maxx_reserve at once could silently clobber each other's write (a lost
    lease, no error), and the shared non-unique .tmp path could raise a bare FileNotFoundError
    out of a concurrent caller. Fired real threads at the same state file to prove the fix
    (an flock-guarded critical section) actually serializes them -- every lease survives and
    nothing raises."""
    import threading

    import maxx_lease

    with tempfile.TemporaryDirectory() as d:
        state_file = Path(d) / "maxx-leases.json"
        n = 20
        errors = []

        def _reserve(i):
            try:
                maxx_lease.maxx_reserve(pct=0.01, label=f"concurrent-{i}", ttl_sec=3600,
                                        state_file=state_file)
            except Exception as exc:  # noqa: BLE001 -- capturing for the assert below
                errors.append(exc)

        threads = [threading.Thread(target=_reserve, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent maxx_reserve raised: {errors}"
        leases = json.loads(state_file.read_text())
        assert len(leases) == n, f"expected {n} surviving leases, got {len(leases)} -- lost a write"
        assert abs(maxx_lease.total_reserved_pct(state_file) - n * 0.01) < 1e-9


def _maxx_share_ceiling_uses_hourly_headroom_not_the_week_bank():
    """Replaces the old maxx_share_check.py, which multiplied a member's budget by
    `week_bank_pct` -- a LAGGING, already-spent number. The moment the week goes over pace
    (bank negative), that clamps to 0.0 and zeros every member's spend even during an hour
    with real headroom. This proves the ceiling is computed from `sustainable_pct_per_hour`
    minus `per_diem_hourly_pct` minus `reserved_pct` instead -- a leading, real-time number
    that stays positive on a healthy hour even while the week bank is deep negative.
    """
    import maxx_share_ceiling

    # A week deep over pace (bank very negative) but a healthy CURRENT hour: sustainable
    # pace is 0.35%/hr, only 0.10%/hr actually spent so far this hour, nothing reserved.
    healthy_hour_bad_week = {
        "verdict": "ok",
        "week_bank_pct": -34.4,             # would clamp headroom_fraction to 0.0 under the
                                             # old formula -- must NOT zero this ceiling.
        "sustainable_pct_per_hour": 0.35,
        "per_diem_hourly_pct": 0.10,
        "reserved_pct": 0,
    }
    orig = maxx_share_ceiling.get_headroom
    try:
        maxx_share_ceiling.get_headroom = lambda: (1.0, "ok", healthy_hour_bad_week)
        assert maxx_share_ceiling.main(["prog", "0.40"]) == 0

        # Same computation, share=1.0, to isolate the raw hourly-headroom formula from the
        # fraction multiply: (0.35 - 0.10 - 0) = 0.25.
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            maxx_share_ceiling.main(["prog", "1.0"])
        assert abs(float(buf.getvalue().strip()) - 0.25) < 1e-6, buf.getvalue()

        # Other members' live reservations subtract too -- a busy fleet has less ceiling
        # left for the next member to self-reserve against.
        maxx_share_ceiling.get_headroom = lambda: (
            1.0, "ok", {**healthy_hour_bad_week, "reserved_pct": 0.20})
        buf = io.StringIO()
        with redirect_stdout(buf):
            maxx_share_ceiling.main(["prog", "1.0"])
        assert abs(float(buf.getvalue().strip()) - 0.05) < 1e-6, buf.getvalue()  # 0.35-0.10-0.20

        # An hour already at or past sustainable pace (once reservations are subtracted) is
        # an honest, printed zero -- not suppressed, not negative.
        maxx_share_ceiling.get_headroom = lambda: (
            1.0, "ok", {**healthy_hour_bad_week, "per_diem_hourly_pct": 0.90})
        buf = io.StringIO()
        with redirect_stdout(buf):
            maxx_share_ceiling.main(["prog", "1.0"])
        assert float(buf.getvalue().strip()) == 0.0, buf.getvalue()

        # Unreadable meter -> prints nothing (fails open: caller falls back to its own
        # pre-existing cap, never reads an absent ceiling as "reserve 0").
        maxx_share_ceiling.get_headroom = lambda: (None, "maxx_unreachable", {})
        buf = io.StringIO()
        with redirect_stdout(buf):
            maxx_share_ceiling.main(["prog", "0.40"])
        assert buf.getvalue().strip() == "", buf.getvalue()

        # FLEET_SHARE_FRACTION > 1.0 (operator typo) must never raise the ceiling above the
        # fleet's own real hourly headroom -- clamped to 1.0 same as the old script.
        maxx_share_ceiling.get_headroom = lambda: (1.0, "ok", healthy_hour_bad_week)
        buf = io.StringIO()
        with redirect_stdout(buf):
            maxx_share_ceiling.main(["prog", "1.0"])
        uncapped = float(buf.getvalue().strip())
        buf = io.StringIO()
        with redirect_stdout(buf):
            maxx_share_ceiling.main(["prog", "1.5"])
        assert float(buf.getvalue().strip()) == uncapped, (uncapped, buf.getvalue())
    finally:
        maxx_share_ceiling.get_headroom = orig


def _maxx_share_ceiling_subtracts_local_leases_not_just_the_remotes_reserved_pct():
    """fleet-code-review BLOCK on PR #184: the ceiling formula read `budget["reserved_pct"]`
    from `get_headroom()` (the plain function), but that field only ever carries whatever the
    REMOTE maxx endpoint reports -- which today is nothing, because the remote never learns
    about a LOCAL maxx_lease.py reservation (gh#161 part 2). The merge of local leases into
    reserved_pct only happened inside maxx_reader.py's own CLI `main()`, which
    maxx_share_ceiling.py never goes through. Net effect: two concurrent callers (this
    instance's judge-judy running twice, or the OTHER instance) each saw the SAME generous
    ceiling and each reserved against it, seeing none of each other's live leases --
    reproducing, in a new form, the exact "no coordination" problem this PR set out to fix.

    This test exercises the REAL integration (an actual on-disk maxx_lease reservation, not a
    mocked reserved_pct in the dict) so it cannot pass the way the original, weaker version of
    this test did -- that one monkeypatched get_headroom with a dict that ALREADY contained
    reserved_pct, which is exactly the value the real code path never produces on its own.
    """
    import tempfile
    from pathlib import Path

    import maxx_lease
    import maxx_share_ceiling

    with tempfile.TemporaryDirectory() as d:
        state_file = Path(d) / "maxx-leases.json"
        orig_state = maxx_lease.STATE_FILE
        orig_headroom = maxx_share_ceiling.get_headroom
        try:
            maxx_lease.STATE_FILE = state_file

            # The remote's own reserved_pct is 0 (its honest, real-world default -- it has no
            # idea a local lease exists). sustainable=0.35, used=0.10 -> raw headroom 0.25.
            remote_budget = {
                "verdict": "ok", "sustainable_pct_per_hour": 0.35,
                "per_diem_hourly_pct": 0.10, "reserved_pct": 0,
            }
            maxx_share_ceiling.get_headroom = lambda: (1.0, "ok", remote_budget)

            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                maxx_share_ceiling.main(["prog", "1.0"])
            assert abs(float(buf.getvalue().strip()) - 0.25) < 1e-6, buf.getvalue()

            # A REAL concurrent lease exists on disk (e.g. judge-judy on the other instance,
            # or an earlier call this same instance made) -- the remote still reports
            # reserved_pct=0 (it never learns about this), but the ceiling MUST see it anyway.
            maxx_lease.maxx_reserve(pct=0.08, label="concurrent-caller", ttl_sec=3600)
            buf = io.StringIO()
            with redirect_stdout(buf):
                maxx_share_ceiling.main(["prog", "1.0"])
            assert abs(float(buf.getvalue().strip()) - 0.17) < 1e-6, buf.getvalue()  # 0.25-0.08
        finally:
            maxx_lease.STATE_FILE = orig_state
            maxx_share_ceiling.get_headroom = orig_headroom


def _maxx_share_ceiling_respects_a_real_over_verdict_not_just_unreadable_meters():
    """fleet-code-review BLOCK on PR #184: `verdict=="over"` is maxx's own DEFINITIVE "stop"
    signal -- get_headroom() returns fraction=0.0 (never None) for it specifically, per
    maxx_reader.py's own header, so a real stop can't be confused with an unreadable meter.
    The ceiling script only checked `fraction is None` and then discarded `fraction`
    entirely, recomputing purely from the hourly fields -- which are populated independently
    of verdict and can look like real headroom even while verdict=="over". That let a real
    hard-stop reading still yield a positive, spendable ceiling.

    Failing scenario this reproduces: maxx returns verdict="over" (session/week over) but
    with healthy-looking hourly numbers (sustainable=0.35, used=0.10) -- plausible in
    practice, since those are independent signals.
    """
    import maxx_share_ceiling

    over_but_hourly_looks_fine = {
        "verdict": "over",
        "sustainable_pct_per_hour": 0.35,
        "per_diem_hourly_pct": 0.10,
        "reserved_pct": 0,
    }
    orig = maxx_share_ceiling.get_headroom
    try:
        # get_headroom() itself returns (0.0, "over", ...) for this verdict -- match that
        # real contract exactly (maxx_reader.py:147-151), not an arbitrary fraction.
        maxx_share_ceiling.get_headroom = lambda: (0.0, "over", over_but_hourly_looks_fine)

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = maxx_share_ceiling.main(["prog", "1.0"])
        assert rc == 0
        assert float(buf.getvalue().strip()) == 0.0, (
            f"verdict=='over' must yield a zero ceiling regardless of hourly fields, got: {buf.getvalue()!r}"
        )
    finally:
        maxx_share_ceiling.get_headroom = orig


def _auto_merge_never_passes_a_strategy_flag_under_a_merge_queue():
    """Arming auto-merge must not pass --squash/--merge/--rebase, and must not eat the error.

    `main` on nonprofit-atlas is merge-queue-controlled (a `merge_queue` ruleset, SQUASH,
    grouping ALLGREEN). Passing an explicit strategy to `gh pr merge` on a queue-controlled
    branch is an invalid combination: gh ERRORS instead of enqueueing --

        ! The merge strategy for main is set by the merge queue

    Confirmed live twice: issue #3108, and again 2026-08-26 on nonprofit-atlas#3307, which sat
    MERGEABLE with statusCheckRollup=SUCCESS and autoMergeRequest=null for hours. minion.md
    step 9 already documents the bare form and says CHECK THE EXIT CODE; these two callers
    shipped the broken one anyway.

    The second half is why nobody noticed: worktree_builder.sh redirected the failure to
    /dev/null and logged "auto-merge armed" on the very next line, so the log asserted success
    for a command that had just failed. Silence reads as health.
    """
    import re as _re
    # Only a flag attached to the command itself -- prose explaining WHY --squash is wrong
    # ("an explicit --squash errors") must not trip this. Stop at the closing backtick/quote
    # so an explanation trailing the command is not read as part of it.
    bad = _re.compile(r"gh pr merge(?:\s+(?:--auto|\"?\$?\{?[A-Za-z_]+\}?\"?|\d+))*"
                      r"\s+--(squash|merge|rebase)\b")
    for rel in ("scripts/worktree_builder.sh", "scripts/auto_update_branch.sh",
                "members/minion/minion.fleet.json",
                "members/minion/minion.md", "members/jefe/jefe.md", "agents/builder.md",
                "README.md"):
        f = ROOT / rel
        if not f.exists():
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if "--auto" in line and bad.search(line):
                raise AssertionError(
                    f"{rel}:{i} arms auto-merge with a strategy flag -- errors under the "
                    f"merge queue instead of enqueueing: {line.strip()[:90]}")

    # The builder must not claim it armed auto-merge without checking the exit code.
    src = (ROOT / "scripts/worktree_builder.sh").read_text()
    arm = [l for l in src.splitlines() if "gh pr merge" in l and "--auto" in l]
    assert arm, "worktree_builder.sh no longer arms auto-merge at all"
    assert not any(_re.search(r">/dev/null 2>&1\s*$", l) for l in arm), \
        "the arming call still discards its error; a failed arm would log as armed"


def _minion_knows_the_browser_exists():
    """A capability the image ships must appear in the charter of whoever needs it.

    fleet-kit#115, filed BY the fleet against itself: #107 put playwright + headless chromium
    in the image, but members/minion/minion.md never mentioned it. Fifteen minion
    self-critiques in 24h named "no browser tooling in this sandbox" as the reason an item's
    OWN acceptance criteria went unmet -- two of them AFTER the browser had shipped. Real PRs
    (e.g. nonprofit-atlas#3231) argued CSS-token consistency where the PRD asked for a
    screenshot.

    A tool nobody is told about is indistinguishable from a tool that was never built.
    """
    md = (ROOT / "members/minion/minion.md").read_text()
    assert "playwright" in md.lower(), \
        "minion.md never mentions the browser #107 shipped -- see fleet-kit#115"
    # The incantation must be runnable, not merely alluded to: headless chromium needs
    # --no-sandbox as root in a container, and nerd.md's proven snippet carries it.
    assert "--no-sandbox" in md, "minion.md's browser snippet omits --no-sandbox; it runs as root"
    assert "sync_playwright" in md, "minion.md gestures at a browser without the working call"


def _jefe_can_unstick_a_pr_that_is_merely_behind():
    """Green + armed + BLOCKED is a THIRD stuck shape, and the only fix is updating the base.

    A merge queue re-tests every entry against the CURRENT base, so a PR whose checks passed
    against a stale base cannot enter the queue however green it looks. Nothing in this fleet
    updated a stale branch, so such a PR stranded itself while every surface called it healthy.

    Confirmed live 2026-08-26: nonprofit-atlas#3307, all checks success, autoMergeRequest
    armed, mergeStateStatus BLOCKED, behind_by=17. jefe's charter covered "armed but stuck"
    and "never armed at all" -- neither of which describes it, and neither remedy (a direct
    `gh pr merge`) is even safe here, since the checks have not run against the base it would
    land on.
    """
    md = (ROOT / "members/jefe/jefe.md").read_text()
    assert "update-branch" in md, \
        "jefe cannot unstick a PR that is merely behind its base -- no update-branch remedy"
    assert "behind_by" in md, "jefe has no way to DIAGNOSE a behind-the-base PR"
    # The remedy must not be "merge it by hand": this shape's checks have not run against the
    # base it would land on, so a direct merge lands untested code.
    idx = md.index("update-branch")
    window = md[idx:idx + 400]
    assert "do NOT merge directly" in window or "not merge directly" in window, \
        "jefe's stale-base remedy must forbid a direct merge -- checks have not run on that base"

    # BLOCKED can also mean the required checks never ATTACHED, not that one failed -- absent
    # reads identically to pending on every surface (gh#3315). Measured on #3307: after
    # update-branch the head carried only `sync-lock=skipped`; an empty-commit push attached
    # test/test-postgres/enforcement-preflight. jefe must know to check by NAME and to push.
    assert "check-runs" in md, "jefe cannot tell an ABSENT required check from a pending one"
    assert "--allow-empty" in md, \
        "jefe has no remedy for absent checks -- only a real push fires synchronize"
    # And must not present the manual fix as the system's answer: automated recovery exists.
    assert "stuck_pr_watch" in md or "3332" in md, \
        "jefe's manual retrigger must point at the automated mechanism it stands in for"


def _jefe_precedent_citations_are_repo_qualified():
    """gh#286: jefe posted PR #3875 / issue #3831 / PR #3853 as "gathered live" evidence on
    gh#269 -- fleet-kit's own outage tracker -- none of which resolve in fleet-kit. They sit in
    nonprofit-atlas's numbering range, the same range jefe.md's own worked examples cite as
    precedent (`nonprofit-atlas#3108`, etc.), unmarked as "a different repo's history" at the
    exact spots a pass composing a status comment would be reading. This locks in two things:
    (1) jefe.md actually carries the verify-before-you-cite guard, and (2) every bare 4+-digit
    `#NNNN` citation in the file (fleet-kit's own numbering is still 3-digit as of this check --
    a genuine future ceiling, not enforced here) has `nonprofit-atlas` within a short window of
    the SAME citation, not merely somewhere in the same paragraph -- an earlier version of this
    check matched on whole paragraphs (some run 300-1700 chars) and would have waved through a
    brand-new unrelated bare citation riding along on an unrelated `nonprofit-atlas` mention
    elsewhere in a long paragraph, exactly the unmarked-precedent shape gh#286 was filed over.
    """
    md = (ROOT / "members/jefe/jefe.md").read_text()
    assert "verify before you cite" in md.lower(), \
        "jefe.md lost its verify-before-you-cite guard -- see gh#286"
    assert "gh pr view" in md and "gh issue view" in md, \
        "jefe.md's citation guard must name the actual verification command"

    # Strip fenced code blocks first -- a future code snippet could legitimately embed a
    # 4+-digit number (a real command's issue-number argument, a URL) that is not a citation
    # at all and must never be counted against the paragraph it sits in.
    stripped = re.sub(r"```.*?```", "", md, flags=re.DOTALL)
    cite = re.compile(r"#\d{4,}")
    window = 120  # chars either side -- covers "nonprofit-atlas already ships ... gh#3315"
    #                 style same-sentence references without spanning into unrelated prose.
    bad = []
    for m in cite.finditer(stripped):
        lo, hi = max(0, m.start() - window), min(len(stripped), m.end() + window)
        if "nonprofit-atlas" not in stripped[lo:hi]:
            line_no = stripped.count("\n", 0, m.start()) + 1
            bad.append(f"line {line_no}: ...{stripped[lo:hi].strip()[:100]}...")
    assert not bad, (
        "jefe.md cites a specific numbered PR/issue with no nonprofit-atlas qualifier nearby "
        f"(gh#286's exact failure shape): {bad}")


def _score_reasoning_is_not_guillotined_mid_word():
    """The Magikarp score's reasoning must survive to the dashboard whole.

    Reif, 2026-08-26, reading the live panel: "getting truncated". Every score ever written
    was cut at exactly 600 characters, mid-word -- 'recurring) shows th',
    'noisy/multi-caused, n'. Measured on the deployed box: all stored rows len==600.

    The cause is a mismatch between two lines of the SAME file. The prompt asks for "one or
    two sentences" but also demands a PR number, its merge date, the specific before/after
    shift in the daily numbers, and a clause justifying the score -- which reliably runs
    700-900 chars. The model complied with the content requirement; the cap then destroyed the
    conclusion. And it happened at WRITE time, so the lost half was never recoverable by any
    UI change.

    This is "never distill an already-distilled field" (a standing decision on this fleet)
    enforced by a hardcoded slice instead of honoured.
    """
    src = (ROOT / "scripts/self_improve_score.sh").read_text()
    assert "[:600]" not in src, \
        "score reasoning is still capped at 600 chars -- shorter than the prompt's own demands"
    # Whatever the cap is, it must clear what the prompt can actually produce.
    import re as _re
    caps = [int(m) for m in _re.findall(r"len\(reasoning\) > (\d+)", src)]
    assert caps, "no explicit length guard on reasoning -- an unbounded field is its own hazard"
    assert min(caps) >= 1200, f"cap {min(caps)} still truncates a compliant answer (700-900 typical)"
    # And a cut must land on a word boundary and admit itself, never stop mid-token.
    assert "rsplit(' ', 1)" in src, "a truncated reasoning still cuts mid-word"
    assert "[truncated]" in src, "a truncated reasoning does not say it was truncated"


def _self_evo_evidence_covers_both_repos():
    """The Magikarp score's self-evolution evidence must not be single-repo-scoped again.

    #176: `self_improve_score.sh`'s `gh pr list` calls ran from `cd "$FLEET_REPO"` with no
    `--repo` flag, so on any box where $FLEET_REPO points somewhere other than fleet-kit's own
    checkout (this container: FLEET_REPO=/repo=nonprofit-atlas), the query only ever saw
    nonprofit-atlas PRs -- but since 2026-08-21 the fleet's actual jefe/dumbledore charter
    fixes land almost entirely in fleet-kit's own repo. The score read flat/low for four days,
    blind to the exact compounding activity it exists to detect. This regression check is the
    static half of the fix (acceptance criterion 5 of #176); the live half was a manual re-run
    confirming a fleet-kit PR became citable in the next self_improve_score.jsonl entry.
    """
    src = (ROOT / "scripts/self_improve_score.sh").read_text()
    # Both sources must be present: $FLEET_REPO-relative (the product repo) AND a
    # KIT_DIR-relative source (fleet-kit's own repo, wherever this script's checkout lives).
    assert "FLEET_REPO_SLUG" in src, "no repo slug derived from $FLEET_REPO for the evidence query"
    assert "KIT_REPO_SLUG" in src, "no repo slug derived from KIT_DIR -- fleet-kit's own PRs are unreachable again"
    assert 'git -C "$1" remote get-url origin' in src or "remote get-url origin" in src, \
        "repo slug is no longer derived from an existing checkout's git remote"
    # Must not query the same repo twice when $FLEET_REPO already IS fleet-kit's own repo.
    assert '"$KIT_REPO_SLUG" != "$FLEET_REPO_SLUG"' in src, \
        "no guard against querying fleet-kit's repo twice when it's already $FLEET_REPO"
    # Each merged PR entry must be tagged with its source repo -- PR numbers can collide
    # across two repos, and the prompt's 'name the specific PR' instruction needs a handle
    # that's unambiguous across both.
    assert "x['repo'] = repo" in src, "merged evidence entries are not tagged with their source repo"
    # Fail-open: a failed/empty gh call on either side must not hard-exit the script.
    assert "except Exception" in src, "evidence merge has no fail-open path for a bad/empty gh response"


def _self_improve_score_evidence_covers_member_branch_shape():
    """#292: same `head:jefe/`/`head:dumbledore/` branch-search miss #237 already fixed in
    fleet_view_server.py's Self-Evolution panel, never ported to this script -- the actual
    Magikarp grader. Most real self-evolution PRs ship on the generic per-item dispatch shape
    `member/dumbledore-<id>-<ts>` / `member/jefe-<id>-<ts>`, invisible to the literal-prefix
    search alone, so the grader's evidence was structurally missing the newest fixes needed to
    show the compounding-loop chain it scores for.

    Runs the real script end-to-end against a stubbed `gh` (answering all four search terms)
    and a stubbed `claude` (capturing the prompt it was handed) -- proves the actual bash +
    embedded python merge/dedup logic, not just that the right strings appear in the source.
    """
    import os
    import subprocess

    tmp = tempfile.mkdtemp()
    log_dir = Path(tmp) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fleet_repo = Path(tmp) / "fleet_repo"
    fleet_repo.mkdir(parents=True, exist_ok=True)
    bin_dir = Path(tmp) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    home_dir = Path(tmp) / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    capture_file = Path(tmp) / "prompt.txt"

    # Same repo slug as fleet-kit's own real checkout (KIT_DIR), so the kit-side queries are
    # skipped as a duplicate of the primary ones -- keeps the fixture to one repo tag.
    kit_slug = subprocess.run(
        ["git", "-C", str(ROOT), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(fleet_repo), "init", "-q"], check=True)
    if kit_slug:
        subprocess.run(["git", "-C", str(fleet_repo), "remote", "add", "origin", kit_slug], check=True)

    # PR #214 matches BOTH `head:dumbledore/` and `head:member/dumbledore-` here (implausible
    # in real branch-naming, done deliberately to prove dedup survives the new search term).
    # PR #215 only matches the new `head:member/jefe-` term -- the exact shape #292 says was
    # invisible before this fix. PR #100/#101 keep the old literal-prefix shape working.
    old_dumbledore_pr = {"number": 100, "title": "old dumbledore fix", "mergedAt": "2026-08-01T00:00:00Z"}
    old_jefe_pr = {"number": 101, "title": "old jefe fix", "mergedAt": "2026-08-02T00:00:00Z"}
    member_dumbledore_pr = {"number": 214, "title": "fix(persona_law): ...", "mergedAt": "2026-08-29T17:32:13Z"}
    member_jefe_pr = {"number": 215, "title": "fix(jefe): ...", "mergedAt": "2026-08-29T10:00:00Z"}

    gh_stub = (bin_dir / "gh")
    gh_stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "args = sys.argv[1:]\n"
        "search = args[args.index('--search') + 1] if '--search' in args else ''\n"
        "table = {\n"
        f"  'head:dumbledore/': {json.dumps([old_dumbledore_pr, member_dumbledore_pr])},\n"
        f"  'head:jefe/': {json.dumps([old_jefe_pr])},\n"
        f"  'head:member/dumbledore-': {json.dumps([member_dumbledore_pr])},\n"
        f"  'head:member/jefe-': {json.dumps([member_jefe_pr])},\n"
        "}\n"
        "print(json.dumps(table.get(search, [])))\n"
    )
    gh_stub.chmod(0o755)

    claude_stub = (bin_dir / "claude")
    claude_stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, os\n"
        "args = sys.argv[1:]\n"
        "prompt = args[args.index('-p') + 1] if '-p' in args else ''\n"
        f"open({str(capture_file)!r}, 'w').write(prompt)\n"
        "print('{\"score\": 42, \"reasoning\": \"test\"}')\n"
    )
    claude_stub.chmod(0o755)

    env = dict(os.environ)
    env.update({
        "FLEET_REPO": str(fleet_repo),
        "FLEET_LOG_DIR": str(log_dir),
        "FLEET_ENV_FILE": str(Path(tmp) / "nonexistent.env"),
        "HOME": str(home_dir),
        "PATH": f"{bin_dir}:{env.get('PATH', '')}",
    })
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "self_improve_score.sh")],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert proc.returncode == 0, (
        f"self_improve_score.sh failed rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert capture_file.exists(), f"claude was never invoked -- stderr={proc.stderr}"
    prompt = capture_file.read_text()

    for num in (100, 101, 214, 215):
        assert f'"number": {num}' in prompt, f"PR #{num} missing from evidence fed to the scorer:\n{prompt[:2000]}"
    assert prompt.count('"number": 214') == 1, \
        f"PR #214 (matched by two search terms) was not deduped: appears {prompt.count(chr(34) + 'number' + chr(34) + ': 214')}x"


def _daily_outcomes_carries_hours_elapsed_for_partial_today():
    """#263: `DAILY_OUTCOMES` must state how much of "today" had elapsed at generation time.

    Live evidence from the 08-30 09:07 UTC run: the grader compared 08-30's partial-day count
    (9.1h elapsed) against 08-29's full 24h count and called a real throughput INCREASE a
    "regression", because the digest never said today was still in progress. This runs the
    real script end-to-end (same stub pattern as
    `_self_improve_score_evidence_covers_member_branch_shape`) against a seeded runs.jsonl with
    one full past day and one partial "today", and checks the actual prompt the grader would
    see -- not just that a string appears in the source.
    """
    import os
    import subprocess

    tmp = tempfile.mkdtemp()
    log_dir = Path(tmp) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fleet_repo = Path(tmp) / "fleet_repo"
    fleet_repo.mkdir(parents=True, exist_ok=True)
    bin_dir = Path(tmp) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    home_dir = Path(tmp) / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    capture_file = Path(tmp) / "prompt.txt"

    subprocess.run(["git", "-C", str(fleet_repo), "init", "-q"], check=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    yesterday = now - datetime.timedelta(days=1)
    day_start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_yesterday = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    runs = []
    # A full past day: 12 "ok" runs spread across all 24h.
    for h in range(0, 24, 2):
        runs.append({"ts": (day_start_yesterday + datetime.timedelta(hours=h)).timestamp(), "status": "ok"})
    # A partial "today": 3 "ok" runs so far, well before the current hour.
    for h in range(0, min(3, max(now.hour, 1))):
        runs.append({"ts": (day_start_today + datetime.timedelta(hours=h)).timestamp(), "status": "ok"})
    runs_file = log_dir / "runs.jsonl"
    runs_file.write_text("\n".join(json.dumps(r) for r in runs) + "\n")

    gh_stub = (bin_dir / "gh")
    gh_stub.write_text("#!/usr/bin/env python3\nprint('[]')\n")
    gh_stub.chmod(0o755)

    claude_stub = (bin_dir / "claude")
    claude_stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, os\n"
        "args = sys.argv[1:]\n"
        "prompt = args[args.index('-p') + 1] if '-p' in args else ''\n"
        f"open({str(capture_file)!r}, 'w').write(prompt)\n"
        "print('{\"score\": 42, \"reasoning\": \"test\"}')\n"
    )
    claude_stub.chmod(0o755)

    env = dict(os.environ)
    env.update({
        "FLEET_REPO": str(fleet_repo),
        "FLEET_LOG_DIR": str(log_dir),
        "FLEET_ENV_FILE": str(Path(tmp) / "nonexistent.env"),
        "HOME": str(home_dir),
        "PATH": f"{bin_dir}:{env.get('PATH', '')}",
    })
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "self_improve_score.sh")],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert proc.returncode == 0, (
        f"self_improve_score.sh failed rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert capture_file.exists(), f"claude was never invoked -- stderr={proc.stderr}"
    prompt = capture_file.read_text()

    today_iso = now.date().isoformat()
    yesterday_iso = yesterday.date().isoformat()
    m = re.search(r'\{"' + re.escape(yesterday_iso) + r'".*?\}\}', prompt)
    assert m, f"DAILY_OUTCOMES block not found in prompt:\n{prompt[-2000:]}"
    daily = json.loads(m.group(0))
    assert daily[yesterday_iso]["hours_elapsed"] == 24, \
        f"a complete past day must carry hours_elapsed=24, got {daily[yesterday_iso]}"
    today_hours = daily[today_iso]["hours_elapsed"]
    assert 0 <= today_hours <= 24 and today_hours != 24, \
        f"today's partial hours_elapsed should reflect wall-clock progress, got {daily[today_iso]}"
    assert "hours_elapsed" in prompt.split("Run outcome counts BY DAY")[1].split("Score 1-100")[0], \
        "grader prompt text does not mention hours_elapsed for normalizing same-day comparisons"


def _adhoc_task_adds_to_the_charter_never_replaces_it():
    """`--task` runs a member ad-hoc with one extra instruction, charter still governing.

    Reif, 2026-08-26: "you can run marie with a custom thing to do ... makes sense that we can
    run something ad-hoc with some prompt addition." The mechanism already existed as --item
    (how gru hands a minion its issue); this generalises it.

    The property that matters is CONTAINMENT: an operator instruction must not become a way to
    talk a member out of its own mandate, checklist, limits or escalation rules. So the runner
    must state that the charter still governs and that a conflict is reported, not obeyed --
    and the charter text must still be present in the prompt, not swapped out.
    """
    src = (Path(__file__).parent / "run_member.sh").read_text()
    assert '--task) TASK=' in src, "--task is not parsed"
    assert 'TASK=""' in src, "TASK unset would abort under set -u"
    # It ADDS: the charter ($PROMPT) is still interpolated after the instruction.
    i = src.find("THIS RUN HAS AN ADDITIONAL INSTRUCTION")
    assert i != -1, "no ad-hoc preamble"
    block = src[i:i + 700]
    assert "$TASK" in block and "$PROMPT" in block, "instruction or charter missing from prompt"
    assert "still governs" in block, "preamble does not assert the charter still governs"
    assert "follow the charter" in block, "preamble does not resolve conflicts toward the charter"
    # An ad-hoc pass is marked so it can be excluded from cost calibration.
    assert "${TASK:+-adhoc}" in src, "ad-hoc runs are not distinguishable in run_id"


def _a_run_records_the_item_it_worked():
    """`run_member.sh --item N` must reach the DB's item_id column, not just the run_id string.

    2026-08-27: EVERY row in the deployed fleet.db had item_id NULL -- including runs whose
    run_id plainly encodes the item (`minion-item3280-4140-...`). run_member.sh parsed --item
    into $ITEM and used it for the run_id, the worktree path and the prompt, but never passed
    --item-id to run_report.py. The column, the CLI flag (`fleet_db.py query --item-id`) and
    the whole downstream write path were all correct and had simply never been given a value.

    This is the day's theme in its purest form: the field exists, the query runs, it returns
    nothing, and nothing anywhere reports an error. It silently disabled `query --item-id`,
    which is how you'd detect two minions working one item -- the exact open question that
    found this bug.

    Asserts the wiring at BOTH call sites, because the kill-trap path (a pass killed mid-work
    is precisely when you most want to know which item was lost) is easy to fix and forget.
    """
    src = (ROOT / "scripts" / "run_member.sh").read_text()
    calls = re.findall(r'python3 "\$KIT_DIR/scripts/run_report\.py"(.*?)>>', src, re.S)
    if not calls:
        raise AssertionError("no run_report.py call sites found in run_member.sh")
    missing = [" ".join(c.split())[:80] for c in calls if "--item-id" not in c]
    if missing:
        raise AssertionError(
            f"{len(missing)}/{len(calls)} run_report.py call(s) drop --item-id -- "
            f"item_id lands NULL: {missing}")


def _a_killed_pass_is_recorded_not_lost():
    """A pass killed from outside must leave a runs.jsonl row, and the trap must be ABLE to run.

    2026-08-26: a deploy cutover (`podman stop -t 10`) SIGKILLed an in-flight marie pass mid-
    work. runs.jsonl got no row at all -- not an error row, NO row -- because the record is
    written only after `claude -p` returns. ~$3 of spend and 17 completed issue-scores looked,
    from every dashboard, like a pass that never ran. Silent loss is worse than a failure row.

    Two independent properties, because the first is useless without the second:

    1. run_report classifies 143/137 (SIGTERM/SIGKILL) as `killed` -- distinct from timed_out
       (had time left) and budget_declined (was spending fine). It means interrupted, re-runnable.
    2. The pass pipeline is BACKGROUNDED and waited on. Bash defers a trap handler while a
       FOREGROUND child runs, so with the pipeline in the foreground the handler would not fire
       until claude exited on its own -- measured at t+60s against a 60s sleep, while podman
       SIGKILLs at t+10s. A trap that cannot run in the grace window is decoration; this asserts
       the structure that makes it real, which review alone did not catch.
    """
    import run_report
    for code in (143, 137):
        rec = run_report.build_record(member="t", run_id="r", kind="llm", exit_code=code,
                                      pass_text="", usage=None, vision_required=False)
        assert rec["status"] == "killed", f"exit {code} -> {rec['status']}, want killed"
    # An interrupted pass must stay distinguishable from the two statuses it superficially
    # resembles; collapsing them is exactly the #3015 class of bug.
    for code, want in ((3, "budget_declined"), (124, "timed_out")):
        rec = run_report.build_record(member="t", run_id="r", kind="llm", exit_code=code,
                                      pass_text="", usage=None, vision_required=False)
        assert rec["status"] == want, f"exit {code} -> {rec['status']}, want {want}"

    src = (Path(__file__).parent / "run_member.sh").read_text()
    assert "trap record_killed_pass TERM INT" in src, "no SIGTERM trap on the pass"
    assert "PASS_PID=$!" in src and 'wait "$PASS_PID"' in src, \
        "pass pipeline is not backgrounded+waited -- the trap cannot fire during claude -p"
    # `wait` reports the job's LAST command (the log loop, always 0), so the subshell must
    # re-raise claude's own status or budget_declined/timed_out silently break.
    assert 'exit "${PIPESTATUS[0]}" ) &' in src, \
        "backgrounded pipeline does not re-raise PIPESTATUS -- claude's exit code is lost"


def _run_member_writes_a_started_row_before_claude_p(src=None):
    """gh#145: a pass that vanishes before EITHER of run_member.sh's two existing write points
    (normal-exit, or record_killed_pass's SIGTERM trap -- see _a_killed_pass_is_recorded_not_lost
    above) still leaves zero runs.jsonl trace, because both of those only fire after `claude -p`
    returns or is trapped. This asserts the fix is actually wired in, not just a library
    function nothing calls: run_member.sh must write a `--started` row, sharing $RUN_ID, before
    the `claude -p` invocation and before the SIGTERM trap even arms -- structural, since a real
    SIGKILL can't be exercised from a unit test.
    """
    src = src if src is not None else (Path(__file__).parent / "run_member.sh").read_text()
    started_call = re.search(r'run_report\.py"\s*--started.*?--run-id "\$RUN_ID"', src, re.S)
    assert started_call, "no `run_report.py --started ... --run-id \"$RUN_ID\"` call found"
    claude_invoke = src.index('claude -p "$PROMPT"')
    trap_arm = src.index("trap record_killed_pass TERM INT")
    assert started_call.start() < claude_invoke, \
        "started row is written AFTER claude -p is invoked -- too late to catch a kill during it"
    assert started_call.start() < trap_arm, \
        "started row is written after the SIGTERM trap arms -- a kill in between still vanishes"


def _run_report_started_row_pairs_with_a_later_completion_by_run_id():
    """gh#145 AC1/AC2: `run_report.py --started` must produce a row shaped so a reconciliation
    query can pair it against whichever completion record (or none) eventually lands for the
    SAME run_id -- same field name, same value, nothing derived or reformatted in between.
    """
    import run_report
    started = run_report.build_started_record(member="marie", run_id="marie-item145-123-9",
                                              item_id="145", lane=None)
    assert started["status"] == run_report.STATUS_STARTED == "started"
    assert started["run_id"] == "marie-item145-123-9"
    assert started["member"] == "marie"
    assert started["item_id"] == "145"
    assert isinstance(started["ts"], float), "no wall-clock timestamp on the started row"

    completion = run_report.build_record(
        member="marie", run_id="marie-item145-123-9", kind="llm", exit_code=0,
        pass_text="Outcome: closed #145\nEvidence: gh issue close 145", usage=None,
        vision_required=False, item_id="145")
    assert completion["run_id"] == started["run_id"], \
        "completion record's run_id must match the started row's -- reconciliation pairs on it"
    assert completion["status"] != run_report.STATUS_STARTED, \
        "a normal completion must never itself read as 'started'"


def _lost_passes_flags_a_started_row_with_no_completion_past_the_grace_window():
    """gh#145 AC3: the reconciliation query itself. A "started" row with no matching completion
    row (same run_id) must surface once it's older than the grace window -- and must NOT surface
    a moment after starting (still plausibly mid-run), and must NOT surface at all once its
    completion (any non-'started' status) lands, however late.
    """
    import fleet_stats
    now = fleet_stats._now_epoch()

    def started(run_id, member, ts_offset):
        return {"run_id": run_id, "member": member, "status": "started",
                "item_id": None, "ts": now - ts_offset}

    def completed(run_id, member, status="killed", ts_offset=0):
        return {"run_id": run_id, "member": member, "status": status, "ts": now - ts_offset}

    runs = [
        started("lost-1", "marie", ts_offset=120 * 60),           # old, no completion -- LOST
        started("fresh-1", "marie", ts_offset=5 * 60),            # too young -- not yet lost
        started("healthy-1", "datta", ts_offset=120 * 60),
        completed("healthy-1", "datta", status="ok", ts_offset=1 * 60),  # paired -- not lost
    ]
    lost = fleet_stats.lost_passes(runs, grace_minutes=90.0)
    lost_ids = {r["run_id"] for r in lost}
    assert lost_ids == {"lost-1"}, f"lost_passes() = {lost_ids}, want exactly {{'lost-1'}}"
    row = lost[0]
    assert row["member"] == "marie"
    assert row["age_minutes"] >= 90.0


def _signal_rate_excludes_all_never_executed_statuses():
    """fleet-kit#150: fleet_stats.py's `_NOT_EXECUTED_STATUSES` once listed only
    `budget_declined`, while run_report.py classifies THREE statuses as "never got the chance
    to do real work" -- `budget_declined`, `timed_out`, `killed` (see
    `_a_killed_pass_is_recorded_not_lost` above). A run killed by a container restart/OOM, or
    one that timed out, was silently counted as an executed-but-failed run: it dragged
    signal_rate down below the fleet's true quality AND stopped `dormant` from firing for a
    member whose recent runs were all kills/timeouts -- a real infra failure misread as a
    quality problem.
    """
    import fleet_stats
    now = fleet_stats._now_epoch()

    def run(member, status, ts_offset=0):
        return {"member": member, "status": status, "ts": now - ts_offset}

    runs = [
        run("a", "budget_declined"),
        run("a", "timed_out"),
        run("a", "killed"),
        run("b", "ok"),
        run("b", "reported_nothing"),
    ]
    summary = fleet_stats.runs_summary(runs, hours=24.0)
    assert summary["executed"] == 2, (
        f"executed = {summary['executed']}, want 2 -- killed/timed_out still counted as executed")
    assert summary["signal_rate"] == 50, (
        f"signal_rate = {summary['signal_rate']}, want 50 (1 ok / 2 executed)")

    # A member whose every in-window run is some mix of the three never-executed statuses
    # must show up as dormant -- that's the whole point of excluding them.
    assert "a" in summary["dormant"], "member 'a' (all killed/timed_out/budget_declined) not dormant"
    assert "b" not in summary["dormant"], "member 'b' (has real runs) wrongly marked dormant"


def _dormant_flags_an_enabled_member_with_zero_runs_in_window():
    """gh#190: `dormant` was built by iterating `by_member`, which only ever contains members
    that appear in the windowed runs -- a member with ZERO rows in the window (the single
    worst case: it stopped firing entirely) could never enter `by_member` and so could never
    be flagged, the one failure mode this tile exists to catch. Fixed by taking the full
    roster (`member_spec.load_all()`) as an optional parameter and adding any `enabled: true`
    member missing from the window entirely. `enabled: false` members (nerd/minion, #166) must
    never be flagged dormant solely for having zero rows -- that's intentional non-scheduling,
    not a silent failure.
    """
    import fleet_stats
    now = fleet_stats._now_epoch()

    def run(member, status, ts_offset=0):
        return {"member": member, "status": status, "ts": now - ts_offset}

    runs = [
        run("has_runs", "ok"),
        run("all_declined", "budget_declined"),
    ]
    roster = [
        {"name": "has_runs", "enabled": True},
        {"name": "all_declined", "enabled": True},
        {"name": "zero_runs_enabled", "enabled": True},
        {"name": "zero_runs_disabled", "enabled": False},
    ]
    summary = fleet_stats.runs_summary(runs, hours=24.0, roster=roster)
    assert "zero_runs_enabled" in summary["dormant"], (
        "an enabled roster member absent from the window entirely must be flagged dormant")
    assert "zero_runs_disabled" not in summary["dormant"], (
        "an enabled:false roster member must never be flagged dormant for zero runs (#166)")
    assert "all_declined" in summary["dormant"], (
        "a member whose in-window runs are all non-executed must still be dormant (regression)")
    assert "has_runs" not in summary["dormant"], (
        "a member with an executed run must never be dormant (regression)")

    # No roster passed at all: today's behavior is unchanged, no zero-run member is ever
    # flagged -- this is what keeps this file's OWN prior runs_summary(runs, hours=24.0) call
    # (no roster arg) passing unmodified.
    summary_no_roster = fleet_stats.runs_summary(runs, hours=24.0)
    assert "zero_runs_enabled" not in summary_no_roster["dormant"], (
        "with no roster passed, a zero-run member must not be flagged (safe default)")


def _runs_summary_excludes_started_rows_from_total_and_signal_rate():
    """gh#437: run_report.py's provisional "started" row (gh#145) was never added to either
    `_NOT_EXECUTED_STATUSES` or `_OK_STATUSES` in `runs_summary()`, so it was double-counted --
    once as an extra `total` run, and once as an executed-but-not-ok failure (the same bucket as
    a real crash), deflating `signal_rate` and inflating `total`. 3rd instance of the same
    "a new run_report.py status doesn't reach fleet_stats.py's classification sets" bug class
    as gh#150 (killed/timed_out) and gh#254 (incomplete_fanout).

    Mixed window: a matched started+completion pair (must contribute exactly one count, the
    completion's), an unmatched started row still mid-run (must contribute zero), plus plain
    ok/quiet rows to give signal_rate a real denominator to get right or wrong.
    """
    import fleet_stats
    now = fleet_stats._now_epoch()

    def run(member, status, run_id=None, ts_offset=0):
        return {"member": member, "status": status, "run_id": run_id, "ts": now - ts_offset}

    runs = [
        run("a", "started", run_id="paired-1", ts_offset=10 * 60),
        run("a", "ok", run_id="paired-1", ts_offset=9 * 60),   # same pass's completion
        run("b", "started", run_id="unmatched-1", ts_offset=1 * 60),  # still running, no completion
        run("a", "ok"),
        run("a", "quiet"),
    ]
    summary = fleet_stats.runs_summary(runs, hours=24.0)

    # 3 real runs (2 ok + 1 quiet) -- the started rows contribute nothing to total/executed.
    assert summary["total"] == 3, f"total = {summary['total']}, want 3 (started rows excluded)"
    assert summary["executed"] == 3, f"executed = {summary['executed']}, want 3"
    assert summary["signal_rate"] == round(100 * 2 / 3), (
        f"signal_rate = {summary['signal_rate']}, want {round(100 * 2 / 3)} (2 ok / 3 executed)")

    # per-member breakdown must apply the same exclusion: member "a" has 3 real rows (paired-1's
    # completion + the standalone ok + quiet), never 4 (its started row must not also count).
    rates = {r["member"]: r for r in summary["agent_rates"]}
    assert rates["a"]["executed"] == 3, (
        f"member a executed = {rates['a']['executed']}, want 3 (paired-1's started row excluded)")
    assert "b" not in rates, "member b has only a started row -- must not appear in agent_rates"

    # "started" must never render as a status/outcome on the hourly chart.
    assert "started" not in summary["statuses"], (
        "'started' leaked into the hourly chart's status vocabulary")


def _status_page_deploy_component_classifies_stale_as_down():
    """gh#367: /status had 5 COMPONENTS rows and no Deploy row, so the fleet's own worst-
    performing pipeline (deploy_success_rate=8-9% at filing) had zero representation on the
    page built specifically so an operator doesn't have to log-dive during an incident.

    Fixed by adding a 6th COMPONENTS tuple pointed at deploy_staleness_check's own log --
    STALE is already a BAD_WORDS entry (no new classify() logic needed). This pins the two
    load-bearing cases from the PRD's acceptance criteria: a STALE line in the window resolves
    to "down" (AC3), and a fresh box with no log file at all resolves to "unknown"/None rather
    than crashing (AC4) -- the same "no file -> all unknown" contract every other row gets.

    The synthetic log line below uses deploy_staleness_check.sh's REAL line shape --
    "[deploy-staleness <ts> UTC] ..." (a check-name prefix ahead of the date, inside the same
    bracket) -- not a bare "[<ts> UTC]". fleet-code-review BLOCKed the first cut of this fix
    (gh#367) because the original test used the bare form, which _TS's old regex matched by
    accident; the real line never matched, silently falling through to the mtime/5-min-cadence
    fallback and misdating every line but the newest across a sustained, multi-tick incident.
    """
    import importlib as _il
    import os as _os
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "scripts"))
    import status_data

    names = next(names for label, names, _desc in status_data.COMPONENTS if label == "Deploy")
    assert names == ["deploy_staleness_check.cron.log", "deploy_staleness_check.log"], (
        f"Deploy COMPONENTS candidate list drifted from the PRD's spec: {names!r}")

    old = _os.environ.get("FLEET_LOG_DIR")
    with tempfile.TemporaryDirectory() as td:
        _os.environ["FLEET_LOG_DIR"] = td
        try:
            _il.reload(status_data)
            now = datetime.datetime.now(datetime.timezone.utc)
            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            (Path(td) / "deploy_staleness_check.log").write_text(
                f"[deploy-staleness {ts} UTC] STALE: local HEAD is 6 commits behind origin/main\n")

            cells, pct = status_data.read_component(names)
            assert cells[-1] == status_data.BAD, (
                f"a STALE line inside the 72h window must classify as down, got {cells[-1]!r}")

            snap = status_data.snapshot()
            deploy = next(c for c in snap["components"] if c["label"] == "Deploy")
            assert deploy["current"] == status_data.BAD, (
                f"snapshot()'s Deploy entry must surface the STALE incident, got {deploy!r}")

            # Sustained incident: deploy_staleness_check.sh ticks hourly, not every 5 minutes
            # like the other checks read_component's fallback was calibrated for. A 6-hour-long
            # outage writes 6 real-timestamped STALE lines, one per hour. Every line must land
            # in its OWN hour bucket via its real timestamp, not get compressed into the last
            # few minutes before mtime by the 5-min-cadence fallback (the exact bug this PR's
            # fleet-code-review BLOCK identified).
            lines = []
            for hours_ago in range(6):
                line_ts = (now - datetime.timedelta(hours=hours_ago)).strftime(
                    "%Y-%m-%d %H:%M:%S")
                lines.append(
                    f"[deploy-staleness {line_ts} UTC] STALE: local HEAD is behind origin/main")
            (Path(td) / "deploy_staleness_check.log").write_text("\n".join(lines) + "\n")
            incident_cells, incident_pct = status_data.read_component(names)
            down_count = sum(1 for c in incident_cells if c == status_data.BAD)
            assert down_count >= 6, (
                f"a 6-hour sustained incident (6 real-timestamped STALE lines) must occupy "
                f"6 distinct down hour-buckets, got {down_count}: {incident_cells!r}")

            # AC4: fresh box, check never ran -- no file at all, not a crash or all-good.
            (Path(td) / "deploy_staleness_check.log").unlink()
            empty_cells, empty_pct = status_data.read_component(names)
            assert all(c == status_data.UNKNOWN for c in empty_cells), (
                "no deploy log file at all must render as unknown, not assumed healthy")
            assert empty_pct is None, "no data yields no uptime percentage, not a fabricated one"
        finally:
            if old is None:
                _os.environ.pop("FLEET_LOG_DIR", None)
            else:
                _os.environ["FLEET_LOG_DIR"] = old
            _il.reload(status_data)


def _status_data_members_reads_fleet_db_with_no_podman_on_path():
    """gh#364: status_data.py's own server (fleet_view_server.py, started by entrypoint.sh)
    runs INSIDE the container that owns fleet.db, so the old `_sqlite()` shelled out to
    `podman exec <container> sqlite3 ...` from a vantage point that never has a `podman`
    binary reachable -- the resulting FileNotFoundError was swallowed by a bare `except
    Exception: return ""`, so `members()` silently returned [] and the /status page's entire
    'Fleet members' card never rendered, with no visual trace it was missing.

    RED against the old code: with no `podman` on $PATH (this container's real topology),
    `_sqlite()` returns "" regardless of what fleet.db holds, so `members(72)` returns [].
    GREEN after the fix: a direct in-process `sqlite3.connect()` needs no subprocess, no
    `podman`, and no container boundary at all -- it reads the real rows.
    """
    import importlib as _il
    import os as _os
    import sqlite3 as _sqlite3
    import sys as _sys
    import time as _time

    _sys.path.insert(0, str(ROOT / "scripts"))
    import status_data

    old_db = _os.environ.get("FLEET_DB")
    old_path = _os.environ.get("PATH")
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "fleet.db")
        conn = _sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE runs (run_id TEXT, member TEXT, status TEXT, "
            "recorded_at REAL, cost_usd REAL)")
        conn.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?)",
            ("r1", "minion", "ok", _time.time(), 0.5))
        conn.commit()
        conn.close()

        _os.environ["FLEET_DB"] = db_path
        # The real topology this bug reproduces: no podman binary reachable from in here.
        _os.environ["PATH"] = td
        try:
            _il.reload(status_data)
            rows = status_data.members(72)
            assert rows, (
                "members(72) returned [] against a fleet.db with a real recent row and no "
                "podman on $PATH -- the Fleet members card would render as if the fleet "
                "were empty")
            assert rows[0]["name"] == "minion", f"unexpected row shape: {rows!r}"
        finally:
            if old_db is None:
                _os.environ.pop("FLEET_DB", None)
            else:
                _os.environ["FLEET_DB"] = old_db
            if old_path is None:
                _os.environ.pop("PATH", None)
            else:
                _os.environ["PATH"] = old_path
            _il.reload(status_data)


def _status_data_other_components_unaffected_by_members_fix():
    """gh#364 AC5: the fix to `_sqlite()`/`members()` must not touch the other four /status
    components (Account pool, Public path, Tunnel, Budget meter) -- they read log files
    directly and are unrelated to fleet.db. Pins that COMPONENTS is unchanged in shape and
    that read_component() still classifies a plain healthy line as OK, for a fixed fixture.
    """
    import importlib as _il
    import os as _os
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "scripts"))
    import status_data

    labels = [label for label, _names, _desc in status_data.COMPONENTS]
    assert labels == [
        "Account pool", "Public path", "Tunnel", "Budget meter (tgp)",
        "Budget meter (gmail)", "Deploy",
    ], f"COMPONENTS list drifted: {labels!r}"

    old = _os.environ.get("FLEET_LOG_DIR")
    with tempfile.TemporaryDirectory() as td:
        _os.environ["FLEET_LOG_DIR"] = td
        try:
            _il.reload(status_data)
            # Real line shape from account_health_check.sh's own healthy branch -- no
            # timestamp in the bracket, lowercase "healthy" right after it.
            (Path(td) / "account_health_check.cron.log").write_text(
                "[account_health_check] healthy -- newest pool-log line is not a failure\n")
            names = next(n for label, n, _d in status_data.COMPONENTS
                         if label == "Account pool")
            cells, pct = status_data.read_component(names)
            assert cells[-1] == status_data.OK, (
                f"Account pool must still classify a healthy line as ok, got {cells[-1]!r}")
        finally:
            if old is None:
                _os.environ.pop("FLEET_LOG_DIR", None)
            else:
                _os.environ["FLEET_LOG_DIR"] = old
            _il.reload(status_data)


def _status_page_public_path_resolves_bare_cron_log_first():
    """gh#387 AC1: this box's own host cron writes `path_health_check.cron.log` with no
    instance suffix (99.76% healthy over ~5.8 days at filing) -- the OLD candidate list's
    first entry, `path_health_check.philanthropy.cron.log`, never exists here, so `_resolve()`
    fell all the way through to the thin `path_health_check.log` fallback and the page showed
    "no data" for a component that has been healthy nearly the whole time. The suffixed name
    is kept as a later fallback candidate (not deleted), per the PRD's own non-goal, in case
    some other deployed instance's host scheduler really does suffix it that way.
    """
    import importlib as _il
    import os as _os
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "scripts"))
    import status_data

    names = next(names for label, names, _desc in status_data.COMPONENTS
                 if label == "Public path")
    assert names[0] == "path_health_check.cron.log", (
        f"bare path_health_check.cron.log must be the first candidate, got {names!r}")
    assert "path_health_check.philanthropy.cron.log" in names, (
        f"the suffixed candidate must be kept as a fallback, not deleted: {names!r}")
    assert names[-1] == "path_health_check.log", (
        f"path_health_check.log must remain the final fallback: {names!r}")

    old = _os.environ.get("FLEET_LOG_DIR")
    with tempfile.TemporaryDirectory() as td:
        _os.environ["FLEET_LOG_DIR"] = td
        try:
            _il.reload(status_data)
            # Only the bare, unsuffixed file exists on this box -- the real topology gh#387
            # found live.
            (Path(td) / "path_health_check.cron.log").write_text(
                "[path_health_check] healthy -- https://... returned 200\n")
            names = next(n for label, n, _d in status_data.COMPONENTS
                         if label == "Public path")
            cells, pct = status_data.read_component(names)
            assert cells[-1] == status_data.OK, (
                f"a bare path_health_check.cron.log with a healthy line must resolve and "
                f"classify as ok, got {cells[-1]!r}")
        finally:
            if old is None:
                _os.environ.pop("FLEET_LOG_DIR", None)
            else:
                _os.environ["FLEET_LOG_DIR"] = old
            _il.reload(status_data)


def _status_page_hourly_log_cadence_not_flattened_to_5min():
    """gh#387 AC3/AC4: `read_component()`'s back-fill for lines with no timestamp of their
    own used to assume every resolved file is written every 5 minutes, regardless of which
    candidate actually resolved. Tunnel's real fallback, `tunnel_health_check.log`, is written
    HOURLY by entrypoint.sh's own crontab (:37) -- the old fixed 5-minute assumption packed 26
    real hourly checks into ~2 hours of buckets, leaving the rest of the 72h window gray even
    though the check ran (and passed) almost the whole time.

    RED against the old code: 26 no-timestamp lines land within ~130 minutes (2 hour-buckets).
    GREEN after the fix: the same 26 lines, resolved from a bare `.log` name, spread across
    ~26 distinct hour-buckets -- one real check per hour, matching how the file was actually
    written. A `.cron.log`-suffixed file with the identical 26 lines must still compress into
    a couple of hours (AC5: the 5-minute-cadence components must not change).
    """
    import importlib as _il
    import os as _os
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "scripts"))
    import status_data

    old = _os.environ.get("FLEET_LOG_DIR")
    with tempfile.TemporaryDirectory() as td:
        _os.environ["FLEET_LOG_DIR"] = td
        try:
            _il.reload(status_data)
            healthy_lines = "\n".join(
                "[tunnel_health_check] healthy -- reachable" for _ in range(26)) + "\n"

            (Path(td) / "tunnel_health_check.log").write_text(healthy_lines)
            hourly_cells, _pct = status_data.read_component(
                ["tunnel_health_check.cron.log", "tunnel_health_check.log"])
            hourly_ok = sum(1 for c in hourly_cells if c == status_data.OK)
            assert hourly_ok >= 20, (
                f"26 hourly-cadence lines with no per-line timestamp must spread across "
                f"~26 hour-buckets, got only {hourly_ok} ok buckets: {hourly_cells!r}")

            (Path(td) / "tunnel_health_check.log").unlink()
            (Path(td) / "tunnel_health_check.cron.log").write_text(healthy_lines)
            fivemin_cells, _pct2 = status_data.read_component(
                ["tunnel_health_check.cron.log", "tunnel_health_check.log"])
            fivemin_ok = sum(1 for c in fivemin_cells if c == status_data.OK)
            assert fivemin_ok <= 3, (
                f"a .cron.log-suffixed (5-minute-cadence) file must NOT be re-cadenced -- "
                f"26 lines at 5 minutes apart span ~2 hours, got {fivemin_ok} ok buckets: "
                f"{fivemin_cells!r}")
        finally:
            if old is None:
                _os.environ.pop("FLEET_LOG_DIR", None)
            else:
                _os.environ["FLEET_LOG_DIR"] = old
            _il.reload(status_data)


def _status_page_banner_distinguishes_unknown_from_good():
    """gh#358: status_page.py computed the banner box's class as a 2-way `bad`/`good` boolean
    (`bad = overall == "down"`), so an `unknown` overall -- the state that fires right now with
    the tgp/gmail budget meters at zero data for 72h -- fell through to `good`, the identical
    class used when every component is actually healthy. The headline text and dot already
    handled all three states correctly; only the surrounding box lied.

    This pins all three `_BANNER_CLASS` branches directly against a monkeypatched
    `status_data.snapshot`, plus the CSS itself carrying a `.banner.unknown` rule whose values
    don't just alias `.banner.good` or `.banner.bad` (PRD AC2) -- a class name alone would pass
    even if it rendered identically.
    """
    import status_data
    import status_page

    def _fake_snapshot(overall):
        def _snap(hours=72):
            return {
                "overall": overall,
                "components": [],
                "hours": hours,
                "members": [],
                "generated_at": "2026-09-05 00:00 UTC",
            }
        return _snap

    real_snapshot = status_data.snapshot
    try:
        for overall, want_class in (("ok", "good"), ("down", "bad"), ("unknown", "unknown")):
            status_data.snapshot = _fake_snapshot(overall)
            html = status_page.render()
            assert f"banner {want_class}" in html, (
                f"overall={overall!r} must render 'banner {want_class}', got: "
                + next((l for l in html.splitlines() if "class='banner" in l), "<no banner line>")
            )
    finally:
        status_data.snapshot = real_snapshot

    # AC2: `.banner.unknown` must be visually distinct from BOTH `.banner.good` and
    # `.banner.bad` -- a different border-color and/or header background, not reusing either.
    def _decls(selector):
        border = re.search(re.escape(selector) + r"\{([^}]*)\}", status_page.CSS)
        head = re.search(re.escape(selector) + r" \.banner-head\{([^}]*)\}", status_page.CSS)
        return (border.group(1) if border else "", head.group(1) if head else "")

    good = _decls(".banner.good")
    bad = _decls(".banner.bad")
    unknown = _decls(".banner.unknown")
    assert unknown != ("", ""), ".banner.unknown must have its own CSS rule"
    assert unknown != good, ".banner.unknown must not render identically to .banner.good"
    assert unknown != bad, ".banner.unknown must not render identically to .banner.bad"


def _status_data_live_alerts_proxies_alert_store_without_touching_overall():
    """gh#399 AC7: status_data.snapshot()'s `live_alerts` key must carry alert_store.py's
    (PR#397) live severity feed verbatim, and must NEVER change `overall` -- that field is the
    older, separate log-derived component-uptime rollup (status_data.py:130-141) and merging
    the two concepts is explicitly out of scope."""
    import status_data
    import alert_store

    fake = {"worst": "critical", "budget_safe": False,
            "counts": {"transient": 0, "degraded": 0, "critical": 1},
            "open": [{"check": "probe_token", "problem": "token rejected", "handles": ["reif"]}]}
    real_alert_snapshot = alert_store.snapshot
    try:
        alert_store.snapshot = lambda *a, **k: fake
        snap = status_data.snapshot()
        assert snap["live_alerts"] == fake, f"live_alerts did not proxy alert_store: {snap.get('live_alerts')}"
        assert snap["overall"] in (status_data.OK, status_data.BAD, status_data.UNKNOWN), (
            "overall must still be one of the log-derived states, untouched by live_alerts")
    finally:
        alert_store.snapshot = real_alert_snapshot


def _status_data_live_alerts_fails_open_on_a_broken_store():
    """A live_alerts() that raised would take status_data.snapshot() down with it -- the exact
    silent-failure class alert_store.snapshot() itself already refuses to produce. Mirror its
    fail-open contract: an exception must still read as unsafe/unknown, never as no-alerts."""
    import status_data
    import alert_store

    real_alert_snapshot = alert_store.snapshot
    try:
        def _boom(*a, **k):
            raise RuntimeError("state file corrupt")
        alert_store.snapshot = _boom
        result = status_data.live_alerts()
        assert result["worst"] == "unknown", f"a broken store must read as unknown, got {result}"
        assert result["budget_safe"] is False, "a broken store must never read as budget_safe"
    finally:
        alert_store.snapshot = real_alert_snapshot


def _status_page_transient_alert_does_not_render_as_a_confirmed_fault():
    """gh#399: alert_store.py's own severity doc says transient "is NOT evidence the watched
    thing is broken; it is evidence we are blind. NEVER pages on its own." A worst=="transient"
    response (e.g. a container mid-restart) must still be visible per AC2's literal "worst !=
    ok", but must never render with the same class as a confirmed critical fault -- that would
    reintroduce, at the display layer, exactly the false-alarm noise this whole feed exists to
    eliminate."""
    import status_page

    html = status_page._live_alert_html(
        {"worst": "transient", "budget_safe": True, "counts": {"transient": 1},
         "open": [{"check": "budget_read", "problem": "meter unreadable", "handles": []}]})
    assert html, "worst=transient must still render something (AC2's literal worst != ok)"
    assert "class='live-alert critical" not in html, (
        "a transient (unconfirmed) condition must not render with the critical class")
    assert "transient" in html


def _status_page_renders_live_alert_banner_distinct_from_component_grid():
    """gh#399 AC7/AC8: when live_alerts.worst != "ok", /status must show a banner distinct
    from the existing per-component uptime grid; when worst == "ok", neither the live-alert
    block nor the component banner's own text should claim a live alert is open."""
    import status_data
    import status_page

    real_snapshot = status_data.snapshot

    def _fake(live_alerts, overall="ok"):
        def _snap(hours=72):
            return {"overall": overall, "components": [], "hours": hours, "members": [],
                     "live_alerts": live_alerts, "generated_at": "2026-09-05 00:00 UTC"}
        return _snap

    try:
        status_data.snapshot = _fake({"worst": "ok", "budget_safe": True, "counts": {}, "open": []})
        html_ok = status_page.render()
        assert "class='live-alert" not in html_ok, "worst=ok must render no live-alert block (AC8)"

        # overall="down" here, deliberately DIFFERENT from live_alerts' own worst -- if
        # rendering live_alerts ever clobbered or was driven by `overall` (the thing AC7
        # forbids), this would catch it, unlike pinning both to "ok"/"critical" together.
        status_data.snapshot = _fake({
            "worst": "critical", "budget_safe": False,
            "counts": {"critical": 2, "degraded": 1},
            "open": [{"check": "probe_token", "problem": "token rejected", "handles": ["reif"]}],
        }, overall="down")
        html_bad = status_page.render()
        assert "class='live-alert" in html_bad, "worst=critical must render the live-alert block"
        assert "2 critical" in html_bad and "1 degraded" in html_bad, (
            "banner must name the actual counts (AC3-equivalent for /status)")
        assert "probe_token" in html_bad and "token rejected" in html_bad, (
            "banner must list the specific open condition, not a generic message (AC4-equivalent)")
        # AC7: the pre-existing component banner must reflect ITS OWN `overall` ("down" here),
        # not be overwritten or dragged along by live_alerts' independent "critical".
        assert "banner bad" in html_bad, (
            "the older per-component banner must still render its own overall=down state "
            "independently of live_alerts")
    finally:
        status_data.snapshot = real_snapshot


def _postflight_dirty_check_catches_a_leaked_absolute_path_write():
    """fleet-kit#78 / nonprofit-atlas#3113 (15+ recurrences): worktree isolation is a `cd`, not
    a sandbox -- it does not stop a tool call that names the shared checkout by its absolute
    path instead of the worktree it was actually placed in. That write lands in $REPO for
    real, uncommitted, where every other concurrently-running member reads and writes.
    Every prior occurrence was found by a human or another pass noticing $REPO dirty, often
    hours later, and rescued by hand -- no automated check existed.

    This asserts the real behavior against a real git repo dirtied exactly the way every
    incident describes (an untracked absolute-path write), not just that the source mentions
    `git status` somewhere -- a clean $REPO must stay silent, and a dirty one must alert
    loudly, attributed to the run that was live when it was found.
    """
    import subprocess
    script_path = ROOT / "scripts" / "postflight_dirty_check.sh"
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@t"],
            ["git", "config", "user.name", "t"],
        ):
            subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, capture_output=True)

        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        member_log = log_dir / "member.log"
        alerts_file = log_dir / "repo_dirty_alerts.log"

        def run_check(label):
            script = (
                "set -uo pipefail\n"
                f'REPO="{repo}"\n'
                f'LOG_DIR="{log_dir}"\n'
                f'log() {{ echo "$*" >> "{member_log}"; }}\n'
                f'. "{script_path}"\n'
                f'check_repo_clean_postflight "{label}"\n'
            )
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
            assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"

        run_check("run-clean")
        assert not alerts_file.exists(), "a clean $REPO alerted anyway"
        assert not member_log.exists() or member_log.read_text().strip() == "", \
            "a clean $REPO logged an alert"

        (repo / "leaked.txt").write_text("leaked content")
        run_check("run-dirty-123")

        assert alerts_file.exists(), "a dirty $REPO produced no alert file at all"
        alerts = alerts_file.read_text()
        assert "run-dirty-123" in alerts, "alert does not attribute the leak to the run"
        assert "leaked.txt" in alerts, "alert does not name the leaked path"
        member_text = member_log.read_text()
        assert "ALERT" in member_text, "no loud alert written to the per-member log"
        assert "leaked.txt" in member_text, "per-member log does not name the leaked path"

        # The SAME leak, still sitting there uncleaned, must not get re-blamed on every later
        # pass that happens to check next -- a real risk once N members share one $REPO.
        member_log.unlink()
        run_check("run-innocent-456")
        alerts_after = alerts_file.read_text()
        assert "run-innocent-456" not in alerts_after, \
            "an unrelated later run got blamed for a leak it didn't cause"
        assert not member_log.exists() or "ALERT" not in member_log.read_text(), \
            "an unrelated later run raised a fresh ALERT for the same stale leak"

        # A genuinely NEW leak (different content) after the repo goes clean again must still
        # alert -- dedup must key off the actual dirt, not just "have we ever seen dirt before".
        subprocess.run(["git", "add", "leaked.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "absorb old leak"], cwd=repo, check=True, capture_output=True)
        member_log.unlink()
        (repo / "second_leak.txt").write_text("different leak")
        run_check("run-dirty-789")
        assert "run-dirty-789" in alerts_file.read_text(), "a fresh, different leak was not alerted"
        assert "ALERT" in member_log.read_text(), "a fresh, different leak raised no ALERT"


def _postflight_dirty_check_alerts_rather_than_hides_a_git_status_failure():
    """A failed `git status` must not be read as "clean" -- that is the exact silent-failure
    mode this check exists to end. Concurrent minions/builders tearing down worktrees against
    the same $REPO at once is precisely when a transient git failure (lock contention, a
    momentarily missing $REPO) is most likely, so treating it as "nothing to report" would
    defeat the feature under its own target scenario.
    """
    import subprocess
    script_path = ROOT / "scripts" / "postflight_dirty_check.sh"
    with tempfile.TemporaryDirectory() as tmp:
        not_a_repo = Path(tmp) / "not-a-repo"
        not_a_repo.mkdir()
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        member_log = log_dir / "member.log"

        script = (
            "set -uo pipefail\n"
            f'REPO="{not_a_repo}"\n'
            f'LOG_DIR="{log_dir}"\n'
            f'log() {{ echo "$*" >> "{member_log}"; }}\n'
            f'. "{script_path}"\n'
            'check_repo_clean_postflight "run-git-broken"\n'
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"

        assert member_log.exists(), "a git-status failure produced no log output at all"
        text = member_log.read_text()
        assert "ALERT" in text, "a git-status failure was silently treated as a clean repo"
        assert (log_dir / "repo_dirty_alerts.log").exists(), \
            "a git-status failure did not reach the shared alerts file"


def _auto_deploy_race_check_detects_the_unrecognized_git_failure():
    """gh#255: on 2026-08-30 05:40 UTC, auto_deploy.cron.log recorded a raw, uncaught git
    failure -- `cannot lock ref`, `Cannot fast-forward to multiple branches`, a merge-conflict
    abort on members/jefe/jefe.md -- that structurally cannot come from auto_deploy.sh's own
    code path (its git fetch/pull are both scoped to exactly one ref). That means some other,
    unidentified process is racing the same checkout, and today it is silent: it exists only
    as raw stderr in a cron-captured log file nobody tails proactively.

    Reproduces this issue's exact log shape fed into the detector and asserts a dedicated
    alert file is written naming the matched text -- and that a clean/normal tick (only
    auto_deploy.sh's own sanctioned lines) produces no alert at all.
    """
    import subprocess
    script_path = ROOT / "scripts" / "auto_deploy_race_check.sh"

    def run(log_dir):
        proc = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True, text=True, timeout=30,
            env={"FLEET_LOG_DIR": str(log_dir), "FLEET_ENV_FILE": "/nonexistent", "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"

    # A clean/normal tick -- only auto_deploy.sh's own sanctioned lines -- must stay silent.
    # Own tempdir: the cron log is append-only in production, and the cursor-based dedup below
    # is exercised by its own dedicated test -- this one only needs to isolate "clean in, no
    # alert out" from "gh#255's shape in, alert out".
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        (log_dir / "auto_deploy.cron.log").write_text("some harmless git fetch chatter\n")
        run(log_dir)
        assert not (log_dir / "auto_deploy_race_alerts.log").exists(), \
            "a clean auto_deploy.cron.log tick produced an alert anyway"

    # gh#255's own reproduced evidence, verbatim in shape.
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        alerts_file = log_dir / "auto_deploy_race_alerts.log"
        (log_dir / "auto_deploy.cron.log").write_text(
            "error: cannot lock ref 'refs/remotes/origin/main': is at abc123 but expected def456\n"
            "fatal: Cannot fast-forward to multiple branches\n"
            "error: Your local changes to the following files would be overwritten by merge:\n"
            "\tmembers/jefe/jefe.md\n"
        )
        (log_dir / "auto_deploy.log").write_text(
            "[2026-08-30 05:49:51 UTC] deploy OK at def456\n"
        )
        run(log_dir)
        assert alerts_file.exists(), "gh#255's own reproduced log shape produced no alert file at all"
        alerts = alerts_file.read_text()
        assert "cannot lock ref" in alerts, "alert does not name the ref-lock race"
        assert "Cannot fast-forward to multiple branches" in alerts, \
            "alert does not name the multi-branch fast-forward failure"
        assert "would be overwritten by merge" in alerts, "alert does not name the merge-conflict abort"
        assert "deploy OK" in alerts, "alert does not cross-reference the next tick's deploy outcome"


def _fixer_check_sh(tmp, stale_prs_json, state_contents=None, state_age_hours=None, env_extra=None):
    """Run the-fixer's check.sh against a stubbed `gh`, return its one stdout line.

    The stub answers the three shapes check.sh asks for: `gh run list` for CI and deploy
    (always green here -- these tests are about the stale-PR path), and `gh pr list` for the
    open-PR sweep, which is fed verbatim from stale_prs_json.
    """
    import os
    import subprocess
    import time

    log_dir = Path(tmp) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    repo = Path(tmp) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    bin_dir = Path(tmp) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    # `gh run list` prints "conclusion sha"; `gh pr list` prints the "num:sha:reason" triples
    # check.sh's own jq expression would have produced. Stubbing at the gh boundary keeps the
    # real dedup/state logic under test instead of reimplementing it.
    (bin_dir / "gh").write_text(
        "#!/bin/bash\n"
        "if [ \"$1\" = \"run\" ]; then echo 'success abc123'; exit 0; fi\n"
        "if [ \"$1\" = \"pr\" ]; then printf '%s' " + repr(stale_prs_json).replace("'", '"') + "; exit 0; fi\n"
        "exit 0\n"
    )
    (bin_dir / "gh").chmod(0o755)

    state = log_dir / "the-fixer.state"
    if state_contents is not None:
        state.write_text(state_contents)
        if state_age_hours is not None:
            old = time.time() - state_age_hours * 3600
            os.utime(state, (old, old))

    env = {
        "FLEET_REPO": str(repo),
        "FLEET_LOG_DIR": str(log_dir),
        "FIXER_STATE_FILE": str(state),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }
    env.update(env_extra or {})
    proc = subprocess.run(
        ["bash", str(ROOT / "members" / "the-fixer" / "check.sh")],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, f"check.sh failed: {proc.stderr.strip()[:300]}"
    return proc.stdout.strip()


def _fixer_dedup_does_not_let_one_stuck_pr_mute_the_batch():
    """nonprofit-atlas, 2026-09-02: the-fixer went blind for 11 hours and reported green.

    The 02:46 UTC pass fired on a four-PR batch and fixed three; #3853 was a merge conflict
    nobody resolved, so its head never moved. The state file stored only that oldest sha, so
    every later pass matched `already-fighting 61a3390` and stopped at Step 1 -- never
    re-listing open PRs. Ten consecutive hourly passes no-op'd while new PRs went red unseen.

    A batch whose membership CHANGED must re-fire: the stuck PR is still stuck, but a
    different PR going red is a new fire and the whole point of fanning out over N independent
    units is that one wedged unit cannot block the others.
    """
    with tempfile.TemporaryDirectory() as tmp:
        # The original batch fires and is recorded.
        first = _fixer_check_sh(tmp, "3853:61a3390:check-failed 3860:1cef138:check-failed ")
        assert first.startswith("FIRE"), f"a fresh stale-PR batch did not fire: {first!r}"

        # #3860 got fixed and dropped out; #3853 is still stuck; #3901 is NEWLY red.
        # This is the exact shape that was silently suppressed in production.
        second = _fixer_check_sh(
            tmp, "3853:61a3390:check-failed 3901:deadbee:check-failed ",
            state_contents=Path(tmp, "logs", "the-fixer.state").read_text(),
        )
        assert second.startswith("FIRE"), (
            "a batch containing a NEWLY red PR was suppressed because one older PR in the "
            f"previous batch is still stuck -- got {second!r}"
        )
        assert "3901" in second, f"re-fire does not name the newly-red PR: {second!r}"


def _fixer_dedup_still_suppresses_an_unchanged_batch():
    """The counterpart guard: dedup must still WORK.

    A batch that is genuinely unchanged tick-over-tick (same PRs, same heads, someone mid-fix)
    must not be re-fought every hour -- that is the whole reason the state file exists, and
    breaking it would make the-fixer re-spend a full fanout's budget on every poll.
    """
    with tempfile.TemporaryDirectory() as tmp:
        batch = "3853:61a3390:check-failed 3860:1cef138:check-failed "
        first = _fixer_check_sh(tmp, batch)
        assert first.startswith("FIRE"), f"fresh batch did not fire: {first!r}"
        second = _fixer_check_sh(
            tmp, batch, state_contents=Path(tmp, "logs", "the-fixer.state").read_text()
        )
        assert second.startswith("green"), (
            f"an unchanged batch re-fired instead of deduping -- got {second!r}"
        )
        assert "already-fighting" in second, f"unexpected green shape: {second!r}"


def _fixer_dedup_expires_so_a_wedge_cannot_last_forever():
    """A dedup with no expiry is how an 11-hour blind spot lasts 11 hours instead of one.

    Even with batch-keying, a batch that never changes -- one stuck PR, nothing else red --
    would suppress indefinitely. After FIXER_DEDUP_MAX_HOURS the same fire must resurface:
    whoever was fixing it finished, gave up, or died, and all three want a fresh look.
    """
    with tempfile.TemporaryDirectory() as tmp:
        batch = "3853:61a3390:check-failed "
        first = _fixer_check_sh(tmp, batch)
        assert first.startswith("FIRE"), f"fresh batch did not fire: {first!r}"
        state_now = Path(tmp, "logs", "the-fixer.state").read_text()

        # Still inside the window: correctly quiet.
        fresh = _fixer_check_sh(tmp, batch, state_contents=state_now, state_age_hours=1)
        assert fresh.startswith("green"), f"deduped fire re-fired only 1h in: {fresh!r}"

        # Past the window: must resurface even though nothing about the batch changed.
        stale = _fixer_check_sh(tmp, batch, state_contents=state_now, state_age_hours=9)
        assert stale.startswith("FIRE"), (
            "an unresolved fire older than FIXER_DEDUP_MAX_HOURS stayed suppressed -- this is "
            f"the permanent-wedge shape the expiry exists to break: {stale!r}"
        )


def _fixer_charter_handles_every_reason_check_sh_emits():
    """gh#287: check.sh grew two new stale-PR reasons and the charter never learned them.

    check.sh classifies a stuck PR into one of five reasons, but the-fixer.md only had
    instructions for three. Live 2026-09-02: PR #3863 came back `no-checks-at-all` in a real
    FIRE batch and its sub-pass had no rule to apply. A reason the charter cannot name is a
    reason the-fixer cannot act on, and the pass is paid for either way.

    Parses the reason literals straight out of check.sh's jq expression so adding a sixth
    reason without a charter rule fails here rather than in production.
    """
    check_sh = (ROOT / "members" / "the-fixer" / "check.sh").read_text()
    charter = (ROOT / "members" / "the-fixer" / "the-fixer.md").read_text()

    reasons = set(re.findall(r'then "([a-z-]+)"|else "([a-z-]+)" end', check_sh))
    flat = {r for pair in reasons for r in pair if r}
    assert len(flat) >= 5, f"expected check.sh to classify at least 5 reasons, parsed {flat!r}"

    missing = sorted(r for r in flat if f"`{r}`" not in charter)
    assert not missing, (
        f"check.sh can emit {missing} but the-fixer.md has no rule naming them -- a sub-pass "
        "dispatched for one of these has no instruction to follow"
    )


def _auto_deploy_race_check_dedups_an_already_recorded_line():
    """AC3/AC5: running the detector twice against a log that already contains one
    previously-recorded matching line must not duplicate the alert -- else every hourly tick
    would re-alert on the SAME race forever, burying the signal a human actually needs (a NEW
    occurrence) in noise from an old, already-seen one.
    """
    import subprocess
    script_path = ROOT / "scripts" / "auto_deploy_race_check.sh"
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        alerts_file = log_dir / "auto_deploy_race_alerts.log"
        (log_dir / "auto_deploy.cron.log").write_text("fatal: cannot lock ref 'refs/remotes/origin/main'\n")

        def run():
            proc = subprocess.run(
                ["bash", str(script_path)],
                capture_output=True, text=True, timeout=30,
                env={"FLEET_LOG_DIR": str(log_dir), "FLEET_ENV_FILE": "/nonexistent", "PATH": "/usr/bin:/bin"},
            )
            assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"

        run()
        assert alerts_file.exists(), "first run against a matching log produced no alert"
        first_run_alerts = alerts_file.read_text()
        assert first_run_alerts.count("cannot lock ref") == 1, "first run itself double-alerted"

        run()  # same file, no new content appended
        second_run_alerts = alerts_file.read_text()
        assert second_run_alerts == first_run_alerts, \
            "re-running against unchanged log content duplicated the alert"

        # A genuinely NEW occurrence appended afterward must still alert -- dedup keys off the
        # actual dirt, not just "have we ever seen dirt before" (scripts/selftest.py:1049's
        # rule, same shape as postflight_dirty_check.sh's own dedup).
        with (log_dir / "auto_deploy.cron.log").open("a") as f:
            f.write("fatal: Cannot fast-forward to multiple branches\n")
        run()
        third_run_alerts = alerts_file.read_text()
        assert third_run_alerts.count("Cannot fast-forward to multiple branches") == 1, \
            "a genuinely new race after the cursor was not alerted"
        assert third_run_alerts.startswith(first_run_alerts), \
            "the earlier, already-recorded alert was rewritten instead of appended to"


def _auto_deploy_race_check_escalates_after_three_consecutive_sanctioned_aborts():
    """gh#275 AC2/AC5: auto_deploy.sh's two sanctioned ABORT guards (dirty working tree,
    diverged HEAD) previously logged one line per tick with no escalation at all -- three real
    occurrences (gh#245, gh#255, gh#275 itself) all self-resolved silently under
    deploy_staleness_check.sh's 4h paging budget, found only by after-the-fact log forensics.

    Feeds the detector a synthetic auto_deploy.log with 3 consecutive sanctioned-ABORT lines
    (one per reason) and no intervening 'deploy OK', and asserts an alert-log line names the
    stuck reason and its consecutive-tick count.
    """
    import subprocess
    script_path = ROOT / "scripts" / "auto_deploy_race_check.sh"

    def run(log_dir):
        proc = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True, text=True, timeout=30,
            env={"FLEET_LOG_DIR": str(log_dir), "FLEET_ENV_FILE": "/nonexistent", "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"

    # Dirty-tree reason, 3 consecutive ticks, no intervening deploy OK.
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        (log_dir / "auto_deploy.log").write_text(
            "[2026-09-02 02:00:00 UTC] ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand.\n"
            "[2026-09-02 02:05:00 UTC] ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand.\n"
            "[2026-09-02 02:10:00 UTC] ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand.\n"
        )
        run(log_dir)
        alerts_file = log_dir / "auto_deploy_race_alerts.log"
        assert alerts_file.exists(), "3 consecutive dirty-tree ABORTs produced no escalation alert"
        alerts = alerts_file.read_text()
        assert "working tree dirty" in alerts, "alert does not name the stuck reason"
        assert "3 consecutive ticks" in alerts, "alert does not name the consecutive-tick count"

        # A 4th tick past the threshold, same unresolved streak, must NOT re-alert -- else a
        # host stuck for hours would repage every 5 minutes instead of once per streak.
        with (log_dir / "auto_deploy.log").open("a") as f:
            f.write("[2026-09-02 02:15:00 UTC] ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand.\n")
        run(log_dir)
        assert alerts_file.read_text().count("working tree dirty has recurred") == 1, \
            "an already-alerted streak re-alerted on a later tick instead of staying quiet until it resolves"

    # Diverged-HEAD reason, independent counter, same threshold.
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        (log_dir / "auto_deploy.log").write_text(
            "[2026-09-02 02:35:02 UTC] ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand.\n"
            "[2026-09-02 02:40:03 UTC] ABORT: local HEAD is not an ancestor of origin/main -- host checkout has diverged. Resolve by hand, not auto-merged.\n"
            "[2026-09-02 02:45:03 UTC] ABORT: local HEAD is not an ancestor of origin/main -- host checkout has diverged. Resolve by hand, not auto-merged.\n"
            "[2026-09-02 02:50:03 UTC] ABORT: local HEAD is not an ancestor of origin/main -- host checkout has diverged. Resolve by hand, not auto-merged.\n"
        )
        run(log_dir)
        alerts_file = log_dir / "auto_deploy_race_alerts.log"
        assert alerts_file.exists(), "3 consecutive diverged-HEAD ABORTs produced no escalation alert"
        alerts = alerts_file.read_text()
        assert "local HEAD is not an ancestor" in alerts, "alert does not name the diverged-HEAD reason"
        # The single leading dirty-tree line must not itself have escalated (only 1 tick, below
        # threshold) -- independent counters, not a shared one.
        assert "working tree dirty has recurred" not in alerts, \
            "the diverged-HEAD counter bled into the dirty-tree counter (or vice versa) -- they must be independent"


def _auto_deploy_race_check_does_not_alert_on_a_self_resolving_sanctioned_abort():
    """gh#275 AC3: a single sanctioned ABORT tick, or a short streak followed by a successful
    deploy, is the NORMAL, expected transient case the guards were built to tolerate (gh#245's
    and gh#255's own timelines had this shape at a finer grain) -- it must not fire an alert.
    """
    import subprocess
    script_path = ROOT / "scripts" / "auto_deploy_race_check.sh"

    def run(log_dir):
        proc = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True, text=True, timeout=30,
            env={"FLEET_LOG_DIR": str(log_dir), "FLEET_ENV_FILE": "/nonexistent", "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"

    # A single sanctioned ABORT tick -- well below threshold.
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        (log_dir / "auto_deploy.log").write_text(
            "[2026-09-02 02:00:00 UTC] ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand.\n"
        )
        run(log_dir)
        assert not (log_dir / "auto_deploy_race_alerts.log").exists(), \
            "a single sanctioned ABORT tick fired an alert -- this is the normal transient case"

    # Two consecutive sanctioned ABORTs followed by a successful deploy -- self-resolves inside
    # the 3-tick threshold, same shape as #245/#255's own finer-grained timelines.
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        (log_dir / "auto_deploy.log").write_text(
            "[2026-09-02 02:00:00 UTC] ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand.\n"
            "[2026-09-02 02:05:00 UTC] ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand.\n"
            "[2026-09-02 02:10:00 UTC] deploy OK at abc123\n"
        )
        run(log_dir)
        assert not (log_dir / "auto_deploy_race_alerts.log").exists(), \
            "a sanctioned-ABORT streak that self-resolved via a successful deploy fired an alert anyway"

    # 3+ consecutive ABORTs followed by a success: the streak crossed threshold but then
    # resolved before this tick ran -- must not alert on a now-resolved streak.
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        (log_dir / "auto_deploy.log").write_text(
            "[2026-09-02 02:00:00 UTC] ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand.\n"
            "[2026-09-02 02:05:00 UTC] ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand.\n"
            "[2026-09-02 02:10:00 UTC] ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand.\n"
            "[2026-09-02 02:15:00 UTC] ABORT: working tree dirty -- refusing to pull over local changes. Resolve by hand.\n"
            "[2026-09-02 02:20:00 UTC] deploy OK at abc123\n"
        )
        run(log_dir)
        assert not (log_dir / "auto_deploy_race_alerts.log").exists(), \
            "a streak that crossed threshold but resolved before this tick ran still alerted"


# Table-driven replacement for the "_X_is_actually_scheduled" pattern (gh#378): four
# near-identical hand-written guards each asserted one hardcoded crontab substring, and every
# one of them was written only AFTER an incident where the matching script shipped with no
# crontab line and silently did nothing until a human or a nerd pass noticed
# (gh#154/#171/#249/#255/#324/#346, and #376 was about to add a 5th bespoke copy instead of
# closing the class). PR#375 (2026-09-04, gh#278-class) proved this exact shape works for the
# gh-api-timeout class -- one table walked by one generalized check, instead of one bespoke
# function per incident. Porting it here: the next incident-response script gets protection by
# adding one table row, not by a future pass hand-writing a 5th/6th near-duplicate function.
_ENTRYPOINT_SCHEDULED_SCRIPTS = (
    # (human label, exact substring expected in entrypoint.sh's crontab, originating issue)
    ("auto_deploy_race_check.sh", "bash /fleet-kit/scripts/auto_deploy_race_check.sh", "gh#255"),
    ("self_improve_score.sh", "self_improve_score.sh", "gh#196-adjacent"),
    ("deploy_staleness_check.sh", "deploy_staleness_check.sh", "gh#201"),
    ("lane_kpi.py", "python3 /fleet-kit/scripts/lane_kpi.py record", "gh#324"),
    ("git_pull_guard.sh", "bash /fleet-kit/scripts/git_pull_guard.sh", "gh#68"),
)


def _every_entrypoint_scheduled_script_is_actually_scheduled():
    """dumbledore's own entrypoint.sh charter is 'a script existing is not the same as a
    script running' -- this is that assertion, checked once for every script this table
    knows about instead of once per bespoke function. A future incident-response script that
    ships inside the container joins this table as a new row; it does not need a new function.
    """
    entry = (Path(__file__).parent.parent / "entrypoint.sh").read_text()
    missing = [
        f"{label} ({issue}): expected {substring!r} in entrypoint.sh's crontab"
        for label, substring, issue in _ENTRYPOINT_SCHEDULED_SCRIPTS
        if substring not in entry
    ]
    assert not missing, (
        "script(s) with no line in entrypoint.sh's crontab -- they will never run:\n"
        + "\n".join(missing))


def _run_member_logs_critical_when_postflight_dirty_check_fails_to_source():
    """gh#183: /fleet-kit is a vendored copy that only refreshes via auto_deploy.sh (#140, no
    scheduler entry). A merged fix to postflight_dirty_check.sh can be absent there even though
    `main` already has it -- and under `set -uo pipefail` (no -e), a plain `.` on a missing file
    used to no-op silently: check_repo_clean_postflight was simply never defined, and the
    worktree-leak safety net (#78) vanished with no trace. Checked at BOTH isolated-worktree
    call sites (run_member.sh's generic member path, worktree_builder.sh's dedicated builder
    path -- same pairing _run_member_and_builder_check_repo_before_removing_the_worktree already
    checks for the postflight CALL, this checks the postflight SOURCE). Extracts the REAL guard
    block out of each script (not a reimplementation) and proves both failure shapes -- the
    source itself failing, and it "succeeding" while the function still ends up undefined --
    log a line containing CRITICAL, and that a healthy source stays silent.
    """
    import subprocess

    start_marker = 'if ! { . "$KIT_DIR/scripts/postflight_dirty_check.sh"; }'

    def extract_guard(script_name):
        src = (ROOT / "scripts" / script_name).read_text()
        assert start_marker in src, \
            f"{script_name} no longer guards its postflight_dirty_check.sh source -- did the gh#183 fix regress?"
        i = src.index(start_marker)
        j = src.index("\nfi\n", i) + len("\nfi")
        snippet = src[i:j]
        assert "CRITICAL" in snippet, \
            f"{script_name}'s postflight-source guard no longer logs CRITICAL on failure"
        return snippet

    def run_guard(guard_snippet, kit_dir, tmp):
        log_file = Path(tmp) / "member.log"
        repo_dir = Path(tmp) / "repo"
        repo_dir.mkdir(exist_ok=True)
        script = (
            f'KIT_DIR="{kit_dir}"\n'
            f'LOG="{log_file}"\n'
            f'LOG_DIR="{tmp}"\n'
            f'REPO="{repo_dir}"\n'
            f'log() {{ echo "$*" >> "{log_file}"; }}\n'
            f"{guard_snippet}\n"
            'check_repo_clean_postflight "test-run"\n'
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"guard snippet itself failed: {proc.stderr.strip()[:300]}"
        return log_file.read_text() if log_file.exists() else ""

    for script_name in ("run_member.sh", "worktree_builder.sh"):
        guard_snippet = extract_guard(script_name)

        with tempfile.TemporaryDirectory() as tmp:
            # Stale vendored copy: the file plain doesn't exist at $KIT_DIR/scripts/.
            missing_dir = Path(tmp) / "missing"
            (missing_dir / "scripts").mkdir(parents=True)
            text = run_guard(guard_snippet, missing_dir, tmp)
            assert "CRITICAL" in text, \
                f"{script_name}: a missing postflight_dirty_check.sh produced no CRITICAL log line"
            assert "DISABLED" in text or "SKIPPED" in text, \
                f"{script_name}: the CRITICAL line doesn't say what it costs"

        with tempfile.TemporaryDirectory() as tmp:
            # Healthy vendored copy: the real file is present and defines the function -- must
            # stay quiet, this guard exists for the ABSENCE case only (gh#183's own non-goal).
            healthy_dir = Path(tmp) / "healthy"
            (healthy_dir / "scripts").mkdir(parents=True)
            real = (ROOT / "scripts" / "postflight_dirty_check.sh").read_text()
            (healthy_dir / "scripts" / "postflight_dirty_check.sh").write_text(real)
            text = run_guard(guard_snippet, healthy_dir, tmp)
            assert "CRITICAL" not in text, \
                f"{script_name}: a present, working postflight_dirty_check.sh still logged CRITICAL -- false alarm"


def _run_member_rejects_a_non_numeric_item():
    """--item flows unsanitized into RUN_ID, the worktree branch name, and (fleet-kit#78) the
    postflight dirty-check's alert log -- validated as a plain issue number so a stray
    character can't forge extra lines into what's meant to be a trustworthy cross-member
    incident feed, or produce a git ref name `worktree add -b` then rejects outright.
    """
    import os
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, FLEET_REPO=tmp, FLEET_LOG_DIR=tmp, FLEET_ENV_FILE="/nonexistent")
        proc = subprocess.run(
            ["bash", str(ROOT / "scripts" / "run_member.sh"), "minion", "--item", "123x"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        assert proc.returncode == 2, \
            f"a non-numeric --item did not exit 2: rc={proc.returncode} stderr={proc.stderr[:300]}"
        assert "must be a plain issue number" in proc.stderr, \
            "no clear FATAL message on a rejected --item"


def _run_member_and_builder_check_repo_before_removing_the_worktree():
    """Both isolated-worktree callers (the generic member path and the dedicated builder path)
    must wire the postflight check in, and check $REPO BEFORE its worktree is torn down --
    once the worktree is removed, WT_PATH/the branch name (often the only clue tying a leak
    back to which pass caused it) is gone too.

    Checked per call site, not by a global "does this string appear before that string
    anywhere in the file" search -- run_member.sh has TWO call sites (its normal-exit
    cleanup_run_worktree, and its kill-signal record_killed_pass, which never removes a
    worktree at all), and a first-occurrence-anywhere check can pass by coincidence without
    actually verifying either function's own internal ordering.
    """
    def function_body(src, def_line):
        i = src.index(def_line)
        # Bash functions here are all `name() {\n ... \n}` at zero indent -- the closing brace
        # is the first line that is exactly "}" after the opening one.
        j = src.index("\n}", i)
        return src[i:j]

    run_member_src = (ROOT / "scripts" / "run_member.sh").read_text()
    assert "postflight_dirty_check.sh" in run_member_src, \
        "run_member.sh does not source postflight_dirty_check.sh"

    cleanup_body = function_body(run_member_src, "cleanup_run_worktree() {")
    ci = cleanup_body.find("check_repo_clean_postflight")
    cj = cleanup_body.find('git -C "$REPO" worktree remove')
    assert ci != -1, "cleanup_run_worktree never calls check_repo_clean_postflight"
    assert cj != -1 and ci < cj, \
        "cleanup_run_worktree checks $REPO only after removing its own worktree"

    killed_body = function_body(run_member_src, "record_killed_pass() {")
    ki = killed_body.find("check_repo_clean_postflight")
    kj = killed_body.find("trap - TERM INT EXIT")
    assert ki != -1, "record_killed_pass never calls check_repo_clean_postflight"
    assert kj != -1 and ki < kj, \
        "record_killed_pass checks $REPO after clearing its own traps"

    builder_src = (ROOT / "scripts" / "worktree_builder.sh").read_text()
    assert "postflight_dirty_check.sh" in builder_src, \
        "worktree_builder.sh does not source postflight_dirty_check.sh"
    builder_cleanup = function_body(builder_src, "cleanup() {")
    bi = builder_cleanup.find("check_repo_clean_postflight")
    bj = builder_cleanup.find('git -C "$REPO" worktree remove')
    assert bi != -1, "worktree_builder.sh's cleanup never calls check_repo_clean_postflight"
    assert bj != -1 and bi < bj, \
        "worktree_builder.sh's cleanup checks $REPO only after removing its own worktree"


def _deploy_drains_inflight_passes():
    """deploy.sh must not kill agent work to ship code.

    Blue-green protects serving, not work: passes run as children of the blue container's cron,
    so the cutover's `podman stop` kills whatever is mid-pass. The gate defers the deploy while
    a pass is in flight (auto_deploy is a cron poll -- it simply retries next tick), bounded so
    a stuck pass cannot block deploys forever.
    """
    src = (Path(__file__).parent / "deploy.sh").read_text()
    assert "drain_inflight_passes" in src, "no drain gate in deploy.sh"
    i = src.find("drain_inflight_passes()")
    j = src.find("log \"building $IMAGE")
    assert i != -1 and j != -1 and i < j, "drain gate must run BEFORE the build/cutover"
    # pgrep matches full command lines, so a bare `run_member.sh` pattern also matches the shell
    # podman spawns to run the check -- the gate would then see a pass forever and never deploy.
    assert "bash .*run_member[.]sh" in src, "drain pattern would self-match its own wrapper"
    assert "FLEET_DRAIN_MAX_S" in src, "drain has no bound -- a stuck pass blocks deploys forever"


def _one_deploy_at_a_time_and_a_countable_drain():
    """The drain gate must not let deploys stack, and must be able to count to zero.

    Both found live 2026-08-26, minutes after the drain shipped:

    1. auto_deploy polls every 5 min, but a drained deploy can hold for FLEET_DRAIN_MAX_S
       (1800s default). Its state file is written only on SUCCESS, so while one deploy drains,
       LAST_DEPLOYED stays stale and every tick starts ANOTHER deploy -- observed as two
       concurrent deploy.sh, the second heading for a cutover while the first still held. Two
       deploys renaming the same containers is the exact mid-cutover race #92 added rollback for.
    2. `pgrep -c` PRINTS "0" and THEN exits 1 on no match, so `|| echo 0` appended a second
       line and $inflight became "0\\n0" -- non-empty, fails -eq, and the gate logged a defer
       with nothing running. A drain that cannot recognise zero never proceeds on its own.
    """
    auto = (Path(__file__).parent / "auto_deploy.sh").read_text()
    assert "flock" in auto, "auto_deploy has no lock -- 5-min ticks stack during a long drain"
    assert "exec 9>" in auto, "flock needs a held fd or the lock is released immediately"
    assert "flock -n 9" in auto, "lock must be non-blocking -- a queued deploy is a slow duplicate"

    # The flock fd MUST NOT reach `podman run`. Its helpers (conmon, slirp4netns) live as long
    # as the container, so an inherited fd is held for hours -- measured 2026-08-26: conmon
    # held fd 9 with an elapsed time matching the container's StartedAt, every tick hit the
    # lock-taken branch, and deploys were silently dead for ~2 hours. Reproduced in isolation:
    # a child spawned without `9>&-` keeps the lock after its parent exits; with it, the lock
    # frees immediately.
    dep2 = (Path(__file__).parent / "deploy.sh").read_text()
    for line in dep2.splitlines():
        if line.strip().startswith("podman run "):
            assert "9>&-" in line, \
                "podman run inherits the deploy lock fd -- the container will hold it forever"
    # `[ cond ] && cmd` returns NON-ZERO when cond is false. Under `set -e`, that status
    # propagates -- as a function's last statement it becomes the function's exit status, and
    # at top level it aborts the script outright. Measured 2026-08-26: the drain's
    # `[ "$waited" -gt 0 ] && log ...` killed three consecutive deploys on its HEALTHIEST path
    # (waited=0, nothing in flight, drain clears instantly) while the same build ran fine by
    # hand -- the failure looked like a clean cordon/uncordon with no error at all.
    for path in ("deploy.sh", "auto_deploy.sh"):
        src2 = (Path(__file__).parent / path).read_text().splitlines()
        for n, line in enumerate(src2, 1):
            st = line.strip()
            if not st.startswith("[ ") or "&&" not in st or st.endswith("|| true"):
                continue
            nxt = next((l.strip() for l in src2[n:] if l.strip() and not l.strip().startswith("#")), "")
            # Safe only when another statement follows in the same block; if the next thing is
            # a block end, this guard's false status becomes the enclosing exit status.
            if nxt in ("}", "fi", "done", "esac", ""):
                raise AssertionError(
                    f"{path}:{n} `[ ... ] && ...` is the last statement in its block -- "
                    f"a false condition returns non-zero and set -e will abort: {st[:60]}")

    assert "STALE LOCK" in auto, \
        "a wedged lock exits 0 through the quiet path; without an alarm it is invisible"

    dep = (Path(__file__).parent / "deploy.sh").read_text()
    i = dep.find("inflight=\"$(podman exec")
    assert i != -1, "drain no longer counts in-flight passes"
    line = dep[i:dep.find("\n", i)]
    assert "|| echo 0" not in line, "`|| echo 0` on pgrep -c yields '0\\n0', which never equals 0"
    assert "tr -cd '0-9'" in line, "in-flight count is not sanitised to digits"


def _auto_deploy_sh_self_heals_a_content_identical_diverged_head_when_opted_in():
    """gh#278: 3 confirmed occurrences (gh#245, gh#275, gh#278 itself) of the diverged-HEAD ABORT
    were all a squash-merged/rebased branch tip whose TREE already matched origin/main byte-for-
    byte -- not a real divergence, just a stale ref (this repo squash-merges every PR, so a
    stranded branch tip can never become an ancestor of main through any future merge; see
    README's "Why the box silently falls behind"). Every occurrence needed a human SSH session
    running the exact recovery the README already documents as safe by hand.

    Runs the REAL auto_deploy.sh (not a hand-copied snippet) against a real git fixture that
    reproduces the actual root cause, with a stubbed deploy.sh standing in for the host-only
    build/podman steps. Self-heal is opt-in (FLEET_AUTO_DEPLOY_SELF_HEAL) -- gh#278's own PRD
    flagged whether an automated `git reset --hard` on the deploy host is acceptable at all as an
    explicit UNKNOWN needing a human sign-off a build pass can't give itself, so the flag defaults
    OFF and this proves the default-off path is byte-for-byte the old ABORT before proving the
    opted-in self-heal path.
    """
    import os
    import subprocess

    src = (ROOT / "scripts" / "auto_deploy.sh").read_text()
    marker_save_log = 'CALLER_LOG_ENV="${FLEET_LOG_DIR:-}"'
    marker_save_name = 'CALLER_CONTAINER_NAME="${FLEET_CONTAINER_NAME:-}"'
    marker_source = '[ -n "${FLEET_INSTANCE_DIR:-}" ] && [ -f "$FLEET_INSTANCE_DIR/fleet.env" ] && { set -a; . "$FLEET_INSTANCE_DIR/fleet.env"; set +a; } || true'
    marker_restore_log = 'FLEET_LOG_DIR="$CALLER_LOG_ENV"'
    marker_restore_name = 'FLEET_CONTAINER_NAME="$CALLER_CONTAINER_NAME"'
    for m in (marker_save_log, marker_save_name, marker_source, marker_restore_log, marker_restore_name):
        assert m in src, f"auto_deploy.sh's fleet.env save/source/restore sequence changed -- expected: {m!r}"
    i1, i2, i3 = src.index(marker_save_log), src.index(marker_source), src.index(marker_restore_log)
    assert i1 < i2 < i3, "auto_deploy.sh's FLEET_LOG_DIR save/source/restore lines are out of order -- reintroduces gh#196"
    i1n, i3n = src.index(marker_save_name), src.index(marker_restore_name)
    assert i1n < i2 < i3n, "auto_deploy.sh's FLEET_CONTAINER_NAME save/source/restore lines are out of order"

    def git(repo, *args, check=True):
        return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True)

    def make_checkout(tmp, name, origin):
        checkout = tmp / name
        git(tmp, "clone", "-q", str(origin), str(checkout))
        for cmd in (("config", "user.email", "t@t"), ("config", "user.name", "t")):
            git(checkout, *cmd)
        deploy_marker = tmp / f"{name}.deploy_stub_ran"
        return checkout, deploy_marker

    def run_auto_deploy(checkout, tmp, name, self_heal, deploy_marker):
        # The real activation path is the instance's fleet.env, NOT an ambient env var --
        # auto_deploy.sh runs from a crontab line that only ever exports FLEET_INSTANCE_DIR/
        # FLEET_CONTAINER_NAME/FLEET_LOG_DIR (up.sh's AUTO_DEPLOY_LINE); anything else must reach
        # the script through the fleet.env it sources. Also plants fleet.env's own container-
        # scoped FLEET_LOG_DIR (/var/log/fleet-kit) AND a hand-edited-looking FLEET_CONTAINER_NAME
        # to prove the source doesn't clobber either caller-provided value (same gh#196 class
        # deploy.sh already guards against for FLEET_LOG_DIR; FLEET_CONTAINER_NAME matters because
        # INSTANCE_KEY/STATE/LOCKFILE are computed from the cron-exported value BEFORE this source,
        # so a fleet.env override reaching deploy.sh unchecked would deploy under a different
        # container name than the one this tick's own state tracking used).
        instance_dir = tmp / f"{name}.instance"
        instance_dir.mkdir(exist_ok=True)
        env_lines = ["FLEET_LOG_DIR=/var/log/fleet-kit\n", "FLEET_CONTAINER_NAME=hand-edited-other-name\n"]
        if self_heal:
            env_lines.append("FLEET_AUTO_DEPLOY_SELF_HEAL=true\n")
        (instance_dir / "fleet.env").write_text("".join(env_lines))

        env = dict(os.environ)
        # Ambient contamination guard: sourcing fleet.env only ever SETS variables present in
        # the file, it never unsets ones already in the parent shell -- so the self_heal=False
        # case must not rely on the caller's own environment happening to be free of this var.
        env.pop("FLEET_AUTO_DEPLOY_SELF_HEAL", None)
        env["HOME"] = str(tmp / f"{name}.home")
        env["FLEET_LOG_DIR"] = str(tmp / f"{name}.logs")
        env["FLEET_CONTAINER_NAME"] = "test"
        env["FLEET_INSTANCE_DIR"] = str(instance_dir)
        env["DEPLOY_STUB_MARKER"] = str(deploy_marker)
        proc = subprocess.run(["bash", str(checkout / "scripts" / "auto_deploy.sh")], cwd=checkout,
                               env=env, capture_output=True, text=True, timeout=30)
        log_file = tmp / f"{name}.logs" / "auto_deploy.log"
        log_text = log_file.read_text() if log_file.exists() else ""
        return proc, log_text

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        origin = tmp / "origin.git"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))

        seed = tmp / "seed"
        seed.mkdir()
        for cmd in (("init", "-q", "-b", "main"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
            git(seed, *cmd)
        git(seed, "remote", "add", "origin", str(origin))
        (seed / "foo.txt").write_text("v1\n")
        scripts_dir = seed / "scripts"
        scripts_dir.mkdir()
        auto_deploy = scripts_dir / "auto_deploy.sh"
        auto_deploy.write_text((ROOT / "scripts" / "auto_deploy.sh").read_text())
        auto_deploy.chmod(0o755)
        deploy_stub = scripts_dir / "deploy.sh"
        # Records the FLEET_LOG_DIR it actually received -- proves auto_deploy.sh's own
        # save/restore around sourcing fleet.env protects what it hands to deploy.sh, not just
        # its own log() calls (the exact gh#196 clobber class).
        deploy_stub.write_text(
            '#!/bin/bash\necho "DEPLOY STUB OK"\n'
            'printf "%s\\n%s\\n" "$FLEET_LOG_DIR" "$FLEET_CONTAINER_NAME" > "${DEPLOY_STUB_MARKER:?}"\n'
        )
        deploy_stub.chmod(0o755)
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "init")
        git(seed, "push", "-q", "origin", "main")

        # --- Scenario A: content-identical divergence (the real gh#278 root cause) ---
        checkout_a, marker_a = make_checkout(tmp, "case-content-identical", origin)
        # Local "feature branch" commit, never merged as-is -- this is the pre-squash tip.
        (checkout_a / "foo.txt").write_text("v2\n")
        git(checkout_a, "commit", "-aq", "-m", "feat: update foo")
        feature_sha = git(checkout_a, "rev-parse", "HEAD").stdout.strip()
        # The "squash merge" landing on origin/main: same tree, brand new commit/SHA.
        git(seed, "fetch", "-q", "origin", "main")
        git(seed, "reset", "-q", "--hard", "origin/main")
        (seed / "foo.txt").write_text("v2\n")
        git(seed, "commit", "-aq", "-m", "Squash merge feat/thing (#1)")
        git(seed, "push", "-q", "origin", "main")
        origin_main_sha = git(seed, "rev-parse", "HEAD").stdout.strip()
        assert feature_sha != origin_main_sha
        git(checkout_a, "checkout", "-q", "-B", "main", feature_sha)
        git(checkout_a, "fetch", "-q", "origin", "main")

        assert git(checkout_a, "merge-base", "--is-ancestor", "HEAD", "origin/main", check=False).returncode != 0, \
            "fixture is wrong: local HEAD must NOT be an ancestor of origin/main"
        assert git(checkout_a, "diff", "--quiet", "origin/main", check=False).returncode == 0, \
            "fixture is wrong: working tree must be content-identical to origin/main"

        # Default (no opt-in): byte-for-byte the old ABORT, nothing self-heals silently.
        proc, log_text = run_auto_deploy(checkout_a, tmp, "case-content-identical-default", self_heal=False, deploy_marker=marker_a)
        assert proc.returncode == 1, f"default behavior must still exit 1: {proc.stderr[:300]}"
        assert "ABORT: local HEAD is not an ancestor of origin/main -- host checkout has diverged. Resolve by hand, not auto-merged." in log_text, \
            f"ABORT line changed or missing with self-heal opted out: {log_text!r}"
        assert "SELF-HEAL" not in log_text, "self-heal fired while FLEET_AUTO_DEPLOY_SELF_HEAL was unset"
        assert not marker_a.exists(), "deploy.sh ran even though the guard should have ABORTed"
        assert git(checkout_a, "rev-parse", "HEAD").stdout.strip() == feature_sha, \
            "checkout was mutated even though self-heal was opted out"

        # Opted in: self-heals and proceeds into the same-tick deploy.
        proc, log_text = run_auto_deploy(checkout_a, tmp, "case-content-identical-optedin", self_heal=True, deploy_marker=marker_a)
        assert proc.returncode == 0, f"opted-in self-heal must succeed: {proc.stderr[:300]} / log={log_text!r}"
        assert "SELF-HEAL: local HEAD diverged but tree matches origin/main -- resetting onto origin/main" in log_text, \
            f"no self-heal start log line: {log_text!r}"
        assert "SELF-HEAL: reset complete, proceeding into normal deploy" in log_text, \
            f"no self-heal completion log line: {log_text!r}"
        assert git(checkout_a, "rev-parse", "HEAD").stdout.strip() == origin_main_sha, \
            "self-heal did not land the checkout on origin/main"
        assert marker_a.exists(), "self-heal did not proceed into deploy.sh"
        assert "deploy OK" in log_text, "self-heal did not complete the normal deploy path"
        seen_log_dir, seen_container_name = marker_a.read_text().splitlines()
        assert seen_log_dir == str(tmp / "case-content-identical-optedin.logs"), (
            "fleet.env's container-scoped FLEET_LOG_DIR clobbered the caller's host-scoped value "
            "on its way to deploy.sh -- the exact gh#196 clobber class"
        )
        assert seen_container_name == "test", (
            "fleet.env's FLEET_CONTAINER_NAME reached deploy.sh unrestored -- diverges from the "
            f"INSTANCE_KEY/STATE/LOCKFILE this tick already computed from it: {seen_container_name!r}"
        )
        state_file = tmp / "case-content-identical-optedin.home" / ".cache" / "fleet-kit" / "auto_deploy.last_sha.test"
        assert state_file.exists() and state_file.read_text().strip() == origin_main_sha, \
            "successful self-heal deploy did not record the new SHA as last-deployed"

        # --- Scenario B: genuine divergence (real content difference) must still ABORT, even
        # opted in -- self-heal is gated strictly on content-identity, never a relaxation.
        checkout_b, marker_b = make_checkout(tmp, "case-genuine-divergence", origin)
        (checkout_b / "foo.txt").write_text("v2-local-only\n")
        git(checkout_b, "commit", "-aq", "-m", "feat: unrelated local-only change")
        local_only_sha = git(checkout_b, "rev-parse", "HEAD").stdout.strip()
        # origin/main moves again, with genuinely different content.
        (seed / "foo.txt").write_text("v3-on-main\n")
        git(seed, "commit", "-aq", "-m", "unrelated main-only change")
        git(seed, "push", "-q", "origin", "main")
        git(checkout_b, "checkout", "-q", "-B", "main", local_only_sha)
        git(checkout_b, "fetch", "-q", "origin", "main")

        assert git(checkout_b, "merge-base", "--is-ancestor", "HEAD", "origin/main", check=False).returncode != 0, \
            "fixture is wrong: local HEAD must NOT be an ancestor of origin/main"
        assert git(checkout_b, "diff", "--quiet", "origin/main", check=False).returncode != 0, \
            "fixture is wrong: tree must genuinely differ from origin/main"

        proc, log_text = run_auto_deploy(checkout_b, tmp, "case-genuine-divergence", self_heal=True, deploy_marker=marker_b)
        assert proc.returncode == 1, f"genuine divergence must still exit 1 even opted in: {proc.stderr[:300]}"
        assert "ABORT: local HEAD is not an ancestor of origin/main -- host checkout has diverged. Resolve by hand, not auto-merged." in log_text, \
            f"ABORT line changed for a genuine divergence: {log_text!r}"
        assert "SELF-HEAL" not in log_text, "self-heal fired on a genuinely diverged tree -- not gated on content-identity"
        assert not marker_b.exists(), "deploy.sh ran despite a genuine, unresolved divergence"
        assert git(checkout_b, "rev-parse", "HEAD").stdout.strip() == local_only_sha, \
            "checkout was mutated despite a genuine, unresolved divergence"


def _git_pull_guard_self_heals_a_stray_branch_and_leaves_a_normal_pull_unchanged():
    """gh#68 (originally nonprofit-atlas#3130, recurred 3x): a bare `git pull --ff-only`
    against $FLEET_REPO fails hard, and stays failed, once the checked-out branch's history can
    never fast-forward onto origin/main again -- the common cause being a squash-merged PR whose
    branch was deleted upstream. Runs the REAL git_pull_guard.sh against a real git fixture that
    reproduces that exact shape (same fixture idea as auto_deploy.sh's own diverged-HEAD test
    above), and proves a normal fast-forward pull is untouched by the new wrapper.
    """
    import subprocess

    def git(repo, *args, check=True):
        return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True)

    def run_guard(repo):
        return subprocess.run(["bash", str(ROOT / "scripts" / "git_pull_guard.sh"), str(repo)],
                               capture_output=True, text=True, timeout=15)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        origin = tmp / "origin.git"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))

        seed = tmp / "seed"
        seed.mkdir()
        for cmd in (("init", "-q", "-b", "main"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
            git(seed, *cmd)
        git(seed, "remote", "add", "origin", str(origin))
        (seed / "foo.txt").write_text("v1\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "init")
        git(seed, "push", "-q", "origin", "main")

        # --- Scenario A: stray branch, squash-merged and deleted upstream ---
        checkout_a = tmp / "case-stray"
        git(tmp, "clone", "-q", str(origin), str(checkout_a))
        for cmd in (("config", "user.email", "t@t"), ("config", "user.name", "t")):
            git(checkout_a, *cmd)
        git(checkout_a, "checkout", "-q", "-b", "member/some-item")
        (checkout_a / "foo.txt").write_text("v2\n")
        git(checkout_a, "commit", "-aq", "-m", "feat: update foo")
        feature_sha = git(checkout_a, "rev-parse", "HEAD").stdout.strip()
        # The squash merge landing on origin/main: same content, brand new commit/SHA -- the
        # feature branch tip can never become an ancestor of it, and (in reality) GitHub then
        # deletes the now-merged branch upstream.
        git(seed, "fetch", "-q", "origin", "main")
        git(seed, "reset", "-q", "--hard", "origin/main")
        (seed / "foo.txt").write_text("v2\n")
        git(seed, "commit", "-aq", "-m", "Squash merge feat/thing (#1)")
        git(seed, "push", "-q", "origin", "main")
        origin_main_sha = git(seed, "rev-parse", "HEAD").stdout.strip()
        assert feature_sha != origin_main_sha

        assert git(checkout_a, "merge-base", "--is-ancestor", "HEAD", "origin/main", check=False).returncode != 0, \
            "fixture is wrong: local HEAD must NOT be an ancestor of origin/main before the guard runs"

        proc = run_guard(checkout_a)
        assert proc.returncode == 0, f"guard must recover a stray branch, not fail: {proc.stdout}{proc.stderr}"
        assert "SELF-HEAL" in proc.stdout, f"no self-heal line emitted: {proc.stdout!r}"
        assert git(checkout_a, "rev-parse", "HEAD").stdout.strip() == origin_main_sha, \
            "guard did not land the checkout on origin/main's real SHA"
        assert git(checkout_a, "branch", "--show-current").stdout.strip() == "main", \
            "guard did not leave the checkout on a branch named main"

        # Idempotent: a second tick with nothing new must stay quiet, not re-trigger self-heal.
        proc2 = run_guard(checkout_a)
        assert proc2.returncode == 0
        assert "SELF-HEAL" not in proc2.stdout, "guard re-ran self-heal on an already-healed, unchanged checkout"

        # --- Scenario B: a normal fast-forward pull must succeed exactly as before ---
        checkout_b = tmp / "case-normal"
        git(tmp, "clone", "-q", str(origin), str(checkout_b))
        for cmd in (("config", "user.email", "t@t"), ("config", "user.name", "t")):
            git(checkout_b, *cmd)
        before_sha = git(checkout_b, "rev-parse", "HEAD").stdout.strip()
        assert before_sha == origin_main_sha

        (seed / "foo.txt").write_text("v3\n")
        git(seed, "commit", "-aq", "-m", "a normal, linear commit")
        git(seed, "push", "-q", "origin", "main")
        new_main_sha = git(seed, "rev-parse", "HEAD").stdout.strip()

        proc = run_guard(checkout_b)
        assert proc.returncode == 0, f"a normal ff pull must still succeed: {proc.stdout}{proc.stderr}"
        assert "fast-forwarded" in proc.stdout, f"expected a plain fast-forward, not a self-heal: {proc.stdout!r}"
        assert "SELF-HEAL" not in proc.stdout, "self-heal fired on a plain fast-forward pull"
        assert git(checkout_b, "rev-parse", "HEAD").stdout.strip() == new_main_sha, \
            "normal pull did not land on the new upstream SHA"


def _git_pull_guard_serializes_via_a_lock_on_the_git_directory():
    """gh#68/gh#255: the container's gitpull cron and the host's auto_deploy.sh can touch the
    exact same .git directory (when a box self-hosts fleet-kit) with no lock between them at
    all -- confirmed live via matching SHA pairs across gitpull.log and auto_deploy's own race
    errors. git_pull_guard.sh takes a flock on `<repo>/.git/fleet_pull.lock` before touching any
    ref; this proves that lock is real and externally observable -- held from OUTSIDE the
    script, the guard must block until it's released, not race past it.
    """
    import subprocess
    import time

    def git(repo, *args, check=True):
        return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        origin = tmp / "origin.git"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        seed = tmp / "seed"
        seed.mkdir()
        for cmd in (("init", "-q", "-b", "main"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
            git(seed, *cmd)
        git(seed, "remote", "add", "origin", str(origin))
        (seed / "foo.txt").write_text("v1\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "init")
        git(seed, "push", "-q", "origin", "main")

        checkout = tmp / "checkout"
        git(tmp, "clone", "-q", str(origin), str(checkout))
        for cmd in (("config", "user.email", "t@t"), ("config", "user.name", "t")):
            git(checkout, *cmd)

        lockfile = checkout / ".git" / "fleet_pull.lock"
        holder = subprocess.Popen(["bash", "-c", f'exec 8>"{lockfile}"; flock 8; sleep 2'])
        time.sleep(0.3)  # let the holder actually acquire the lock before the guard starts

        start = time.monotonic()
        proc = subprocess.run(["bash", str(ROOT / "scripts" / "git_pull_guard.sh"), str(checkout)],
                               capture_output=True, text=True, timeout=15)
        elapsed = time.monotonic() - start
        holder.wait(timeout=5)

        assert elapsed >= 1.5, (
            f"git_pull_guard.sh did not block on an externally-held lock on {lockfile} "
            f"(elapsed={elapsed:.2f}s) -- gh#255's race is unguarded again")
        assert proc.returncode == 0, f"guard failed once the lock freed: {proc.stdout}{proc.stderr}"

        # auto_deploy.sh locks the identical path formula inside its OWN repo dir -- when
        # $FLEET_REPO and its KIT_DIR really are the same checkout, they lock each other out.
        auto_deploy_src = (ROOT / "scripts" / "auto_deploy.sh").read_text()
        assert 'GIT_LOCKFILE="$KIT_DIR/.git/fleet_pull.lock"' in auto_deploy_src, \
            "auto_deploy.sh no longer locks the same fleet_pull.lock inside its own .git dir"


def _judge_block_pulls_the_pr_out_of_the_queue():
    """fleet-kit#523: main is merge-queue-controlled on both repos, and `fleet-code-review` is
    not (cannot be) a required check there -- the queue only waits for checks that report on
    its own temporary branch, and judge-judy posts on the PR head. So the verdict has to move
    the arm itself: BLOCK dequeues + disarms, approve arms. Otherwise a BLOCK is a comment."""
    src = (ROOT / "members" / "judge-judy" / "judge-judy.sh").read_text()
    assert "dequeuePullRequest" in src and "--disable-auto" in src, \
        "a BLOCK must dequeue the PR and disarm auto-merge -- the queue merges whatever is armed"
    block = src.index("fleet-code-review: BLOCK")
    assert 'unqueue_pr "$PR"' in src[block:], "the BLOCK branch never calls unqueue_pr"
    approve = src.index('"Code review passed')
    assert 'gh pr merge "$PR" --auto' in src[approve:block], "an approve must (re)arm auto-merge"


def _judge_judy_ticks_dont_overlap():
    """A judge-judy cron tick that overlaps a still-running prior tick must not review.

    fleet-kit#194: PR #182 turned judge-judy.sh from a single-PR-per-tick script into a loop
    that drains the whole PR queue up to FLEET_TICK_BUDGET_USD, so a busy tick can legitimately
    run past the 15-minute cron interval -- long enough for the next cron fire to start a
    second, fully concurrent process. Two processes racing pick_pr's read-then-post_status can
    both pick the same head and both post a status; whichever POST lands last wins, silently
    flipping a fresher verdict back to a stale one -- live-confirmed on PR #175 (approve ->
    block from race ordering alone). Same flock-over-a-pidfile pattern as
    auto_deploy.sh/deploy.sh (see _one_deploy_at_a_time_and_a_countable_drain above).
    """
    src = (Path(__file__).parent.parent / "members" / "judge-judy" / "judge-judy.sh").read_text()
    assert "flock" in src, "judge-judy has no lock -- overlapping ticks can double-review a head"
    assert "exec 9>" in src, "flock needs a held fd or the lock is released immediately"
    assert "flock -n 9" in src, "lock must be non-blocking -- a queued tick is a slow duplicate"

    # The lock must be acquired before pick_pr is ever CALLED (not just before it's defined --
    # the function definition itself always precedes its first call site in this file).
    lock_i = src.find('exec 9>"$LOCKFILE"')
    call_j = src.find('pick_pr "$EXPLICIT_PR" "$SKIPPED_THIS_TICK"')
    assert lock_i != -1 and call_j != -1 and lock_i < call_j, \
        "lock must be acquired before pick_pr's first call site in the tick loop"

    # On lock contention the script must exit clean without picking, reviewing, or posting --
    # a non-zero exit here would make a routine overlap look like a cron failure.
    contention_i = src.find("! flock -n 9")
    assert contention_i != -1, "no lock-contention branch"
    tail = src[contention_i:contention_i + 200]
    assert "exit 0" in tail, "lock-held branch must exit 0 -- overlap is expected, not an error"
    assert call_j > contention_i, "pick_pr must not be reachable before the lock check"


def _judge_judy_lock_lives_somewhere_persistent():
    """fleet-kit#207: the single-tick mutex above only mutexes anything if concurrent ticks can
    actually see each other's lockfile.

    $HOME is the per-pass ephemeral container/worktree, so a lockfile under $HOME/.cache can
    only ever contend against itself inside that same container -- it can never block a
    concurrent tick running in a different container/worktree, which is exactly how cron ticks
    and the blue/green deploy cutover both spawn processes here. Live-confirmed: a fresh 3-way
    pass-start collision reproduced on PR #175 even after the flock fix (#200) was deployed, and
    "another judge-judy tick still holds" never once fired across 106 pass-start events (~25h)
    of log history. Same failure class as gh#215's check.sh fix (PR #216): default state onto
    $FLEET_LOG_DIR, the confirmed cross-pass-persistent path.
    """
    src = (Path(__file__).parent.parent / "members" / "judge-judy" / "judge-judy.sh").read_text()
    lock_line = next(line for line in src.splitlines() if line.strip().startswith("LOCKFILE="))
    assert "$HOME" not in lock_line, \
        f"LOCKFILE must not default onto ephemeral $HOME: {lock_line!r}"
    assert "LOG_DIR" in lock_line, \
        f"LOCKFILE should live under the persistent LOG_DIR, not a fresh ad-hoc path: {lock_line!r}"


def _judge_judy_strikes_are_scoped_by_head_and_leave_diagnosable_evidence():
    """gh#221: a parse-strike used to vanish with no evidence, and the strike count itself was
    never proven to be scoped to the head it fired at.

    Three PRs (fleet-kit#182, #184, #219) hit consecutive unparseable/empty reviewer output and
    got a hard, merge-blocking `state=error` -- #182 and #184 later merged (most likely via a
    follow-up push producing a new head), #219 sat live-blocked with no follow-up commit and no
    way to inspect what the model had actually returned, since `$OUT_FILE` is a `mktemp` file
    judge-judy.sh's own `cleanup_pass` deletes every iteration.

    This asserts, statically, the two properties #221's PRD makes acceptance criteria on:
    1. `STRIKE_FILE`'s key already includes `$HEAD_SHA` -- so a genuinely new head (a follow-up
       push) can never inherit a stale strike count from an old sha. This is the assertion
       AC3 asks for explicitly: it was implied by the existing code path but never checked by a
       test.
    2. Every strike (not only the one that trips `state=error`) copies the raw model output to
       a durable, non-tmp location UNDER `$STRIKE_DIR` -- and does so BEFORE `cleanup_pass` (the
       function that deletes `$OUT_FILE`) is ever called on that same iteration -- so a
       live-blocked PR like #219 always leaves something to diagnose.
    """
    src = (Path(__file__).parent.parent / "members" / "judge-judy" / "judge-judy.sh").read_text()

    # AC3: the strike file is scoped by BOTH pr and head sha, so a new push (new $HEAD_SHA)
    # starts its own key and cannot inherit an old head's strike count.
    assert 'STRIKE_FILE="$STRIKE_DIR/pr-${PR}-${HEAD_SHA}.strikes"' in src, \
        "STRIKE_FILE is no longer keyed by pr-<PR>-<HEAD_SHA> -- a new head could inherit a stale strike count"

    # AC1: on every strike, the raw output is captured to a durable path under STRIKE_DIR
    # (never under the tmp dir cleanup_pass empties), keyed by pr+head so it doesn't collide
    # across PRs or heads.
    assert 'RAW_CAPTURE="$STRIKE_DIR/pr-${PR}-${HEAD_SHA}' in src, \
        "no durable, pr+head-keyed raw-output capture path on a parse strike"
    assert 'cp "$OUT_FILE" "$RAW_CAPTURE"' in src, \
        "a strike no longer copies the raw $OUT_FILE content anywhere durable"

    # The capture must happen INSIDE the unparseable-verdict branch, strictly before
    # cleanup_pass is invoked for that same iteration -- capturing after cleanup would copy a
    # file that's already gone.
    strike_branch = src.index('if [ -z "$VERDICT" ]; then')
    capture_i = src.index('cp "$OUT_FILE" "$RAW_CAPTURE"', strike_branch)
    cleanup_i = src.index("cleanup_pass", capture_i)
    assert strike_branch < capture_i < cleanup_i, \
        "raw-output capture does not run, inside the strike branch, before cleanup_pass deletes $OUT_FILE"

    # AC2: the human-facing side (state=error) must point at where the capture lives, not just
    # log it -- a PR comment has no length limit, unlike post_status's 139-char description.
    error_branch = src.index('post_status "$HEAD_SHA" "error"', strike_branch)
    error_window = src[error_branch:error_branch + 900]
    assert "RAW_CAPTURE" in error_window, \
        "state=error path does not reference the raw-output capture path at all"
    assert "gh pr comment" in error_window, \
        "state=error has no PR comment pointing a human at the captured raw output"


def _board_github_file_item_can_add_a_priority_label():
    """gh#5: judge-judy's own fix-item filing needs the filed issue to carry
    fleet:priority-high, not just fleet:backlog -- build_file_cmd's pure-builder contract
    (unit-tested, never executed) is the right place to pin that, same pattern as the
    existing lane-label test would be if one existed.
    """
    import board_github

    cmd = board_github.build_file_cmd("fix: PR #9 failed code review", "the findings text",
                                       priority="high")
    assert cmd[:2] == ["gh", "issue"], f"not a gh issue create command: {cmd}"
    label_arg = cmd[cmd.index("--label") + 1]
    labels = label_arg.split(",")
    assert "fleet:backlog" in labels, f"fleet:backlog missing from filed labels: {labels}"
    assert "fleet:priority-high" in labels, f"fleet:priority-high missing from filed labels: {labels}"
    body_arg = cmd[cmd.index("--body") + 1]
    assert body_arg == "the findings text", "findings text did not make it into the issue body"

    # No priority requested -> no priority label, and lane still composes independently.
    no_priority = board_github.build_file_cmd("t", "b")
    assert "fleet:priority-high" not in no_priority[no_priority.index("--label") + 1]
    with_lane = board_github.build_file_cmd("t", "b", lane="frontend", priority="high")
    lane_labels = with_lane[with_lane.index("--label") + 1].split(",")
    assert "fleet:lane:frontend" in lane_labels and "fleet:priority-high" in lane_labels, \
        f"lane and priority labels must compose, not clobber each other: {lane_labels}"


def _judge_judy_files_a_fix_item_on_block():
    """gh#5: "nothing repairs a PR after judge-judy fails it" -- a VERDICT: block used to end
    at a commit status + PR comment, with no code path ever consuming that verdict again.
    Reif's decision (quoted on gh#5): don't build a dedicated fix persona, file a P1 backlog
    item off the block path instead so gru's normal build lane picks it up.

    Static assertions, same style as the other judge-judy checks above: the filing call must
    (1) exist inside the block branch, after the failure status is posted (findings-and-status
    land before the fix item, matching this script's own comment-first ordering discipline),
    (2) pass $FINDINGS through to the filed issue's body, (3) request the priority-high label,
    and (4) never be allowed to fail the tick -- board_github.py is a best-effort `||` step,
    not a `set -e` hard dependency.
    """
    src = (Path(__file__).parent.parent / "members" / "judge-judy" / "judge-judy.sh").read_text()

    block_branch = src.index('if [ "$VERDICT" = "VERDICT: approve" ]')
    status_i = src.index('post_status "$HEAD_SHA" "failure"', block_branch)
    file_i = src.index("board_github.py", status_i)
    report_i = src.index('report_run "$PR" "$HEAD_SHA"', status_i)
    assert status_i < file_i < report_i, \
        "fix-item filing must run in the block branch, after the failure status, before report_run"

    window = src[file_i - 400:file_i + 400]
    assert '"$FIX_BODY"' in window or "FINDINGS" in window, \
        "filed issue body has no path back to $FINDINGS"
    assert "--priority high" in window or "--priority" in window, \
        "fix item is filed with no --priority flag -- gh#5 AC2 needs fleet:priority-high, not just backlog"
    assert "priority high" in src, "no 'high' priority requested anywhere for the filed fix item"

    # Best-effort: a filing failure must warn and move on, never take the tick down with it.
    file_line = next(line for line in src.splitlines() if "board_github.py" in line and "file " in line)
    tail = src[src.index(file_line):src.index(file_line) + 400]
    assert "||" in tail and "WARN" in tail, \
        "fix-item filing has no || WARN fallback -- a filing failure would crash the tick instead of logging"


def _marie_sweeps_the_whole_backlog_not_just_the_new():
    """marie must re-judge the OLD backlog, not only what changed since last pass.

    Two gaps found live 2026-08-26, both the same shape -- a rule that only ever applies to
    items a pass happens to touch, so the pre-existing backlog is never revisited:

    1. Part C2 scores "every item that gets a priority label", which in practice means only
       items touched this pass. Measured: 54 of 79 open issues carried a priority label and NO
       complexity label, and the count was drifting UP. Since gru packs each hour BY
       complexity, an unscored issue is invisible to the packer -- the backlog was quietly
       starving the schedule, and a human was running marie ad-hoc to compensate.
    2. Part B's four cruft tests are all mechanical (fixed / duplicate / obsolete / superseded)
       -- every one asks whether an item was DONE. None asks whether it is still WANTED, even
       though the mandate already forbids open items that contradict the repo's north star and
       Part 0 re-reads that north star fresh every pass BECAUSE it changes.

    The forced TodoWrite list is what a pass actually executes, so a part that is not on it is
    a part that gets skipped -- that list must name every part the charter defines.
    """
    charter = (Path(__file__).parent.parent / "members" / "marie" / "marie.md").read_text()
    assert "## Part C3" in charter, "no complexity backfill -- the old backlog stays unsized"
    assert "Off-vision" in charter, "Part B has no vision-drift test; all four others are mechanical"
    assert "oldest first" in charter.lower(), "sweep is not ordered oldest-first"
    # Closing on vibes is the expensive failure here: a wrong close destroys signal nobody has
    # seen yet, which is strictly worse than leaving a stale issue open.
    assert "named conflict" in charter.lower() or "NAMED conflict" in charter, \
        "off-vision close has no evidence bar"

    todo = charter[charter.find("call TodoWrite"):charter.find("## Part A")]
    for part in ("Part A", "Part B", "Part C", "Part C3", "Part D"):
        assert part in todo, f"{part} missing from the forced checklist -- a pass will skip it"
    import re
    m = re.search(r"exactly these (\d+) items", charter)
    assert m, "checklist count not stated"
    stated = int(m.group(1))
    numbered = len(re.findall(r"^\d+\. Part |^\d+\. Write the report", todo, re.M))
    assert stated == numbered, f"checklist says {stated} items but lists {numbered}"


def _deploy_sh_host_log_dir_survives_sourcing_the_instances_container_scoped_fleet_env():
    """Real production incident, 2026-08-29: deploy.sh sources the instance's fleet.env
    (needed for FLEET_ACCOUNTS/FLEET_REPO_URL), but that file's FLEET_LOG_DIR is meant for
    the CONTAINER (/var/log/fleet-kit -- what run_member.sh and every member see once
    running inside), while deploy.sh itself runs on the HOST. Sourcing it unguarded let
    fleet.env's container-scoped value silently overwrite whatever the caller (auto_deploy.sh,
    a human, cron) had already exported, so `mkdir -p "$LOG_DIR"` tried to create
    /var/log/fleet-kit ON THE HOST -- root:syslog-owned, not writable by the operator account.
    Both fleet instances' auto_deploy.sh failed at deploy.sh's very first real line, every
    5-minute tick, the moment main actually moved for the first time in a while (104
    consecutive failures observed before this was caught and fixed).

    Proves the actual save/restore lines from deploy.sh (extracted by content, not
    hand-copied) leave a caller-provided FLEET_LOG_DIR untouched by the instance's fleet.env.
    """
    import tempfile
    from pathlib import Path

    src = (HERE / "deploy.sh").read_text()
    marker_save = 'CALLER_LOG_ENV="${FLEET_LOG_DIR:-}"'
    marker_source = '[ -f "$INSTANCE_DIR/fleet.env" ] && { set -a; . "$INSTANCE_DIR/fleet.env"; set +a; } || true'
    marker_restore = 'FLEET_LOG_DIR="$CALLER_LOG_ENV"'
    for m in (marker_save, marker_source, marker_restore):
        assert m in src, f"deploy.sh's save/source/restore sequence changed -- expected to find: {m!r}"
    # The three lines must appear in THIS order (save, then source, then restore) -- any other
    # order reintroduces the clobber.
    i1, i2, i3 = src.index(marker_save), src.index(marker_source), src.index(marker_restore)
    assert i1 < i2 < i3, "save/source/restore lines are out of order in deploy.sh"

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "fleet.env").write_text(
            "FLEET_REPO_URL=https://example.invalid/repo.git\n"
            "FLEET_LOG_DIR=/var/log/fleet-kit\n"  # the real, always-present container path
        )
        harness = f"""#!/bin/bash
set -euo pipefail
INSTANCE_DIR="{d}"
{marker_save}
{marker_source}
{marker_restore}
LOG_DIR="${{FLEET_LOG_DIR:-$HOME/fallback-logs}}"
echo "$LOG_DIR"
"""
        harness_path = d / "harness.sh"
        harness_path.write_text(harness)

        import subprocess
        # Case 1: caller already exported a real host path -- must survive the source untouched.
        out = subprocess.run(["bash", str(harness_path)], capture_output=True, text=True,
                              env={"HOME": "/tmp", "FLEET_LOG_DIR": "/home/ubuntu/real-host-logs"})
        assert out.stdout.strip() == "/home/ubuntu/real-host-logs", (
            f"caller's FLEET_LOG_DIR was clobbered by fleet.env's container-scoped value: {out.stdout!r}"
        )

        # Case 2: caller set nothing -- must fall through to the script's own host default,
        # never to fleet.env's /var/log/fleet-kit (which mkdir -p cannot create on the host).
        out = subprocess.run(["bash", str(harness_path)], capture_output=True, text=True,
                              env={"HOME": "/tmp"})
        assert out.stdout.strip() == "/tmp/fallback-logs", (
            f"no caller override still resolved to the container path: {out.stdout!r}"
        )


def _deploy_cordons_then_drains_and_always_uncordons():
    """The drain must stop NEW work, not just wait, and must never leave the fleet off.

    Waiting for zero only terminates if nothing new starts -- and this fleet never idles: gru
    fires hourly and BLOCKS until every minion it spawned finishes, while continuing to spawn
    more. Measured live 2026-08-26: in-flight went 2 -> 5 DURING a drain. The count was rising,
    so the deploy was heading for its 1800s bound to force-kill exactly the work the gate
    exists to protect. Cordon-then-drain (the standard node-rollout shape) is what terminates.

    The dangerous half is the restore: a deploy that dies while cordoned would leave
    FLEET_ENABLED=false and silently stop EVERY member indefinitely -- worse than the killed
    pass this all started with. So uncordon must be on a trap covering every exit path, and
    must also run explicitly before the cutover replaces that trap with its own.
    """
    src = (Path(__file__).parent / "deploy.sh").read_text()
    assert "FLEET_ENABLED=false" in src, "drain does not cordon -- it will wait for a fleet that never idles"
    assert "uncordon_fleet" in src, "no uncordon -- a failed deploy would leave the fleet off"
    assert "trap uncordon_fleet EXIT INT TERM" in src, "uncordon is not on an exit trap"

    # fleet.env is BIND-MOUNTED into the container, and a bind mount follows the INODE. Any
    # edit that writes a new file and renames it over the old one (sed -i, most editors) leaves
    # the container reading the ORIGINAL inode forever. Found live 2026-08-26: the cordon used
    # `sed -i.deploybak`, so the host read FLEET_ENABLED=false while the container still read
    # true -- members kept starting mid-drain (in-flight 3 -> 7 WHILE cordoned) and the deploy
    # log announced a cordon that was never in effect. refresh_container.sh's header documents
    # this same trap for this same file.
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "sed -i" not in code, "cordon uses sed -i -- replaces the inode, so the container never sees it"
    assert "cordon_write" in src, "no inode-preserving writer for fleet.env"

    body = src[src.find("drain_inflight_passes()"):src.find("\ndrain_inflight_passes\n")]
    assert body.count("uncordon_fleet\n            return 0") == 2, \
        "not every drain return path uncordons"
    # bash keeps ONE handler per signal, so the cutover's own trap REPLACES the drain's. That is
    # only safe because the drain uncordons explicitly before execution ever reaches it.
    assert src.find("\ndrain_inflight_passes\n") < src.find("trap cutover_failed"), \
        "cutover trap is set before the drain runs -- it would clobber the uncordon trap"


def _deploy_log_is_durable_regardless_of_caller():
    """deploy.sh's log() must survive the run whether or not auto_deploy.sh is the caller.

    gh#196: auto_deploy.sh only captures deploy.sh's stdout into auto_deploy.log because IT
    redirects the child process (`bash deploy.sh >> "$LOG" 2>&1`) -- a human running deploy.sh
    directly, up.sh, or any future push-based trigger left zero durable record. log() must
    append to its own file under FLEET_LOG_DIR, same convention auto_deploy.sh already uses.
    """
    import subprocess
    src = (ROOT / "scripts" / "deploy.sh").read_text()
    assert "DEPLOY_LOG" in src and ">> \"$DEPLOY_LOG\"" in src, \
        "log() does not append to a durable file -- stdout only, same gap as gh#196"
    # Newline-anchored: a bare `\nLOG_DIR=` search would also match `FLEET_LOG_DIR=` (the
    # save/restore lines added around the fleet.env source, see the ceiling test above) --
    # this must find the LOCAL LOG_DIR assignment specifically, not any line ending in that
    # substring.
    i = src.find("\nLOG_DIR=")
    j = src.find("\nlog() {")
    assert i != -1 and j != -1 and i < j, "log destination must be set up before log() is defined"
    i += 1  # drop the leading newline so `setup` starts at "LOG_DIR=", not mid-blank-line
    setup = src[i:j]
    assert '${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}' in setup, \
        "deploy.sh does not reuse auto_deploy.sh's own FLEET_LOG_DIR convention"

    # A failed deploy must be distinguishable from a success by grepping the log alone, not by
    # a human parsing prose closely -- every failure/rollback log line must say FAILED or ERROR.
    for marker in ("FAILED: green never answered", "FAILED mid-cutover", "FAILED after cutover",
                   "FATAL ERROR: no live"):
        assert marker in src, f"failure path no longer logs a distinguishable line: {marker!r}"

    log_body = src[src.find("log() {"):src.find("\n}", src.find("log() {"))]

    with tempfile.TemporaryDirectory() as tmp:
        script = (
            "set -euo pipefail\n"
            f'export FLEET_LOG_DIR="{tmp}"\n'
            f"{setup}\n{log_body}\n}}\n"
            'log "first run"\n'
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
        deploy_log = Path(tmp) / "deploy.log"
        assert deploy_log.exists(), "log() ran but produced no deploy.log at all"
        assert "first run" in deploy_log.read_text()

        # Second, separate invocation -- simulates a second deploy.sh run, not a second log()
        # call in the same process -- must append, never truncate the first run's entry.
        script2 = script.replace('log "first run"', 'log "second run FAILED"')
        proc2 = subprocess.run(["bash", "-c", script2], capture_output=True, text=True, timeout=30)
        assert proc2.returncode == 0, f"bash failed: {proc2.stderr.strip()[:300]}"
        text = deploy_log.read_text()
        assert "first run" in text and "second run FAILED" in text, \
            "a second run clobbered the first instead of appending"


def _marie_writes_a_prd_and_minion_reads_it():
    """marie is m-PM: she must make an item BUILDABLE, and minion must consume that.

    Part C's Confidence criterion already diagnoses the failure -- a vague issue "wastes a
    minion's whole pass on clarifying-question paralysis" -- and its only response was to rank
    the item lower. Ranking a bad spec lower does not make it buildable; nobody else writes
    one (gru chooses, minion builds), so the spec never got written and minion built from
    whatever prose the original reporter happened to file.

    Two halves, and the first is useless without the second: marie must WRITE the PRD, and
    minion must READ it. A PRD posted where nobody looks is worse than none -- it costs a pass
    and changes nothing.

    Bounded to gru's next-build queue (unclaimed priority-high, cap 5/pass) on purpose: a PRD
    for every open item would eat the pass that keeps the board true, and most of the backlog
    is never built. Honest UNKNOWNs over invention, because a plausible invented requirement
    is a bug that ships, while a named gap is a 30-second fix for a human.
    """
    marie = (Path(__file__).parent.parent / "members" / "marie" / "marie.md").read_text()
    assert "## Part C4" in marie, "marie writes no PRD -- ranking a vague issue lower never fixes it"
    for section in ("## Problem", "## Goal / Non-goals", "## Acceptance criteria"):
        assert section in marie, f"PRD format missing {section}"
    assert "UNKNOWN" in marie, "PRD has no honest-gap escape -- invites invented requirements"
    assert "fleet:prd" in marie, "no label marking an item as spec'd; passes would rewrite PRDs"
    assert "5 per pass" in marie or "Cap: 5" in marie, "PRD writing is unbounded"
    assert "never edit the body" in marie.lower() or "never edit the body" in marie, \
        "PRD must be a comment -- the body is the reporter's record"

    minion = (Path(__file__).parent.parent / "members" / "minion" / "minion.md").read_text()
    assert "fleet:prd" in minion, "minion never checks for a PRD -- marie would write into a void"
    assert "--comments" in minion, "minion reads only the body, so a PRD comment is invisible to it"
    assert "UNKNOWN" in minion, "minion is not told to leave UNKNOWNs alone rather than guess"
    assert "latest" in minion.lower() or "most recent" in minion.lower(), \
        "minion has no rule for picking among multiple PRD-shaped comments on the same issue"

    # The forced checklist is what a pass executes; a part missing from it is a part skipped.
    todo = marie[marie.find("call TodoWrite"):marie.find("## Part A")]
    assert "Part C4" in todo, "Part C4 missing from the forced checklist"
    import re
    m = re.search(r"exactly these (\d+) items", marie)
    stated, listed = int(m.group(1)), len(re.findall(r"^\d+\. ", todo, re.M))
    assert stated == listed, f"checklist says {stated} items but lists {listed}"


def _fixer_catches_the_no_answer_class():
    """A required check that never reports is invisible to a green-or-red sweep.

    The merge gate is binary: it arms on GREEN, alarms on RED. There is a third outcome it was
    never built for -- NEITHER. A workflow that dies before its jobs launch (`startup_failure`)
    posts no check, and one that never triggers posts nothing at all. The PR then sits BLOCKED
    forever: it cannot merge (a required check is missing) and cannot alarm (nothing went red).

    the-fixer's three original shapes each miss it for their own reason: $failed wants
    conclusion == FAILURE (a startup failure says STARTUP_FAILURE); $conflict wants DIRTY (the
    branch merges fine); $wedged wants status != COMPLETED (a startup failure IS completed --
    it completed by dying, and a run that never started is not in the rollup to filter at all).

    Measured live 2026-08-26: 3 of 10 open PRs were non-draft, BLOCKED, with ZERO checks, while
    other PRs the same hour had 3 checks each -- so checks do fire on that repo; those shas
    simply never got a run. Separately 2 of the last 25 workflow runs concluded startup_failure.
    Old jq surfaced 1 PR; new jq surfaced 2 on the same data, the extra one being a real stuck PR.

    Drafts are excluded on purpose -- a draft with no checks is normal (workflows commonly skip
    drafts), so alarming there would fire on every WIP branch.
    """
    src = (Path(__file__).parent.parent / "members" / "the-fixer" / "check.sh").read_text()
    assert "STARTUP_FAILURE" in src, "a run that died before launching is still invisible"
    assert "no-checks-at-all" in src, "a PR with zero checks is still invisible"
    assert "check-never-ran" in src, "no reason string distinguishes a never-run check"
    assert "$noanswer" in src and "$noran" in src, "no-answer shapes are not in the select"
    # The zero-check shape MUST exclude drafts and MUST be age-gated, or it fires on every
    # freshly-opened PR before CI has had a chance to post anything.
    i = src.find("$noanswer")
    clause = src[max(0, i - 260):i]
    assert "isDraft" in clause, "zero-check shape does not exclude drafts -- fires on every WIP"
    assert "createdAt" in clause, "zero-check shape is not age-gated -- fires on brand-new PRs"


def _datta_dispatches_and_nerds_analyse():
    """The analysis pair mirrors gru/minion: one measures coverage, the other does the work.

    Ported from the source fleet's 7 lane charters (growth, searchquality, ui, datadog, devops,
    lens, revenue), which ran as ad-hoc cloud routines with no shared runner, no run records,
    and no spawn discipline. Under the gru/minion shape they get all three for free.

    The properties that make it work rather than just exist:

    - datta must NOT analyse. The split is the point -- a dispatcher that also does the work
      stops measuring coverage honestly, because the lane it just examined always looks fresh.
    - nerd needs BOTH halves. A checklist alone only catches failures someone already suffered;
      open-ended exploration is where the unowned problems live.
    - Every lane's KPI ships with its guardrail AND its named cheat. A bare KPI is numerator
      theater -- each of the seven has an obvious gamification (delete the stale metrics and
      freshness hits 100%; ship nothing and deploy success is 100%), so the charter names each
      one explicitly rather than hoping the model does not find it.
    - UNCAPPED, with evidence. "Nothing new" with no evidence line is the exact pass that
      rotted the source fleet: 108 of 211 recorded passes filed ZERO.
    - Access is declared. Two lanes need third-party logins (GSC, GA4, Clarity) that may be
      absent; a nerd must file the gap naming the variable, never invent the number. A
      fabricated metric is worse than a missing one -- the gap gets fixed, the fabrication gets
      ranked and built on.
    """
    import json
    root = Path(__file__).parent.parent
    datta = (root / "members" / "datta" / "datta.md").read_text()
    nerd = (root / "members" / "nerd" / "nerd.md").read_text()

    assert "massive user value" in datta, \
        "datta dispatches by staleness alone -- coverage becomes the goal instead of the means"
    assert "never analyse" in datta.lower() or "never analyse a lane yourself" in datta, \
        "datta does not hold the dispatcher/worker split"
    assert "FLEET_RUN_NOW=1" in datta, "datta cannot spawn a nerd (nerd ships enabled:false)"
    assert "lane=" in datta and "lane=<name>" in nerd, "no lane is handed to the nerd"

    for lane in ("growth", "searchquality", "ui", "datadog", "devops", "lens", "revenue"):
        assert lane in nerd, f"lane {lane} lost in the port"
    assert nerd.count("must not") >= 6, "guardrails missing -- a bare KPI is numerator theater"
    assert "cheat" in nerd.lower(), "the obvious gamification of each KPI is not named"
    assert "UNCAPPED" in nerd, "the file-or-say-why law did not come across"
    assert "unverified" in nerd, "no honest-gap escape; invites fabricated metrics"
    for var in ("GSC_SA_KEY", "GSC_PROPERTY", "GA4_PROPERTY_ID", "CLARITY_API_TOKEN"):
        assert var in nerd, f"{var} not declared, so a nerd cannot tell if it can measure"
    assert "never build the fix" in nerd.lower(), "nerd may wander into minion's lane"

    # growth's checklist was hand-tuned 2026-08-26 against live numbers. The properties that
    # make it act rather than just observe:
    assert "MAXIMIZE INDEXED PAGES" in nerd, "growth lost its stated target"
    # Four different 'indexation' numbers exist and are NOT interchangeable -- comparing across
    # them is the classic wrong finding (98,171 surfaced vs ~27% sampled measure different
    # things), so the charter tabulates all four and their nature.
    for n in ("pages surfaced", "shards fetched", "Page Indexing buckets"):
        assert n in nerd, f"growth does not distinguish '{n}' from the other indexation numbers"
    assert "n=15" in nerd, "sample size not stated -- invites filing a regression on noise"
    # 3.7M discovered-not-indexed is a crawl-budget judgment, NOT a discovery gap (shards are
    # 339/339). A nerd that reads it as discovery files sitemap work that changes nothing.
    assert "NOT a discovery problem" in nerd, "the discovered-not-indexed bucket is misframed"
    # The corpus-growth half needs concrete verified gaps, or it degenerates into 'make more
    # pages' -- which makes discovered-not-indexed worse, not better.
    assert "CONFIRMED MISSING" in nerd and "category" in nerd, "no verified page-type gap named"
    # A deliberate noindex re-filed every pass trains the reader to ignore the lane.
    assert "deliberate" in nerd, "no rule separating legitimate noindex from accidental"

    # Every lane was hand-tuned 2026-08-26 against numbers measured on the live product. The
    # guard is that each stays SUBSTANTIVE: the thin one-liners they replaced described a
    # topic without telling a pass what to actually do, which is how a lane ends up filing
    # "nothing new" forever.
    import re as _re
    for lane in ("growth", "searchquality", "ui", "datadog", "devops", "lens", "revenue"):
        i = nerd.find(f"**{lane}** \u2014")
        assert i != -1, f"{lane} lost its checklist heading"
        j = nerd.find("\n\n**", i + 5)
        body = nerd[i:j if j > 0 else i + 1200]
        assert len(body.split()) >= 90, f"{lane} checklist thinned back to a topic label"
    # The two failure modes every lane shares, stated where the pass will read them.
    assert "clean number that is WRONG is worse" in nerd, "datadog lost the integrity rule"
    assert "signup is not a payment" in nerd, "revenue lost the KPI/guardrail distinction"
    assert "incident, not a finding" in nerd, "devops would file a live incident and move on"
    # A browser now ships; a lane that curls HTML and infers is not doing the job.
    assert "playwright" in nerd, "ui/lens never told to use the browser that ships in the image"

    # A checklist alone only finds failures someone already suffered. The discovery half is
    # where a lane's real work comes from -- and it needs MEMORY, because a one-shot pass that
    # cannot read its own history re-derives the same finding forever and files it again.
    assert "What did I do last time" in nerd, "no pass memory -- nerd re-derives every pass"
    assert "self_critique" in nerd, "past-you already named the next pass's work; unused"
    assert "fleet.db" in nerd, "no mechanism given for reading prior passes"
    assert "outcome IS NOT NULL" in nerd, \
        "killed/declined passes have no outcome; reading them as 'I did nothing' is wrong"
    assert "already shipped" in nerd, "nerd never checks merged PRs -- refiles fixed things"
    assert "massive user value" in nerd, "discovery is framed as breakage-hunting only"
    # The reason the nerds exist, stated where every pass reads it first. Without this a
    # metrics-driven pass optimises the proxy and never asks whether anyone is better off --
    # and every KPI here CAN be moved without creating any user value, which is why each
    # lane's cheat is named. When the KPI and the user disagree, the user is right, and that
    # disagreement is the most valuable thing a pass can report: the instrument has drifted.
    assert "PROXY, never the goal" in nerd, "KPI presented as the goal rather than a proxy"
    assert "who is better off" in nerd, \
        "findings need not state their user value, so 'moves the metric' passes for a reason"
    assert "buildable" in nerd, "no judgment step; unbuildable opportunities file as findings"

    # The tools those steps need must actually exist in the image, or the steps silently no-op.
    dockerfile = (root / "Dockerfile").read_text()
    assert "sqlite3" in dockerfile, "sqlite3 CLI missing -- pass memory queries return nothing"
    assert "google-auth" in dockerfile, "no GSC/GA4 auth lib -- growth/datadog cannot measure"
    # Some numbers exist ONLY in a rendered page -- GSC's Page Indexing report (3.7M
    # discovered-not-indexed, 1.68M noindex) has no API at all. Without a browser that lane
    # files "cannot read, no browser" every pass forever.
    assert "playwright install" in dockerfile, "no browser -- the API-less reports stay unread"
    # apt's chromium on Ubuntu 22.04 is a snap stub that cannot run in a container, and plain
    # `chromium` has no candidate; playwright's own build is the one that actually launches.
    assert "playwright install --with-deps" in dockerfile, \
        "chromium installs but fails at launch without its shared libs"

    # The credential a nerd needs must be MOUNTED, or the browser is useless: the service
    # account lives on the app's box, not the fleet box. Optional by design -- an instance that
    # does not analyse these lanes must be unaffected.
    dep = (root / "scripts" / "deploy.sh").read_text()
    assert "FLEET_ANALYTICS_CREDS_DIR" in dep, "analytics credentials are never mounted"
    assert ":/fleet-kit/.analytics:ro" in dep, "credential mount is not read-only"
    i = dep.find("FLEET_ANALYTICS_CREDS_DIR")
    guard = dep[i:i + 200]
    assert "-d " in guard, "mount is not guarded on the directory existing -- breaks a deploy"
    # pip 22.0.2 on this base predates --break-system-packages and exits 'no such option',
    # failing the whole build. Verified against the real image, not assumed.
    df_code = "\n".join(l for l in dockerfile.splitlines() if not l.lstrip().startswith("#"))
    assert "--break-system-packages" not in df_code, \
        "flag unsupported by this base image's pip (22.0.2) -- breaks the build"

    # nerd must never self-fire: datta decides coverage, so a cron-fired nerd would run a lane
    # nobody chose. Same contract minion has.
    spec = json.loads((root / "members" / "nerd" / "nerd.fleet.json").read_text())
    assert spec["enabled"] is False, "nerd self-fires -- it would run lanes datta never chose"
    assert spec.get("schedule"), "empty schedule fails member_spec validation (found live)"

    # minion must never self-fire either: it has no logic to pick its own item, only to work
    # whatever --item gru hands it. A cron-fired minion would either crash or improvise a claim
    # outside gru's claim-race-safe dispatch path, corrupting the invariant claim hygiene
    # depends on (fleet-kit#155).
    minion_spec = json.loads((root / "members" / "minion" / "minion.fleet.json").read_text())
    assert minion_spec["enabled"] is False, \
        "minion self-fires -- it would improvise a claim outside gru's claim-race-safe dispatch path"
    assert minion_spec.get("schedule"), "empty schedule fails member_spec validation (found live)"


def _nerd_structural_na_marker_wires_to_datta_downrank():
    """gh#451: `datta.md`'s down-rank rule (`datta.md:76-99`, gh#339/PR#441) resets a lane's
    UNEXAMINED score to 0 only if its last 3 `nerd` runs all have an `outcome` starting with the
    literal marker `STRUCTURAL-N/A`. Before this fix, `nerd.md`'s N/A paragraphs (growth,
    searchquality, revenue) only said "state N/A explicitly" in free prose and never told a pass
    to emit that literal marker, so the rule was 100% dormant (confirmed live: `fleet.db` had
    zero `STRUCTURAL-N/A` rows despite 15+ consecutive N/A passes).

    Two halves, since the marker and the rule that consumes it live in different files with no
    shared code path (this is prose read by two different LLM passes, not a function call):

    1. Each of nerd.md's three current N/A paragraphs now instructs emitting the marker.
    2. A synthetic 3-row sequence that matches the marker datta.md's own rule is written
       against (a small model of `datta.md:87-95`'s spec, since that logic has no Python
       module of its own to import) actually resets UNEXAMINED to 0, and a non-unanimous or
       short sequence does not -- the down-rank must never fire as a default or on partial
       evidence, per datta.md's own text.
    """
    root = Path(__file__).parent.parent
    nerd = (root / "members" / "nerd" / "nerd.md").read_text()
    datta = (root / "members" / "datta" / "datta.md").read_text()

    for lane in ("growth", "searchquality", "revenue"):
        # Each of these three lanes has TWO headings: the generic source-fleet checklist
        # (nonprofit-atlas-shaped) earlier in the file, and fleet-kit's own N/A override
        # paragraph later -- rfind gets the fleet-kit-specific one this issue targets.
        heading = nerd.rfind(f"**{lane}** —")
        assert heading != -1, f"{lane} lost its fleet-kit-native N/A paragraph"
        body = nerd[heading:heading + 1600]
        assert "STRUCTURAL-N/A" in body, \
            f"{lane}'s N/A paragraph never tells nerd to emit the marker datta.md keys on (gh#451)"
    assert "startswith(\"STRUCTURAL-N/A\")" in datta or "starts with the literal marker" in datta, \
        "datta.md's down-rank rule text moved/changed -- re-check gh#451's wiring still matches"

    # A minimal model of datta.md:87-95's specified rule: fewer than 3 rows, or the 3 not
    # unanimous, means score UNEXAMINED as normal; only a unanimous 3-row STRUCTURAL-N/A streak
    # resets it to 0. This is the "Python equivalent under test" of a rule that otherwise only
    # exists as prose an LLM dispatcher reads.
    def down_ranked_unexamined(last_3_outcomes, raw_hours):
        if len(last_3_outcomes) < 3:
            return raw_hours
        if all(o.strip().startswith("STRUCTURAL-N/A") for o in last_3_outcomes):
            return 0.0
        return raw_hours

    unanimous = ["STRUCTURAL-N/A: no revenue surface on fleet-kit"] * 3
    assert down_ranked_unexamined(unanimous, 47.0) == 0.0, \
        "3 unanimous STRUCTURAL-N/A rows must reset UNEXAMINED to 0"

    too_few = unanimous[:2]
    assert down_ranked_unexamined(too_few, 47.0) == 47.0, \
        "fewer than 3 rows must never trigger the down-rank"

    broken_streak = ["STRUCTURAL-N/A: still N/A"] * 2 + ["QUIET -- found nothing this pass"]
    assert down_ranked_unexamined(broken_streak, 47.0) == 47.0, \
        "one non-marker row must break the streak, never a partial down-rank"


def _nerd_invalid_lane_rejected_before_lane_work():
    """gh#374: a `lane=<name>` dispatch outside the canonical seven must be rejected BEFORE any
    lane-specific work begins, not discovered only after a full pass ran.

    Measured live 2026-09-04/05: two dispatches (`lane=audience`, `lane=coordination`) used
    names that appear nowhere in nerd.md's own table or this file's
    `_datta_dispatches_and_nerds_analyse` list -- each burned a full nerd pass with no matching
    checklist, no expected credentials, and no code surface, because nothing validated the
    `lane=` value before the charter ran. Runs the real `run_member.sh` end-to-end (same
    stub-free pattern used elsewhere in this file for scripts that exit before touching
    network/gh/claude) against a bogus lane name, and checks:

    1. it exits 0 and writes exactly one runs.jsonl row, so a rejection is a real record, not
       silence,
    2. that row's status is `ok` with a real artifact in its Outcome (never
       `reported_nothing` -- AC2's own bar),
    3. the rejection happened before spec resolution / worktree creation / `claude -p` ever
       ran -- proven by the log never reaching "pass start", which only prints after all of
       that (see run_member.sh's normal-exit path).
    """
    import os
    import subprocess

    tmp = tempfile.mkdtemp()
    log_dir = Path(tmp) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update({
        "FLEET_REPO": str(ROOT),
        "FLEET_LOG_DIR": str(log_dir),
        "FLEET_ENV_FILE": str(Path(tmp) / "nonexistent.env"),
    })
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "run_member.sh"), "nerd",
         "--task", "lane=coordination — prior context that does not belong to this repo"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, (
        f"rejected dispatch must exit 0, got {proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )

    runs_file = log_dir / "runs.jsonl"
    assert runs_file.exists(), f"no runs.jsonl written for a rejected dispatch\nstderr={proc.stderr}"
    lines = [l for l in runs_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 1, f"expected exactly one run record, got {len(lines)}: {lines}"
    rec = json.loads(lines[0])
    assert rec["status"] == "ok", f"rejected dispatch landed status={rec['status']!r}, want ok (AC2)"
    assert "coordination" in (rec.get("outcome") or ""), \
        f"Outcome does not name the rejected lane: {rec.get('outcome')!r}"
    assert rec.get("lane") == "coordination", f"lane column not set: {rec.get('lane')!r}"

    nerd_log = (log_dir / "nerd.log").read_text()
    assert "REJECTED" in nerd_log, "no rejection logged"
    assert "pass start" not in nerd_log, \
        "rejection reached 'pass start' -- lane-specific work began before validation (AC1/AC2)"

    # AC4: the seven real lanes must be completely unaffected -- proven by NOT hitting the
    # rejection path (it falls through to the normal enabled-spec check instead, which for the
    # real, disabled-by-design nerd.fleet.json exits 0 with its own distinct log line).
    proc2 = subprocess.run(
        ["bash", str(ROOT / "scripts" / "run_member.sh"), "nerd",
         "--dry-run", "--task", "lane=growth — normal task"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc2.returncode == 0, f"valid lane broke: rc={proc2.returncode} stderr={proc2.stderr}"
    assert "REJECTED" not in proc2.stdout and "REJECTED" not in proc2.stderr, \
        "a valid canonical lane was rejected -- AC4 behavior change"


def _every_pass_files_a_written_report():
    """A pass costs real money; it owes a memo, not three one-line fields.

    Reif, 2026-08-26: "I want a report after each run, I paid for it after all." A gru pass
    runs 78 turns and $1.53; what came back was Outcome/Evidence/Self-critique -- one line
    each -- and the only alternative was the 170-line raw transcript. Neither is a report.

    `Report:` is the ONE multi-line field in the contract. Every other field is `(.+?)$` by
    construction because `status` keys off them, so a parser that swallowed paragraphs would
    make `Outcome:` unbounded. This one captures everything up to the next contract line, so
    prose survives into runs.jsonl whole.

    Captured, NEVER enforced: a missing report must not change `status`. A pass that did real
    work and skipped the prose is still successful -- making the memo load-bearing would turn
    a formatting slip into a false failure, the exact bug persona_law §10b exists to prevent.
    """
    import run_report
    text = ("Report:\nBOTTOM LINE: nothing shipped.\n\n1. one\n2. two\n\n"
            "WHAT TO IMPROVE: check the meter.\n\n"
            "Outcome: QUIET -- see #3321\nEvidence: maxx_reader.py -> 0.0\n"
            "Self-critique: no observed data\n")
    r = run_report.parse_report(text)
    assert r.get("report"), "the written report is not captured"
    assert "BOTTOM LINE" in r["report"] and "WHAT TO IMPROVE" in r["report"], \
        "report body truncated -- it must survive whole, it is already a distillation"
    # It must STOP at the next contract line, or the parseable fields get swallowed into prose.
    assert "Outcome:" not in r["report"], "report bleeds into the fields status is derived from"
    assert r["outcome"] and r["self_critique"], "capturing the report broke the §10b fields"

    # Absent report must be harmless -- status comes from Outcome:, never from the memo.
    plain = run_report.parse_report("Outcome: did a thing #12\nEvidence: ran it\n")
    assert plain.get("report") is None, "a pass with no report should carry None, not junk"
    assert run_report.classify(plain, vision_required=False, exit_code=0) == "ok", \
        "a missing report changed status -- a formatting slip must never fail a real pass"

    # Every member must be TOLD to write one, or the field stays empty forever.
    import glob
    charters = glob.glob(str(Path(__file__).parent.parent / "members" / "*" / "*.md"))
    missing = [Path(c).name for c in charters
               if "Outcome:" in Path(c).read_text() and "10c" not in Path(c).read_text()]
    assert not missing, f"charters know the fields but not the report: {missing}"

    law = (Path(__file__).parent.parent / "agents" / "persona_law.md").read_text()
    assert "## 10c" in law, "the report contract is not in the law every member inherits"

    # And it must reach the DB, or dumbledore/lens can only read reports by grepping raw logs.
    import fleet_db
    assert "report" in fleet_db.SCHEMA, "fleet.db has no report column"
    assert any(c[0] == "report" for c in fleet_db._ADD_COLUMNS), \
        "existing fleet.db files would never gain the column -- history lost"


def _every_scheduled_member_is_actually_on_cron():
    """A member's own spec does NOT put it on cron -- entrypoint.sh's hand-written list does.

    Found live 2026-08-26 by roomba, filed as nonprofit-atlas#3321: datta shipped
    enabled:true with schedule.hourly_at_minute=12 and had run ZERO times since 11:39 that
    morning, because adding a member's spec and charter does not add it to
    /etc/cron.d/fleet-kit. The dashboard read "never run"; nothing else complained. The same
    class already bit this kit once -- the crontab predated run_member.sh and silently ran 2 of
    7 members forever (see entrypoint.sh's own comment).

    So the two lists must agree, and this is the check that makes disagreement loud:
      enabled + scheduled in its spec  =>  a run_member.sh line in entrypoint.sh
    Two documented exceptions, both spawned BY another member rather than by cron:
      minion  -- spawned by gru with --item
      nerd    -- spawned by datta with --task lane=<name>
    Both ship enabled:false precisely so a cron tick can never run one nobody chose.
    """
    import json, glob
    root = Path(__file__).parent.parent
    entry = (root / "entrypoint.sh").read_text()
    spawned_by_a_member = {"minion", "nerd"}
    missing = []
    for f in sorted(glob.glob(str(root / "members" / "*" / "*.fleet.json"))):
        spec = json.loads(Path(f).read_text())
        name = spec["name"]
        if name in spawned_by_a_member or not spec.get("enabled"):
            continue
        if not (spec.get("schedule") or {}):
            continue
        # gru fires through its own fanout wrapper, not a bare run_member.sh line.
        if f"run_member.sh {name}" in entry or f"run_{name}_fanout.sh" in entry:
            continue
        missing.append(name)
    assert not missing, (
        f"enabled+scheduled but never wired into cron, so they will NEVER run: {missing}. "
        "A spec does not schedule a member; entrypoint.sh's crontab does.")


def _fleet_cron_members_gates_entrypoint_crontab():
    """gh#138: fleet.env's own header can declare an instance "judge-judy only", but
    entrypoint.sh's crontab used to be a single hardcoded list installed unconditionally --
    the config file and the crontab disagreed about what the instance was, and nothing caught
    it. FLEET_CRON_MEMBERS is the fix: an optional allowlist entrypoint.sh's crontab builder
    actually consults.

    Extracts the real crontab-generation block out of entrypoint.sh (not a reimplementation)
    and runs it under three scenarios:
      - unset -> falls back to today's full hardcoded list, byte-for-identical
      - a 2-member subset -> the generated crontab contains lines for exactly those two
      - an unknown/misspelled name -> boot fails loudly (non-zero exit, names the bad entry)
    """
    import os
    import subprocess

    entry = (ROOT / "entrypoint.sh").read_text()
    start_marker = "    ALL_CRON_MEMBERS=("
    end_marker = '\n    } > "$CRONTAB"'
    assert start_marker in entry, "entrypoint.sh no longer defines ALL_CRON_MEMBERS -- did gh#138's fix regress?"
    i = entry.index(start_marker)
    j = entry.index(end_marker, i) + len(end_marker)
    snippet = entry[i:j]

    def run_block(tmp, fleet_cron_members=None):
        crontab_path = Path(tmp) / "crontab"
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir(exist_ok=True)
        header = (
            f'set -euo pipefail\n'
            f'FLEET_REPO="{tmp}"\n'
            f'TOKEN_FILE="{tmp}/token"\n'
            f'LOG_DIR="{log_dir}"\n'
            f'FLEET_GRU_CADENCE="*"\n'
            f'PUBLIC_URL=""\n'
            f'PUBLIC_PATH_URL=""\n'
            f'FLEET_VIEW_PORT=8420\n'
            f'CRONTAB="{crontab_path}"\n'
        )
        script = header + snippet.replace('    CRONTAB=/etc/cron.d/fleet-kit\n', '')
        env = dict(os.environ)
        if fleet_cron_members is not None:
            env["FLEET_CRON_MEMBERS"] = fleet_cron_members
        else:
            env.pop("FLEET_CRON_MEMBERS", None)
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
        crontab_text = crontab_path.read_text() if crontab_path.exists() else ""
        return proc, crontab_text

    known_members = ("the-fixer", "judge-judy", "gru", "jefe", "roomba", "marie", "datta",
                      "dumbledore", "sentry")

    with tempfile.TemporaryDirectory() as tmp:
        proc, crontab_text = run_block(tmp)
        assert proc.returncode == 0, f"unset FLEET_CRON_MEMBERS should not fail boot: {proc.stderr[:500]}"
        for name in known_members:
            marker = f"run_gru_fanout.sh" if name == "gru" else f"run_member.sh {name}"
            assert marker in crontab_text, \
                f"FLEET_CRON_MEMBERS unset dropped {name!r} from the generated crontab -- not byte-identical to today's behavior"

    with tempfile.TemporaryDirectory() as tmp:
        proc, crontab_text = run_block(tmp, fleet_cron_members="judge-judy,jefe")
        assert proc.returncode == 0, f"a valid 2-member subset should not fail boot: {proc.stderr[:500]}"
        assert "run_member.sh judge-judy" in crontab_text and "run_member.sh jefe" in crontab_text, \
            "the named subset's own members are missing from the generated crontab"
        for name in known_members:
            if name in ("judge-judy", "jefe"):
                continue
            marker = "run_gru_fanout.sh" if name == "gru" else f"run_member.sh {name}"
            assert marker not in crontab_text, \
                f"FLEET_CRON_MEMBERS=judge-judy,jefe still scheduled {name!r} -- allowlist not enforced"

    with tempfile.TemporaryDirectory() as tmp:
        proc, crontab_text = run_block(tmp, fleet_cron_members="judge-judy,bogus-name")
        assert proc.returncode != 0, \
            "an unknown FLEET_CRON_MEMBERS entry must fail boot loudly, not silently drop it or schedule nothing"
        assert "bogus-name" in proc.stdout + proc.stderr, \
            "the boot failure doesn't name the bad entry"


# _self_improve_score_is_actually_scheduled and _deploy_staleness_check_is_actually_scheduled
# (found live by dumbledore 2026-08-28, gh#201) were folded into the table-driven
# _every_entrypoint_scheduled_script_is_actually_scheduled above (gh#378) alongside
# _auto_deploy_race_check_is_actually_scheduled and _lane_kpi_is_actually_scheduled.


def _account_and_tunnel_health_checks_are_actually_scheduled():
    """Same failure class as _self_improve_score_is_actually_scheduled, two scripts over.

    gh#171: account_health_check.sh and tunnel_health_check.sh are the fleet's only outage
    pagers (README step 6 names them explicitly). PR#169 wired both into schedulers/systemd
    and schedulers/launchd -- the bare-host path -- but entrypoint.sh's own crontab, the
    container-native path every container deployment actually uses, had zero lines for
    either. #154 (which asked for these to be scheduled) closed with the container path still
    unfixed -- a closed issue naming a live gap is worse than an open one.
    """
    # Match the actual cron invocation, not just the bare filename -- both scripts are also
    # named in surrounding comment prose (this very check's own docstring included), so a
    # bare `"account_health_check.sh" in entry` would still pass with the cron line deleted.
    entry = (Path(__file__).parent.parent / "entrypoint.sh").read_text()
    missing = [
        s for s in ("bash /fleet-kit/scripts/account_health_check.sh", "bash /fleet-kit/scripts/tunnel_health_check.sh")
        if s not in entry
    ]
    assert not missing, (
        f"{missing} have no cron line in entrypoint.sh -- the fleet's only outage pagers "
        "will never run on a container deployment, so an all-accounts-exhausted event or a "
        "502'd tunnel pages nobody."
    )


def _has_host_only_scheduler(root, script_name: str) -> bool:
    """True when schedulers/ carries the full host-side trio for this script."""
    sd = root / "schedulers" / "systemd"
    ld = root / "schedulers" / "launchd"
    services = [f for f in sd.glob("fleetkit-*.service") if script_name in f.read_text()]
    if not services:
        return False
    timers = [sd / (f.stem + ".timer") for f in services]
    if not all(t.exists() for t in timers):
        return False
    return any(script_name in f.read_text() for f in ld.glob("com.fleetkit.*.plist"))


def _required_health_check_scripts_in_readme_are_scheduled():
    """Closes the FAILURE CLASS, not just one instance of it (gh#249).

    _account_and_tunnel_health_checks_are_actually_scheduled above hard-codes two script
    names -- it would not have caught path_health_check.sh (PR#238) shipping with zero cron
    line, the third recurrence of the exact same gap (gh#154, gh#171, gh#249). This check
    instead walks schedulers/README.md's own required-jobs table: any row marked
    **required** whose Script column is a `scripts/*_check.sh` pager must have a matching
    `bash /fleet-kit/scripts/<name>_check.sh` invocation in entrypoint.sh's crontab heredoc.
    A future pager only has to earn a required row in that table and this check covers it --
    no new selftest function needed.

    Scoped to the `*_check.sh` naming convention (the pager family: account/tunnel/path
    health) rather than every required row, because build/review are wired through
    run_member.sh/worktree_builder.sh, not a bare script invocation -- a blanket check would
    false-positive on those.
    """
    import re
    root = Path(__file__).parent.parent
    readme = (root / "schedulers" / "README.md").read_text()
    entry = (root / "entrypoint.sh").read_text()
    missing = []
    for line in readme.splitlines():
        if "**required**" not in line:
            continue
        m = re.search(r"`(scripts/(\w+_check\.sh))`", line)
        if not m:
            continue
        script_path, script_name = m.group(1), m.group(2)
        if f"bash /fleet-kit/{script_path}" in entry:
            continue
        # Host-only pagers (fleet-kit#512): a check whose whole point is "the container's
        # cron is dead" cannot live in that container's crontab. Its scheduled shape is the
        # one _account_heartbeat_and_budget_read_have_host_only_schedulers pins -- a systemd
        # unit that names the script, its timer, and a launchd plist that names it.
        if _has_host_only_scheduler(root, script_name):
            continue
        missing.append(script_name)
    assert not missing, (
        f"{missing} are marked required in schedulers/README.md but have no cron line in "
        "entrypoint.sh -- a required outage pager that looks shipped (merged PR, a README "
        "row) but never actually fires is worse than one never attempted.")


def _account_heartbeat_and_budget_read_have_host_only_schedulers():
    """gh#376: account_heartbeat.sh and budget_read_check.sh (both shipped in PR#338) sat
    completely unscheduled for two days -- the 5th/6th recurrence of the pager-wiring-gap
    class this file's other `_is_actually_scheduled` checks close. This one is deliberately
    NOT folded into `_every_entrypoint_scheduled_script_is_actually_scheduled`'s table: both
    scripts `podman exec` into the fleet's own container, which entrypoint.sh's in-container
    crontab cannot do to itself (no podman socket is bind-mounted there) -- a live rot-hunt
    finding on this issue caught an earlier attempt that would have added them to entrypoint.sh
    and shipped a check that lies (green "scheduled", broken every real tick). The correct,
    and only working, target is a host-side scheduler, so this checks THAT shape instead:
    both scripts have a systemd unit+timer and a launchd plist, and -- the regression this
    docstring's "earlier attempt" refers to -- neither ever gains an entrypoint.sh cron line.
    """
    root = Path(__file__).parent.parent
    entry = (root / "entrypoint.sh").read_text()
    scripts = ("account_heartbeat.sh", "budget_read_check.sh", "member_liveness_check.sh")
    problems = []
    for script in scripts:
        if f"bash /fleet-kit/scripts/{script}" in entry:
            problems.append(
                f"{script} has a cron line in entrypoint.sh -- it cannot run inside the fleet's "
                "own container (podman exec into itself / watching that container's own dead "
                "cron); this looks scheduled but fails every tick")
    stem = {"account_heartbeat.sh": "account-heartbeat", "budget_read_check.sh": "budget-read",
            "member_liveness_check.sh": "member-liveness"}
    for script in scripts:
        unit = stem[script]
        service = root / "schedulers" / "systemd" / f"fleetkit-{unit}.service"
        timer = root / "schedulers" / "systemd" / f"fleetkit-{unit}.timer"
        plist = root / "schedulers" / "launchd" / f"com.fleetkit.{unit}.plist"
        if not service.exists() or script not in service.read_text():
            problems.append(f"{service} missing or does not reference {script}")
        if not timer.exists():
            problems.append(f"{timer} missing")
        if not plist.exists() or script not in plist.read_text():
            problems.append(f"{plist} missing or does not reference {script}")
    assert not problems, "\n".join(problems)


def _ntfy_topic_is_deferred_to_tick_time_not_baked_in_at_boot():
    """gh#279: entrypoint.sh's cron lines for the three health-check pagers used to interpolate
    `NTFY_TOPIC=${NTFY_TOPIC:-}` at boot time -- a literal value (or empty string) frozen into
    the generated crontab the moment the container started. A human editing fleet.env's
    NTFY_TOPIC= afterward (the exact recovery step gh#269's outage needed) had no effect until a
    full container restart re-ran entrypoint.sh from scratch.

    This guards the fix: the crontab text for each of the three pagers must re-source fleet.env
    (via FLEET_ENV_FILE, tick-time) rather than embed a literal `NTFY_TOPIC=<boot-time-value>`
    on the cron line itself. Scoped narrowly to `NTFY_TOPIC=` immediately after `export` on those
    three lines -- PUBLIC_URL/FLEET_VIEW_PORT/PUBLIC_PATH_URL staying boot-time-interpolated is
    explicitly out of scope (gh#279's own non-goals).
    """
    entry = (ROOT / "entrypoint.sh").read_text()
    pagers = ("account_health_check.sh", "tunnel_health_check.sh", "path_health_check.sh")
    baked = []
    resourced = []
    for line in entry.splitlines():
        if not any(f"bash /fleet-kit/scripts/{p}" in line for p in pagers):
            continue
        if re.search(r"\bNTFY_TOPIC=", line):
            baked.append(line.strip())
        if "FLEET_ENV_FILE" in line and "set -a" in line:
            resourced.append(line.strip())
    assert not baked, (
        f"{baked!r} still bakes a boot-time NTFY_TOPIC= value into the cron line -- a fleet.env "
        "edit after container boot will never reach the pager until a full restart (gh#279).")
    assert len(resourced) == len(pagers), (
        f"expected all {len(pagers)} pager cron lines to re-source fleet.env via FLEET_ENV_FILE "
        f"before exec'ing the script, found {len(resourced)} -- NTFY_TOPIC would not be read "
        "fresh at tick-time.")


def _every_gh_api_call_is_timeout_guarded():
    """gh#278-class: a bare `gh api` call with no timeout can wedge the calling script
    indefinitely under host-level disruption, with zero trace -- confirmed live three separate
    times in three different scripts (deploy_staleness_check.sh went dark for 6 consecutive
    hourly ticks, PR#370; auto_update_branch.sh and judge-judy.sh carried the identical
    unguarded shape, fixed alongside this check). Three incident-by-incident PRs patching one
    script each is not a fix, it's the same bug recurring -- this check makes the whole CLASS
    fail CI instead, so a fourth site can never ship unguarded.

    Deliberately a plain grep, not a shell parser: `timeout` must appear on the same line as
    `gh api` (the pattern every existing fix uses), which is precise enough to catch a bare
    call while staying simple enough that this check itself won't rot.
    """
    offenders = []
    for sh in ROOT.rglob("*.sh"):
        if "node_modules" in sh.parts:
            continue
        for lineno, line in enumerate(sh.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "gh api" in line and "timeout" not in line:
                offenders.append(f"{sh.relative_to(ROOT)}:{lineno}")
    assert not offenders, (
        "bare `gh api` call(s) with no `timeout` wrapper on the same line -- wrap each in "
        f"`timeout 25s gh api ...` (gh#278-class, see PR#370): {offenders}"
    )


def _deploy_staleness_check_reads_a_baked_sha_and_only_alerts_past_budget():
    """The check must compare something REAL (a SHA baked at build time), and must only write
    a durable record when actually past budget -- not on every tick, or the STALE line this
    issue exists to produce drowns in routine noise the same way auto_deploy.sh's own comment
    warns against for its lock-contention branch.

    gh#201: /fleet-kit is never a real git checkout in production (Dockerfile's own
    `COPY . /fleet-kit` with .dockerignore excluding .git/), so the check can't `git log` the
    live tree -- it has to read back a SHA deploy.sh baked in at build time and compare it to
    main's current HEAD over the GitHub API.
    """
    src = (ROOT / "scripts" / "deploy_staleness_check.sh").read_text()
    assert ".deploy_sha" in src, "does not read the SHA deploy.sh bakes into the image at build time"
    assert "STALENESS_BUDGET_S" in src, "no staleness budget -- would alert on every normal deploy lag"
    assert 'log "STALE' in src, "no distinguishable STALE record -- same gap gh#196 fixed for a normal deploy line"
    # The in-sync path must not itself write the durable STALE line.
    quiet_branch = src[src.find('if [ "$DEPLOYED_SHA" = "$MAIN_SHA" ]'):src.find("# Diverged.")]
    assert "log " not in quiet_branch, "logs even when in sync -- would bury the STALE line in noise"

    deploy_src = (ROOT / "scripts" / "deploy.sh").read_text()
    assert "DEPLOY_SHA=" in deploy_src and "--build-arg DEPLOY_SHA=" in deploy_src, \
        "deploy.sh does not bake the built SHA into the image -- the staleness check has nothing to read"

    docker_src = (ROOT / "Dockerfile").read_text()
    assert "ARG DEPLOY_SHA" in docker_src and ".deploy_sha" in docker_src, \
        "Dockerfile does not accept/write DEPLOY_SHA -- deploy.sh's build-arg has nowhere to land"


# _lane_kpi_is_actually_scheduled (gh#324) was folded into
# _every_entrypoint_scheduled_script_is_actually_scheduled above (gh#378).


def _lane_kpi_classifies_ticks_and_ignores_in_progress_drains():
    """gh#324 AC2: success/failure/ABORT lines are each one tick; a `drain:` line (deploy.sh's
    own in-flight-pass wait, captured into the same log) belongs to a tick not yet resolved and
    must count as neither -- otherwise a still-running deploy would be silently miscounted
    before its own outcome is even known.
    """
    import lane_kpi
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "auto_deploy.log"
        log.write_text(
            "[2026-09-01 00:00:00 UTC] main moved: local=a remote=b -- pulling + deploying\n"
            "[2026-09-01 00:00:01 UTC] drain: 2 agent pass(es) in flight -- deferring cutover\n"
            "[2026-09-01 00:05:00 UTC] deploy OK at b\n"
            "[2026-09-01 01:00:00 UTC] DEPLOY FAILED at c\n"
            "[2026-09-01 02:00:00 UTC] ABORT: working tree dirty -- refusing to pull\n"
            "[2026-09-01 03:00:00 UTC] ABORT: local HEAD is not an ancestor of origin/main\n"
            # A tick still draining at scan time -- no resolution line yet, must not count.
            "[2026-09-01 04:00:00 UTC] drain: 1 agent pass(es) in flight -- deferring cutover\n"
        )
        successes, total = lane_kpi.classify_ticks(log, since=0.0)
        assert successes == 1, successes
        assert total == 4, total  # 1 OK + 1 FAILED + 2 ABORT; the two drain: lines don't count

        # The trailing-window cutoff actually excludes old ticks, not just accepts everything.
        since = lane_kpi._line_epoch(
            "[2026-09-01 01:00:00 UTC] DEPLOY FAILED at c\n"
        )
        successes2, total2 = lane_kpi.classify_ticks(log, since=since)
        assert total2 == 3, total2  # drops the 00:00 OK tick, keeps FAILED + 2 ABORT


def _lane_kpi_is_append_only_and_distinguishes_missing_from_stale():
    """gh#324 AC3/AC5/AC6: a consumer must be able to tell "never computed" (None) from "real
    reading, possibly stale" (a dict with a `stale` flag) from a silently-frozen last value --
    and consecutive denominators must both survive so a >10% swing (kpi-doctrine.md rule 3) is
    detectable from the store alone, not just the newest row.
    """
    import lane_kpi, fleet_db
    with tempfile.TemporaryDirectory() as d:
        conn = fleet_db.connect(Path(d) / "fleet.db")

        # Never run: must read as missing, not as a 0% rate.
        assert lane_kpi.read_latest(conn) is None

        log = Path(d) / "auto_deploy.log"
        log.write_text("[2026-09-01 00:00:00 UTC] deploy OK at a\n")
        r1 = lane_kpi.compute_and_record(conn, log, now=1000.0)
        assert r1["value"] == 1.0 and r1["denominator"] == 1, r1

        # A second reading with a different denominator must not overwrite the first --
        # append-only, so both remain queryable for a rule-3 swing check.
        log.write_text(
            "[2026-09-01 00:00:00 UTC] deploy OK at a\n"
            "[2026-09-01 00:05:00 UTC] DEPLOY FAILED at b\n"
        )
        lane_kpi.compute_and_record(conn, log, now=2000.0)
        rows = conn.execute(
            "SELECT denominator FROM lane_kpi WHERE lane=? AND metric=? ORDER BY computed_at",
            (lane_kpi.LANE, lane_kpi.METRIC),
        ).fetchall()
        assert [r[0] for r in rows] == [1, 2], rows

        fresh = lane_kpi.read_latest(conn, now=2000.0 + 1.0)
        assert fresh is not None and fresh["stale"] is False, fresh
        stale = lane_kpi.read_latest(conn, now=2000.0 + 2 * lane_kpi.EXPECTED_INTERVAL_S + 1)
        assert stale is not None and stale["stale"] is True, stale

        # Zero ticks in the window is "no data", not a real 0% -- NULL, never 0.0.
        empty_log = Path(d) / "empty.log"
        empty_log.write_text("")
        r_empty = lane_kpi.compute_and_record(
            conn, empty_log, lane="devops", metric="deploy_success_rate_test_empty", now=3000.0)
        assert r_empty["value"] is None and r_empty["denominator"] == 0, r_empty


def _fleet_kpi_roomba_catches_all_three_real_evaluated_phrasings():
    """gh#343: `_ROOMBA_PATTERNS` used to anchor only on a digit sitting immediately before the
    literal word "evaluated" ("N evaluated"), silently dropping the other two real phrasings
    roomba's free-text outcome prose routinely uses -- a verb-before-number order ("evaluated N
    worktree(s)") and an extra "worktree(s)" word wedged between the digit and the verb ("N
    worktree(s) evaluated"). Quantified against a real fleet.db window: the narrow pattern
    matched 12/23 runs (summed 13) where a loosened reference matched 18/23 (summed 19) -- a
    dashboard KPI reading ~30-40% low despite the sweep having actually run.

    Sample strings below are the exact "MISS" quotes from the issue body. The fourth
    ("Ran full worktree sweep (0/1 removed...)") never says "evaluated" at all -- it's a
    genuinely different, undocumented phrasing (noted in PR body per the issue's own
    "grep the full corpus" instruction) and correctly still returns None: building a pattern
    for it would mean guessing at a generic count-shape, which this file's own docstring
    already rules out.
    """
    import fleet_kpi
    assert fleet_kpi.extract_kpi(
        "roomba",
        "Worktree sweep evaluated 1 worktree (0 removed, correctly kept as too-young); "
        "crew health sweep clean.",
    ) == (1, "worktrees evaluated")
    assert fleet_kpi.extract_kpi(
        "roomba",
        "Worktree sweep + crew health sweep both QUIET — 1 worktree evaluated/0 removed, "
        "12/12 crew healthy.",
    ) == (1, "worktrees evaluated")
    assert fleet_kpi.extract_kpi(
        "roomba",
        "QUIET — worktree sweep and crew-health sweep both came back clean on fleet-kit "
        "(1 worktree evaluated/0 removed/0 kept-ambiguous), gh#204 reconfirmed open.",
    ) == (1, "worktrees evaluated")
    assert fleet_kpi.extract_kpi(
        "roomba",
        "Ran full worktree sweep (0/1 removed, 0 ambiguous) and crew health sweep (0/12 "
        "ghosts) on fleet-kit; re-confirmed gh#204 still open with fresh evidence, no new "
        "backlog filed.",
    ) is None

    # The original bare-number phrasing this pattern was first built for must still match.
    assert fleet_kpi.extract_kpi("roomba", "Worktree sweep clean (5 evaluated/0 removed)") == (
        5, "worktrees evaluated")

    # Other members' patterns are untouched by this change.
    assert fleet_kpi.extract_kpi(
        "marie", "Cleared stale fleet:claimed on #2075 and #2759"
    ) == (1, "issues triaged")
    assert fleet_kpi.extract_kpi(
        "judge-judy", "approved PR #4250 and blocked PR #4200"
    ) == (2, "PRs reviewed")


def _fleet_kpi_marie_catches_her_real_triage_verb_vocabulary():
    """gh#351: `_MARIE_PATTERNS`'s verb-anchored regex only recognized cleared/closed/ranked,
    silently ZERO-counting the rest of marie's real triage vocabulary -- triaged, corrected
    priority, backfilled/scored/bumped complexity, wrote/posted a PRD, labeled -- and never
    caught the explicit "N new ranking(s)" count phrasing at all (same failure class as
    gh#343's roomba gap, one file over). Quantified in the issue over a real 48h fleet.db
    window: 30/41 runs matched (summed 35) before, 37/41 (summed 54) after broadening.

    Sample strings below are the verb phrasings the issue quotes from marie's real outcome
    prose.
    """
    import fleet_kpi
    assert fleet_kpi.extract_kpi("marie", "Triaged #3201 for priority") == (1, "issues triaged")
    # NOTE: a multi-issue reference list after the verb undercounts to 1, not 2 -- the same
    # pre-existing greedy-\D{0,6} quirk already baked into the "Cleared ... #2075 and #2759"
    # case below. Out of scope here (non-goal: don't change extract_kpi's summation semantics,
    # only the pattern list content) -- asserting the ACTUAL behavior, not the ideal one.
    assert fleet_kpi.extract_kpi(
        "marie", "Corrected priority on #3202 and #3203"
    ) == (1, "issues triaged")
    assert fleet_kpi.extract_kpi(
        "marie", "Backfilled complexity on #3204"
    ) == (1, "issues triaged")
    assert fleet_kpi.extract_kpi("marie", "Bumped #3205's complexity to 3") == (1, "issues triaged")
    assert fleet_kpi.extract_kpi(
        "marie", "Scored complexity on #3206 and #3207"
    ) == (1, "issues triaged")
    assert fleet_kpi.extract_kpi("marie", "Wrote a PRD for #3208") == (1, "issues triaged")
    assert fleet_kpi.extract_kpi("marie", "Posted a PRD for #3209") == (1, "issues triaged")
    assert fleet_kpi.extract_kpi("marie", "Labeled #3210 as cruft") == (1, "issues triaged")

    # explicit-count phrasing that names no issue numbers at all -- neither prior family could
    # ever have caught this.
    assert fleet_kpi.extract_kpi("marie", "3 new rankings this pass") == (3, "issues triaged")
    assert fleet_kpi.extract_kpi("marie", "1 new ranking this pass") == (1, "issues triaged")

    # a genuinely zero-action pass must still return a real zero, not None or a fabricated
    # non-zero.
    assert fleet_kpi.extract_kpi(
        "marie", "QUIET -- reviewed backlog, 0 new rankings, nothing to triage"
    ) == (0, "issues triaged")

    # the original verb phrasings this pattern was first built for must still match.
    assert fleet_kpi.extract_kpi(
        "marie", "Cleared stale fleet:claimed on #2075 and #2759"
    ) == (1, "issues triaged")
    assert fleet_kpi.extract_kpi("marie", "Closed #2217 as cruft") == (1, "issues triaged")

    # issues merely mentioned for context, never a triage verb, still correctly return None.
    assert fleet_kpi.extract_kpi("marie", "confirmed 65 open issues, 5 newly-merged PRs") is None

    # other members' patterns are untouched by this change.
    assert fleet_kpi.extract_kpi(
        "roomba", "Worktree sweep clean (5 evaluated/0 removed)"
    ) == (5, "worktrees evaluated")
    assert fleet_kpi.extract_kpi(
        "judge-judy", "approved PR #4250 and blocked PR #4200"
    ) == (2, "PRs reviewed")


def _fleet_kpi_marie_ignores_explicit_zero_counted_verb_gh409():
    """gh#409: the verb-anchored `_MARIE_PATTERNS` entry (shipped for gh#351) had no check for
    a leading explicit `0` before its own verb, so a QUIET pass's own zero-count boilerplate --
    "0 claims cleared (gh#98 self-resolved via merged PR#404)" -- got credited as real triage
    just because a `#NNNN` reference happened to follow in the same clause, named for context
    rather than as a counted action. Confirmed live against fleet.db: 5/60 (~8%) of marie's
    counted runs were spurious-credit this way.

    Samples below are the five flagged live outcome strings (fleet.db runs.jsonl,
    ts=1788384904/1788406720/1788421165/1788518142/1788593826), plus the issue's own two named
    genuine-nonzero counter-examples that must keep counting.
    """
    import fleet_kpi

    zero_credit_samples = [
        "Re-verified fleet-kit's open backlog (#5-#311, 62 issues) is fully clean — 0 claims "
        "cleared, 0 cruft closed, 0 label gaps, 0 PRDs owed — via diff-since-20:3xUTC-pass "
        "(PR #312, issues #160/#151), memory updated at "
        "project_fleet_kit_backlog_pristine_20260902.md",
        "Reviewed all 63 open fleet-kit issues (#17–#318) — 0 stale claims cleared (0 "
        "existed), 0 cruft closed (35 issues #220–#318 freshly evidence-checked, all "
        "correctly still open), 0 priority/complexity/backlog label gaps found or fixed, 0 new "
        "PRDs needed (all 25 high-priority issues already have one).",
        "Reviewed all 65 open fleet-kit issues (#17-#326) for stale claims and cruft — 0 claims "
        "cleared, 0 cruft closed (all confirmed still-live via grep/git-log/PR-search "
        "evidence), 1 missing priority-comment added (#326, "
        "https://github.com/The-Good-Project-Team/fleet-kit/issues/326#issuecomment-552228377",
        "QUIET — verified fleet-kit backlog (92 open issues, #17-#376) is fully triaged: 0 "
        "stale claims, 0 cruft closes needed (issue #360 auto-closed correctly via PR#377), 0 "
        "label gaps of any kind, 0 PRDs owed.",
        "fleet-kit backlog pass 2026-09-05 ~07:20 UTC — 0 claims cleared (gh#98 self-resolved "
        "via merged PR#404), 0 cruft closed after depth-verifying #254-#293 (16 issues), 0 "
        "label gaps found across all 97 open issues (#17-#405), resume point for next pass is "
        "#308.",
    ]
    for outcome in zero_credit_samples:
        got = fleet_kpi.extract_kpi("marie", outcome)
        assert got is None or got[0] == 0, (
            f"gh#409 regression: expected no positive credit, got {got!r} for {outcome!r}"
        )

    # genuine nonzero triage in the same run must still count -- the issue's own two named
    # counter-examples.
    assert fleet_kpi.extract_kpi("marie", "#368/#369 labeled") is None  # no recognized verb form
    assert fleet_kpi.extract_kpi("marie", "labeled #368/#369") == (1, "issues triaged")
    assert fleet_kpi.extract_kpi("marie", "Ranked and PRD'd #405") == (1, "issues triaged")

    # the original verb phrasings this pattern was first built for must still match.
    assert fleet_kpi.extract_kpi(
        "marie", "Cleared stale fleet:claimed on #2075 and #2759"
    ) == (1, "issues triaged")
    assert fleet_kpi.extract_kpi("marie", "Closed #2217 as cruft") == (1, "issues triaged")

    # other members' patterns are untouched by this change.
    assert fleet_kpi.extract_kpi(
        "roomba", "Worktree sweep clean (5 evaluated/0 removed)"
    ) == (5, "worktrees evaluated")
    assert fleet_kpi.extract_kpi(
        "judge-judy", "approved PR #4250 and blocked PR #4200"
    ) == (2, "PRs reviewed")


def _fleet_kpi_gru_jefe_minion_ship_a_real_prs_shipped_count():
    """gh#230: `_KPI_TABLE` omitted gru/jefe/minion entirely, so `/api/kpi` returned a null
    total for the fleet's own build-orchestration chain even though their outcome text is
    heavily PR-shaped ("Shipped PR #226 ... and PR #227 ... both auto-merge armed"). Samples
    below are the issue's own quoted strings.
    """
    import fleet_kpi
    for member in ("gru", "jefe", "minion"):
        assert fleet_kpi.extract_kpi(
            member, "Shipped PR #226 (fix retry backoff) and PR #227 (add health check), "
                    "both auto-merge armed."
        ) == (2, "PRs shipped")
        assert fleet_kpi.extract_kpi(member, "Opened pull/4488, auto-merge armed.") == (
            1, "PRs shipped")

        # a run that shipped nothing (blocked/already-fixed/QUIET, no PR mention at all) is a
        # real zero, not None -- the same "real zero vs true None" contract `extract_kpi`'s own
        # docstring documents, extended here per gh#230 AC3.
        assert fleet_kpi.extract_kpi(
            member, "QUIET -- item was already fixed, nothing to build this pass."
        ) == (0, "PRs shipped")

        # a crashed/budget-declined run has no outcome at all -- still a true None, same as
        # every other member.
        assert fleet_kpi.extract_kpi(member, None) is None

        # fleet-code-review BLOCK, 2026-09-05: an unanchored "PR #\d+"/"pull/\d+" match counted
        # a blocked/attempted PR as "shipped" just because it was mentioned. Only the PR named
        # after an actual shipping verb, in its own clause, counts.
        assert fleet_kpi.extract_kpi(
            member,
            "Attempted PR #225 but CI failed, blocked. Retried and shipped PR #226.",
        ) == (1, "PRs shipped")


def _fleet_kpi_pr_ref_no_space_and_plural_shared_prefix_gh448():
    """gh#448: `_PR_REF` (shipped today via PR#434/gh#230) required a literal space between
    "PR" and "#", and one "PR" token per reference -- silently undercounting two real, common
    outcome-prose shapes to zero: no-space "PR#N" (33/261 real gru/jefe/minion outcomes in
    runs.jsonl) and the plural shared-prefix "PRs #A and #B" (5/261). Samples below are the
    issue's own quoted acceptance-criteria strings.
    """
    import fleet_kpi
    for member in ("gru", "jefe", "minion"):
        # AC1: no-space "PR#N" form, interleaved with unrelated "gh#N" issue-cause references
        # in the SAME clause -- those must NOT be credited as shipped PRs (only the 3 real
        # "PR#N" refs should count, not the 3 "gh#N" issue refs mixed in beside them).
        assert fleet_kpi.extract_kpi(
            member,
            "Shipped gh#196→PR#199, gh#194→PR#200, gh#51→PR#198 in "
            "The-Good-Project-Team/fleet-kit, all auto-merge armed.",
        ) == (3, "PRs shipped")

        # AC2: plural shared-prefix form -- one "PRs" keyword governing a short "and"-joined
        # list, no second "PR" token before the second number.
        assert fleet_kpi.extract_kpi(
            member, "... both shipped green, auto-merge-armed PRs #178 and #177."
        ) == (2, "PRs shipped")

        # AC3: existing singular "PR #N" / "pull/N" coverage must still pass unchanged.
        assert fleet_kpi.extract_kpi(
            member, "Shipped PR #226 (fix retry backoff) and PR #227 (add health check), "
                    "both auto-merge armed."
        ) == (2, "PRs shipped")
        assert fleet_kpi.extract_kpi(member, "Opened pull/4488, auto-merge armed.") == (
            1, "PRs shipped")


def _fleet_kpi_nerd_catches_filed_and_commented_verbs():
    """gh#225: `_KPI_TABLE` had entries for only roomba/marie/judge-judy -- nerd (one of the
    fleet's highest-volume members) returned `total: null` on `/api/kpi`, visually identical to
    the module's own deliberately-excluded members, despite consistently count-shaped outcome
    prose ("Filed gh#N...", "Filed N issues...", "Commented on gh#N...", the issue's own three
    quoted shapes). A 2026-09-05 pull of nerd's full real runs.jsonl history (237 outcomes) found
    "Posted"/"Edited" alongside "Filed"/"Commented" as real, frequent verbs, and found the same
    prose routinely NEGATES those verbs for a QUIET pass ("no new issue filed", "already filed",
    "not re-posted") while still naming old issue numbers later in the same sentence -- the exact
    gh#409 bug nerd itself found in marie's patterns, one file over.
    """
    import fleet_kpi
    # the issue's own three quoted shapes
    assert fleet_kpi.extract_kpi(
        "nerd", "Filed gh#380 -- FLEET_GRU_CADENCE Settings picker ships invalid cron values."
    ) == (1, "issues filed/commented")
    assert fleet_kpi.extract_kpi(
        "nerd", "Filed 3 issues in The-Good-Project-Team/fleet-kit -- #141, #150, #155."
    ) == (3, "issues filed/commented")
    assert fleet_kpi.extract_kpi(
        "nerd", "Commented on gh#339 with fresh same-day dispatch evidence."
    ) == (1, "issues filed/commented")

    # real full-URL phrasing (github.com/.../issues/NNN, no bare '#') must match despite the
    # literal '.' inside "github.com" -- the naive "stop at any '.'" clause boundary missed
    # 100/237 real runs entirely until narrowed to "stop only at a sentence-ending '.'".
    assert fleet_kpi.extract_kpi(
        "nerd",
        "Filed https://github.com/The-Good-Project-Team/fleet-kit/issues/150 -- "
        "fleet_stats.py's signal_rate/dormant logic excludes budget_declined.",
    ) == (1, "issues filed/commented")

    # multiple distinct actions in one outcome sum together, same summing contract as
    # roomba/marie's patterns -- 1 filed issue plus 2 separate cross-link comments = 3 actions,
    # not 1 clause = 1 action.
    assert fleet_kpi.extract_kpi(
        "nerd",
        "Filed https://github.com/The-Good-Project-Team/fleet-kit/issues/378 (bespoke selftest "
        "guards should generalize); commented cross-links on #376 and #371.",
    ) == (3, "issues filed/commented")

    # Posted/Edited are real, frequent verbs in nerd's actual prose (49 and 2 occurrences in a
    # 237-run sample respectively) -- neither of the issue's own 3 quoted shapes covers them.
    assert fleet_kpi.extract_kpi(
        "nerd", "Posted fresh evidence to gh#278 confirming the live 16:05-16:10 UTC recurrence."
    ) == (1, "issues filed/commented")
    assert fleet_kpi.extract_kpi(
        "nerd", "Edited https://github.com/The-Good-Project-Team/fleet-kit/issues/68 to prepend "
        "a correction banner."
    ) == (1, "issues filed/commented")

    # a genuinely zero-action QUIET pass must still return a real zero via sum_kpi_over_runs
    # (member is in _KPI_TABLE), never a fabricated non-zero from an old issue number named in
    # the same "nothing new" sentence -- same failure class as gh#409.
    assert fleet_kpi.extract_kpi(
        "nerd", "QUIET -- no new issue filed since gh#379/#143/#339 already fully cover this."
    ) is None
    assert fleet_kpi.extract_kpi(
        "nerd", "No new gh#143 comment posted -- the prior pass already filed the escalation."
    ) is None
    assert fleet_kpi.extract_kpi(
        "nerd", "Reconfirmed gh#352 and gh#356 still open via live evidence; not re-filed."
    ) is None
    assert fleet_kpi.extract_kpi(
        "nerd", "gh#399 was already filed by lens; not re-posted."
    ) is None
    got = fleet_kpi.sum_kpi_over_runs(
        "nerd",
        [{"member": "nerd", "outcome": "QUIET -- no new issue filed since gh#143 already covers this."}],
    )
    assert got == {
        "member": "nerd", "total": 0, "unit": "", "runs_with_kpi": 0, "runs_total": 1,
    }, got

    # issues merely mentioned as prior context alongside a real filing don't get double-counted
    # into the same action.
    assert fleet_kpi.extract_kpi(
        "nerd",
        "Filed https://github.com/The-Good-Project-Team/fleet-kit/issues/379 -- revenue lane "
        "has zero lane_kpi rows, same infra-gap class as gh#330/#345/#352/#365.",
    ) == (1, "issues filed/commented")

    # other members' patterns are untouched by this change.
    assert fleet_kpi.extract_kpi(
        "roomba", "Worktree sweep clean (5 evaluated/0 removed)"
    ) == (5, "worktrees evaluated")
    assert fleet_kpi.extract_kpi(
        "marie", "Cleared stale fleet:claimed on #2075 and #2759"
    ) == (1, "issues triaged")
    assert fleet_kpi.extract_kpi(
        "judge-judy", "approved PR #4250 and blocked PR #4200"
    ) == (2, "PRs reviewed")

    # the-fixer stays out of _KPI_TABLE per this issue's own non-goals -- its PR-mention hit
    # rate doesn't fit a "shipped" headline.
    assert fleet_kpi.extract_kpi("the-fixer", "Fixed PR #4250's failing gate.") is None

    # the docstring's old, false "dashboard shows a pass/fail ratio instead" claim is gone.
    src = (Path(__file__).parent / "fleet_kpi.py").read_text()
    assert "pass/fail ratio for" not in src, "docstring still claims the nonexistent UI fallback"

def _no_member_ships_a_cap():
    """Caps are off fleet-wide: control by selection and charter quality, not truncation.

    Reif, 2026-08-26: "all caps come off -- we must control via intelligence vs by force."
    Measured on 142 real minion runs, 43 hit the 60-turn wall vs 8 near the budget cap, and
    every `stop_reason: tool_use` row sat at ~61 turns -- the CLI cutting a pass mid-tool-call
    with budget to spare, converting expensive-but-finishable work into paid-for nothing.

    The regression this guards is a DEFAULT creeping back: `run_member.sh` used to default to
    60 turns / $5 when a spec omitted them, so deleting the keys alone would have changed
    nothing. An absent cap must reach the CLI as NO FLAG.
    """
    import member_spec
    for s in member_spec.load_all():
        assert "max_turns" not in s["llm"], f"{s['name']} ships a turn cap"
        assert "max_budget_usd" not in s["mandate"]["limits"], f"{s['name']} ships a budget cap"
        # timeout_s stays -- wall-clock is the one backstop a runaway pass still needs.
        assert s["mandate"]["limits"].get("timeout_s"), f"{s['name']} lost its timeout backstop"

    # An omitted cap must not be resurrected as a default by the runner.
    src = (Path(__file__).parent / "run_member.sh").read_text()
    assert "get('max_turns') or ''" in src, "run_member.sh reintroduced a max_turns default"
    assert "get('max_budget_usd') or ''" in src, "run_member.sh reintroduced a budget default"
    assert '[ -n "$MAX_TURNS" ] && CAP_ARGS+=' in src, "empty cap no longer omits the flag"


def _overrides_are_narrow():
    import overrides
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "ov.jsonl"
        spec = _members()[0]
        overrides.set_override(spec["name"], "max_turns", 7, by="selftest",
                               why="proving the dial works", store=store)
        eff, applied = overrides.apply(spec, store=store)
        assert eff["llm"]["max_turns"] == 7 and applied
        # The git spec must not be mutated. Specs ship UNCAPPED as of 2026-08-26 (no max_turns
        # key at all), so the property to assert is "the source is untouched" -- absent stays
        # absent -- not "it holds some other number."
        assert spec["llm"].get("max_turns") != 7, "the git spec must not be mutated"
        # Authority is never live-tunable.
        try:
            overrides.set_override(spec["name"], "tools", ["x"], by="selftest",
                                   why="should refuse", store=store)
        except overrides.OverrideError:
            return
        raise AssertionError("tools must NOT be live-tunable")


def _overrides_store_is_not_under_home_dot_claude():
    """gh#51: the live-override store used to default under $HOME/.claude/ -- Claude Code's own
    managed config directory -- where something in there swept the file within hours, silently
    reverting every throttle an operator believed was still in force. A static guard so the
    default can't drift back there unnoticed the way it did before anyone caught it.
    """
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items()
           if k not in ("FLEET_OVERRIDES_PATH", "FLEET_LOG_DIR")}
    proc = subprocess.run(
        [sys.executable, "-c", "import overrides; print(overrides.STORE)"],
        cwd=str(HERE), env=env, capture_output=True, text=True, timeout=30, check=True)
    resolved = proc.stdout.strip()
    home_dot_claude = str(Path.home() / ".claude")
    assert home_dot_claude not in resolved, \
        f"STORE still resolves under $HOME/.claude with no env overrides set: {resolved}"
    # FLEET_OVERRIDES_PATH must still win outright -- no behavior change for anyone using it.
    with tempfile.TemporaryDirectory() as d:
        explicit = str(Path(d) / "explicit-overrides.jsonl")
        proc = subprocess.run(
            [sys.executable, "-c", "import overrides; print(overrides.STORE)"],
            cwd=str(HERE), env=dict(env, FLEET_OVERRIDES_PATH=explicit),
            capture_output=True, text=True, timeout=30, check=True)
        assert proc.stdout.strip() == explicit, "FLEET_OVERRIDES_PATH no longer takes precedence"


def _env_example_exists():
    assert (ROOT / "fleet.env.example").is_file()
    assert not (ROOT / "fleet.env").exists(), "fleet.env is yours to create and must stay untracked"


def _schedulers_for_both_platforms():
    assert list((ROOT / "schedulers" / "launchd").glob("*.plist")), "no launchd templates"
    assert list((ROOT / "schedulers" / "systemd").glob("*.timer")), "no systemd timers"


def _api_key_never_reaches_an_llm():
    """No script that execs `claude -p` may leave FLEET_API_KEY in its environment.

    That key authorizes POST /api/run_now, which spawns
    `claude -p --dangerously-skip-permissions` on the fleet box. Every one of these scripts
    sources fleet.env with `set -a`, which exports it -- so without an explicit unset, all nine
    members run holding the ability to spawn unlimited runs, and the key sits in nine agents'
    contexts where one prompt-injected issue body could print it into a PR comment.

    No member needs it (nothing under members/ calls that endpoint), so it is dropped before
    the exec. This test fails if a NEW spawner is added without the same unset.
    """
    import re
    spawners = []
    for path in (ROOT / "scripts").glob("*.sh"):
        text = path.read_text()
        if "claude -p" not in text:
            continue
        # A script that only delegates (exec's another runner) is covered by that runner.
        if re.search(r"exec bash .*run_member\.sh", text):
            continue
        spawners.append((path.name, text))

    assert spawners, "no claude -p spawners found -- test is looking in the wrong place"
    # Only scripts that actually SOURCE fleet.env export the key. A mere mention of the
    # filename in a comment does not (account_pool.sh does exactly that, and is always sourced
    # BY a caller that has already unset it).
    sources_env = re.compile(r"^\s*\[ -f .*fleet\.env.*\].*set -a", re.M)
    missing = [name for name, text in spawners
               if sources_env.search(text) and "unset FLEET_API_KEY" not in text]
    assert not missing, (
        f"these scripts exec `claude -p` with FLEET_API_KEY still exported: {missing} -- "
        "add `unset FLEET_API_KEY` after the fleet.env source")


def _write_routes_are_authenticated():
    """Every POST route must sit behind the auth gate, and it must fail CLOSED.

    Live incident 2026-08-25: this server was published through a Cloudflare tunnel with no
    auth of any kind. A stranger who knew the URL could POST a member name to /api/run_now and
    spawn `claude -p --dangerously-skip-permissions` on the fleet box -- burning the account
    pool's budget, with repo write access and a gh token. Confirmed reachable from off-box.

    The gate lives at the top of do_POST rather than per-route so a NEW write route added later
    inherits it by default. This test pins both that placement and the fail-closed default.
    """
    src = (ROOT / "scripts" / "fleet_view_server.py").read_text()

    # The gate must be inside do_POST, before any route dispatch.
    post = src.split("def do_POST", 1)
    assert len(post) == 2, "do_POST not found"
    body = post[1]
    gate = body.find("_authorized()")
    assert gate != -1, "do_POST has no _authorized() gate -- write routes are unauthenticated"

    # /api/login is deliberately ahead of the gate -- it exists to SATISFY the gate, so
    # sitting behind it would make it unreachable (it carries its own fail-closed check,
    # pinned by _fleet_view_login_is_still_fail_closed). Every OTHER route must follow the
    # gate: the point of this assertion is that a new write route inherits auth by default.
    import re as _re
    routes = [(m.start(), m.group(1)) for m in
              _re.finditer(r'if path == "(/api/[a-z_]+)"', body)]
    early = [name for at, name in routes if at < gate and name != "/api/login"]
    assert not early, f"write route(s) dispatched before the auth gate: {early}"
    assert any(name != "/api/login" for _at, name in routes), \
        "no write routes found after the gate -- the scan is not matching real dispatch"

    # Fail closed: no key configured must mean no remote writes, never "auth disabled".
    auth = src.split("def _authorized", 1)[1].split("\n    def ", 1)[0]
    assert "return False" in auth, "_authorized never denies -- cannot be failing closed"
    assert "compare_digest" in auth, "key compared without hmac.compare_digest (timing leak)"


def _fleet_settings_rejects_unsafe_or_malformed_dial_values():
    """gh#233: /api/fleet_settings wrote any DIAL_FIELDS value straight to fleet.env with zero
    validation. Two confirmed-live consequences: (1) fleet.env is bash-sourced as root by both
    entrypoint.sh and run_member.sh, so an unescaped shell metacharacter in ANY field is root
    command execution on the next cron tick; (2) FLEET_GRU_CADENCE=0,30 (set via this exact
    endpoint) fed cron's hour field, and Vixie cron rejected the whole crontab file, silencing
    every scheduled member for ~40h (2026-09-03 21:00 -> 09-05 12:45).

    One deliberately malformed value per field family, per gh#233's own AC5, plus the proven
    real-world value that caused the outage.
    """
    import fleet_view_server as fvs

    malformed = [
        ("FLEET_GRU_CADENCE", "0,30"),          # the value that actually broke cron
        ("FLEET_GRU_CADENCE", "$(rm -rf /)"),   # shell metachar via the cron-hour family
        ("FLEET_SHARE_FRACTION", "1.5"),        # out-of-range fraction
        ("FLEET_GRU_ALLOWANCE_FRACTION", "nope"),
        ("FLEET_QUEUE_CAP", "-1"),               # negative int
        ("FLEET_CADENCE_BUILD", "3;rm -rf /"),   # shell metachar via a seconds field
        ("FLEET_MAX_BUDGET_USD", "-5"),          # negative float
        ("FLEET_BUILDER_MODEL", "claude`id`"),   # shell metachar via a free-text field
        ("FLEET_CODE_REVIEW_MODEL", "a\nFLEET_ENABLED=false"),  # newline line-injection
    ]
    for key, value in malformed:
        err = fvs._validate_dial_value(key, value)
        assert err, f"{key}={value!r} should have been rejected but validated clean"

    valid = [
        ("FLEET_GRU_CADENCE", "*/2"), ("FLEET_GRU_CADENCE", ""), ("FLEET_GRU_CADENCE", "3"),
        ("FLEET_SHARE_FRACTION", "0.25"), ("FLEET_QUEUE_CAP", "10"),
        ("FLEET_CADENCE_BUILD", "3600"), ("FLEET_MAX_BUDGET_USD", "5"),
        ("FLEET_BUILDER_MODEL", "claude-sonnet-5"),
    ]
    for key, value in valid:
        err = fvs._validate_dial_value(key, value)
        assert err is None, f"{key}={value!r} should have validated clean, got: {err}"

    # All-or-nothing at the handler: the errors pass must complete (and gate the response)
    # before the write loop's first write_env_field call, in source order, so one bad field
    # in a multi-field request can never leave fleet.env partially written.
    src = (ROOT / "scripts" / "fleet_view_server.py").read_text()
    handler = src.split('if path == "/api/fleet_settings"', 1)[1].split('\n        if path ==', 1)[0]
    first_error_check = handler.find("_validate_dial_value")
    first_write = handler.find("write_env_field(")
    assert first_error_check != -1, "/api/fleet_settings no longer calls _validate_dial_value"
    assert first_write != -1, "/api/fleet_settings no longer writes via write_env_field"
    assert first_error_check < first_write, \
        "/api/fleet_settings writes before validating -- not all-or-nothing"


def _fleet_view_ui_can_actually_authenticate_a_write():
    """The Settings dials (and every other write button) could never save from a browser.

    Live 2026-09-02, Reif editing Gru allowance through the tunnel: the page showed
    "save failed" and the server logged `DENIED /api/fleet_settings ... (bad or missing
    X-Fleet-Key)`. Root cause is a contradiction between two deliberate decisions:

      * do_POST requires X-Fleet-Key for any non-localhost client (_authorized), and
      * _cors deliberately omits X-Fleet-Key from Allow-Headers, precisely so a browser
        CANNOT send the write key.

    So every one of the 11 POST routes was unreachable from the UI it was built for -- the
    dashboard was read-only for any remote operator, and nobody noticed because the tests
    only ever asserted that writes are DENIED, never that a legitimate operator can be
    ALLOWED. "Fail closed" was pinned; "opens for the right person" was not.

    The fix is a same-origin session cookie: the page is served by this same server, so a
    cookie rides along on its own fetch() without needing a CORS-exposed header, and it is
    still unavailable to a cross-site attacker (Allow-Origin `*` forbids credentials, and
    the cookie is SameSite=Strict).

    This test pins the capability, not the mechanism's spelling: given a configured key,
    SOME credential a browser can actually present must authorize a write.
    """
    src = (ROOT / "scripts" / "fleet_view_server.py").read_text()
    page = (ROOT / "scripts" / "fleet_view.html").read_text()

    auth = src.split("def _authorized", 1)[1].split("\n    def ", 1)[0]
    assert "Cookie" in auth or "cookie" in auth, (
        "_authorized accepts only X-Fleet-Key, but _cors deliberately keeps that header "
        "out of Allow-Headers -- no browser can ever authorize a write. The UI's own "
        "buttons are dead against a remote server."
    )

    # There must be a way for the operator to establish that credential from the page.
    assert "/api/login" in src, "no login route -- nothing can ever set the session cookie"
    assert "/api/login" in page, "the page never offers a way to authenticate"

    # And the login route itself must be reachable before the write gate rejects it,
    # otherwise it can never be called by the very client that needs it.
    post = src.split("def do_POST", 1)[1]
    login_at = post.find('"/api/login"')
    gate_at = post.find("_authorized()")
    assert login_at != -1 and login_at < gate_at, (
        "/api/login sits behind the auth gate it exists to satisfy -- unreachable by design"
    )


def _fleet_view_login_is_still_fail_closed():
    """The login route must not become a hole in the gate it feeds.

    It is the ONE route ahead of _authorized(), so it carries the whole burden itself: with
    no FLEET_API_KEY configured it must refuse (never "no key means anything works"), and it
    must compare in constant time like the header path already does.
    """
    src = (ROOT / "scripts" / "fleet_view_server.py").read_text()
    assert "def _handle_login" in src, "login logic not isolated -- cannot audit it"
    login = src.split("def _handle_login", 1)[1].split("\n    def ", 1)[0]
    assert "compare_digest" in login, "login compares the key without constant-time compare"
    assert "return" in login and "False" in login or "401" in login, \
        "login never refuses -- it cannot be failing closed"
    assert "HttpOnly" in login, "session cookie is readable by page scripts (XSS lifts it)"
    assert "SameSite=Strict" in login, "cookie rides cross-site requests -- CSRF on every write"


def _fixer_sees_a_green_but_parked_pr():
    """the-fixer must alarm on a PR that is green, mergeable, and going nowhere.

    Its five original shapes all key on RED or ABSENT -- failed, conflicted, hung, errored,
    never reported. A PR where every check PASSED and nothing ever armed auto-merge is none of
    those, so it sat invisible: no error, no alarm, open forever.

    Live proof case (fleet-kit#291, 2026-09-02): judge-judy BLOCKed it 15:30, auto_update_branch
    rebased it, judge-judy re-reviewed 15:47 -> fleet-code-review=SUCCESS, selftest SUCCESS,
    mergeStateStatus CLEAN, automerge=none. The self-heal loop ran end to end and stopped one
    step short of done. auto_update_branch.sh arms these now, but an ARM CAN ITSELF FAIL and
    that failure is only a log line -- this shape is the alarm for that.

    Runs check.sh's REAL jq selector (extracted from the script, not retyped) against fixture
    PRs, so the expression under test is the one that ships.
    """
    import json
    import re
    import shutil
    import subprocess

    if not shutil.which("jq"):
        return  # jq absent here; shape is also covered by the live dry-run recorded in the PR

    src = (ROOT / "members" / "the-fixer" / "check.sh").read_text()
    m = re.search(r"gh pr list --state open --limit 30.*?-q '(.*?)^\s*' 2>>", src, re.S | re.M)
    assert m, "cannot find check.sh's pr-list jq expression -- did its shape change?"
    expr = m.group(1).replace('\'"$cutoff"\'', "2026-09-02T14:00:00Z")

    old = "2026-09-02T10:00:00Z"   # before the cutoff
    new = "2026-09-02T23:00:00Z"   # after it
    green = [{"__typename": "CheckRun", "conclusion": "SUCCESS", "status": "COMPLETED",
              "startedAt": old}]

    prs = [
        # the #291 shape: green, mergeable, unarmed, stale -> MUST be caught
        {"number": 291, "headRefOid": "a" * 40, "mergeStateStatus": "CLEAN", "isDraft": False,
         "autoMergeRequest": None, "updatedAt": old, "createdAt": old,
         "statusCheckRollup": green},
        # armed -> not parked, it is on its way
        {"number": 292, "headRefOid": "b" * 40, "mergeStateStatus": "CLEAN", "isDraft": False,
         "autoMergeRequest": {"enabledAt": old}, "updatedAt": old, "createdAt": old,
         "statusCheckRollup": green},
        # draft -> deliberately not ready
        {"number": 293, "headRefOid": "c" * 40, "mergeStateStatus": "CLEAN", "isDraft": True,
         "autoMergeRequest": None, "updatedAt": old, "createdAt": old,
         "statusCheckRollup": green},
        # green + unarmed but JUST updated -> new, not parked (no flapping on fresh PRs)
        {"number": 294, "headRefOid": "d" * 40, "mergeStateStatus": "CLEAN", "isDraft": False,
         "autoMergeRequest": None, "updatedAt": new, "createdAt": new,
         "statusCheckRollup": green},
    ]

    proc = subprocess.run(["jq", "-r", expr], input=json.dumps(prs),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"jq failed: {proc.stderr.strip()[:300]}"
    picked = {}
    for line in proc.stdout.strip().splitlines():
        if line.strip():
            parts = line.split(":")
            picked[parts[0]] = parts[2]

    assert "291" in picked, (
        "a green, mergeable, unarmed PR is invisible to the-fixer -- it can sit open forever "
        "with nothing red to alarm on (the #291 shape)"
    )
    assert picked["291"] == "green-but-parked", \
        f"caught #291 but mislabelled it as {picked['291']!r}"
    assert "292" not in picked, "an ARMED PR was called parked -- it is on its way to merging"
    assert "293" not in picked, "a DRAFT was called parked -- a draft is explicitly not ready"
    assert "294" not in picked, \
        "a PR that went green seconds ago was called parked -- the arming sweep has not run yet"


def _fixer_does_not_fire_on_a_parked_pr_already_in_the_merge_queue():
    """gh#4305: on a merge-queue-controlled repo, autoMergeRequest stays null for a PR that is
    already enqueued -- the queue entry isn't reflected in that field, so check.sh's original
    `$parked` condition (keyed only on autoMergeRequest) misclassified an already-armed,
    mid-queue PR as green-but-parked. Live case: PR #4285, this repo, 2026-09-04 -- the-fixer
    spawned a sub-pass to "arm" a PR that was already mid-queue, wasting a turn budget.

    `gh pr list --json`/`gh pr view --json` have no `mergeQueueEntry` field at all, so the fix
    is a follow-up `gh api graphql` call (`queued_prs()`) for any green-but-parked candidate.
    This stubs `gh` end to end -- including `repo view` and `api graphql` -- so the real
    exclusion path runs, not just the jq expression `_fixer_sees_a_green_but_parked_pr` covers.
    """
    import shutil
    import subprocess

    if not shutil.which("jq"):
        return  # jq absent here; the exclusion path needs it same as check.sh itself does

    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        repo = Path(tmp) / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)

        # #100 is green-but-parked per the pr-list stub, and IS already in the merge queue --
        # must be excluded. #300 is green-but-parked and NOT in the queue -- must still fire.
        (bin_dir / "gh").write_text(
            "#!/usr/bin/env python3\n"
            "import json, subprocess, sys\n"
            "args = sys.argv[1:]\n"
            "if args[:1] == ['run']:\n"
            "    print('success abc123'); sys.exit(0)\n"
            "if args[:1] == ['pr']:\n"
            "    sys.stdout.write('100:' + 'a'*40 + ':green-but-parked '\n"
            "                     '300:' + 'b'*40 + ':green-but-parked ')\n"
            "    sys.exit(0)\n"
            "if args[:2] == ['repo', 'view']:\n"
            "    print('acme testrepo'); sys.exit(0)\n"
            "if args[:2] == ['api', 'graphql']:\n"
            "    query = next((a[len('query='):] for a in args if a.startswith('query=')), '')\n"
            "    jqf = args[args.index('-q') + 1]\n"
            "    data = {}\n"
            "    if 'pr100:' in query:\n"
            "        data['pr100'] = {'pullRequest': {'mergeQueueEntry': {'state': 'QUEUED'}}}\n"
            "    if 'pr300:' in query:\n"
            "        data['pr300'] = {'pullRequest': {'mergeQueueEntry': None}}\n"
            "    proc = subprocess.run(['jq', '-r', jqf], input=json.dumps({'data': data}),\n"
            "                          capture_output=True, text=True)\n"
            "    sys.stdout.write(proc.stdout)\n"
            "    sys.exit(0)\n"
            "sys.exit(0)\n"
        )
        (bin_dir / "gh").chmod(0o755)

        env = {
            "FLEET_REPO": str(repo),
            "FLEET_LOG_DIR": str(log_dir),
            "FIXER_STATE_FILE": str(log_dir / "the-fixer.state"),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
        }
        proc = subprocess.run(
            ["bash", str(ROOT / "members" / "the-fixer" / "check.sh")],
            capture_output=True, text=True, timeout=30, env=env,
        )
        assert proc.returncode == 0, f"check.sh failed: {proc.stderr.strip()[:300]}"
        out = proc.stdout.strip()
        assert "300" in out, f"a genuinely parked PR (not queued) was dropped: {out!r}"
        assert "100" not in out, (
            f"a PR already enqueued in the merge queue was still fired on as parked: {out!r}"
        )


def _green_pr_with_no_auto_merge_gets_armed():
    """A PR nothing armed must not be able to sit green forever.

    auto-merge is armed in exactly ONE place in this repo -- worktree_builder.sh, at
    PR-creation time. A PR opened by anything else (a human, an external agent, a hand-pushed
    branch) is never armed, so the whole self-heal loop can run end to end and still stop one
    step short of merging, with nothing red for the-fixer to find.

    Live case (fleet-kit#291, 2026-09-02): judge-judy BLOCKed it 15:30, auto_update_branch
    rebased it, judge-judy re-reviewed 15:47 -> fleet-code-review=SUCCESS, selftest green,
    mergeStateStatus CLEAN, automerge=none. Green and parked, indefinitely.

    Stubs `gh` at the same boundary the-fixer's tests do, so the real sweep logic is under
    test. Three PRs: one unarmed (must be armed), one already armed and one draft (must not
    be touched -- a draft is explicitly "not ready", and re-arming muddies the log).
    """
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"; log_dir.mkdir(parents=True, exist_ok=True)
        repo = Path(tmp) / "repo"; repo.mkdir(parents=True, exist_ok=True)
        bin_dir = Path(tmp) / "bin"; bin_dir.mkdir(parents=True, exist_ok=True)
        calls = Path(tmp) / "merge_calls.txt"

        # #291 unarmed, #292 armed, #293 draft+unarmed. The -q expression is evaluated by gh
        # itself in the real thing, so the stub returns what that filter WOULD select.
        #
        # The merge stub rejects a strategy flag exactly as the real `gh` CLI does on a
        # merge-queue-controlled main (fleet-kit#523: fleet-kit's main has a queue now, same as
        # nonprofit-atlas's). A regression back to `--squash` fails this instead of passing
        # silently -- that regression is what left philanthropy PRs unarmed (PR #4215).
        (bin_dir / "gh").write_text(
            "#!/bin/bash\n"
            "if [ \"$1\" = \"repo\" ]; then echo 'The-Good-Project-Team/fleet-kit'; exit 0; fi\n"
            "if [ \"$1\" = \"pr\" ] && [ \"$2\" = \"list\" ]; then\n"
            "  case \"$*\" in *autoMergeRequest*) echo 291; echo 294 ;; *) ;; esac\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = \"pr\" ] && [ \"$2\" = \"view\" ]; then echo \"sha$3\"; exit 0; fi\n"
            "if [ \"$1\" = \"api\" ]; then\n"
            "  # --jq is applied by the real gh; the stub answers what that filter would print.\n"
            "  case \"$*\" in *statuses/sha294*) echo failure ;; *) echo null ;; esac\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = \"pr\" ] && [ \"$2\" = \"merge\" ]; then\n"
            "  case \"$*\" in\n"
            "    *--squash*|*--merge*|*--rebase*) echo '! The merge strategy for main is set "
            "by the merge queue' >&2; exit 1 ;;\n"
            "    *) : ;;\n"
            "  esac\n"
            f"  echo \"$3\" >> {calls}\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n"
        )
        (bin_dir / "gh").chmod(0o755)

        proc = subprocess.run(
            ["bash", str(ROOT / "scripts" / "auto_update_branch.sh")],
            capture_output=True, text=True, timeout=30,
            env={"FLEET_REPO": str(repo), "FLEET_LOG_DIR": str(log_dir),
                 "FLEET_ENV_FILE": "/nonexistent", "PATH": f"{bin_dir}:/usr/bin:/bin"},
        )
        assert proc.returncode == 0, f"script failed: {proc.stderr.strip()[:300]}"

        armed = calls.read_text().split() if calls.exists() else []
        assert "291" in armed, (
            "a green, unarmed PR was never armed -- it can sit open forever: no error, "
            "nothing red, and the-fixer only hunts red"
        )
        assert "293" not in armed, "a DRAFT PR was armed -- a draft is explicitly not ready"
        # fleet-kit#523: #294's head carries fleet-code-review=failure. fleet-code-review is
        # not a required check (a merge queue only waits for checks on its own temporary
        # branch), so re-arming a judge-blocked PR enqueues and MERGES it. Seen live
        # 2026-09-06 01:50Z: #494/#505/#518, all BLOCKed, re-armed by this loop and queued.
        assert "294" not in armed, "a judge-blocked head was re-armed -- the queue would merge it"

        logtext = (log_dir / "auto_update_branch.log").read_text()
        assert "armed" in logtext, "the tick summary never reports how many PRs it armed"


def _fleet_view_reads_the_api_key_from_the_env_file():
    """A key present in fleet.env must be readable by the auth path.

    The container is handed FLEET_ENV_FILE (a PATH) and never the file's values, so the
    server's os.environ has no FLEET_API_KEY no matter what fleet.env holds. Measured live
    2026-09-02 on fleet-kit-server-fleet: fleet.env carried a real key, `printenv
    FLEET_API_KEY` inside the container was empty, and POST /api/login answered

        503 no FLEET_API_KEY configured on this instance

    for the correct key and the wrong one alike -- fail-closed, honest-looking, and a total
    lockout. read_env_state had always read the FILE for exactly this reason; the auth path
    read os.environ instead.

    The existing login test greps the SOURCE (compare_digest, HttpOnly), so it stayed green
    through all of it -- source text cannot show that the key is unreadable. This one runs
    the accessor against a real file with a cleared environment.
    """
    import os as _os
    import importlib as _il
    import tempfile as _tf
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "scripts"))
    with _tf.TemporaryDirectory() as td:
        envf = Path(td) / "fleet.env"
        envf.write_text("# comment\nFLEET_ENABLED=true\nFLEET_API_KEY=secret-from-file\n")
        old_env, old_key = _os.environ.get("FLEET_ENV_FILE"), _os.environ.get("FLEET_API_KEY")
        _os.environ["FLEET_ENV_FILE"] = str(envf)
        _os.environ.pop("FLEET_API_KEY", None)   # the real container state
        try:
            fvs = _il.import_module("fleet_view_server")
            _il.reload(fvs)                      # rebind ENV_FILE to the temp file
            assert fvs.api_key() == "secret-from-file", (
                f"key in fleet.env is invisible to the auth path (got {fvs.api_key()!r}) -- "
                "login answers 503 for every key, correct or not"
            )
            assert fvs.read_env_values().get("FLEET_ENABLED") == "true", "env parse broke"
        finally:
            for k, v in (("FLEET_ENV_FILE", old_env), ("FLEET_API_KEY", old_key)):
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v


def _gru_allowance_dial_actually_changes_the_number():
    """FLEET_GRU_ALLOWANCE_FRACTION was dead config: every value gave the same allowance.

    Live 2026-09-02 on fleet-kit-server-fleet: per_diem_hourly_pct=0.349, ceiling at
    FLEET_SHARE_FRACTION=0.20 was 0.0142. gru.md said

        allowance = (per_diem_hourly_pct - reserved_pct) * FLEET_GRU_ALLOWANCE_FRACTION
        allowance = min(allowance, FLEET_SHARE_CEILING_PCT)

    so min(0.349*F, 0.0142) == 0.0142 for ANY F above ~0.04. Reif set the dial 0.25 -> 0.75
    and nothing changed, because the clamp always won. Worse, the number it always produced
    was the instance's ENTIRE slice -- gru took 100%, leaving nothing for the other eight
    members the fraction exists to reserve for.

    The two vars answer nested questions ("what share of the account is ours?" then "what
    share of ours is gru's?") so they MULTIPLY. This pins that a change to the dial actually
    moves the output, which is the property min() destroyed.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "scripts"))
    from gru_allowance import compute

    ceiling = "0.0142"   # the real measured ceiling from the incident
    quarter = float(compute(ceiling, "0.25"))
    three_q = float(compute(ceiling, "0.75"))
    assert quarter != three_q, (
        f"dial is dead: 0.25 and 0.75 both yield {quarter} -- this is the min() bug"
    )
    # Compare against the true product, not 3*quarter -- the output is rounded to 4 decimals,
    # so scaling a rounded value re-rounds and drifts (3*0.0036 = 0.0108, not 0.0106).
    assert abs(three_q - float(ceiling) * 0.75) < 5e-5, "fraction does not scale the ceiling"
    assert abs(quarter - float(ceiling) * 0.25) < 5e-5, "fraction does not scale the ceiling"
    assert three_q > quarter, "a larger fraction did not yield a larger allowance"

    # gru must never take the whole instance slice: 1-F is what the other eight members get.
    assert three_q < float(ceiling), (
        "gru's allowance equals the entire instance ceiling -- nothing left for marie, jefe, "
        "judge-judy, the-fixer, roomba, dumbledore, messenger"
    )
    assert abs(three_q - 0.0106) < 0.0001, f"expected 0.0142*0.75=0.0106, got {three_q}"


def _gru_allowance_fails_open_and_clamps_typos():
    """No trustworthy ceiling must CONSERVE, never silently mean "unlimited".

    Same law as maxx_reader.py / maxx_share_ceiling.py: an unreadable meter may only ever
    narrow ambition. An empty ceiling prints nothing so gru falls back to its own documented
    default; a real 0.0 is an honest answer and IS printed.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "scripts"))
    from gru_allowance import compute

    assert compute("", "0.75") == "", "missing ceiling did not fail open"
    assert compute(None, "0.75") == "", "absent ceiling did not fail open"
    assert compute("garbage", "0.75") == "", "unparseable ceiling did not fail open"
    assert compute("0.0", "0.75") == "0.0000", "a real zero ceiling must be reported, not hidden"

    # An operator typo must never raise gru above the instance's own slice.
    assert float(compute("0.0142", "1.5")) <= 0.0142, "fraction >1.0 exceeded the ceiling"
    assert float(compute("0.0142", "-1")) >= 0.0, "negative fraction produced a negative allowance"
    # An unset fraction falls back to the documented default, not to 1.0 (the whole slice).
    assert float(compute("0.0142", "")) < 0.0142, "unset fraction defaulted to the entire ceiling"


def _gru_charter_does_not_reinstate_the_broken_math():
    """gru.md must point at the script, not re-derive the number in prose.

    The charter's own rule is "do not do this arithmetic in your head -- you are provably bad
    at it," and then it asked gru to do exactly that. A prose formula is what let the wrong
    base (per_diem_hourly_pct is the hour's BURN, not its headroom) go unnoticed.
    """
    charter = (ROOT / "members" / "gru" / "gru.md").read_text()
    assert "gru_allowance.py" in charter, "charter does not point at the deterministic script"
    assert "min(allowance_pct, FLEET_SHARE_CEILING_PCT)" not in charter, \
        "charter still tells gru to min() the two fractions -- the dead-dial bug"


def _lease_ledger_is_shared_across_instances():
    """Two instances on one box share ONE maxx account pool -- and had two private ledgers.

    Live 2026-09-02: philanthropy kept maxx-leases.json under instances/nonprofit-atlas/logs
    and fleet-kit-server-fleet kept its own under fleet-kit-server-fleet/logs. Neither could
    see the other's in-flight spend, so `reserved_pct` -- the number maxx_share_ceiling.py
    subtracts specifically to prevent double-spend, and whose result its own comment calls
    "already-coordinated" -- was never coordinated across instances at all.
    """
    import importlib, os, sys as _sys
    _sys.path.insert(0, str(ROOT / "scripts"))
    with tempfile.TemporaryDirectory() as tmp:
        shared = Path(tmp) / "shared"
        os.environ["FLEET_LEASE_DIR"] = str(shared)
        os.environ["FLEET_LOG_DIR"] = str(Path(tmp) / "per-instance")
        import maxx_lease
        importlib.reload(maxx_lease)
        try:
            assert str(shared) in str(maxx_lease.STATE_FILE), (
                f"FLEET_LEASE_DIR ignored -- ledger still at {maxx_lease.STATE_FILE}, so each "
                "instance keeps a private ledger and cannot coordinate"
            )
            # A lease taken by one instance must be visible in the other's global total.
            maxx_lease.maxx_reserve(0.05, "a", 3600, instance="philanthropy")
            total = maxx_lease.total_reserved_pct()
            assert abs(total - 0.05) < 1e-9, f"lease invisible in shared total: {total}"
            mine = maxx_lease.reserved_pct_for(instance="server-fleet")
            assert mine == 0.0, "another instance's lease counted against this one's slice"
        finally:
            os.environ.pop("FLEET_LEASE_DIR", None)
            os.environ.pop("FLEET_LOG_DIR", None)
            importlib.reload(maxx_lease)


def _an_instance_cannot_spend_past_its_own_slice():
    """A share must be a RESERVATION, not a rate limit on a race.

    Reif, 2026-09-02: "we could reserve a slice of the hourly for a certain instance, say 30%
    -- and then before gru gets there, it could be all gone." That was literally true: the
    ceiling was computed from whatever remained at the moment of asking, so the first caller
    took the pot and a later caller's 30% was 30% of the leftovers.

    Enforcement means a caller is REFUSED once its own live leases fill its budget, and that
    the refusal protects the neighbour's slice rather than the global pot.
    """
    import importlib, os, sys as _sys
    _sys.path.insert(0, str(ROOT / "scripts"))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["FLEET_LEASE_DIR"] = tmp
        import maxx_lease
        importlib.reload(maxx_lease)
        try:
            budget = 0.30
            maxx_lease.maxx_reserve(0.20, "gru", 3600, instance="A", budget_pct=budget)
            maxx_lease.maxx_reserve(0.09, "judge", 3600, instance="A", budget_pct=budget)
            try:
                maxx_lease.maxx_reserve(0.05, "greedy", 3600, instance="A", budget_pct=budget)
            except maxx_lease.LeaseDenied:
                pass
            else:
                raise AssertionError(
                    "instance A reserved past its own 0.30 slice -- the share is unenforced"
                )
            # B's slice is untouched by A having exhausted its own.
            b = maxx_lease.maxx_reserve(0.30, "b-gru", 3600, instance="B", budget_pct=budget)
            assert b, "instance B was blocked by A's spend -- slices are not independent"
            assert abs(maxx_lease.reserved_pct_for(instance="A") - 0.29) < 1e-9
            assert abs(maxx_lease.reserved_pct_for(instance="B") - 0.30) < 1e-9
        finally:
            os.environ.pop("FLEET_LEASE_DIR", None)
            importlib.reload(maxx_lease)


def _share_ceiling_is_a_slice_of_the_hour_not_the_leftovers():
    """The ceiling must not shrink just because a NEIGHBOUR spent first.

    Old formula: (sustainable - used - reserved) * share -- an instance arriving after a
    greedy neighbour got `share` of the remainder. New: sustainable * share, minus only what
    this instance itself already holds, then clamped to what genuinely remains globally.
    """
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "stubs"; stub.mkdir()
        # A neighbour has burned a big chunk of the hour; this instance has spent nothing.
        (stub / "maxx_reader.py").write_text(
            "def get_headroom():\n"
            "    return (1.0, 'ok', {'sustainable_pct_per_hour': 1.0,\n"
            "                        'per_diem_hourly_pct': 0.50, 'reserved_pct': 0})\n")
        (stub / "maxx_lease.py").write_text(
            "def total_reserved_pct():\n    return 0.0\n"
            "def reserved_pct_for(*a, **k):\n    return 0.0\n")
        kit = Path(tmp) / "scripts"; kit.mkdir()
        import shutil
        shutil.copy(ROOT / "scripts" / "maxx_share_ceiling.py", kit / "maxx_share_ceiling.py")
        for f in stub.glob("*.py"):
            shutil.copy(f, kit / f.name)
        out = subprocess.run([sys.executable, str(kit / "maxx_share_ceiling.py"), "0.30"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        assert out, "ceiling produced no reading"
        got = float(out)
        # Slice of the HOUR: 1.0 * 0.30 = 0.30, and 0.50 remains globally so it is not clamped.
        assert abs(got - 0.30) < 1e-4, (
            f"expected a 0.30 slice of the hour, got {got} -- this is the old "
            "share-of-the-leftovers behaviour (0.5*0.3=0.15) the fix removes"
        )


def _oversubscribed_shares_are_caught():
    """Strict slices only hold if the slices FIT.

    Two instances at 0.60 each reserve 120% of the hour: both stay inside their "own" share,
    every local check passes, and the account is oversubscribed -- the exact race the slices
    remove, restored silently by a config typo. Nothing checked this, so the guard is a script
    a human or cron can run.
    """
    import os, subprocess
    script = ROOT / "scripts" / "check_share_sum.sh"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, frac in (("a", "0.60"), ("b", "0.20")):
            (root / name).mkdir()
            (root / name / "fleet.env").write_text(f"FLEET_SHARE_FRACTION={frac}\n")
        env = {**os.environ, "FLEET_INSTANCES_ROOT": str(root)}
        ok = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
        assert ok.returncode == 0, f"0.60+0.20 wrongly flagged: {ok.stdout}"

        (root / "b" / "fleet.env").write_text("FLEET_SHARE_FRACTION=0.60\n")
        bad = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
        assert bad.returncode == 1, "0.60+0.60 = 1.2 was not flagged as oversubscribed"
        assert "OVERSUBSCRIBED" in bad.stdout


def _check_share_sum_sees_siblings_from_inside_a_container_via_published_shares():
    """gh#293: the ROOT scan above can never fire inside a real container -- findmnt there
    shows only single files bind-mounted from the host, never the `instances/` PARENT tree
    `${FLEET_INSTANCES_ROOT:-$HOME/fleet-kit/instances}` needs (that tree also carries every
    sibling's live CLAUDE_CODE_OAUTH_TOKEN/FLEET_MAXX_KEY, so mounting it wholesale would leak
    credentials across instances). This is the REAL deployed shape: FLEET_INSTANCES_ROOT points
    nowhere (unset, or -- as here -- pointed at a path that simply does not exist), so the old
    scan matches zero directories and total stays 0 no matter how oversubscribed the fleet
    really is.

    FLEET_SHARE_DIR is the fix: a small, purpose-built, non-secret shared directory (same shape
    as FLEET_LEASE_DIR/maxx_lease.py's already-shipped fix for the identical problem) that each
    instance publishes just {instance, fraction, published_at} into. Pre-populates two
    "siblings'" published files by hand (standing in for their own earlier check_share_sum.sh
    runs) and runs the script as a THIRD instance to prove it sees all three, not just itself.
    """
    import os, subprocess, time
    script = ROOT / "scripts" / "check_share_sum.sh"
    with tempfile.TemporaryDirectory() as tmp:
        share_dir = Path(tmp) / "shares"
        share_dir.mkdir()
        now = int(time.time())
        (share_dir / "a.json").write_text(json.dumps({"instance": "a", "fraction": 0.3, "published_at": now}))
        (share_dir / "b.json").write_text(json.dumps({"instance": "b", "fraction": 0.2, "published_at": now}))

        base_env = {**os.environ, "FLEET_INSTANCES_ROOT": str(Path(tmp) / "no-such-instances-tree"),
                    "FLEET_SHARE_DIR": str(share_dir), "FLEET_INSTANCE_NAME": "c"}

        ok = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                             env={**base_env, "FLEET_SHARE_FRACTION": "0.4"})
        assert ok.returncode == 0, f"a(0.3)+b(0.2)+c(0.4)=0.9 wrongly flagged: {ok.stdout}{ok.stderr}"
        assert "a: 0.3" in ok.stdout and "b: 0.2" in ok.stdout and "c: 0.4" in ok.stdout, (
            f"did not see every sibling's published share: {ok.stdout}"
        )

        bad = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                              env={**base_env, "FLEET_SHARE_FRACTION": "0.6"})
        assert bad.returncode == 1, f"a(0.3)+b(0.2)+c(0.6)=1.1 was not flagged as oversubscribed: {bad.stdout}"
        assert "OVERSUBSCRIBED" in bad.stdout


def _check_share_sum_never_reports_ok_on_stale_or_missing_shares():
    """AC4: a stale or unreadable sibling must never be silently folded into a passing "ok" --
    that is the exact "dial that looks set but does nothing" failure this whole issue is about,
    just one layer down (a sibling's PUBLISH went stale instead of the mount never existing).
    """
    import os, subprocess
    script = ROOT / "scripts" / "check_share_sum.sh"

    with tempfile.TemporaryDirectory() as tmp:
        share_dir = Path(tmp) / "shares"
        share_dir.mkdir()
        # Ancient published_at -- long past any sane staleness window.
        (share_dir / "old.json").write_text(json.dumps({"instance": "old", "fraction": 0.9, "published_at": 1}))
        env = {**os.environ, "FLEET_INSTANCES_ROOT": str(Path(tmp) / "no-such-instances-tree"),
               "FLEET_SHARE_DIR": str(share_dir), "FLEET_INSTANCE_NAME": "c", "FLEET_SHARE_FRACTION": "0.1"}
        stale = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
        assert stale.returncode != 0, (
            f"a stale sibling share was silently accepted as ok: {stale.stdout}"
        )
        assert "STALE" in stale.stdout, f"stale share was not called out in the report: {stale.stdout}"
        assert "shares total 0.1" in stale.stdout, (
            f"stale sibling's 0.9 leaked into the total instead of being excluded: {stale.stdout}"
        )

    # The mount/publish path itself is broken (FLEET_SHARE_DIR points at something that is not
    # a usable directory) -- zero fresh shares found, must not read as "ok: shares total 0".
    with tempfile.TemporaryDirectory() as tmp:
        not_a_dir = Path(tmp) / "not-a-dir"
        not_a_dir.write_text("")  # a FILE, so publish_share.sh's mkdir/write both fail
        env = {**os.environ, "FLEET_INSTANCES_ROOT": str(Path(tmp) / "no-such-instances-tree"),
               "FLEET_SHARE_DIR": str(not_a_dir), "FLEET_INSTANCE_NAME": "c", "FLEET_SHARE_FRACTION": "0.6"}
        broken = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
        assert broken.returncode != 0 and "ok:" not in broken.stdout, (
            f"an unusable FLEET_SHARE_DIR still reported ok: {broken.stdout}"
        )
        assert "UNKNOWN" in broken.stdout


def _jefe_owns_the_fleet_wide_token_budget():
    """jefe is the always-on health pass, so cross-instance spend is its layer.

    Reif, 2026-09-02: jefe "is supposed to help with management of token budget across the
    fleet." It already read per-member cost (fleet_db.py spend) but had nothing about the
    account-pool slices every instance shares -- which is exactly where the two live bugs
    hid (a dial combined with min() so every value gave the same number, and per-instance
    lease ledgers that made reserved_pct meaningless across instances).

    Pins that the charter names the tools AND that every tool it names actually exists -- a
    charter citing a missing script sends a pass hunting instead of executing (the same
    failure the-fixer hit with a relative check.sh path).
    """
    charter = (ROOT / "members" / "jefe" / "jefe.md").read_text()
    for dial in ("FLEET_SHARE_FRACTION", "FLEET_GRU_ALLOWANCE_FRACTION"):
        assert dial in charter, f"jefe.md never mentions {dial} -- it cannot manage what it cannot name"
    for tool in ("check_share_sum.sh", "gru_allowance.py", "maxx_share_ceiling.py", "maxx_lease.py"):
        assert tool in charter, f"jefe.md does not tell jefe to check {tool}"
        assert (ROOT / "scripts" / tool).exists(), \
            f"jefe.md cites scripts/{tool} but it does not exist -- the pass will hunt for it"


def _law_carries_the_pr_and_report_contracts():
    """The law every member inherits must carry the PR contract.

    These are the rules that decide whether a human can actually read what the fleet produces
    (Reif, 2026-09-05: PRs in plain language, with an OKR link, under a 50-char title). They live
    in persona_law.md rather than one charter because every member that opens a PR inherits them
    -- and a rule that lives in only one charter is a rule the next member added does not have.

    Reports are deliberately NOT capped: Reif asked for the LONG form of run messages in the
    hourly digest (gh#4455), so a ceiling in the law would starve the surface he actually reads.

    Asserts the contract is PRESENT, not that any given PR obeys it: whether a body reads as
    plain language is a judgement, and a selftest that tried to score prose would be measuring
    typography. What it can prove is that the instruction has not been silently dropped by an
    edit, which is the failure this catches.
    """
    law = (ROOT / "agents" / "persona_law.md").read_text()

    assert "## 10d" in law, \
        "the PR contract is not in the law every member inherits"
    for needle in ("## What this does", "## How this fits the OKR"):
        assert needle in law, f"10d does not name the required PR block {needle!r}"
    assert "50 characters" in law, \
        "10d does not carry the PR title limit -- a 95-char title list is unscannable"
    assert "docs/VISION.md" in law, \
        "10d tells members to name a KR but never points at where the KRs are defined"


def _bash_eval(setup: str, expr: str) -> str:
    """Source account_pool.sh in a scratch HOME and echo one expression's result."""
    import subprocess
    pool = ROOT / "scripts" / "account_pool.sh"
    with tempfile.TemporaryDirectory() as tmp:
        script = f'set -uo pipefail\nexport FLEET_LOG_DIR="{tmp}"\n{setup}\nsource "{pool}"\n{expr}\n'
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
        return proc.stdout.strip()


def _run_loop_actually_uses_the_ordering():
    """account_pool_run must ITERATE the ordering, not just have the function defined.

    Written because the first cut of this feature defined _account_pool_order correctly, passed
    every unit test above (they call the function directly), and still ran the old fixed order --
    the run loop was never wired to it. A function nobody calls is the same "authority written
    down and ignored" failure run_member.sh's own header exists to document.

    Drives the REAL entry point: seed gmail as the soonest reset, make every account's command
    fail as 'other' (so the loop tries them all and gates nobody on the first tick), and assert
    from the log which account the pool reached for FIRST.
    """
    import subprocess
    pool = ROOT / "scripts" / "account_pool.sh"
    now = int(time.time())
    with tempfile.TemporaryDirectory() as tmp:
        state = pathlib.Path(tmp) / "account-pool-exhausted.state"
        log = pathlib.Path(tmp) / "account-pool.log"
        # tgp resets far out, gmail resets soon -> gmail must be reached for FIRST, inverting
        # FLEET_ACCOUNTS order. Both gates are still in the future, so the budget verdict skips
        # both without spending a call -- which is what makes this a clean assertion: the log
        # records the ORDER the loop walked them in, with no command actually run.
        state.write_text(f"tgp {now + 86400}\ngmail {now + 600}\n")
        script = (
            "set -uo pipefail\n"
            f'export FLEET_LOG_DIR="{tmp}"\n'
            'export FLEET_ACCOUNTS="tgp gmail"\n'
            f'export ACCOUNT_POOL_STATE_FILE="{state}"\n'
            f'export ACCOUNT_POOL_LOG_FILE="{log}"\n'
            f'source "{pool}"\n'
            "account_pool_run bash -c 'exit 1' >/dev/null 2>&1 || true\n"
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
        text = log.read_text() if log.exists() else ""

    tried = [ln.split("account=")[1].split()[0]
             for ln in text.splitlines() if "budget verdict=" in ln]
    assert tried, f"pool logged no attempt at all -- log was:\n{text[:500]}"
    assert tried[0] == "gmail", \
        f"run loop ignored the ordering (tried {tried}) -- is account_pool_run still iterating $ACCOUNT_POOL_ORDER?"


def _pool_order(state_lines, accounts="tgp gmail"):
    """Return _account_pool_order's output as a list, given a seeded state file."""
    import subprocess
    pool = ROOT / "scripts" / "account_pool.sh"
    with tempfile.TemporaryDirectory() as tmp:
        state = pathlib.Path(tmp) / "account-pool-exhausted.state"
        state.write_text("".join(line + "\n" for line in state_lines))
        script = (
            "set -uo pipefail\n"
            f'export FLEET_LOG_DIR="{tmp}"\n'
            f'export FLEET_ACCOUNTS="{accounts}"\n'
            f'export ACCOUNT_POOL_STATE_FILE="{state}"\n'
            f'source "{pool}"\n'
            "_account_pool_order\n"
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
        return proc.stdout.split()


def _soonest_reset_account_is_tried_first():
    """The account whose quota resets SOONEST is drained first.

    Quota that resets in an hour is worth less than quota that resets in five days -- whatever
    the near-reset account does not spend is simply lost. A fixed FLEET_ACCOUNTS order always
    drains the same account first, so the other one's window can lapse with quota unspent.

    Seeded so the SECOND account in FLEET_ACCOUNTS resets sooner: order must invert.
    """
    now = int(time.time())
    got = _pool_order([f"tgp {now + 86400}", f"gmail {now + 600}"])
    assert got == ["gmail", "tgp"], \
        f"soonest-reset account not tried first: {got}"

    # ...and the reverse seeding must NOT invert, or the test would pass on any reordering.
    got = _pool_order([f"tgp {now + 600}", f"gmail {now + 86400}"])
    assert got == ["tgp", "gmail"], \
        f"ordering ignored the epochs it was given: {got}"


def _unknown_reset_keeps_configured_order():
    """No known reset for anyone == the old fixed order, exactly.

    This is the normal steady state: both accounts healthy, nothing gated, so nothing has ever
    reported a reset time. The feature must be a NO-OP here rather than inventing an order.
    """
    assert _pool_order([]) == ["tgp", "gmail"], "empty state file changed the order"
    # A malformed epoch must degrade to never-gated, never sort as garbage.
    assert _pool_order(["gmail notanumber"]) == ["tgp", "gmail"], \
        "unreadable state file reordered the pool"


def _known_reset_outranks_unknown():
    """A known future reset sorts ahead of an account with no known reset.

    The known-reset account is the one holding perishable quota; the unknown one is not known
    to be perishable at all.
    """
    now = int(time.time())
    got = _pool_order([f"gmail {now + 600}"])
    assert got == ["gmail", "tgp"], f"known reset did not outrank unknown: {got}"


def _lapsed_reset_outranks_never_gated():
    """gh#462: a gate whose reset epoch has already PASSED ("lapsed") sorts ahead of an account
    that was never gated at all -- the exact case the original PR claimed to fix and didn't.

    Before the fix, _account_pool_order only split "known future reset" from "everything else",
    which silently merged a just-lapsed account into the same bucket as a never-gated one and
    left both in unmodified FLEET_ACCOUNTS order -- so this assertion FAILS against the pre-fix
    code (gmail would come out second, identical to no ordering at all) and passes after it.
    The old test here asserted the opposite (that a lapsed gate keeps configured order), which
    was itself asserting the no-op bug as correct behavior -- see _unknown_reset_keeps_configured_order.
    """
    now = int(time.time())
    # gmail was gated but its reset has already passed; tgp was never gated. gmail's quota just
    # refreshed and is the most perishable in the pool, so it must be tried first.
    got = _pool_order([f"gmail {now - 5000}"])
    assert got == ["gmail", "tgp"], \
        f"lapsed-but-now-eligible account did not outrank never-gated: {got}"

    # ...and the reverse seeding must NOT invert, or the test would pass on any reordering.
    got = _pool_order([f"tgp {now - 5000}"])
    assert got == ["tgp", "gmail"], f"ordering ignored which account actually lapsed: {got}"


def _lapsed_reset_sorts_ahead_of_known_future_gate_too():
    """A three-way mix: a lapsed reset must still outrank a never-gated account even when a
    THIRD, still-gated account is also present -- the known-future bucket must not swallow or
    reorder the lapsed one.
    """
    now = int(time.time())
    got = _pool_order([f"tgp {now + 86400}", f"gmail {now - 5000}"],
                       accounts="tgp gmail primary")
    assert got == ["tgp", "gmail", "primary"], (
        f"three-bucket ordering wrong: {got} "
        "(want known-future 'tgp' first, lapsed 'gmail' second, never-gated 'primary' last)"
    )


def _every_configured_account_survives_ordering():
    """Ordering may reorder, never drop or duplicate -- a dropped account is an account that
    silently never gets tried, which is the failover-is-theater class this pool exists to end.
    """
    now = int(time.time())
    for state in ([], [f"tgp {now + 10}"], [f"tgp {now + 10}", f"gmail {now + 20}"],
                  ["tgp junk", f"gmail {now + 20}"]):
        got = _pool_order(state, accounts="tgp gmail primary")
        assert sorted(got) == ["gmail", "primary", "tgp"], \
            f"ordering dropped or duplicated an account for state={state}: {got}"


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
    """A stated reset must win over the short fallback, so we don't hammer a genuine limit.

    The reset hour must be derived from `now`, not hardcoded: a fixed "1pm" sits under 600s
    from rolling to tomorrow in the ~10 minutes before 13:00 UTC, which turned this into a
    false CI failure independent of any code change (#210 -- confirmed live on PR#209's
    2026-08-29T12:53:12Z run). Picking an hour a few hours ahead of `now` keeps the asserted
    gap (a real reset, not the 300s no-reset-time fallback) comfortably over 600s regardless
    of wall-clock time, including across a midnight rollover.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    target_hour = (now.hour + 3) % 24
    ampm = "am" if target_hour < 12 else "pm"
    h12 = target_hour % 12 or 12
    out = _bash_eval(
        "", f'_account_pool_mark_exhausted acct "hit your weekly limit, resets {h12}{ampm} (UTC)" >/dev/null; '
            'now=$(date +%s); epoch=$(awk \'{print $2}\' "$ACCOUNT_POOL_STATE_FILE"); '
            'echo $(( epoch - now ))'
    )
    assert int(out) > 600, "a stated reset time collapsed to the short fallback"


def _weekly_reset_date_and_hour_is_parsed_not_just_the_hour():
    """46586b3 added a date+hour weekly-reset form ("resets Sep 4, 12am (UTC)") that
    _account_pool_parse_reset must try BEFORE the hour-only form above, or a weekly reset days
    away silently falls through to the ~300s no-reset-time fallback and the account gets
    re-tried (and re-gated) every tick until the real reset (confirmed live on dino
    2026-09-01: gmail's real "resets Sep 4, 12am (UTC)" wasn't recognized by either branch).

    Asserts the exact parsed epoch, not just "> 600s" -- an epoch that merely clears 600s could
    still mean the date got dropped and only the hour-only branch fired.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    target_date = (now + datetime.timedelta(days=3)).date()
    mon = target_date.strftime("%b")
    day = target_date.day
    expected_epoch = int(
        datetime.datetime(
            target_date.year, target_date.month, target_date.day, 0, 0, 0,
            tzinfo=datetime.timezone.utc,
        ).timestamp()
    )
    out = _bash_eval(
        "",
        f'_account_pool_mark_exhausted acct "hit your weekly limit, resets {mon} {day}, 12am (UTC)" >/dev/null; '
        'awk \'{print $2}\' "$ACCOUNT_POOL_STATE_FILE"',
    )
    got_epoch = int(out)
    assert got_epoch == expected_epoch, (
        f"date+hour reset parsed to epoch {got_epoch}, expected {expected_epoch} "
        f"({mon} {day} 00:00 UTC) -- date branch not honored, likely fell through to the "
        f"hour-only branch or the 300s no-reset-time fallback"
    )


def _run_pool_then_readiness(account_cmds):
    """Run account_pool_run once per entry in `account_cmds` (a shell command each) against a
    shared scratch state, then return account_readiness.sh's own output line -- gh#134's ACs
    are about what readiness reports, not the state file's internal shape, so tests should
    assert on that final line rather than the file.
    """
    import subprocess
    pool = ROOT / "scripts" / "account_pool.sh"
    readiness = ROOT / "scripts" / "account_readiness.sh"
    with tempfile.TemporaryDirectory() as tmp:
        lines = [
            "set -uo pipefail",
            f'export FLEET_LOG_DIR="{tmp}"',
            'export FLEET_ACCOUNTS="acct"',
            f'source "{pool}"',
        ]
        for cmd in account_cmds:
            lines.append(f"account_pool_run bash -c {json.dumps(cmd)} >/dev/null 2>&1 || true")
        lines.append(f'bash "{readiness}"')
        proc = subprocess.run(
            ["bash", "-c", "\n".join(lines)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode in (0, 1), f"bash failed: {proc.stderr.strip()[:300]}"
        return proc.stdout.strip().splitlines()[-1]


_UNAUTH_FAIL = 'echo "Error: not logged in" >&2; exit 1'
_OTHER_FAIL = 'echo "some weird transient glitch" >&2; exit 1'
_SUCCEED = 'echo ok'


def _unauthenticated_failure_gates_the_account_on_first_occurrence():
    """gh#134 AC1: the unauthenticated branch must write readiness state BEFORE this fix it was
    a bare `continue` -- account_readiness.sh kept reporting the account healthy for an entire
    auth outage. Gates on the first occurrence (unlike 'other' below): a login failure is
    unambiguous, not the kind of thing a retry self-corrects.
    """
    out = _run_pool_then_readiness([_UNAUTH_FAIL])
    assert "ready=0" in out, f"one unauthenticated failure did not reduce readiness: {out!r}"
    assert "gated=acct:unauthenticated" in out, (
        f"gated account's reason is not visibly tagged 'unauthenticated': {out!r}"
    )


def _single_other_failure_does_not_gate_the_account():
    """gh#134 AC2: a single transient/'other' failure is a blip, not an outage -- it must not
    gate the account (contrast with unauthenticated, which does gate on one occurrence).
    """
    out = _run_pool_then_readiness([_OTHER_FAIL])
    assert "ready=1" in out, f"one 'other' failure wrongly gated the account: {out!r}"


def _other_failure_gates_after_consecutive_threshold():
    """gh#134 AC2: only N CONSECUTIVE 'other' failures for the same account trip the gate.
    account_pool.sh's own default threshold is 3 -- two failures must still leave it ready,
    the third must gate it with a visible 'other' reason (distinct from 'exhausted').
    """
    out_two = _run_pool_then_readiness([_OTHER_FAIL, _OTHER_FAIL])
    assert "ready=1" in out_two, f"two consecutive 'other' failures gated early: {out_two!r}"

    out_three = _run_pool_then_readiness([_OTHER_FAIL, _OTHER_FAIL, _OTHER_FAIL])
    assert "ready=0" in out_three, (
        f"three consecutive 'other' failures did not trip the gate: {out_three!r}"
    )
    assert "gated=acct:other" in out_three, (
        f"gate tripped by 'other' failures is not tagged 'other': {out_three!r}"
    )


def _success_clears_the_other_failure_streak():
    """gh#134 AC3: 'a single transient failure followed by a success does NOT trip the new
    gated state' -- a success must reset the consecutive-failure counter, not just leave it
    paused, or two failures either side of a success would wrongly add up to the threshold.
    """
    out = _run_pool_then_readiness([_OTHER_FAIL, _OTHER_FAIL, _SUCCEED, _OTHER_FAIL, _OTHER_FAIL])
    assert "ready=1" in out, (
        f"failures either side of an intervening success wrongly summed to the gate threshold: {out!r}"
    )


def _exhausted_gate_is_still_visibly_tagged_exhausted():
    """gh#134 AC4/AC5: the pre-existing exhausted branch's state-file format (2 columns, no
    reason) must still read back correctly through the extended readiness output -- a missing
    3rd column must default to 'exhausted', not blank or crash.
    """
    out = _run_pool_then_readiness(['echo "You have reached your usage limit" >&2; exit 1'])
    assert "ready=0" in out, f"an exhausted account was not gated: {out!r}"
    assert "gated=acct:exhausted" in out, (
        f"a legacy 2-column exhausted entry did not default to reason 'exhausted': {out!r}"
    )


def _non_primary_account_without_override_does_not_inherit_the_ambient_token():
    """992ebfe: only the literal "primary" account may inherit the ambient
    CLAUDE_CODE_OAUTH_TOKEN. Any other account with no CLAUDE_CODE_OAUTH_TOKEN_<NAME> override
    must run with that var UNSET, not silently authenticated as whichever account the ambient
    token actually belongs to.

    Live incident, 2026-09-01: "gmail" (no CLAUDE_CODE_OAUTH_TOKEN_GMAIL configured) silently
    inherited the ambient token belonging to "tgp", hit tgp's real weekly cap, and reported the
    failure under gmail's name -- failover was theater, gmail's own credentials.json was never
    tried.
    """
    out = _bash_eval(
        'export FLEET_ACCOUNTS="gmail"\nexport CLAUDE_CODE_OAUTH_TOKEN="ambient-primary-token"',
        "account_pool_run bash -c 'echo TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-UNSET}'",
    )
    assert "TOKEN=UNSET" in out, (
        f"non-primary account with no override still saw CLAUDE_CODE_OAUTH_TOKEN set -- {out!r}"
    )


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


def _account_health_check_actually_pages_when_configured():
    """gh#269: the fleet ran a real ~25h all-accounts-exhausted incident and its only outage
    pager never printed the literal string PAGED, even once, across 69 hourly ticks -- and the
    age/threshold logic this file already unit-tests above (_pool_logs_successes_so_downtime_
    is_measurable) read correct on inspection. Root cause found live, not by re-reading the
    script: entrypoint.sh bakes NTFY_TOPIC into the generated crontab from fleet.env at boot
    (entrypoint.sh:205-211), this box's fleet.env has never had it set, and every tick the
    script's own `${NTFY_TOPIC:?...}` guard fires FIRST and exits before the age/threshold
    logic ever runs -- so a correct-looking check never actually executed its paging branch on
    this box, ever. That guard failing loudly on an unset credential is intentional (a silent
    no-op pager is worse), so this is not "fix the guard" -- it is proving the code the guard
    protects actually pages once someone DOES configure it, which nothing had ever verified by
    running it, only by reading it.

    Exercises the real script end-to-end: a stale success line plus a fresh all-accounts-failed
    line (the real incident's shape), NTFY_TOPIC set, podman stubbed to report DNS healthy (the
    auth-flap/exhaustion class this incident actually was, not the dead-network class), curl
    stubbed to record instead of hitting the real network. Asserts PAGED is printed, the state
    file lands (so a 5-minute cron doesn't re-page every tick), and the ntfy call itself fires
    with the right title and message.

    NOTE: gh#338 made NTFY_TOPIC optional (`NTFY_TOPIC="${NTFY_TOPIC:-}"`) -- paging now routes
    through fleet_alert.sh, which sends BOTH email (durable, no NTFY_TOPIC needed) and ntfy
    (only if NTFY_TOPIC is set). ntfy-only alerting was the gh#269-era design this test used to
    pin; the `${NTFY_TOPIC:?...}` guard that made an unset topic fail loudly no longer exists on
    purpose, so the mirror-image case below now asserts the NEW contract instead: an unset
    NTFY_TOPIC must NOT block the check from running or from attempting delivery.
    """
    import subprocess
    script_path = ROOT / "scripts" / "account_health_check.sh"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log_dir = tmp / "logs"
        log_dir.mkdir()
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        ntfy_calls = tmp / "ntfy_calls.log"

        # Stub curl: record the call instead of reaching the real network (this file's own
        # header promises "no network" for a fresh-clone check).
        (bin_dir / "curl").write_text('#!/bin/bash\necho "$@" >> "$NTFY_CALLS_FILE"\nexit 0\n')
        (bin_dir / "curl").chmod(0o755)
        # Stub podman: DNS resolves fine inside the container, regardless of subcommand -- this
        # incident was the auth-flap/exhaustion class, so the script must go straight to paging
        # rather than detouring into the dead-network auto-recovery branch.
        (bin_dir / "podman").write_text("#!/bin/bash\nexit 0\n")
        (bin_dir / "podman").chmod(0o755)

        # Timestamps must be RECENT, not a 2020 literal. On 2026-09-04 this fixture's
        # 2020-01-01 dates made account_health_check compute a 3,511,675-minute outage and
        # send a REAL page ("ALL accounts exhausted") to a human -- the script calls
        # fleet_alert.sh by absolute path, so the stubbed `curl` on PATH above never
        # intercepted it. The check now refuses ages beyond MAX_PLAUSIBLE_OUTAGE_MINUTES as
        # synthetic, which is right, and which this fixture must respect to test anything.
        from datetime import datetime, timedelta, timezone
        _now = datetime.now(timezone.utc)
        _ok_at = (_now - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S")
        _fail_at = (_now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        pool_log = log_dir / "account-pool.log"
        pool_log.write_text(
            f"[{_ok_at} UTC] account_pool: account=tgp call succeeded\n"
            f"[{_fail_at} UTC] account_pool: ALL accounts in 'tgp gmail' failed this call\n"
        )

        base_env = {
            "FLEET_LOG_DIR": str(log_dir),
            "ACCOUNT_HEALTH_THRESHOLD_MINUTES": "30",
            "NTFY_CALLS_FILE": str(ntfy_calls),
            # Belt and braces with NTFY_CALLS_FILE: the alert helper is invoked by absolute
            # path, so a PATH stub alone cannot stop it reaching Resend. This is what keeps
            # the suite from emailing a human, as it did on 2026-09-04.
            "SELFTEST": "1",
            "PATH": f"{bin_dir}:/usr/bin:/bin",
        }

        # Configured: must actually page.
        proc = subprocess.run(
            ["bash", str(script_path)], capture_output=True, text=True, timeout=30,
            env={**base_env, "NTFY_TOPIC": "selftest-fake-topic"},
        )
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
        assert "PAGED" in proc.stdout, (
            "a real outage-shaped pool log with NTFY_TOPIC configured never printed PAGED -- "
            f"stdout: {proc.stdout[:500]!r} stderr: {proc.stderr[:500]!r}"
        )
        assert (log_dir / ".account_health_paged.state").exists(), \
            "PAGED but no state file written -- a 5-minute cron would re-page every tick"
        assert ntfy_calls.exists() and "ALL accounts exhausted" in ntfy_calls.read_text(), \
            "PAGED but the ntfy call itself never fired (or fired with the wrong message)"

        # NTFY_TOPIC unset (gh#338): email is the durable channel now, so this must still work
        # rather than fail closed -- the ntfy leg is simply skipped by fleet_alert.sh itself.
        (log_dir / ".account_health_paged.state").unlink()
        ntfy_calls.unlink(missing_ok=True)
        proc = subprocess.run(
            ["bash", str(script_path)], capture_output=True, text=True, timeout=30,
            env={**base_env, "NTFY_TOPIC": ""},
        )
        assert proc.returncode == 0, (
            f"an unset NTFY_TOPIC must not block the check from running: {proc.stderr.strip()[:300]}"
        )
        assert "PAGED" in proc.stdout, (
            "an unset NTFY_TOPIC must not stop the check from attempting to page (email still "
            f"can) -- stdout: {proc.stdout[:500]!r} stderr: {proc.stderr[:500]!r}"
        )
        assert (log_dir / ".account_health_paged.state").exists(), \
            "PAGED but no state file written with NTFY_TOPIC unset"
        # No ntfy leg fires with NTFY_TOPIC unset -- fleet_alert.sh's ntfy branch is itself
        # gated on NTFY_TOPIC, confirming the check didn't route around fleet_alert.sh entirely.
        assert not ntfy_calls.exists(), "an unset NTFY_TOPIC must never reach the ntfy leg"


def _liveness_fixture(tmp, newest_ok_age_s, exhausted_reset_in_s=None):
    """A fleet.db with one ok run at the given age, plus the selftest alert sandbox."""
    import sqlite3
    log_dir = tmp / "logs"; log_dir.mkdir(exist_ok=True)
    db = sqlite3.connect(str(log_dir / "fleet.db"))
    db.execute("create table runs (run_id text, member text, status text, recorded_at real, primary key (run_id, recorded_at))")
    now = time.time()
    db.execute("insert into runs values ('jefe-1', 'jefe', 'ok', ?)", (now - newest_ok_age_s,))
    db.execute("insert into runs values ('minion-2', 'minion', 'started', ?)", (now - 60,))
    db.commit(); db.close()
    if exhausted_reset_in_s is not None:
        (log_dir / "account-pool-exhausted.state").write_text(
            f"tgp {int(now + exhausted_reset_in_s)}\ngmail {int(now + exhausted_reset_in_s)}\n")
    calls = tmp / "calls.log"
    env = {"FLEET_LOG_DIR": str(log_dir), "FLEET_INSTANCE_NAME": "selftest-inst",
           "NTFY_CALLS_FILE": str(calls), "SELFTEST": "1", "PATH": "/usr/bin:/bin"}
    return env, calls


def _run_liveness(env):
    import subprocess
    return subprocess.run(["bash", str(ROOT / "scripts" / "member_liveness_check.sh")],
                          capture_output=True, text=True, timeout=30, env=env)


def _member_liveness_pages_critical_when_no_member_has_done_work():
    """fleet-kit#512: the dead man's switch. Sep 3-5 the crontab was discarded and no member
    ran for 40h while every probe stayed green. Newest `ok` in fleet.db older than the window,
    with no exhaustion state, must page critical with problem=silent on the FIRST tick."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env, calls = _liveness_fixture(tmp, newest_ok_age_s=5 * 3600)
        proc = _run_liveness(env)
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
        assert "PAGED silent" in proc.stdout, f"stdout: {proc.stdout!r} stderr: {proc.stderr[:300]!r}"
        text = calls.read_text() if calls.exists() else ""
        assert "problem=silent severity=critical" in text, f"calls: {text!r}"
        assert "silent for 5h" in text, f"page must carry the silence length: {text!r}"


def _member_liveness_is_quiet_and_resolves_when_a_member_worked_recently():
    """A recent `ok` is a heartbeat: no page, and the check closes any alarm it opened before
    (fleet_alert.sh itself drops a recovery nobody was paged for, so this is safe every tick)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env, calls = _liveness_fixture(tmp, newest_ok_age_s=10 * 60)
        proc = _run_liveness(env)
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
        assert proc.stdout.strip().endswith("OK"), f"stdout: {proc.stdout!r}"
        text = calls.read_text() if calls.exists() else ""
        assert "page " not in text, f"a 10-minute-old ok run must never page: {text!r}"
        assert "resolve " in text, f"a healthy tick must resolve the check: {text!r}"


def _member_liveness_names_the_reset_when_the_pool_is_exhausted():
    """Aug 30 - Sep 1: 25h of budget_declined runs, no page. Genuinely out of tokens is a
    STATE, paged once, degraded not critical, and the page carries the pool's own reset time
    -- so a human knows nothing is broken and when it resumes."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env, calls = _liveness_fixture(tmp, newest_ok_age_s=6 * 3600, exhausted_reset_in_s=20 * 3600)
        proc = _run_liveness(env)
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
        assert "PAGED out_of_tokens" in proc.stdout, f"stdout: {proc.stdout!r} stderr: {proc.stderr[:300]!r}"
        text = calls.read_text() if calls.exists() else ""
        assert "problem=out_of_tokens severity=degraded" in text, f"calls: {text!r}"
        assert "out of tokens until" in text and "UTC" in text, f"page must name the reset: {text!r}"
        # A reset already in the PAST is not exhaustion -- that pool should be working again.
        (tmp / "logs" / "account-pool-exhausted.state").write_text(f"tgp {int(time.time()) - 3600}\n")
        calls.unlink(missing_ok=True)
        proc = _run_liveness(env)
        assert "PAGED silent" in proc.stdout, f"a stale reset must fall through to silent: {proc.stdout!r}"


def _fleet_alert_queues_an_undelivered_alarm_and_retries_it_next_call():
    """fleet-kit#512: on 2026-09-04 both legs failed and the log said ALARM UNDELIVERED -- and
    that was the end of it. Now an alarm neither channel took waits in a queue and is retried
    at the front of the next call, so a mail-provider blip delays a page instead of eating it.
    Exercises the real script: no alert.env in the sandbox (email leg skips), NTFY_TOPIC set,
    curl stubbed to fail then succeed."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        bin_dir = tmp / "bin"; bin_dir.mkdir()
        calls = tmp / "curl_calls.log"
        (bin_dir / "curl").write_text('#!/bin/bash\necho "$@" >> "$CURL_CALLS"\nexit "${CURL_RC:-0}"\n')
        (bin_dir / "curl").chmod(0o755)
        log = tmp / "fleet_alert.log"; queue = tmp / "alerts_undelivered.jsonl"
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "NTFY_TOPIC": "selftest-fake-topic",
               "FLEET_ALERT_LOG": str(log), "FLEET_ALERT_QUEUE": str(queue),
               "CURL_CALLS": str(calls), "SELFTEST": "1"}
        script = str(ROOT / "scripts" / "fleet_alert.sh")

        proc = subprocess.run(["bash", script, "first title", "first body"], capture_output=True,
                              text=True, timeout=30, env={**env, "CURL_RC": "22"})
        assert proc.returncode == 0, f"helper must never exit non-zero: {proc.stderr[:300]}"
        assert queue.exists() and queue.read_text().count("\n") == 1, \
            f"an alarm both legs dropped must be queued, got: {queue.read_text() if queue.exists() else None!r}"
        assert "first title" in queue.read_text()
        assert "UNDELIVERED (queued" in log.read_text(), log.read_text()

        calls.unlink(missing_ok=True)
        proc = subprocess.run(["bash", script, "second title", "second body"], capture_output=True,
                              text=True, timeout=30, env={**env, "CURL_RC": "0"})
        assert proc.returncode == 0, proc.stderr[:300]
        sent = calls.read_text()
        assert "[retry] first title" in sent, f"queued alarm must be retried first: {sent!r}"
        assert "second title" in sent, sent
        assert sent.index("[retry] first title") < sent.index("second title"), "retry goes before the new alarm"
        assert queue.read_text().strip() == "", f"delivered retry must leave the queue: {queue.read_text()!r}"


def _number_read_fetches_from_a_url_and_renders_five_lines():
    """fleet-kit#513: the venture's number goes above every charter. --fetch reads the URL
    (file:// here, so the suite touches no network) and writes number.json + a history line;
    --render prints the header with values and 7d deltas; a stale read says STALE up front."""
    import subprocess, json as _json
    script = ROOT / "scripts" / "number_read.py"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "number.json.src"
        src.write_text(_json.dumps({
            "as_of": "2026-09-05T23:00:00Z",
            "number": {"name": "Stripe MRR", "value": 10.83, "unit": "$/mo", "delta_7d": 0.83},
            "guardrail": {"name": "entities with >=1 attributable interaction", "value": 572, "unit": "entities", "delta_7d": 181},
            "channel": {"name": "sessions, last full week", "value": 108849, "unit": "sessions/wk", "delta_7d": 12470},
            "errors": [],
        }))
        env = {"PATH": "/usr/bin:/bin", "FLEET_LOG_DIR": str(tmp / "logs"),
               "FLEET_NUMBER_URL": src.as_uri(), "FLEET_NUMBER_TOKEN": "t"}
        proc = subprocess.run(["python3", str(script), "--fetch"], capture_output=True, text=True, timeout=30, env=env)
        assert proc.returncode == 0, proc.stderr[:300]
        assert (tmp / "logs" / "number.json").exists(), "fetch must write number.json"
        assert (tmp / "logs" / "number_history.jsonl").read_text().count("\n") == 1, "fetch must append one history line"

        proc = subprocess.run(["python3", str(script), "--render"], capture_output=True, text=True, timeout=30, env=env)
        out = proc.stdout
        lines = [l for l in out.splitlines() if l.strip()]
        assert len(lines) == 5, f"header must be five lines, got {len(lines)}: {out!r}"
        assert "Stripe MRR = 10.83 $/mo (+0.83 in 7d)" in out, out
        assert "572 entities (+181 in 7d)" in out, out
        assert "108,849 sessions/wk (+12,470 in 7d)" in out, out
        assert "STALE" not in out, "a fresh read must not say STALE"

        data = _json.loads((tmp / "logs" / "number.json").read_text())
        data["fetched_at"] -= 3 * 86400
        (tmp / "logs" / "number.json").write_text(_json.dumps(data))
        proc = subprocess.run(["python3", str(script), "--render"], capture_output=True, text=True, timeout=30, env=env)
        assert proc.stdout.splitlines()[0].startswith("THE NUMBER") and "STALE" in proc.stdout.splitlines()[0], proc.stdout

        proc = subprocess.run(["python3", str(script), "--render"], capture_output=True, text=True, timeout=30,
                              env={**env, "FLEET_NUMBER_URL": ""})
        assert proc.stdout.strip() == "", f"no URL must render nothing: {proc.stdout!r}"

        before = (tmp / "logs" / "number.json").read_text()
        proc = subprocess.run(["python3", str(script), "--fetch"], capture_output=True, text=True, timeout=30,
                              env={**env, "FLEET_NUMBER_URL": (tmp / "missing.json").as_uri()})
        assert proc.returncode == 0 and "keeping previous" in proc.stderr, proc.stderr
        assert (tmp / "logs" / "number.json").read_text() == before


def _number_read_never_renders_zero_for_an_unmeasured_reading():
    """KPI doctrine rule 5 at the prompt: a reading the endpoint could not take is 'unmeasured',
    never 0 -- a zero here would tell every member the business has no revenue."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib
    nr = importlib.import_module("number_read")
    out = nr.render({"fetched_at": int(time.time()), "as_of": "x",
                     "number": None, "guardrail": {"name": "g", "value": 0, "unit": "entities", "delta_7d": None},
                     "channel": None, "errors": ["number: credential_missing: STRIPE_API_KEY"]})
    assert "Number: unmeasured" in out, out
    assert "g = 0 entities (delta unmeasured)" in out, out
    assert "credential_missing" in out, out
    assert " 0 $/mo" not in out


def _run_member_puts_the_number_header_above_item_and_task():
    """The header must be the FIRST thing a member reads -- above --item and --task -- and its
    absence must be silent (no URL, no header, no failure)."""
    src = (ROOT / "scripts" / "run_member.sh").read_text()
    hook = src.index('number_read.py" --render')
    item = src.index('if [ -n "$ITEM" ]; then')
    task = src.index('if [ -n "$TASK" ]; then')
    assert hook < item < task, "number header must be composed before --item and --task"
    assert "|| true" in src[hook:hook + 200], "a failed render must never kill the member run"
    entry = (ROOT / "entrypoint.sh").read_text()
    assert "number_read.py --fetch" in entry, "nothing schedules the fetch -- the header would be STALE forever"
    assert "FLEET_NUMBER_URL" in (ROOT / "fleet.env.example").read_text()


def _roomba_runs_as_a_script_and_records_a_quiet_pass():
    """fleet-kit#514: roomba is a shell runner now. Its spec dispatches run_member.sh to
    roomba.sh, and that script drives the real roomba.py on a real (tmp) git repo and records
    the pass through run_report.py -- so fleet.db/status/fleet_kpi see it exactly as before,
    minus the 25-30 turns of window the model pass spent re-reading its own dry-run."""
    import subprocess
    spec = json.loads((ROOT / "members" / "roomba" / "roomba.fleet.json").read_text())
    assert spec.get("llm", {}).get("runner") == "members/roomba/roomba.sh", spec.get("llm")
    assert spec.get("kind") == "shell"
    runner = ROOT / "members" / "roomba" / "roomba.sh"
    assert runner.exists() and runner.stat().st_mode & 0o111, "roomba.sh must be executable for run_member.sh"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        repo = tmp / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        logs = tmp / "logs"
        env = {"PATH": "/usr/bin:/bin", "FLEET_REPO": str(repo), "FLEET_LOG_DIR": str(logs), "HOME": str(tmp)}
        proc = subprocess.run(["bash", str(runner)], capture_output=True, text=True, timeout=60, env=env)
        assert proc.returncode == 0, f"rc={proc.returncode} stderr={proc.stderr[:400]}"
        assert proc.stdout.startswith("QUIET"), f"an empty repo must be a quiet pass: {proc.stdout!r}"
        runs = (logs / "runs.jsonl").read_text().strip().splitlines()
        assert len(runs) == 1, f"exactly one run must be recorded, got {len(runs)}: {runs!r}"
        rec = json.loads(runs[-1])
        assert rec.get("member") == "roomba" and rec.get("kind") == "shell", rec
        assert rec.get("status") == "quiet", rec
        assert "0 evaluated, 0 removed" in (rec.get("outcome") or ""), rec.get("outcome")


def _datta_cadence_is_a_validated_cron_hour_dial():
    """fleet-kit#514: the datta hour field is instance-tunable like gru's, and joins the same
    validated family -- so the value that discarded a whole crontab for 40h (0,30) is refused
    for this dial too, from the Settings page and from selftest."""
    import fleet_view_server as fvs
    entry = (ROOT / "entrypoint.sh").read_text()
    assert '12 ${FLEET_DATTA_CADENCE:-*} * * *' in entry, "datta line must splice FLEET_DATTA_CADENCE into the hour field"
    assert "FLEET_DATTA_CADENCE" in fvs.DIAL_FIELDS and "FLEET_DATTA_CADENCE" in fvs._CRON_HOUR_FIELDS
    assert fvs._validate_dial_value("FLEET_DATTA_CADENCE", "0,30"), "0,30 must be refused (it is minutes, not an hour)"
    assert fvs._validate_dial_value("FLEET_DATTA_CADENCE", "$(id)")
    for ok in ("", "*", "9", "*/6", "0,12", "1-5"):
        assert fvs._validate_dial_value("FLEET_DATTA_CADENCE", ok) is None, ok
    assert "FLEET_DATTA_CADENCE" in (ROOT / "fleet.env.example").read_text()


def _sync_health_check_pages_on_a_real_stalled_offset_not_on_a_caught_up_one():
    """gh#273: tail_runs_forever is the only thing keeping fleet.db in sync with runs.jsonl,
    and nothing watched whether it was still alive -- account_health_check.sh,
    tunnel_health_check.sh and path_health_check.sh watch the account pool, the tunnel, and
    per-instance dashboard routing, none of them fleet.db's own freshness. sync_health_check.sh
    closes that gap by comparing sync_state.offset (fleet.db) against runs.jsonl's on-disk
    byte size.

    Same style as _account_health_check_actually_pages_when_configured: exercises the real
    script end-to-end rather than re-deriving its logic by inspection, curl stubbed via
    NTFY_CALLS_FILE so nothing reaches the real network.

    (a) A stalled offset against a runs.jsonl that kept growing -- with the gap already old
        enough to cross the threshold -- must page.
    (b) An offset that tracks the file size (freshly synced) must NOT page, and must clear any
        prior gap/paged state so a real recovery is announced instead of staying silent.
    """
    import os
    import subprocess
    script_path = ROOT / "scripts" / "sync_health_check.sh"
    fleet_db_path = ROOT / "scripts" / "fleet_db.py"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log_dir = tmp / "logs"
        log_dir.mkdir()
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        ntfy_calls = tmp / "ntfy_calls.log"
        runs_file = log_dir / "runs.jsonl"

        (bin_dir / "curl").write_text('#!/bin/bash\necho "$@" >> "$NTFY_CALLS_FILE"\nexit 0\n')
        (bin_dir / "curl").chmod(0o755)

        base_env = {
            "FLEET_LOG_DIR": str(log_dir),
            "SYNC_HEALTH_THRESHOLD_MINUTES": "15",
            "NTFY_CALLS_FILE": str(ntfy_calls),
            "SELFTEST": "1",
            "PATH": f"{bin_dir}:/usr/bin:/bin",
        }

        def _run_check(env_extra):
            return subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=30,
                env={**base_env, **env_extra},
            )

        def _sync():
            proc = subprocess.run(
                [sys.executable, str(fleet_db_path), "sync"], capture_output=True, text=True,
                timeout=30, env={**os.environ, "FLEET_LOG_DIR": str(log_dir)},
            )
            assert proc.returncode == 0, f"fleet_db.py sync failed: {proc.stderr[:300]}"

        runs_file.write_text(json.dumps({
            "run_id": "r1", "member": "minion", "kind": "build",
            "status": "ok", "outcome": "x", "evidence": "y",
        }) + "\n")
        _sync()  # offset now == size -- a real, caught-up starting point.

        # (b) caught up: must not page, must print healthy.
        proc = _run_check({"NTFY_TOPIC": "selftest-fake-topic"})
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
        assert "healthy" in proc.stdout, (
            f"offset tracking runs.jsonl's size was not reported healthy -- stdout: {proc.stdout[:400]!r}"
        )
        assert "PAGED" not in proc.stdout, "a caught-up offset must never page"
        assert not ntfy_calls.exists(), "a caught-up offset must never reach the ntfy leg"

        # New data lands but nothing syncs it -- the thread-died scenario. Back-date the gap's
        # own first-seen marker past the threshold so this run behaves as a PERSISTED gap, not
        # a fresh one from this instant (same reasoning account_health_check.sh's own test
        # documents: a gap measured from "just now" can never cross a threshold).
        runs_file.write_text(runs_file.read_text() + json.dumps({
            "run_id": "r2", "member": "minion", "kind": "build",
            "status": "ok", "outcome": "x", "evidence": "y",
        }) + "\n")
        old_epoch = int(datetime.datetime.now().timestamp()) - 20 * 60
        (log_dir / ".sync_health_gap_since.state").write_text(str(old_epoch))

        # (a) stalled offset (fleet.db was never re-synced) vs a grown runs.jsonl: must page.
        proc = _run_check({"NTFY_TOPIC": "selftest-fake-topic"})
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
        assert "PAGED" in proc.stdout, (
            "a 20-minute-old offset/size gap (threshold 15m) never printed PAGED -- "
            f"stdout: {proc.stdout[:500]!r} stderr: {proc.stderr[:500]!r}"
        )
        assert (log_dir / ".sync_health_paged.state").exists(), \
            "PAGED but no state file written -- a 5-minute cron would re-page every tick"
        assert ntfy_calls.exists() and "fleet.db sync stalled" in ntfy_calls.read_text(), \
            "PAGED but the ntfy call itself never fired (or fired with the wrong message)"

        # Recovery: catching fleet.db up again must clear both state files and stop paging.
        ntfy_calls.unlink(missing_ok=True)
        _sync()
        proc = _run_check({"NTFY_TOPIC": "selftest-fake-topic"})
        assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
        assert "healthy" in proc.stdout, f"did not recover after re-sync -- stdout: {proc.stdout[:400]!r}"
        assert not (log_dir / ".sync_health_gap_since.state").exists(), "gap marker survived recovery"
        assert not (log_dir / ".sync_health_paged.state").exists(), "paged marker survived recovery"


def _sync_health_check_repages_on_a_fixed_interval():
    """Mirrors account_health_check.sh's own gh#266 fix (see
    _account_health_check_repages_on_a_fixed_interval_gh266 below): a long-lived sync-loop
    outage must not page once and then go silent for the rest of it. Exercises PAGED_STATE_FILE
    directly (no need to re-derive an old gap through the full first-page flow) with its own
    (raw-epoch) first line already older, or younger, than SYNC_HEALTH_REPAGE_MINUTES.
    """
    import subprocess
    script_path = ROOT / "scripts" / "sync_health_check.sh"

    def _run(paged_minutes_ago):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            log_dir = tmp / "logs"
            log_dir.mkdir()
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            ntfy_calls = tmp / "ntfy_calls.log"
            runs_file = log_dir / "runs.jsonl"

            (bin_dir / "curl").write_text('#!/bin/bash\necho "$@" >> "$NTFY_CALLS_FILE"\nexit 0\n')
            (bin_dir / "curl").chmod(0o755)

            # A real, persisted gap: fleet.db was never synced (offset stays 0) against a
            # non-empty runs.jsonl, with the gap's own age already past THRESHOLD_MINUTES.
            runs_file.write_text(json.dumps({
                "run_id": "r1", "member": "minion", "kind": "build",
                "status": "ok", "outcome": "x", "evidence": "y",
            }) + "\n")
            old_gap_epoch = int(datetime.datetime.now().timestamp()) - 20 * 60
            (log_dir / ".sync_health_gap_since.state").write_text(str(old_gap_epoch))

            paged_epoch = int(datetime.datetime.now().timestamp()) - paged_minutes_ago * 60
            (log_dir / ".sync_health_paged.state").write_text(str(paged_epoch) + "\n")

            env = {
                "FLEET_LOG_DIR": str(log_dir),
                "SYNC_HEALTH_THRESHOLD_MINUTES": "15",
                "SYNC_HEALTH_REPAGE_MINUTES": "60",
                "NTFY_CALLS_FILE": str(ntfy_calls),
                "NTFY_TOPIC": "selftest-fake-topic",
                "SELFTEST": "1",
                "PATH": f"{bin_dir}:/usr/bin:/bin",
            }
            proc = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=30, env=env,
            )
            assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
            calls = ntfy_calls.read_text() if ntfy_calls.exists() else ""
            state = (log_dir / ".sync_health_paged.state").read_text() \
                if (log_dir / ".sync_health_paged.state").exists() else ""
            return proc.stdout, calls, state

    # PAGED_STATE_FILE's timestamp is 90 minutes old, past the 60-minute repage interval.
    stdout_old, calls_old, state_old = _run(90)
    assert "STILL" in calls_old, (
        "a PAGED_STATE_FILE timestamp older than SYNC_HEALTH_REPAGE_MINUTES did not re-page -- "
        f"stdout: {stdout_old[:400]!r} ntfy calls: {calls_old[:400]!r}"
    )
    assert "last_repage=" in state_old, (
        f"a re-page fired but PAGED_STATE_FILE was not updated with the new repage timestamp: {state_old!r}"
    )

    # PAGED_STATE_FILE's timestamp is 10 minutes old, well inside the 60-minute repage interval.
    stdout_young, calls_young, _state_young = _run(10)
    assert calls_young == "", (
        "a PAGED_STATE_FILE timestamp younger than SYNC_HEALTH_REPAGE_MINUTES wrongly re-paged: "
        f"{calls_young[:400]!r}"
    )
    assert "already paged" in stdout_young, (
        "between re-page intervals the existing 'gap open ... already paged' line must still "
        f"print: {stdout_young[:400]!r}"
    )


def _account_health_check_repages_on_a_fixed_interval_gh266():
    """gh#266: account_health_check.sh paged exactly once per outage, then went silent for the
    rest of it no matter how long it ran -- the 2026-08-30->09-01 outage paged once at 33
    minutes then logged 585 consecutive silent ticks over the remaining ~48h45m
    (account_health_check.cron.log:164). This asserts the fix's actual live behavior end to
    end: with STATE_FILE's own stored timestamp older than ACCOUNT_HEALTH_REPAGE_MINUTES, a
    SECOND _ntfy call fires while the outage is still open; with it younger, no second call
    fires and the existing "already paged" tick line still prints (AC4).
    """
    import subprocess
    from datetime import datetime, timedelta, timezone
    script_path = ROOT / "scripts" / "account_health_check.sh"

    def _run(paged_minutes_ago):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            log_dir = tmp / "logs"
            log_dir.mkdir()
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            ntfy_calls = tmp / "ntfy_calls.log"

            (bin_dir / "curl").write_text('#!/bin/bash\necho "$@" >> "$NTFY_CALLS_FILE"\nexit 0\n')
            (bin_dir / "curl").chmod(0o755)
            (bin_dir / "podman").write_text("#!/bin/bash\nexit 0\n")
            (bin_dir / "podman").chmod(0o755)

            _now = datetime.now(timezone.utc)
            ok_at = (_now - timedelta(minutes=200)).strftime("%Y-%m-%d %H:%M:%S")
            fail_at = (_now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
            (log_dir / "account-pool.log").write_text(
                f"[{ok_at} UTC] account_pool: account=tgp call succeeded\n"
                f"[{fail_at} UTC] account_pool: ALL accounts in 'tgp gmail' failed this call\n"
            )
            state_file = log_dir / ".account_health_paged.state"
            paged_at = (_now - timedelta(minutes=paged_minutes_ago)).strftime("%Y-%m-%d %H:%M UTC")
            state_file.write_text(paged_at + "\n")

            env = {
                "FLEET_LOG_DIR": str(log_dir),
                "ACCOUNT_HEALTH_THRESHOLD_MINUTES": "30",
                "ACCOUNT_HEALTH_REPAGE_MINUTES": "60",
                "NTFY_CALLS_FILE": str(ntfy_calls),
                "NTFY_TOPIC": "selftest-fake-topic",
                "SELFTEST": "1",
                "PATH": f"{bin_dir}:/usr/bin:/bin",
            }
            proc = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=30, env=env,
            )
            assert proc.returncode == 0, f"bash failed: {proc.stderr.strip()[:300]}"
            calls = ntfy_calls.read_text() if ntfy_calls.exists() else ""
            state = state_file.read_text() if state_file.exists() else ""
            return proc.stdout, calls, state

    # STATE_FILE's timestamp is 90 minutes old, past the 60-minute repage interval.
    stdout_old, calls_old, state_old = _run(90)
    assert "STILL" in calls_old, (
        "a STATE_FILE timestamp older than ACCOUNT_HEALTH_REPAGE_MINUTES did not re-page -- "
        f"stdout: {stdout_old[:400]!r} ntfy calls: {calls_old[:400]!r}"
    )
    assert "last_repage=" in state_old, (
        f"a re-page fired but STATE_FILE was not updated with the new repage timestamp: {state_old!r}"
    )

    # STATE_FILE's timestamp is 10 minutes old, well inside the 60-minute repage interval.
    stdout_young, calls_young, _state_young = _run(10)
    assert calls_young == "", (
        "a STATE_FILE timestamp younger than ACCOUNT_HEALTH_REPAGE_MINUTES wrongly re-paged: "
        f"{calls_young[:400]!r}"
    )
    assert "already paged" in stdout_young, (
        "between re-page intervals the existing 'failing but only ...m old ... already paged' "
        f"line must still print: {stdout_young[:400]!r}"
    )


def _nothing_hardcodes_a_read_of_the_frozen_instance_log_mirror():
    """No script or charter may read instances/<name>/logs/*.jsonl as a live data source.

    #132: `/fleet-kit/instances/nonprofit-atlas/logs/runs.jsonl` froze at 1798 lines while the
    canonical `$FLEET_LOG_DIR/runs.jsonl` (bind-mounted from a host instances/<name>/logs/ dir
    by deploy.sh, see up.sh:13) kept growing -- reading the frozen copy made all 12 roster
    members look stale-by-hours simultaneously, indistinguishable from a fleet-wide scheduler
    outage that per-member raw logs proved was not happening. `instances/` is gitignored and
    dockerignored on purpose (host/deployment state, never baked into the image or the repo),
    so the only fix this repo can own is refusing to let any script grow a habit of reading
    that path directly -- everything must go through $FLEET_LOG_DIR instead.

    #132's PRD cites a prior fix for the same drift class on a `roomba_ghosts_state.json` file;
    that file was not found anywhere in this repo when this check was written (grepped clean),
    so this check cannot be pinned to it -- it stands alone, generalized to the whole
    instances/*/logs/*.jsonl file class rather than one name.
    """
    pattern = re.compile(r"""instances/[^/\s"'{}]+/logs/\S*\.jsonl""")
    hits = []
    for path in ROOT.rglob("*"):
        if path.is_dir() or path == Path(__file__).resolve():
            continue
        if ".git" in path.parts or path.suffix not in {".py", ".sh", ".md"}:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        hits.extend(f"{path.relative_to(ROOT)}: {m.group(0)}" for m in pattern.finditer(text))
    assert not hits, f"hardcoded read of the frozen instances/*/logs mirror: {hits}"


def _self_evolution_panel_catches_the_member_dash_branch_shape():
    """#237: the Self-Evolution panel missed PR #214 (dumbledore, merged) because its branch,
    `member/dumbledore-186507-1788016382`, is the generic per-item dispatch shape shared with
    minion/roomba/the-fixer -- not the `dumbledore/...` shape the panel's one `head:` search
    matched. Confirmed live: the panel read "nothing lately" (30h-98h old PRs only) when the
    true answer was a ~6h-old dumbledore PR editing the fleet's own conduct rules.

    Also proves the four raw `head:` searches are deduplicated by PR number before being
    returned -- a fixture where PR #214 appears in two of the four raw lists must still yield
    it exactly once in self_evolution.
    """
    import fleet_view_server as fvs

    member_dumbledore_pr = {
        "number": 214, "title": "fix(persona_law): ...", "mergedAt": "2026-08-29T17:32:13Z",
        "url": "https://github.com/x/y/pull/214", "author": {"login": "dumbledore"},
        "files": [], "headRefName": "member/dumbledore-186507-1788016382",
    }
    member_jefe_pr = {
        "number": 215, "title": "fix(jefe): ...", "mergedAt": "2026-08-29T10:00:00Z",
        "url": "https://github.com/x/y/pull/215", "author": {"login": "jefe"},
        "files": [], "headRefName": "member/jefe-999-1788000000",
    }

    def fake_gh(*args, timeout=15):
        if "--search" in args:
            search = args[args.index("--search") + 1]
            if search == "head:dumbledore/":
                # Same PR also (implausibly) matches this search, to prove dedup.
                return json.dumps([member_dumbledore_pr])
            if search == "head:member/dumbledore-":
                return json.dumps([member_dumbledore_pr])
            if search == "head:member/jefe-":
                return json.dumps([member_jefe_pr])
            return "[]"
        return "[]"

    orig_gh = fvs._gh
    fvs._gh = fake_gh
    try:
        state = fvs.poll_gh_state()
    finally:
        fvs._gh = orig_gh

    numbers = [pr["number"] for pr in state["self_evolution"]]
    assert 214 in numbers, f"PR #214 (member/dumbledore-... branch) missing from self_evolution: {numbers}"
    assert numbers.count(214) == 1, f"PR #214 appears more than once, dedup failed: {numbers}"
    assert 215 in numbers, f"PR #215 (member/jefe-... branch) missing from self_evolution: {numbers}"
    mergedats = [pr["mergedAt"] for pr in state["self_evolution"]]
    assert mergedats == sorted(mergedats, reverse=True), "self_evolution not sorted by mergedAt desc"


def _pr_tile_rollup_reflects_mergeability_not_just_ci():
    """#179: the PR tile's badge came from `statusCheckRollup` (CI) alone -- a PR stuck BEHIND
    or BLOCKED (GitHub's own mergeability verdict, unrelated to CI) still rendered green as
    long as its checks were green. Confirmed live 2026-08-29: 7/7 open PRs were BEHIND/BLOCKED
    while the dashboard showed passing/green for every one with green CI.

    Four fixtures, one call: a BEHIND PR and a DIRTY (real merge conflict) PR, both with
    all-green checks, must NOT roll up to "green" (AC2, and the PRD's own "dirty-or-other"
    bucket); a CLEAN PR with all-green checks must still roll up to "green" -- no regression to
    the healthy case (AC3); a BLOCKED PR with a FAILING check keeps "failing", the worse of the
    two signals, rather than being masked by the newer "blocked" bucket.
    """
    import fleet_view_server as fvs

    def _pr(number, merge_state, checks):
        return {"number": number, "title": f"{merge_state} fixture", "isDraft": False,
                "headRefName": f"x/{number}", "url": f"https://github.com/x/y/pull/{number}",
                "statusCheckRollup": checks, "mergeStateStatus": merge_state,
                "updatedAt": "2026-08-29T00:00:00Z"}

    green_check = [{"state": "SUCCESS"}]
    failing_check = [{"state": "FAILURE"}]

    behind_green_pr = _pr(301, "BEHIND", green_check)
    clean_green_pr = _pr(302, "CLEAN", green_check)
    blocked_failing_pr = _pr(303, "BLOCKED", failing_check)
    dirty_green_pr = _pr(304, "DIRTY", green_check)

    def fake_gh(*args, timeout=15):
        if args[:2] == ("pr", "list") and "open" in args:
            return json.dumps([behind_green_pr, clean_green_pr, blocked_failing_pr, dirty_green_pr])
        return "[]"

    orig_gh = fvs._gh
    fvs._gh = fake_gh
    try:
        state = fvs.poll_gh_state()
    finally:
        fvs._gh = orig_gh

    by_number = {pr["number"]: pr for pr in state["prs"]}
    assert by_number[301]["_rollup"] != "green", \
        f"BEHIND PR with green checks rolled up green: {by_number[301]['_rollup']}"
    assert by_number[302]["_rollup"] == "green", \
        f"CLEAN PR with green checks regressed off green: {by_number[302]['_rollup']}"
    assert by_number[303]["_rollup"] == "failing", \
        f"BLOCKED+FAILING PR should keep the worse 'failing' signal: {by_number[303]['_rollup']}"
    assert by_number[304]["_rollup"] != "green", \
        f"DIRTY PR with green checks rolled up green: {by_number[304]['_rollup']}"


def _up_sh_never_emits_the_shared_image_tag_into_a_generated_fleet_env():
    """gh#395: two instances on one box both defaulting to the same `fleet-kit:latest` image
    tag meant their independent 5-minute auto_deploy.sh builds raced on one shared image --
    the confirmed root cause of the 27h outage PR#393 patched the symptom of. The fix is
    up.sh deriving a per-instance tag from $NAME and writing it into the fleet.env it
    generates, so deploy.sh's `${FLEET_IMAGE_NAME:-fleet-kit:latest}` read (scripts/deploy.sh:94)
    picks up the per-instance value instead of falling through to the shared default.

    Extracts the REAL fleet.env-generation block out of up.sh (not a reimplementation) and runs
    it for two different --name values against the real fleet.env.example template, same
    pattern as _run_member_logs_critical_when_postflight_dirty_check_fails_to_source above.
    Asserts each generated fleet.env's FLEET_IMAGE_NAME line is instance-derived and distinct
    -- closing the class the way _every_entrypoint_scheduled_script_is_actually_scheduled
    (PR#382) closed the pager-wiring class, so a 3rd hardcoded-shared-resource regression
    (a future edit dropping the `-e "s|^FLEET_IMAGE_NAME=.*"` line, say) fails CI instead of
    waiting for a nerd pass to find it.
    """
    import subprocess

    up_sh = (ROOT / "up.sh").read_text()
    start_marker = 'if [ ! -f "$ENV_FILE" ]; then'
    assert start_marker in up_sh, "up.sh no longer guards fleet.env generation -- did gh#395's fix regress?"
    i = up_sh.index(start_marker)
    j = up_sh.index("\nfi\n", i) + len("\nfi")
    snippet = up_sh[i:j]
    assert "FLEET_IMAGE_NAME" in snippet, \
        "up.sh's fleet.env-generation block no longer writes FLEET_IMAGE_NAME -- did gh#395's fix regress?"

    def generate(name, tmp):
        env_file = Path(tmp) / "fleet.env"
        script = (
            f'set -euo pipefail\n'
            f'cd "{ROOT}"\n'
            f'NAME="{name}"\n'
            f'IMAGE_TAG="fleet-kit:{name}"\n'
            f'ACCOUNTS="primary"\n'
            f'ENV_FILE="{env_file}"\n'
            f"{snippet}\n"
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"fleet.env-generation snippet itself failed: {proc.stderr.strip()[:300]}"
        return env_file.read_text()

    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        env_a = generate("alpha", tmp_a)
        env_b = generate("beta", tmp_b)

    def image_name_line(env_text):
        lines = [l for l in env_text.splitlines() if l.startswith("FLEET_IMAGE_NAME=")]
        assert len(lines) == 1, f"expected exactly one FLEET_IMAGE_NAME= line, got: {lines}"
        return lines[0]

    line_a = image_name_line(env_a)
    line_b = image_name_line(env_b)
    assert line_a == "FLEET_IMAGE_NAME=fleet-kit:alpha", \
        f"instance 'alpha' did not get its own derived tag: {line_a}"
    assert line_b == "FLEET_IMAGE_NAME=fleet-kit:beta", \
        f"instance 'beta' did not get its own derived tag: {line_b}"
    assert line_a != line_b, "two different --name instances produced the SAME FLEET_IMAGE_NAME"
    assert "fleet-kit:latest" not in (line_a, line_b), \
        "a generated fleet.env's FLEET_IMAGE_NAME still fell through to the shared 'fleet-kit:latest' tag"


if __name__ == "__main__":
    check("PR tile rollup reflects mergeability, not just CI (#179)", _pr_tile_rollup_reflects_mergeability_not_just_ci)
    check("member specs load and validate", _member_specs_validate)
    check("member_spec's OWN default MEMBERS_DIR resolves (not just an explicit path)", _members_dir_default_is_right)
    check("report contract: ok + silence is recorded", _report_contract)
    check("a fan-out parent that never reports is incomplete_fanout, not reported_nothing", _incomplete_fanout_is_not_reported_nothing)
    check("datta's --task \"lane=<lane>\" dispatch shape also reads as incomplete_fanout (gh#257)", _incomplete_fanout_matches_task_dispatch_shape)
    check("a detected gh#167 trailing-turn loss is report_lost, not reported_nothing (gh#257 AC2/AC3)", _report_lost_is_not_reported_nothing)
    check("gh#167 trailing-loss signal flows end-to-end, stream_log.py -> run_report.py (gh#257 AC2/AC3)", _gh167_trailing_loss_flows_end_to_end_through_stream_log_and_run_report)
    check("run_member.sh wires --trailing-loss-out/--trailing-loss between the two scripts (gh#257)", _run_member_wires_trailing_loss_flag)
    check("classify() prefers incomplete_fanout over report_lost on overlap (gh#461)", _incomplete_fanout_outranks_trailing_loss_on_overlap)
    check("_ARTIFACT accepts a backtick-wrapped path/PID/SHA (#251)", _artifact_regex_accepts_backtick_spans)
    check("a pass's Prediction survives for the NEXT pass to verify", _rsi_lines_survive_to_the_next_pass)
    check("fleet.db run_id collisions don't lose a verdict", _fleet_db_run_id_collisions_dont_lose_a_verdict)
    check("query_runs(item_id=) matches free-text #N mentions, not just the build-claim column (gh#405)", _fleet_db_query_runs_item_id_matches_free_text_mentions)
    check("query_runs(item_id=\"\") behaves like item_id=None, not an unlimited full-table scan (gh#484)", _fleet_db_query_runs_empty_item_id_is_treated_like_none)
    check("fleet.db composite-PK migration is lock-serialized", _fleet_db_composite_pk_migration_is_lock_serialized)
    check("fanout packs the hour by complexity, in percent", _fanout_packs_the_hour_by_complexity)
    check("cost_bridge converts real spend into fanout's --observed shape", _cost_bridge_converts_real_spend_into_fanouts_observed_shape)
    check("claim_history blocks an item that keeps dead-ending", _claim_history_blocks_an_item_that_keeps_dead_ending)
    check("gru.md checks claim_history before claiming", _gru_md_checks_claim_history_before_claiming)
    check("maxx reader reports the fleet's hourly slice, not a laptop's pacing", _maxx_reader_reports_the_fleets_hourly_slice_not_a_laptops_pacing)
    check("maxx lease reserves, releases, and self-expires", _maxx_lease_reserves_releases_and_self_expires)
    check("maxx lease concurrent reserves don't clobber each other", _maxx_lease_concurrent_reserves_dont_clobber_each_other)
    check("maxx share ceiling uses hourly headroom, not the week bank", _maxx_share_ceiling_uses_hourly_headroom_not_the_week_bank)
    check("maxx share ceiling subtracts local leases, not just the remote's reserved_pct", _maxx_share_ceiling_subtracts_local_leases_not_just_the_remotes_reserved_pct)
    check("maxx share ceiling respects a real over verdict, not just unreadable meters", _maxx_share_ceiling_respects_a_real_over_verdict_not_just_unreadable_meters)
    check("no member ships a turn or budget cap", _no_member_ships_a_cap)
    check("minion knows the browser in its own image exists", _minion_knows_the_browser_exists)
    check("score reasoning is not guillotined mid-word", _score_reasoning_is_not_guillotined_mid_word)
    check("self-evolution evidence covers fleet-kit's own repo, not just $FLEET_REPO", _self_evo_evidence_covers_both_repos)
    check("self_improve_score.sh's evidence catches the member/<name>-<id> branch shape", _self_improve_score_evidence_covers_member_branch_shape)
    check("DAILY_OUTCOMES carries hours_elapsed for a partial today (#263)", _daily_outcomes_carries_hours_elapsed_for_partial_today)
    check("jefe can unstick a PR that is merely behind its base", _jefe_can_unstick_a_pr_that_is_merely_behind)
    check("jefe.md's precedent citations are repo-qualified, and the verify-before-you-cite guard is present", _jefe_precedent_citations_are_repo_qualified)
    check("arming auto-merge passes no strategy flag, and checks it worked", _auto_merge_never_passes_a_strategy_flag_under_a_merge_queue)
    check("--task adds to a charter, never replaces it", _adhoc_task_adds_to_the_charter_never_replaces_it)
    check("a killed pass is recorded, not silently lost", _a_killed_pass_is_recorded_not_lost)
    check("run_member.sh writes a started row before claude -p and before the SIGTERM trap arms", _run_member_writes_a_started_row_before_claude_p)
    check("run_report's started row pairs with a later completion by run_id", _run_report_started_row_pairs_with_a_later_completion_by_run_id)
    check("lost_passes flags a started row with no completion past the grace window", _lost_passes_flags_a_started_row_with_no_completion_past_the_grace_window)
    check("signal_rate/dormant exclude killed+timed_out, not just budget_declined", _signal_rate_excludes_all_never_executed_statuses)
    check("a leaked absolute-path write into $REPO is caught and alerted", _postflight_dirty_check_catches_a_leaked_absolute_path_write)
    check("a git-status failure alerts rather than reading as clean", _postflight_dirty_check_alerts_rather_than_hides_a_git_status_failure)
    check("auto-deploy race check detects an unrecognized git failure outside auto_deploy.sh's own path", _auto_deploy_race_check_detects_the_unrecognized_git_failure)
    check("the-fixer dedup does not let one stuck PR mute the batch", _fixer_dedup_does_not_let_one_stuck_pr_mute_the_batch)
    check("the-fixer dedup still suppresses an unchanged batch", _fixer_dedup_still_suppresses_an_unchanged_batch)
    check("the-fixer dedup expires so a wedge cannot last forever", _fixer_dedup_expires_so_a_wedge_cannot_last_forever)
    check("the-fixer charter handles every reason check.sh emits", _fixer_charter_handles_every_reason_check_sh_emits)
    check("auto-deploy race check dedups an already-recorded line", _auto_deploy_race_check_dedups_an_already_recorded_line)
    check("auto-deploy race check escalates after 3 consecutive sanctioned ABORTs", _auto_deploy_race_check_escalates_after_three_consecutive_sanctioned_aborts)
    check("auto-deploy race check does not alert on a self-resolving sanctioned ABORT", _auto_deploy_race_check_does_not_alert_on_a_self_resolving_sanctioned_abort)
    check("every entrypoint.sh-scheduled incident script is actually scheduled (gh#378, table-driven)", _every_entrypoint_scheduled_script_is_actually_scheduled)
    check("run_member.sh logs CRITICAL when postflight_dirty_check.sh fails to source", _run_member_logs_critical_when_postflight_dirty_check_fails_to_source)
    check("run_member.sh rejects a non-numeric --item", _run_member_rejects_a_non_numeric_item)
    check("both worktree callers check $REPO before tearing the worktree down", _run_member_and_builder_check_repo_before_removing_the_worktree)
    check("deploy drains in-flight passes before cutover", _deploy_drains_inflight_passes)
    check("deploys never stack, and the drain can count to zero", _one_deploy_at_a_time_and_a_countable_drain)
    check("auto_deploy.sh self-heals a content-identical diverged HEAD only when opted in", _auto_deploy_sh_self_heals_a_content_identical_diverged_head_when_opted_in)
    check("git_pull_guard.sh self-heals a stray branch and leaves a normal pull unchanged", _git_pull_guard_self_heals_a_stray_branch_and_leaves_a_normal_pull_unchanged)
    check("git_pull_guard.sh serializes via a lock on the .git directory", _git_pull_guard_serializes_via_a_lock_on_the_git_directory)
    check("judge-judy ticks don't overlap", _judge_judy_ticks_dont_overlap)
    check("a judge-judy BLOCK pulls the PR out of the merge queue (fleet-kit#523)",
          _judge_block_pulls_the_pr_out_of_the_queue)
    check("judge-judy lock lives somewhere persistent", _judge_judy_lock_lives_somewhere_persistent)
    check("judge-judy strikes are head-scoped and leave diagnosable evidence", _judge_judy_strikes_are_scoped_by_head_and_leave_diagnosable_evidence)
    check("board_github file_item can add a priority label alongside backlog/lane", _board_github_file_item_can_add_a_priority_label)
    check("judge-judy files a priority-high fix item when it blocks a PR", _judge_judy_files_a_fix_item_on_block)
    check("marie re-judges the whole backlog, not just the new", _marie_sweeps_the_whole_backlog_not_just_the_new)
    check("marie writes a build-ready PRD and minion reads it", _marie_writes_a_prd_and_minion_reads_it)
    check("the-fixer catches a check that never answers", _fixer_catches_the_no_answer_class)
    check("datta dispatches by coverage, nerds analyse one lane", _datta_dispatches_and_nerds_analyse)
    check("nerd rejects an invalid lane before any lane-specific work (gh#374)", _nerd_invalid_lane_rejected_before_lane_work)
    check("nerd's STRUCTURAL-N/A marker wires to datta's down-rank rule (gh#451)", _nerd_structural_na_marker_wires_to_datta_downrank)
    check("a run records the item it worked", _a_run_records_the_item_it_worked)
    check("every pass files a written report", _every_pass_files_a_written_report)
    check("every scheduled member is actually on cron", _every_scheduled_member_is_actually_on_cron)
    check("FLEET_CRON_MEMBERS gates entrypoint.sh's generated crontab", _fleet_cron_members_gates_entrypoint_crontab)
    check("account + tunnel health checks are actually scheduled", _account_and_tunnel_health_checks_are_actually_scheduled)
    check("every required health-check script in README is actually scheduled", _required_health_check_scripts_in_readme_are_scheduled)
    check("account-heartbeat + budget-read have host-only schedulers, never an entrypoint.sh line (gh#376)", _account_heartbeat_and_budget_read_have_host_only_schedulers)
    check("NTFY_TOPIC is deferred to tick-time, not baked in at boot", _ntfy_topic_is_deferred_to_tick_time_not_baked_in_at_boot)
    check("every gh api call in a shell script is timeout-guarded", _every_gh_api_call_is_timeout_guarded)
    check("deploy staleness check reads a baked SHA and only alerts past budget", _deploy_staleness_check_reads_a_baked_sha_and_only_alerts_past_budget)
    check("deploy.sh's host log dir survives sourcing the instance's container-scoped fleet.env", _deploy_sh_host_log_dir_survives_sourcing_the_instances_container_scoped_fleet_env)
    check("deploy cordons the fleet, then drains, and always uncordons", _deploy_cordons_then_drains_and_always_uncordons)
    check("deploy.sh's log is durable regardless of caller", _deploy_log_is_durable_regardless_of_caller)
    check("overrides tune dials, refuse authority", _overrides_are_narrow)
    check("overrides store never resolves under $HOME/.claude", _overrides_store_is_not_under_home_dot_claude)
    check("fleet.env.example present, fleet.env untracked", _env_example_exists)
    check("schedulers ship for macOS and Linux", _schedulers_for_both_platforms)
    check("fleet-view write routes are authenticated and fail closed", _write_routes_are_authenticated)
    check("fleet_settings rejects unsafe or malformed dial values (gh#233)", _fleet_settings_rejects_unsafe_or_malformed_dial_values)
    check("fleet-view UI can actually authenticate a write", _fleet_view_ui_can_actually_authenticate_a_write)
    check("fleet-view login is still fail-closed", _fleet_view_login_is_still_fail_closed)
    check("gru allowance dial actually changes the number", _gru_allowance_dial_actually_changes_the_number)
    check("gru allowance fails open and clamps typos", _gru_allowance_fails_open_and_clamps_typos)
    check("gru charter does not reinstate the broken math", _gru_charter_does_not_reinstate_the_broken_math)
    check("lease ledger is shared across instances", _lease_ledger_is_shared_across_instances)
    check("an instance cannot spend past its own slice", _an_instance_cannot_spend_past_its_own_slice)
    check("share ceiling is a slice of the hour, not the leftovers", _share_ceiling_is_a_slice_of_the_hour_not_the_leftovers)
    check("oversubscribed instance shares are caught", _oversubscribed_shares_are_caught)
    check("check_share_sum sees siblings from inside a container via published shares",
          _check_share_sum_sees_siblings_from_inside_a_container_via_published_shares)
    check("check_share_sum never reports ok on stale or missing published shares",
          _check_share_sum_never_reports_ok_on_stale_or_missing_shares)
    check("jefe owns the fleet-wide token budget", _jefe_owns_the_fleet_wide_token_budget)
    check("the-fixer sees a green-but-parked PR", _fixer_sees_a_green_but_parked_pr)
    check("the-fixer does not fire on a parked PR already in the merge queue",
          _fixer_does_not_fire_on_a_parked_pr_already_in_the_merge_queue)
    check("a green PR with no auto-merge gets armed", _green_pr_with_no_auto_merge_gets_armed)
    check("fleet-view reads FLEET_API_KEY from fleet.env", _fleet_view_reads_the_api_key_from_the_env_file)
    check("FLEET_API_KEY never reaches an LLM pass", _api_key_never_reaches_an_llm)
    check("incidental 'rate limit' text does not gate an account", _classifier_ignores_incidental_rate_limit_text)
    check("law carries the PR contract", _law_carries_the_pr_and_report_contracts)
    check("soonest-reset account is tried first", _soonest_reset_account_is_tried_first)
    check("no known reset keeps configured order", _unknown_reset_keeps_configured_order)
    check("known reset outranks unknown reset", _known_reset_outranks_unknown)
    check("lapsed reset outranks never-gated (gh#462)", _lapsed_reset_outranks_never_gated)
    check("lapsed reset still outranks never-gated alongside a known-future gate", _lapsed_reset_sorts_ahead_of_known_future_gate_too)
    check("ordering never drops an account", _every_configured_account_survives_ordering)
    check("run loop actually uses the ordering", _run_loop_actually_uses_the_ordering)
    check("a real usage limit is still classified exhausted", _classifier_still_catches_a_real_limit)
    check("exhaustion with no stated reset backs off minutes, not an hour", _unparseable_exhaustion_gates_briefly_not_for_an_hour)
    check("a stated reset time is honored over the fallback", _a_real_reset_time_is_still_honored)
    check("a date+hour weekly reset is parsed, not just the hour-only form", _weekly_reset_date_and_hour_is_parsed_not_just_the_hour)
    check("an unauthenticated failure gates readiness on the first occurrence", _unauthenticated_failure_gates_the_account_on_first_occurrence)
    check("a single 'other' failure does not gate the account", _single_other_failure_does_not_gate_the_account)
    check("'other' failures gate only after the consecutive threshold", _other_failure_gates_after_consecutive_threshold)
    check("a success clears the 'other' failure streak", _success_clears_the_other_failure_streak)
    check("an exhausted gate still reads back tagged 'exhausted' via readiness", _exhausted_gate_is_still_visibly_tagged_exhausted)
    check("a non-primary account with no override does not inherit the ambient oauth token", _non_primary_account_without_override_does_not_inherit_the_ambient_token)
    check("pool logs successes so outage length is measurable", _pool_logs_successes_so_downtime_is_measurable)
    check("account health check actually pages when configured (and never claims to when it isn't)", _account_health_check_actually_pages_when_configured)
    check("account health check re-pages on a fixed interval instead of once (gh#266)", _account_health_check_repages_on_a_fixed_interval_gh266)
    check("roomba runs as a script and records a quiet pass through run_report (fleet-kit#514)", _roomba_runs_as_a_script_and_records_a_quiet_pass)
    check("FLEET_DATTA_CADENCE is a validated cron-hour dial (fleet-kit#514)", _datta_cadence_is_a_validated_cron_hour_dial)
    check("number_read fetches from a URL and renders the five-line header (fleet-kit#513)", _number_read_fetches_from_a_url_and_renders_five_lines)
    check("number_read never renders zero for an unmeasured reading (fleet-kit#513)", _number_read_never_renders_zero_for_an_unmeasured_reading)
    check("run_member puts the number header above --item and --task (fleet-kit#513)", _run_member_puts_the_number_header_above_item_and_task)
    check("member liveness pages critical when no member has done work (fleet-kit#512)", _member_liveness_pages_critical_when_no_member_has_done_work)
    check("member liveness is quiet and resolves after a recent ok run (fleet-kit#512)", _member_liveness_is_quiet_and_resolves_when_a_member_worked_recently)
    check("member liveness names the reset time when the pool is exhausted (fleet-kit#512)", _member_liveness_names_the_reset_when_the_pool_is_exhausted)
    check("fleet_alert queues an undelivered alarm and retries it next call (fleet-kit#512)", _fleet_alert_queues_an_undelivered_alarm_and_retries_it_next_call)
    check("sync health check pages on a real stalled offset, not on a caught-up one (gh#273)", _sync_health_check_pages_on_a_real_stalled_offset_not_on_a_caught_up_one)
    check("sync health check re-pages on a fixed interval instead of once", _sync_health_check_repages_on_a_fixed_interval)
    check("nothing hardcodes a read of the frozen instances/*/logs mirror", _nothing_hardcodes_a_read_of_the_frozen_instance_log_mirror)
    check("self-evolution panel catches the member/<name>-<id> branch shape", _self_evolution_panel_catches_the_member_dash_branch_shape)
    check("gru.md clamps allowance_pct to FLEET_SHARE_CEILING_PCT", _gru_md_clamps_allowance_to_share_ceiling)
    check("lane_kpi classifies ticks and ignores in-progress drains", _lane_kpi_classifies_ticks_and_ignores_in_progress_drains)
    check("lane_kpi is append-only and distinguishes missing from stale", _lane_kpi_is_append_only_and_distinguishes_missing_from_stale)
    check("fleet_kpi's roomba pattern catches all three real 'evaluated' phrasings", _fleet_kpi_roomba_catches_all_three_real_evaluated_phrasings)
    check("fleet_kpi's marie pattern catches her real triage verb vocabulary", _fleet_kpi_marie_catches_her_real_triage_verb_vocabulary)
    check("fleet_kpi's marie pattern ignores an explicit-zero-counted verb (gh#409)", _fleet_kpi_marie_ignores_explicit_zero_counted_verb_gh409)
    check("fleet_kpi's gru/jefe/minion ship a real 'PRs shipped' count", _fleet_kpi_gru_jefe_minion_ship_a_real_prs_shipped_count)
    check("fleet_kpi's PR ref catches no-space 'PR#N' and plural shared-prefix forms (gh#448)", _fleet_kpi_pr_ref_no_space_and_plural_shared_prefix_gh448)
    check("fleet_kpi's nerd pattern catches filed/commented/posted/edited verbs", _fleet_kpi_nerd_catches_filed_and_commented_verbs)
    check("dormant flags an enabled member with zero runs in-window, given a roster", _dormant_flags_an_enabled_member_with_zero_runs_in_window)
    check("runs_summary() excludes provisional started rows from total/signal_rate/agent_rates (gh#437)", _runs_summary_excludes_started_rows_from_total_and_signal_rate)
    check("status page's Deploy component classifies a STALE line as down (gh#367)", _status_page_deploy_component_classifies_stale_as_down)
    check("status_data.members() reads fleet.db in-process, no podman on $PATH needed (gh#364)", _status_data_members_reads_fleet_db_with_no_podman_on_path)
    check("status_data's other four components are unaffected by the members() fix (gh#364)", _status_data_other_components_unaffected_by_members_fix)
    check("status page's Public path resolves the bare cron.log before the suffixed/legacy fallbacks (gh#387)", _status_page_public_path_resolves_bare_cron_log_first)
    check("status page's hourly-cadence log is not flattened to a 5-minute back-fill (gh#387)", _status_page_hourly_log_cadence_not_flattened_to_5min)
    check("status page banner distinguishes unknown from good and bad (gh#358)", _status_page_banner_distinguishes_unknown_from_good)
    check("up.sh never emits the shared 'fleet-kit:latest' tag into a generated fleet.env (gh#395)", _up_sh_never_emits_the_shared_image_tag_into_a_generated_fleet_env)
    check("status_data.live_alerts() proxies alert_store without touching overall (gh#399)", _status_data_live_alerts_proxies_alert_store_without_touching_overall)
    check("status_data.live_alerts() fails open on a broken store (gh#399)", _status_data_live_alerts_fails_open_on_a_broken_store)
    check("status page renders a live-alert banner distinct from the component grid (gh#399)", _status_page_renders_live_alert_banner_distinct_from_component_grid)
    check("status page's transient alert does not render as a confirmed fault (gh#399)", _status_page_transient_alert_does_not_render_as_a_confirmed_fault)

    for n in ok:
        print(f"  ok    {n}")
    for n, why in fail:
        print(f"  FAIL  {n}\n        {why}")
    print(f"\n{len(ok)} passed, {len(fail)} failed")
    raise SystemExit(1 if fail else 0)
