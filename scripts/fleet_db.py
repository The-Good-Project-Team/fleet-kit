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

import json
import os
import sqlite3
import sys
from pathlib import Path

LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
RUNS_FILE = LOG_DIR / "runs.jsonl"
DB_FILE = Path(os.environ.get("FLEET_DB_PATH", LOG_DIR / "fleet.db")).expanduser()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id              TEXT PRIMARY KEY,
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
  cost_usd                      REAL,
  num_turns                      INTEGER,
  input_tokens                    INTEGER,
  output_tokens                    INTEGER,
  cache_read_tokens                 INTEGER,
  cache_creation_tokens               INTEGER,
  duration_ms                          INTEGER,
  stop_reason                           TEXT,
  recorded_at                            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_member_time ON runs(member, recorded_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_item ON runs(item_id);

-- Byte offset into runs.jsonl already synced, so sync() only reads what's new. Single row
-- (id=0). If runs.jsonl shrinks (rotated/truncated) sync() resets this to 0 and re-reads.
CREATE TABLE IF NOT EXISTS sync_state (id INTEGER PRIMARY KEY CHECK (id = 0), offset INTEGER NOT NULL);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    p = db_path or DB_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO sync_state (id, offset) VALUES (0, 0)")
    conn.commit()
    return conn


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
        tokens.get("cost_usd"), tokens.get("num_turns"),
        tokens.get("input_tokens"), tokens.get("output_tokens"),
        tokens.get("cache_read_input_tokens"), tokens.get("cache_creation_input_tokens"),
        tokens.get("duration_ms"), tokens.get("stop_reason"),
        rec.get("_recorded_at") or 0.0,
    )


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
            conn.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, member, kind, item_id, pr, status, exit_code, outcome, evidence,
                    vision_link, self_critique, cost_usd, num_turns, input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens, duration_ms, stop_reason, recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                _row_from_record(rec),
            )
            n += 1
        new_offset = fh.tell()
    conn.execute("UPDATE sync_state SET offset = ? WHERE id = 0", (new_offset,))
    conn.commit()
    return n


def spend(conn: sqlite3.Connection, member: str | None = None, hours: float = 24.0) -> list[dict]:
    """Per-member trailing spend -- the number a self-tuning pass reads before deciding
    anything. Grouped, not a raw dump: a member deciding whether to throttle ITSELF wants its
    own total, not 200 individual rows to sum by eye."""
    import time
    since = time.time() - hours * 3600
    q = """SELECT member, COUNT(*) as runs, SUM(cost_usd) as total_cost,
                  AVG(cost_usd) as avg_cost, SUM(num_turns) as total_turns,
                  SUM(CASE WHEN status IN ('ok','quiet') THEN 1 ELSE 0 END) as ok_runs
           FROM runs WHERE recorded_at >= ?"""
    params: list = [since]
    if member:
        q += " AND member = ?"
        params.append(member)
    q += " GROUP BY member ORDER BY total_cost DESC NULLS LAST"
    cur = conn.execute(q, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def query_runs(conn: sqlite3.Connection, *, member: str | None = None, status: str | None = None,
               item_id: str | None = None, limit: int = 100) -> list[dict]:
    q = "SELECT * FROM runs WHERE 1=1"
    params: list = []
    if member:
        q += " AND member = ?"; params.append(member)
    if status:
        q += " AND status = ?"; params.append(status)
    if item_id:
        q += " AND item_id = ?"; params.append(item_id)
    q += " ORDER BY recorded_at DESC LIMIT ?"
    params.append(limit)
    cur = conn.execute(q, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Query/sync the fleet's SQLite run index.")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("sync", help="read new runs.jsonl lines into fleet.db")

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
