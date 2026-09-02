#!/usr/bin/env python3
"""maxx_lease.py — local reservation ledger for maxx's per-diem allowance.

Provenance: gh#161 part 2. gru.md steps 3a/6 have documented `maxx_reserve(pct=...,
label=..., ttl_sec=...)` / `maxx_release(lease_id=...)` since 2026-08-something, but neither
function existed anywhere in this repo -- every gru pass silently no-op'd the reservation,
so `reserved_pct` in maxx_reader.py's output stayed permanently 0 no matter how many hours of
spend should logically still be reserved. This module implements the contract gru.md already
documents; nothing there needs to change once this lands.

This is a LOCAL-ONLY lease ledger, not a real remote reservation against maxx itself (that is
gh#161 part 1, a human/credential decision, out of scope here). It exists purely so that
back-to-back gru passes (cron does not wait for one pass to fully land before the next fires)
can see each other's in-flight spend.

Persists to a flat JSON file under $FLEET_LOG_DIR, using the same tmp-file-then-`mv` atomic
write account_pool.sh's state file already uses -- no new dependency, no sqlite table.

A lease past `created_at + ttl_sec` is treated as expired and excluded from
`total_reserved_pct()` without an explicit release call -- gru.md's documented backstop: "a
lease that outlives its own hour self-expires instead of choking every later pass forever."
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import time
import uuid
from pathlib import Path

LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()

# The ledger is SHARED ACROSS INSTANCES when FLEET_LEASE_DIR is set, and per-instance when it
# is not. That distinction is the whole point: every instance on this box draws from ONE maxx
# account pool, so a ledger only this instance can see cannot coordinate anything.
#
# Live 2026-09-02 (Reif): philanthropy and fleet-kit-server-fleet each kept their own
# maxx-leases.json under their own $FLEET_LOG_DIR, so `reserved_pct` never contained the other
# instance's in-flight spend. maxx_share_ceiling.py subtracts reserved_pct specifically to
# prevent double-spend and its comment calls the result "already-coordinated" -- across
# instances it was not. Both files sat at `[]` while both fleets were running.
#
# Set FLEET_LEASE_DIR to a path bind-mounted into every instance's container (a sibling of the
# shared KIT_DIR on the host). The flock in _locked() already serializes concurrent
# read-modify-write, and it locks the ledger PATH -- so pointing several containers at one
# bind-mounted file is exactly the case it was written for, no new machinery needed.
STATE_FILE = Path(
    os.environ.get("FLEET_LEASE_DIR") or LOG_DIR
).expanduser() / "maxx-leases.json"

# Who is holding a lease. Leases are tagged so a slice can be enforced PER INSTANCE: an
# instance's own live leases are what count against its share, while everyone's leases count
# against the global pot.
INSTANCE = (os.environ.get("FLEET_INSTANCE_NAME") or os.environ.get("FLEET_CONTAINER_NAME")
            or "default")


class LeaseDenied(RuntimeError):
    """A reservation was refused because it would exceed the caller's own slice.

    Raised, not returned as a sentinel: a caller that ignores this and spends anyway is
    exactly the double-spend the slice exists to prevent, so it must be impossible to miss
    by accident.
    """


@contextlib.contextmanager
def _locked(state_file: Path):
    """Exclusive, blocking lock around a read-modify-write cycle. Back-to-back gru passes
    (cron does not wait for one to land before the next fires) can otherwise both read the
    same on-disk list before either writes, silently clobbering one lease with the other's
    write -- the same flat-file-state race class as fleet-kit#51. Serializing the whole
    read+modify+write under one lock also means only one process is ever inside
    `_write_leases` at a time, so its shared tmp path can't be raced out from under it either."""
    lock_path = state_file.with_suffix(state_file.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_leases(state_file: Path) -> list[dict]:
    try:
        return json.loads(state_file.read_text())
    except (FileNotFoundError, ValueError):
        return []


def _write_leases(state_file: Path, leases: list[dict]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(leases))
    tmp.replace(state_file)


def _unexpired(leases: list[dict], now: float) -> list[dict]:
    return [lease for lease in leases if lease["created_at"] + lease["ttl_sec"] > now]


def maxx_reserve(pct: float, label: str, ttl_sec: int, state_file: Path | None = None,
                 instance: str | None = None, budget_pct: float | None = None) -> str:
    """Record a new lease and return its lease_id. Prunes expired leases while it's at it.

    `budget_pct` turns this into an ENFORCED slice rather than an advisory one. When given,
    the reservation is refused (LeaseDenied) if this instance's own live leases plus `pct`
    would exceed it. Without that check a "30% share" is only a rate limit on a race -- every
    caller computes its slice from whatever is left at the moment it asks, so whoever asks
    first takes the pot and a later caller's share is 30% of the remainder, not 30% of the
    hour (Reif, 2026-09-02: "before gru gets there, it could be all gone").

    Note this counts only THIS instance's leases against the budget: the point of a slice is
    that a greedy neighbour exhausts its own share and physically cannot reach into yours.
    """
    state_file = state_file or STATE_FILE
    instance = instance or INSTANCE
    lease_id = f"{label}-{uuid.uuid4().hex[:8]}"
    with _locked(state_file):
        now = time.time()
        leases = _unexpired(_read_leases(state_file), now)
        if budget_pct is not None:
            mine = sum(l["pct"] for l in leases if l.get("instance") == instance)
            if mine + pct > budget_pct + 1e-9:
                raise LeaseDenied(
                    f"{instance}: reserving {pct:.4f} would exceed its slice "
                    f"({mine:.4f} already held of {budget_pct:.4f})")
        leases.append({"lease_id": lease_id, "pct": pct, "label": label,
                        "instance": instance, "created_at": now, "ttl_sec": ttl_sec})
        _write_leases(state_file, leases)
    return lease_id


def maxx_release(lease_id: str, state_file: Path | None = None) -> None:
    """Drop a lease early. Always safe to call -- a lease that already expired and was pruned
    is simply not found, never an error (gru.md step 6 calls this unconditionally, even on a
    failure path)."""
    state_file = state_file or STATE_FILE
    with _locked(state_file):
        leases = [lease for lease in _unexpired(_read_leases(state_file), time.time())
                  if lease["lease_id"] != lease_id]
        _write_leases(state_file, leases)


def reserved_pct_for(instance: str | None = None, state_file: Path | None = None) -> float:
    """Sum of one instance's own currently-unexpired leases -- what counts against its slice.

    Leases written before instance tagging existed carry no "instance" key; they are counted
    against nobody's slice but still count in total_reserved_pct(), which is the conservative
    reading (they shrink the global pot, they never license extra local spend).
    """
    state_file = state_file or STATE_FILE
    instance = instance or INSTANCE
    with _locked(state_file):
        live = _unexpired(_read_leases(state_file), time.time())
        return sum(l["pct"] for l in live if l.get("instance") == instance)


def total_reserved_pct(state_file: Path | None = None) -> float:
    """Sum of every currently-unexpired lease's pct. Prunes expired leases as a side effect,
    same self-cleaning shape as account_pool.sh's exhaustion state file."""
    state_file = state_file or STATE_FILE
    with _locked(state_file):
        now = time.time()
        leases = _read_leases(state_file)
        live = _unexpired(leases, now)
        if len(live) != len(leases):
            _write_leases(state_file, live)
        return sum(lease["pct"] for lease in live)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_reserve = sub.add_parser("reserve", help="record a new lease, print its lease_id")
    p_reserve.add_argument("--pct", type=float, required=True)
    p_reserve.add_argument("--label", required=True)
    p_reserve.add_argument("--ttl-sec", type=int, default=3600)

    p_release = sub.add_parser("release", help="drop a lease early")
    p_release.add_argument("--lease-id", required=True)

    sub.add_parser("total", help="sum of all currently-unexpired leases' pct")

    args = parser.parse_args()
    if args.cmd == "reserve":
        print(json.dumps({"lease_id": maxx_reserve(args.pct, args.label, args.ttl_sec)}))
    elif args.cmd == "release":
        maxx_release(args.lease_id)
        print(json.dumps({"released": args.lease_id}))
    elif args.cmd == "total":
        print(json.dumps({"reserved_pct": total_reserved_pct()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
