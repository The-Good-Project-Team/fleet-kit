#!/usr/bin/env python3
"""ask.py -- a real channel for "a member hit a wall the fleet cannot act on" (gh#568).

THE GAP THIS CLOSES. Today the only channel is applying `fleet:needs-human-op` to an issue and
stopping. That label carries no structured `why`/`unblocks`/`proposed` -- a human reading it has
to reconstruct the ask from the issue body by hand -- and the fleet keeps no record of how it was
answered, so it cannot learn from its own past asks. This is the first buildable child of
fleet-kit#558's authority ladder: a `fleet.db` table (`asks`, see fleet_db.py's SCHEMA) plus this
CLI, so any member can call `ask.py file` instead of dead-ending on a label.

NON-GOALS (this issue's own body): no authority.json / permission-escalation logic, no console
or superadmin UI, no migration of existing `fleet:needs-human-op` issues into asks. This is
purely the record + CLI.

Pure core (`file_ask`/`answer_ask`/`list_asks`), thin DB seam (`fleet_db.connect`), CLI (`main`)
-- same split as cost_bridge.py / claim_history.py.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_db  # noqa: E402

ASK_COLUMNS = (
    "id", "member", "why", "unblocks", "proposed", "status",
    "answer", "answered_by", "answered_at", "filed_at",
)


def file_ask(conn, member: str, why: str, unblocks: str | None = None,
            proposed: str | None = None, filed_at: float | None = None) -> int:
    """Insert a new open ask. Returns its id."""
    cur = conn.execute(
        "INSERT INTO asks (member, why, unblocks, proposed, status, filed_at) "
        "VALUES (?, ?, ?, ?, 'open', ?)",
        (member, why, unblocks, proposed, filed_at if filed_at is not None else time.time()),
    )
    conn.commit()
    return cur.lastrowid


def answer_ask(conn, ask_id: int, answer: str, answered_by: str,
              status: str = "answered", answered_at: float | None = None) -> bool:
    """Answer an open ask exactly once. Returns True on success, False if `ask_id` does not
    exist or was already answered (AC3: a second call is a no-op, never a silent overwrite).

    The UPDATE's own `WHERE answered_at IS NULL` is what makes this atomic against a second,
    concurrent `answer` call racing this one -- whichever UPDATE's WHERE clause still matches
    wins, sqlite's single-writer lock serializes the two, and the loser's rowcount is 0.
    """
    cur = conn.execute(
        "UPDATE asks SET status = ?, answer = ?, answered_by = ?, answered_at = ? "
        "WHERE id = ? AND answered_at IS NULL",
        (status, answer, answered_by,
         answered_at if answered_at is not None else time.time(), ask_id),
    )
    conn.commit()
    return cur.rowcount == 1


def list_asks(conn, status: str | None = "open", member: str | None = None,
             limit: int = 100) -> list[dict]:
    q = f"SELECT {', '.join(ASK_COLUMNS)} FROM asks WHERE 1=1"
    params: list = []
    if status and status != "all":
        q += " AND status = ?"; params.append(status)
    if member:
        q += " AND member = ?"; params.append(member)
    q += " ORDER BY filed_at DESC LIMIT ?"
    params.append(limit)
    cur = conn.execute(q, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _notify(member: str, ask_id: int, why: str) -> None:
    """One NTFY (+ email) per member per rolling hour, via the fleet's shared fleet_alert.sh
    channel -- same helper every other check pages through, so a filed ask reaches a human the
    same way an alarm does and gets the same undelivered-retry queue for free (AC4).

    Rate limit: `problem` is keyed to the current hour bucket, so alert_store.py's own
    (check, problem) dedupe -- not new state this module has to keep -- pages once per member
    per hour and silently skips (`SKIP already paged`) every later ask that member files inside
    the same hour. `severity=critical` is what makes that first page fire immediately rather
    than waiting on a debounce window: a fleet member blocked right now needs a human to see it
    now, not after a condition has "persisted."

    Best-effort only, same contract as fleet_alert.sh itself: a member's ask is already
    committed to fleet.db by the time this runs, so a failed/undeliverable page must never make
    `ask.py file` itself fail.
    """
    script = HERE / "fleet_alert.sh"
    problem = f"{member}:{int(time.time() // 3600)}"
    title = f"fleet ask filed by {member}"
    body = f"ask #{ask_id}: {why}"
    try:
        subprocess.run(
            ["bash", str(script), "--check", "ask", "--problem", problem,
             "--severity", "critical", "--handle", member, title, body],
            capture_output=True, timeout=30, check=False,
        )
    except Exception:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="File, answer, or list fleet asks -- the structured channel for a member "
                     "that hit a wall the fleet cannot act on (gh#568).")
    ap.add_argument("--db-path", help="override fleet.db path (default: fleet_db.DB_FILE)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_file = sub.add_parser("file", help="file a new ask")
    p_file.add_argument("--member", required=True)
    p_file.add_argument("--why", required=True, help="why this needs a human")
    p_file.add_argument("--unblocks", help="what gets unstuck once this is answered")
    p_file.add_argument("--proposed", help="a proposed answer, if the filer has one")
    p_file.add_argument("--no-notify", action="store_true",
                        help="skip the NTFY/email page (tests, or a caller paging separately)")

    p_answer = sub.add_parser("answer", help="answer an open ask exactly once")
    p_answer.add_argument("id", type=int)
    p_answer.add_argument("--answer", required=True)
    p_answer.add_argument("--answered-by", required=True)
    p_answer.add_argument("--status", default="answered")

    p_list = sub.add_parser("list", help="list asks")
    p_list.add_argument("--status", default="open", help="open|answered|all (default: open)")
    p_list.add_argument("--member")
    p_list.add_argument("--limit", type=int, default=100)

    a = ap.parse_args(argv)
    conn = fleet_db.connect(Path(a.db_path) if a.db_path else None)

    if a.cmd == "file":
        ask_id = file_ask(conn, a.member, a.why, a.unblocks, a.proposed)
        print(f"ask {ask_id} filed")
        if not a.no_notify:
            _notify(a.member, ask_id, a.why)
        return 0

    if a.cmd == "answer":
        ok = answer_ask(conn, a.id, a.answer, a.answered_by, a.status)
        if not ok:
            exists = conn.execute("SELECT 1 FROM asks WHERE id = ?", (a.id,)).fetchone()
            reason = "already answered" if exists else "no such ask"
            print(f"ask.py: answer {a.id}: {reason}", file=sys.stderr)
            return 1
        print(f"ask {a.id} answered")
        return 0

    if a.cmd == "list":
        print(json.dumps(list_asks(conn, status=a.status, member=a.member, limit=a.limit),
                         indent=2))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
