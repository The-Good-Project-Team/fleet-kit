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

    dep = (Path(__file__).parent / "deploy.sh").read_text()
    i = dep.find("inflight=\"$(podman exec")
    assert i != -1, "drain no longer counts in-flight passes"
    line = dep[i:dep.find("\n", i)]
    assert "|| echo 0" not in line, "`|| echo 0` on pgrep -c yields '0\\n0', which never equals 0"
    assert "tr -cd '0-9'" in line, "in-flight count is not sanitised to digits"


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

    # nerd must never self-fire: datta decides coverage, so a cron-fired nerd would run a lane
    # nobody chose. Same contract minion has.
    spec = json.loads((root / "members" / "nerd" / "nerd.fleet.json").read_text())
    assert spec["enabled"] is False, "nerd self-fires -- it would run lanes datta never chose"
    assert spec.get("schedule"), "empty schedule fails member_spec validation (found live)"


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
    first_route = body.find('if path == "/api/')
    assert gate != -1, "do_POST has no _authorized() gate -- write routes are unauthenticated"
    assert gate < first_route, "auth gate sits AFTER a route -- that route is unprotected"

    # Fail closed: no key configured must mean no remote writes, never "auth disabled".
    auth = src.split("def _authorized", 1)[1].split("\n    def ", 1)[0]
    assert "return False" in auth, "_authorized never denies -- cannot be failing closed"
    assert "compare_digest" in auth, "key compared without hmac.compare_digest (timing leak)"


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
    check("a pass's Prediction survives for the NEXT pass to verify", _rsi_lines_survive_to_the_next_pass)
    check("fanout packs the hour by complexity, in percent", _fanout_packs_the_hour_by_complexity)
    check("no member ships a turn or budget cap", _no_member_ships_a_cap)
    check("--task adds to a charter, never replaces it", _adhoc_task_adds_to_the_charter_never_replaces_it)
    check("a killed pass is recorded, not silently lost", _a_killed_pass_is_recorded_not_lost)
    check("deploy drains in-flight passes before cutover", _deploy_drains_inflight_passes)
    check("deploys never stack, and the drain can count to zero", _one_deploy_at_a_time_and_a_countable_drain)
    check("marie re-judges the whole backlog, not just the new", _marie_sweeps_the_whole_backlog_not_just_the_new)
    check("marie writes a build-ready PRD and minion reads it", _marie_writes_a_prd_and_minion_reads_it)
    check("the-fixer catches a check that never answers", _fixer_catches_the_no_answer_class)
    check("datta dispatches by coverage, nerds analyse one lane", _datta_dispatches_and_nerds_analyse)
    check("deploy cordons the fleet, then drains, and always uncordons", _deploy_cordons_then_drains_and_always_uncordons)
    check("overrides tune dials, refuse authority", _overrides_are_narrow)
    check("fleet.env.example present, fleet.env untracked", _env_example_exists)
    check("schedulers ship for macOS and Linux", _schedulers_for_both_platforms)
    check("fleet-view write routes are authenticated and fail closed", _write_routes_are_authenticated)
    check("FLEET_API_KEY never reaches an LLM pass", _api_key_never_reaches_an_llm)
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
