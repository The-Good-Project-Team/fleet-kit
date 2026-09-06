#!/usr/bin/env python3
"""fleet_db — SQLite mirror of runs.jsonl, for real search + aggregate queries.

WHY A DB AT ALL, given this kit's whole thesis is "no separate store." runs.jsonl is the
source of truth and stays append-only, crash-safe, and grep-able with zero setup -- that
doesn't change. What jsonl cannot do is a WHERE clause: "this member's cost over the last 24h",
"every blocked review this week", "runs for item #482." Past a few hundred rows that's a real
gap, not a nice-to-have -- and it's exactly what a member's own self-tuning pass needs to answer
before it can decide anything (see `spend()` below).

STILL NOT A SEPARATE STORE IN THE SENSE THAT MATTERS: this file is 100% DISPOSABLE. `rm
fleet.db && fleet_db.py sync` rebuilds it byte-for-byte from runs.jsonl. It is an INDEX, not a
ledger -- if this file and runs.jsonl ever disagree, runs.jsonl is right, full stop. That is
also why sync() is idempotent (INSERT OR REPLACE keyed on run_id, no dedup logic to get wrong)
rather than append-only itself: replaying jsonl twice must produce the same table, not double
rows.

stdlib `sqlite3` only -- same reasoning as JSON-not-YAML in member_spec.py: whatever this needs
to run on must already have it, no `pip install` step to silently fail on a fresh box.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
RUNS_FILE = LOG_DIR / "runs.jsonl"
DB_FILE = Path(os.environ.get("FLEET_DB_PATH", LOG_DIR / "fleet.db")).expanduser()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id              TEXT NOT NULL,
  member               TEXT NOT NULL,
  kind                  TEXT,
  item_id                TEXT,
  pr                      TEXT,
  status                   TEXT,
  exit_code                 INTEGER,
  outcome                    TEXT,
  evidence                    TEXT,
  vision_link                  TEXT,
  self_critique                 TEXT,
  report                         TEXT,
  prediction                     TEXT,
  score_now                       TEXT,
  last_verdict                     TEXT,
  cost_usd                      REAL,
  num_turns                      INTEGER,
  input_tokens                    INTEGER,
  output_tokens                    INTEGER,
  cache_read_tokens                 INTEGER,
  cache_creation_tokens               INTEGER,
  duration_ms                          INTEGER,
  stop_reason                           TEXT,
  lane                                   TEXT,
  recorded_at                            REAL NOT NULL,
  -- Composite, not bare run_id (fleet-kit#212): judge-judy's run_id is `review-<pr>-<sha>`,
  -- not per-invocation, so two genuinely different concurrent reviews of the same PR head
  -- share a run_id. Under a bare PRIMARY KEY, INSERT OR REPLACE silently kept only one
  -- verdict per sync() -- runs.jsonl still had both, fleet.db quietly lost one. Widening the
  -- key to (run_id, recorded_at) lets two distinct runs coexist while a literal re-sync of
  -- the same jsonl line (same run_id AND same recorded_at, sync()'s own idempotency case)
  -- still replaces in place rather than duplicating.
  PRIMARY KEY (run_id, recorded_at)
);
CREATE INDEX IF NOT EXISTS idx_runs_member_time ON runs(member, recorded_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_item ON runs(item_id);

-- Byte offset into runs.jsonl already synced, so sync() only reads what's new. Single row
-- (id=0). If runs.jsonl shrinks (rotated/truncated) sync() resets this to 0 and re-reads.
CREATE TABLE IF NOT EXISTS sync_state (id INTEGER PRIMARY KEY CHECK (id = 0), offset INTEGER NOT NULL);

-- gh#324: independent lane KPIs (e.g. devops's deploy_success_rate), computed by a job that
-- shares no process context with the agent whose lane it grades (kpi-doctrine.md rule 1) --
-- see scripts/lane_kpi.py, the only writer of this table. Append-only, one row per computed
-- reading -- never UPDATEd or REPLACEd -- so consecutive denominators both stay on record for
-- a rule-3 >10%-swing check, and a consumer can tell "just computed" from stale by reading the
-- newest row's own timestamp (rule 5) instead of trusting a single mutable cell. `value` is
-- NULLable: a trailing window with zero ticks (denominator=0) has no rate to report and must
-- read as "no data" -- storing 0.0 there would be indistinguishable from a real 0% success
-- rate, exactly the conflation rule 5 forbids.
CREATE TABLE IF NOT EXISTS lane_kpi (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  lane         TEXT NOT NULL,
  metric       TEXT NOT NULL,
  value        REAL,
  denominator  INTEGER NOT NULL,
  computed_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lane_kpi_lane_metric_time ON lane_kpi(lane, metric, computed_at);

-- gh#568: a real channel for "a member hit a wall the fleet cannot act on" -- today the only
-- option is `fleet:needs-human-op` and stop, a label with no structured why/unblocks/proposed
-- and no record of how it was answered. See scripts/ask.py, the only reader/writer. `answer`,
-- `answered_by` and `answered_at` are all NULL together (open) or all set together (answered) --
-- ask.py's own `answer` command is what keeps that invariant, never a hand UPDATE.
CREATE TABLE IF NOT EXISTS asks (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  member       TEXT NOT NULL,
  why          TEXT NOT NULL,
  unblocks     TEXT,
  proposed     TEXT,
  status       TEXT NOT NULL DEFAULT 'open',
  answer       TEXT,
  answered_by  TEXT,
  answered_at  REAL,
  filed_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asks_status ON asks(status);
CREATE INDEX IF NOT EXISTS idx_asks_member ON asks(member);
"""


# Columns added to `runs` after the table shipped. CREATE TABLE IF NOT EXISTS is a no-op
# against a db that already exists, so a new column in SCHEMA alone reaches a fresh box and
# NOBODY else -- the live fleet.db keeps the old shape and every INSERT then fails on column
# count. Expand-contract: ADD COLUMN is backward-compatible (old rows read NULL = "wasn't
# captured", same convention _row_from_record already uses for absent token fields), so a
# rolled-back deploy still reads and writes this table fine.
_ADD_COLUMNS = (
    ("prediction", "TEXT"),
    ("score_now", "TEXT"),
    ("last_verdict", "TEXT"),
    # The written report (persona_law §10c). Added via _ADD_COLUMNS rather than SCHEMA so an
    # existing fleet.db gains it on the next connect() -- no rebuild, no lost history.
    ("report", "TEXT"),
    # Structured mirror of a dispatcher's `lane=<name>` --task prefix (run_report.py's --lane).
    # Replaces datta's prior keyword-match of outcome/evidence prose for lane attribution --
    # only set on lane-dispatched passes (nerd today), NULL everywhere else.
    ("lane", "TEXT"),
)


@contextlib.contextmanager
def _migration_lock(db_path: Path):
    """`connect()` is called from multiple threads (fleet_view_server's background tail
    thread and per-request handlers all call `fleet_db.connect()` independently), and
    `_migrate_composite_pk` below is a rename/rebuild/drop of `runs`, not an idempotent
    ADD COLUMN -- two threads both seeing the pre-migration schema at once would both try to
    rename the same table and one gets a raw `sqlite3.OperationalError`. Same flock-over-a-
    sidecar-file pattern maxx_lease.py already uses for its own read-modify-write race:
    serialize the whole migration so only one thread is ever inside it, and every later
    thread's own PRAGMA table_info check (taken after acquiring the lock) then sees the
    already-migrated schema and returns immediately."""
    lock_path = db_path.with_suffix(db_path.suffix + ".migrate.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _migrate_composite_pk(conn: sqlite3.Connection) -> None:
    """fleet-kit#212: a live fleet.db predating the composite key still has run_id as a bare
    PRIMARY KEY -- CREATE TABLE IF NOT EXISTS is a no-op against it, same reason _ADD_COLUMNS
    exists above, so without this the fix reaches only a freshly rebuilt db and NOBODY else.
    SQLite can't ALTER a PRIMARY KEY in place, so rebuild: rename the old table aside, let
    SCHEMA create the new-shaped one, copy every row across by its old column list (so a
    legacy table still missing an _ADD_COLUMNS column just copies what it has), then drop the
    old table. The already-collided historical rows (fewer rows in fleet.db than distinct
    run_ids in runs.jsonl) are NOT recovered by this -- that backfill is explicitly out of
    scope (issue body's Non-goals); this only stops NEW collisions going forward.
    """
    pk_cols = [name for _, name in sorted(
        (r[5], r[1]) for r in conn.execute("PRAGMA table_info(runs)") if r[5]
    )]
    if pk_cols != ["run_id"]:
        return  # already migrated (or a fresh db that never had the old schema)
    # `ALTER TABLE ... RENAME TO` carries every index over onto the renamed table (SQLite
    # keeps indexes attached by table, not by name), so idx_runs_member_time/_status/_item
    # would still exist afterward -- just pointing at runs_legacy_pk. SCHEMA's `CREATE INDEX
    # IF NOT EXISTS` then no-ops on those exact names (the check is name-only, not
    # name+table), and DROP TABLE below cascades and deletes them for good. Drop them by name
    # first so the names are free for SCHEMA to reattach to the new `runs` table.
    conn.execute("DROP INDEX IF EXISTS idx_runs_member_time")
    conn.execute("DROP INDEX IF EXISTS idx_runs_status")
    conn.execute("DROP INDEX IF EXISTS idx_runs_item")
    conn.execute("ALTER TABLE runs RENAME TO runs_legacy_pk")
    conn.executescript(SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs_legacy_pk)")]
    col_list = ", ".join(cols)
    conn.execute(f"INSERT INTO runs ({col_list}) SELECT {col_list} FROM runs_legacy_pk")
    conn.execute("DROP TABLE runs_legacy_pk")
    conn.commit()


def _migrate(conn: sqlite3.Connection, db_path: Path) -> None:
    # SCHEMA's own CREATE TABLE/INDEX IF NOT EXISTS statements have to live under the SAME
    # lock as _migrate_composite_pk, not run unlocked ahead of it: a bare
    # `conn.executescript(SCHEMA)` on one connection can interleave, statement-by-statement,
    # with another thread's in-progress rename/rebuild -- e.g. run its own
    # `CREATE INDEX ... ON runs` right after a concurrent thread's `ALTER TABLE runs RENAME TO
    # runs_legacy_pk` has committed but before that thread's own SCHEMA re-apply has recreated
    # `runs`, raising a raw `sqlite3.OperationalError: no such table: main.runs`. Applying
    # SCHEMA here, inside the flock, closes that window the same way _migrate_composite_pk's
    # own internal window was already closed.
    with _migration_lock(db_path):
        conn.executescript(SCHEMA)
        _migrate_composite_pk(conn)
    have = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    for name, decl in _ADD_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    p = db_path or DB_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    _migrate(conn, p)
    conn.execute("INSERT OR IGNORE INTO sync_state (id, offset) VALUES (0, 0)")
    conn.commit()
    return conn


# Single source of truth for the INSERT's column list AND the collision check's SELECT below
# -- keeping these as one tuple means the two can never silently drift out of order.
RUN_COLUMNS = (
    "run_id", "member", "kind", "item_id", "pr", "status", "exit_code", "outcome", "evidence",
    "vision_link", "self_critique", "report", "prediction", "score_now", "last_verdict",
    "cost_usd", "num_turns", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "duration_ms", "stop_reason", "lane",
    "recorded_at",
)


def _row_from_record(rec: dict) -> tuple:
    # `tokens` is run_report.py's one shape for this (see that module's build_record) --
    # field names match pass_accounting.py's split() verbatim. Absent fields stay NULL, not 0:
    # 0 is a real "spent nothing," NULL is "wasn't captured" -- collapsing them would make a
    # mechanical member's true zero-cost run indistinguishable from a run whose usage capture
    # broke, which is exactly the silent-gap class this kit exists to prevent repeating.
    tokens = rec.get("tokens") or {}
    return (
        rec.get("run_id"), rec.get("member"), rec.get("kind"),
        rec.get("item_id"), rec.get("pr"), rec.get("status"), rec.get("exit_code"),
        rec.get("outcome"), rec.get("evidence"), rec.get("vision_link"), rec.get("self_critique"),
        rec.get("report"),
        rec.get("prediction"), rec.get("score_now"), rec.get("last_verdict"),
        tokens.get("cost_usd"), tokens.get("num_turns"),
        tokens.get("input_tokens"), tokens.get("output_tokens"),
        tokens.get("cache_read_input_tokens"), tokens.get("cache_creation_input_tokens"),
        tokens.get("duration_ms"), tokens.get("stop_reason"),
        rec.get("lane"),
        rec.get("_recorded_at") or 0.0,
    )


def sync_offset(conn: sqlite3.Connection) -> int:
    """Current sync_state.offset, with no sync() call. For a caller that needs to MEASURE
    drift against runs.jsonl (gh#273's staleness watchdog) rather than close it -- calling
    sync() first would mask exactly the gap the watchdog exists to detect.
    """
    return conn.execute("SELECT offset FROM sync_state WHERE id = 0").fetchone()[0]


def sync(conn: sqlite3.Connection, runs_file: Path | None = None) -> int:
    """Read new lines since the last sync, upsert them. Returns rows synced."""
    import time
    rf = runs_file or RUNS_FILE
    if not rf.exists():
        return 0
    size = rf.stat().st_size
    offset = conn.execute("SELECT offset FROM sync_state WHERE id = 0").fetchone()[0]
    if size < offset:
        offset = 0  # rotated/truncated underneath us
    n = 0
    with rf.open() as fh:
        fh.seek(offset)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # `ts` is run_report.py's own wall-clock stamp, written the moment the pass
            # finished (build_record). Prefer it over "now" -- if sync() ever falls behind
            # (crashed poller, cron gap) and catches up on a backlog in one burst, every
            # backlogged row would otherwise get recorded_at = the burst's moment, not its
            # own run time, silently corrupting every "last N hours" freshness query against
            # this table (including dumbledore's own self-critique query in persona_law.md
            # §11 and fleet_view's trailing-spend charts). `_recorded_at` stays as an explicit
            # override hook for callers that want ingestion-time instead (e.g. tests).
            rec.setdefault("_recorded_at", rec.get("ts") or time.time())
            row = _row_from_record(rec)
            run_id, recorded_at = row[0], row[-1]
            # fleet-kit#212 AC3: the composite key above stops two DISTINCT runs from
            # colliding (they get different recorded_at), but a genuine collision -- two
            # different records that somehow land on the identical (run_id, recorded_at) pair
            # -- would still silently overwrite under INSERT OR REPLACE. Detect and log it
            # loudly rather than let it stay invisible; a real re-sync of the same jsonl line
            # (AC2) produces an identical row here and stays silent, by design.
            existing = conn.execute(
                f"SELECT {', '.join(RUN_COLUMNS)} FROM runs WHERE run_id = ? AND recorded_at = ?",
                (run_id, recorded_at),
            ).fetchone()
            if existing is not None and tuple(existing) != row:
                print(
                    f"fleet_db: COLLISION run_id={run_id!r} recorded_at={recorded_at!r} "
                    "already has a DIFFERENT row in fleet.db -- one record is about to be "
                    "silently overwritten (both are still in runs.jsonl)",
                    file=sys.stderr,
                )
            conn.execute(
                f"""INSERT OR REPLACE INTO runs ({', '.join(RUN_COLUMNS)})
                    VALUES ({', '.join('?' * len(RUN_COLUMNS))})""",
                row,
            )
            n += 1
        new_offset = fh.tell()
    conn.execute("UPDATE sync_state SET offset = ? WHERE id = 0", (new_offset,))
    conn.commit()
    return n


def spend(conn: sqlite3.Connection, member: str | None = None, hours: float = 24.0) -> list[dict]:
    """Per-member trailing spend -- the number a self-tuning pass reads before deciding
    anything. Grouped, not a raw dump: a member deciding whether to throttle ITSELF wants its
    own total, not 200 individual rows to sum by eye.

    gh#185: `ok_runs`'s CASE now reuses fleet_stats.py's `_NOT_EXECUTED_STATUSES` set (#150) as
    an explicit exclusion, rather than relying on 'ok'/'quiet' happening to already be disjoint
    from it -- so if that taxonomy ever grows a status that isn't obviously a fail (the way
    #150 itself widened `_NOT_EXECUTED_STATUSES` from one status to three), a run that never
    got the chance to execute still can't count toward ok_runs by accident. `runs` and every
    other field below are deliberately left alone (AC3/Non-goals): this is scoped to ok_runs'
    own numerator, not a redefinition of the denominator every field here shares.
    """
    import time

    import fleet_stats
    since = time.time() - hours * 3600
    not_executed = fleet_stats._NOT_EXECUTED_STATUSES
    not_executed_sql = ",".join("?" for _ in not_executed)
    q = f"""SELECT member, COUNT(*) as runs, SUM(cost_usd) as total_cost,
                  AVG(cost_usd) as avg_cost, SUM(num_turns) as total_turns,
                  SUM(CASE WHEN status IN ('ok','quiet') AND status NOT IN ({not_executed_sql})
                           THEN 1 ELSE 0 END) as ok_runs
           FROM runs WHERE recorded_at >= ?"""
    params: list = [*not_executed, since]
    if member:
        q += " AND member = ?"
        params.append(member)
    q += " GROUP BY member ORDER BY total_cost DESC NULLS LAST"
    cur = conn.execute(q, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def query_runs(conn: sqlite3.Connection, *, member: str | None = None, status: str | None = None,
               item_id: str | None = None, limit: int = 100) -> list[dict]:
    """gh#405: `item_id` is only ever written by a `--item N` build-claim pass -- 67/1720 rows
    fleet-wide (3.9%). Every marie/nerd/dumbledore/... pass that merely DISCUSSES issue N does
    so in free text only, so an exact-match filter told 96% of callers "nothing" when the fleet
    had plenty to say. Widen to an OR: the exact build-claim column, or a `#N` mention in
    outcome/evidence/self_critique -- the exact match stays a strict subset of this result.
    """
    q = "SELECT * FROM runs WHERE 1=1"
    params: list = []
    if member:
        q += " AND member = ?"; params.append(member)
    if status:
        q += " AND status = ?"; params.append(status)
    if item_id:
        like = f"%#{item_id}%"
        q += " AND (item_id = ? OR outcome LIKE ? OR evidence LIKE ? OR self_critique LIKE ?)"
        params.extend([item_id, like, like, like])
    q += " ORDER BY recorded_at DESC"
    if not item_id:
        q += " LIMIT ?"; params.append(limit)
    cur = conn.execute(q, params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    if item_id:
        # The LIKE above is a superset -- it also matches "#1430" while looking for "#143" --
        # so the real match is re-checked here, word-boundary anchored (`#` already pins the
        # left edge; the negative lookahead pins the right). LIMIT is applied AFTER this
        # narrowing, not in the SQL above: pushing it into the query would risk truncating the
        # result to false positives before they get filtered back out.
        pattern = re.compile(r"#" + re.escape(item_id) + r"(?!\d)")
        rows = [r for r in rows
                if r.get("item_id") == item_id
                or any(pattern.search(r.get(f) or "") for f in ("outcome", "evidence", "self_critique"))]
        rows = rows[:limit]
    return rows


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Query/sync the fleet's SQLite run index.")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("sync", help="read new runs.jsonl lines into fleet.db")

    sub.add_parser("offset", help="print sync_state.offset without syncing (for staleness checks)")

    p_rebuild = sub.add_parser("rebuild", help="drop + rebuild fleet.db from runs.jsonl entirely")

    p_spend = sub.add_parser("spend", help="trailing spend, grouped by member")
    p_spend.add_argument("--member")
    p_spend.add_argument("--hours", type=float, default=24.0)

    p_query = sub.add_parser("query", help="recent runs, optionally filtered")
    p_query.add_argument("--member")
    p_query.add_argument("--status")
    p_query.add_argument("--item-id")
    p_query.add_argument("--limit", type=int, default=100)

    a = ap.parse_args(argv)
    if a.cmd == "rebuild":
        if DB_FILE.exists():
            DB_FILE.unlink()
        conn = connect()
        n = sync(conn)
        print(f"rebuilt {DB_FILE}: {n} runs")
        return 0

    conn = connect()
    if a.cmd == "offset":
        print(sync_offset(conn))
        return 0
    if a.cmd == "sync" or a.cmd is None:
        n = sync(conn)
        print(f"synced {n} new runs")
        return 0
    if a.cmd == "spend":
        sync(conn)
        print(json.dumps(spend(conn, member=a.member, hours=a.hours), indent=2))
        return 0
    if a.cmd == "query":
        sync(conn)
        print(json.dumps(query_runs(conn, member=a.member, status=a.status,
                                    item_id=a.item_id, limit=a.limit), indent=2))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
