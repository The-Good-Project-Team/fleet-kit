#!/usr/bin/env python3
"""memory_tend.py -- the deterministic half of tending a memory dir (fleet-kit#784, job 2 of
philanthropy#4410).

MEMORY.md is read into context by every member on every pass. It only ever grew: 15,686 bytes
/ 64 entries on 2026-09-09 against its own cap of 10,240 bytes / 50 entries
(memory_md_cap_policy.md), and the tgp root's memory dir held 740 files. Judgment (which two
entries are one lesson, which resolved entry still teaches something) is the daily librarian
pass's job. Everything a script can decide is decided here so the model never spends a turn
counting bytes:

  - dead index lines: `- [title](file.md) ...` whose file does not exist -> dropped (--execute)
  - orphans: *.md files in the dir that no index line points at -> listed
  - resolved candidates: index lines or files whose text says RESOLVED / CLOSED / SUPERSEDED /
    CORRECTED / won't-fix -> listed for the pass to judge
  - similar titles: pairs of index titles that read alike (SequenceMatcher >= 0.72) -> listed
    as merge candidates
  - size and entry count against the cap -> over/under, and by how much

Never touches anything but MEMORY.md, and only with --execute, and only to remove dead lines.
Every other change is the pass's own Edit, inside the memory dir, on purpose.

Usage: memory_tend.py [--root DIR ...] [--execute] [--cap-bytes 10240] [--cap-entries 50] [--json]
Default roots: every /root/.claude-*/projects/*/memory that holds a MEMORY.md.
"""
from __future__ import annotations

import argparse
import difflib
import glob
import json
import re
import sys
from pathlib import Path

DEFAULT_ROOT_GLOB = "/root/.claude-*/projects/*/memory"
CAP_BYTES = 10240
CAP_ENTRIES = 50
INDEX_RE = re.compile(r"^- \[(?P<title>[^\]]*)\]\((?P<file>[^)]+)\)(?P<rest>.*)$")
RESOLVED_RE = re.compile(r"\b(RESOLVED|CLOSED|SUPERSEDED|CORRECTED|CORRECTION|won'?t[- ]fix|MERGED)\b", re.IGNORECASE)
SIMILAR = 0.72


def find_roots(explicit: list[str] | None) -> list[Path]:
    if explicit:
        return [Path(r) for r in explicit]
    return [Path(p) for p in sorted(glob.glob(DEFAULT_ROOT_GLOB)) if (Path(p) / "MEMORY.md").exists()]


def parse_index(text: str) -> list[dict]:
    out = []
    for i, line in enumerate(text.splitlines()):
        m = INDEX_RE.match(line)
        if m:
            out.append({"line_no": i, "title": m.group("title").strip(), "file": m.group("file").strip(),
                        "rest": m.group("rest").strip(), "line": line})
    return out


def tend(root: Path, *, execute: bool, cap_bytes: int, cap_entries: int) -> dict:
    index_path = root / "MEMORY.md"
    text = index_path.read_text(encoding="utf-8", errors="ignore") if index_path.exists() else ""
    entries = parse_index(text)
    files = {p.name for p in root.glob("*.md") if p.name != "MEMORY.md"}
    referenced = {e["file"] for e in entries}

    dead = [e for e in entries if e["file"] not in files and not (root / e["file"]).exists()]
    orphans = sorted(files - referenced)
    resolved = []
    for e in entries:
        hay = e["title"] + " " + e["rest"]
        p = root / e["file"]
        if p.exists():
            try:
                hay += " " + p.read_text(encoding="utf-8", errors="ignore")[:1500]
            except OSError:
                pass
        if RESOLVED_RE.search(hay):
            resolved.append({"file": e["file"], "title": e["title"]})
    similar = []
    titles = [(e["file"], e["title"].lower()) for e in entries]
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            ratio = difflib.SequenceMatcher(None, titles[i][1], titles[j][1]).ratio()
            if ratio >= SIMILAR:
                similar.append({"a": titles[i][0], "b": titles[j][0], "ratio": round(ratio, 2)})

    removed = 0
    if execute and dead:
        dead_lines = {e["line_no"] for e in dead}
        kept = [l for i, l in enumerate(text.splitlines()) if i not in dead_lines]
        index_path.write_text("\n".join(kept) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
        removed = len(dead)
        text = index_path.read_text(encoding="utf-8", errors="ignore")
        entries = parse_index(text)

    size = len(text.encode("utf-8"))
    return {
        "root": str(root), "size_bytes": size, "entries": len(entries), "files": len(files),
        "cap_bytes": cap_bytes, "cap_entries": cap_entries,
        "over_cap": size > cap_bytes or len(entries) > cap_entries,
        "bytes_over": max(0, size - cap_bytes), "entries_over": max(0, len(entries) - cap_entries),
        "dead": [{"file": e["file"], "title": e["title"]} for e in dead], "dead_removed": removed,
        "orphans": orphans, "resolved_candidates": resolved, "similar": similar,
    }


def as_text(r: dict) -> str:
    cap = "OVER CAP" if r["over_cap"] else "under cap"
    lines = [f"{r['root']}: {r['size_bytes']}B / {r['entries']} entries ({cap}; cap {r['cap_bytes']}B / "
             f"{r['cap_entries']}); {r['files']} files; {len(r['orphans'])} orphan(s); "
             f"{len(r['dead'])} dead index line(s){' removed' if r['dead_removed'] else ''}; "
             f"{len(r['resolved_candidates'])} resolved candidate(s); {len(r['similar'])} similar pair(s)"]
    for d in r["dead"]:
        lines.append(f"  dead      {d['file']}  ({d['title']})")
    for o in r["orphans"][:40]:
        lines.append(f"  orphan    {o}")
    if len(r["orphans"]) > 40:
        lines.append(f"  orphan    ... {len(r['orphans']) - 40} more")
    for c in r["resolved_candidates"]:
        lines.append(f"  resolved? {c['file']}  ({c['title']})")
    for s in r["similar"]:
        lines.append(f"  similar   {s['a']}  ~  {s['b']}  ({s['ratio']})")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", action="append", default=None)
    ap.add_argument("--execute", action="store_true", help="remove dead index lines (the only write)")
    ap.add_argument("--cap-bytes", type=int, default=CAP_BYTES)
    ap.add_argument("--cap-entries", type=int, default=CAP_ENTRIES)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv[1:])
    roots = [r for r in find_roots(a.root) if r.is_dir()]
    if not roots:
        print("memory_tend: no memory dir with a MEMORY.md found", file=sys.stderr)
        return 1
    results = [tend(r, execute=a.execute, cap_bytes=a.cap_bytes, cap_entries=a.cap_entries) for r in roots]
    if a.json:
        print(json.dumps(results, indent=1))
    else:
        print("\n".join(as_text(r) for r in results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
