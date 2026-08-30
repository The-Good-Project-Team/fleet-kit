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
    bad = _re.compile(r"gh pr merge(?:\s+(?:--auto|\"?\$?[A-Za-z_{}\"]*PR_NUM[\"}]*|\d+))*"
                      r"\s+--(squash|merge|rebase)\b")
    for rel in ("scripts/worktree_builder.sh", "members/minion/minion.fleet.json",
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


def _self_improve_score_is_actually_scheduled():
    """self_improve_score.sh is not a member -- the check above can't see it, and it didn't.

    Found live by dumbledore 2026-08-28: self_improve_score.jsonl did not exist anywhere under
    FLEET_LOG_DIR, because entrypoint.sh's hand-written crontab had no line for it at all -- the
    exact same "spec/reality exists, but nothing put it on cron" failure class as datta
    (nonprofit-atlas#3321, fixed in #114), recurring in the one place #114's own fix cannot
    reach: _every_scheduled_member_is_actually_on_cron only walks members/*/*.fleet.json, and
    this script has no member spec to walk. dumbledore's and jefe's entire read of the Magikarp
    score depends on this file existing; a silent gap here breaks the one feedback loop this
    whole kit is built around, with no crash and no failing check -- until now.
    """
    entry = (Path(__file__).parent.parent / "entrypoint.sh").read_text()
    assert "self_improve_score.sh" in entry, (
        "self_improve_score.sh has no line in entrypoint.sh's crontab -- it will never run, "
        "so self_improve_score.jsonl never gets written and dumbledore/jefe read nothing.")


def _deploy_staleness_check_is_actually_scheduled():
    """Same failure class as _self_improve_score_is_actually_scheduled, one script over.

    gh#201: deploy_staleness_check.sh is the independent gate that catches a deploy that never
    ran at all -- it is worthless if nothing puts it on cron, exactly the "spec/reality exists,
    but nothing scheduled it" gap that bit datta (nonprofit-atlas#3321) and self_improve_score.sh
    (gh#196-adjacent) before it.
    """
    entry = (Path(__file__).parent.parent / "entrypoint.sh").read_text()
    assert "deploy_staleness_check.sh" in entry, (
        "deploy_staleness_check.sh has no line in entrypoint.sh's crontab -- it will never run, "
        "so a dark deploy pipeline goes back to being invisible until a human stumbles onto it.")


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


if __name__ == "__main__":
    check("member specs load and validate", _member_specs_validate)
    check("member_spec's OWN default MEMBERS_DIR resolves (not just an explicit path)", _members_dir_default_is_right)
    check("report contract: ok + silence is recorded", _report_contract)
    check("a pass's Prediction survives for the NEXT pass to verify", _rsi_lines_survive_to_the_next_pass)
    check("fleet.db run_id collisions don't lose a verdict", _fleet_db_run_id_collisions_dont_lose_a_verdict)
    check("fleet.db composite-PK migration is lock-serialized", _fleet_db_composite_pk_migration_is_lock_serialized)
    check("fanout packs the hour by complexity, in percent", _fanout_packs_the_hour_by_complexity)
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
    check("jefe can unstick a PR that is merely behind its base", _jefe_can_unstick_a_pr_that_is_merely_behind)
    check("arming auto-merge passes no strategy flag, and checks it worked", _auto_merge_never_passes_a_strategy_flag_under_a_merge_queue)
    check("--task adds to a charter, never replaces it", _adhoc_task_adds_to_the_charter_never_replaces_it)
    check("a killed pass is recorded, not silently lost", _a_killed_pass_is_recorded_not_lost)
    check("signal_rate/dormant exclude killed+timed_out, not just budget_declined", _signal_rate_excludes_all_never_executed_statuses)
    check("a leaked absolute-path write into $REPO is caught and alerted", _postflight_dirty_check_catches_a_leaked_absolute_path_write)
    check("a git-status failure alerts rather than reading as clean", _postflight_dirty_check_alerts_rather_than_hides_a_git_status_failure)
    check("run_member.sh logs CRITICAL when postflight_dirty_check.sh fails to source", _run_member_logs_critical_when_postflight_dirty_check_fails_to_source)
    check("run_member.sh rejects a non-numeric --item", _run_member_rejects_a_non_numeric_item)
    check("both worktree callers check $REPO before tearing the worktree down", _run_member_and_builder_check_repo_before_removing_the_worktree)
    check("deploy drains in-flight passes before cutover", _deploy_drains_inflight_passes)
    check("deploys never stack, and the drain can count to zero", _one_deploy_at_a_time_and_a_countable_drain)
    check("judge-judy ticks don't overlap", _judge_judy_ticks_dont_overlap)
    check("judge-judy lock lives somewhere persistent", _judge_judy_lock_lives_somewhere_persistent)
    check("judge-judy strikes are head-scoped and leave diagnosable evidence", _judge_judy_strikes_are_scoped_by_head_and_leave_diagnosable_evidence)
    check("marie re-judges the whole backlog, not just the new", _marie_sweeps_the_whole_backlog_not_just_the_new)
    check("marie writes a build-ready PRD and minion reads it", _marie_writes_a_prd_and_minion_reads_it)
    check("the-fixer catches a check that never answers", _fixer_catches_the_no_answer_class)
    check("datta dispatches by coverage, nerds analyse one lane", _datta_dispatches_and_nerds_analyse)
    check("a run records the item it worked", _a_run_records_the_item_it_worked)
    check("every pass files a written report", _every_pass_files_a_written_report)
    check("every scheduled member is actually on cron", _every_scheduled_member_is_actually_on_cron)
    check("self_improve_score.sh is actually scheduled", _self_improve_score_is_actually_scheduled)
    check("deploy staleness check is actually scheduled", _deploy_staleness_check_is_actually_scheduled)
    check("account + tunnel health checks are actually scheduled", _account_and_tunnel_health_checks_are_actually_scheduled)
    check("deploy staleness check reads a baked SHA and only alerts past budget", _deploy_staleness_check_reads_a_baked_sha_and_only_alerts_past_budget)
    check("deploy.sh's host log dir survives sourcing the instance's container-scoped fleet.env", _deploy_sh_host_log_dir_survives_sourcing_the_instances_container_scoped_fleet_env)
    check("deploy cordons the fleet, then drains, and always uncordons", _deploy_cordons_then_drains_and_always_uncordons)
    check("deploy.sh's log is durable regardless of caller", _deploy_log_is_durable_regardless_of_caller)
    check("overrides tune dials, refuse authority", _overrides_are_narrow)
    check("overrides store never resolves under $HOME/.claude", _overrides_store_is_not_under_home_dot_claude)
    check("fleet.env.example present, fleet.env untracked", _env_example_exists)
    check("schedulers ship for macOS and Linux", _schedulers_for_both_platforms)
    check("fleet-view write routes are authenticated and fail closed", _write_routes_are_authenticated)
    check("FLEET_API_KEY never reaches an LLM pass", _api_key_never_reaches_an_llm)
    check("incidental 'rate limit' text does not gate an account", _classifier_ignores_incidental_rate_limit_text)
    check("a real usage limit is still classified exhausted", _classifier_still_catches_a_real_limit)
    check("exhaustion with no stated reset backs off minutes, not an hour", _unparseable_exhaustion_gates_briefly_not_for_an_hour)
    check("a stated reset time is honored over the fallback", _a_real_reset_time_is_still_honored)
    check("pool logs successes so outage length is measurable", _pool_logs_successes_so_downtime_is_measurable)
    check("nothing hardcodes a read of the frozen instances/*/logs mirror", _nothing_hardcodes_a_read_of_the_frozen_instance_log_mirror)

    for n in ok:
        print(f"  ok    {n}")
    for n, why in fail:
        print(f"  FAIL  {n}\n        {why}")
    print(f"\n{len(ok)} passed, {len(fail)} failed")
    raise SystemExit(1 if fail else 0)
