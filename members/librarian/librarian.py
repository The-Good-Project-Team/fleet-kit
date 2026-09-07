#!/usr/bin/env python3
"""librarian.py -- redacts secret-shaped strings from Claude Code session transcripts in place,
and enforces a compress/drop retention window. This is librarian's job 1 (nonprofit-atlas#4410
seq:1, philanthropy#4439): 726MB of session transcripts under /root/.claude-*/projects with no
retention policy held live GitHub OAuth tokens (39 files, 56 occurrences) and plaintext Postgres
credentials (15 files) at filing time.

REDACTION IS NOT ROTATION. Scrubbing a transcript does not un-leak a credential that already sat
on disk -- this script's report names every distinct class it found so a human can rotate the
real thing; it never claims the leak is "fixed".

SAFETY: never touches /repo or a charter file, transcript store only. main() refuses to run
against a root that looks like a repo checkout (a .git dir, or a path ending in "/repo") --
that is the mechanism that keeps an identically-shaped fake secret inside a /repo code fence
untouched even though the same string inside a transcript gets redacted. A directory literally
named "memory" is skipped everywhere (scrub AND retention) -- a fleet member's curated memory
notes can live inside the same account tree as its raw transcripts, and that is not a session
log to scrub or age out.

Dry-run by default. Pass --execute to actually write/delete anything.

INCREMENTAL SCRUB: a full-text regex scan of the whole transcript store (multi-GB, thousands of
files, measured 20+ minutes cold against this fleet's real corpus) cannot run every hour inside
this member's own 900s timeout once it's a routine tick rather than a one-time backfill. So scrub
(never retention -- that's a cheap stat() per file, not a content scan) only re-reads files whose
mtime is newer than the watermark left by the last successful --execute run; a closed transcript
that's already been scrubbed once never needs re-reading. --full-scan bypasses the watermark for
the first run against a real corpus, or after the pattern list changes.

CHECKPOINTING: an --execute run also saves the watermark every CHECKPOINT_EVERY_FILES files,
not only after the whole scan finishes (see run_scrub()) -- a run killed mid-scan by its own
timeout still banks the files it got through before the kill, rather than the next tick
restarting from since=0.0 (gh#588).

--full-scan ALSO reopens every already-compressed .jsonl.gz transcript (decompress, scrub,
recompress if changed) -- an ordinary incremental tick never does, since gunzipping the whole
archived corpus on every hourly run would defeat the watermark's purpose. This means a
.jsonl.gz is unconditionally re-read on every --full-scan regardless of when it was archived,
rather than tracked against a separate "pattern list last changed" watermark -- --full-scan is
already the documented slow, explicit-opt-in path (see INCREMENTAL SCRUB above), and a real
corpus's compressed fraction is a small tail next to the multi-GB raw scan it already pays for.
"""
from __future__ import annotations

import argparse
import gzip
import glob
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

COMPRESS_AFTER_DAYS = float(os.environ.get("LIBRARIAN_COMPRESS_DAYS", "30"))
DROP_AFTER_DAYS = float(os.environ.get("LIBRARIAN_DROP_DAYS", "90"))

DEFAULT_ROOT_GLOB = "/root/.claude-*/projects"
DEFAULT_STATE_FILE = os.environ.get(
    "LIBRARIAN_STATE_FILE", os.path.expanduser("~/.cache/fleet-kit/librarian_state.json")
)

# How often (in files scanned) an --execute run checkpoints the watermark mid-loop, on top of
# the final save once the whole scan finishes -- gh#588: a run SIGKILLed by its own 900s
# timeout partway through a real corpus otherwise banks zero progress no matter how many files
# it scrubbed before the kill.
CHECKPOINT_EVERY_FILES = int(os.environ.get("LIBRARIAN_CHECKPOINT_FILES", "200"))

# A directory with this exact name is a curated memory store, not a transcript dump, wherever
# it appears in the tree (see module docstring) -- never scrub or age-sweep it.
PROTECTED_DIR_NAMES = {"memory"}

TRANSCRIPT_SUFFIXES = (".jsonl", ".jsonl.gz")

# Human-facing rollup label per pattern class -- this is what the end-of-run report groups by,
# so "39 files / GitHub OAuth" stays one line instead of five near-duplicate gho/ghp/ghs/ghu/ghr
# rows. The in-file marker itself stays per-prefix ([REDACTED:gho], not [REDACTED:github_oauth])
# -- see PATTERNS below -- because a human rotating credentials benefits from knowing exactly
# which token *type* leaked, even once they're grouped for the summary count.
CLASS_LABELS = {
    "gho": "GitHub OAuth",
    "ghp": "GitHub OAuth",
    "ghs": "GitHub OAuth",
    "ghu": "GitHub OAuth",
    "ghr": "GitHub OAuth",
    "sk-ant": "Anthropic API key",
    "pgpassword": "Postgres",
    "postgres_url": "Postgres",
    "secret_env": "Secret-shaped env var",
}

# Ordered: specific patterns before the generic secret_env catch-all, and each substitution's
# marker text starts with "[REDACTED:" -- secret_env's own value group excludes anything already
# starting with that prefix (see its regex below) so a key name that trips BOTH a specific
# pattern and the generic one (e.g. "GH_TOKEN=gho_xxx" matches gho_ first, and would also look
# like a *_TOKEN= pair to secret_env) never gets double-redacted into a less specific marker.
def _github_token_pattern(prefix: str) -> re.Pattern:
    return re.compile(rf"\b{prefix}_[A-Za-z0-9]{{20,255}}\b")


PATTERNS: list[tuple[str, re.Pattern, "callable"]] = []


def _simple_sub(cls: str):
    return lambda m: f"[REDACTED:{cls}]"


for _prefix in ("gho", "ghp", "ghs", "ghu", "ghr"):
    PATTERNS.append((_prefix, _github_token_pattern(_prefix), _simple_sub(_prefix)))

PATTERNS.append((
    "sk-ant",
    re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b"),
    _simple_sub("sk-ant"),
))
PATTERNS.append((
    "pgpassword",
    re.compile(r"\bPGPASSWORD=\S+"),
    # Whole match, key included: AC2 (philanthropy#4439) greps for the literal string
    # "PGPASSWORD=" post-scrub and expects 0 hits, so the key name can't survive either --
    # unlike secret_env below, there's only one key spelling here, so nothing useful is lost.
    _simple_sub("pgpassword"),
))
PATTERNS.append((
    "postgres_url",
    re.compile(r"\bpostgres(?:ql)?://[^:\s]+:[^@\s]+@\S+"),
    _simple_sub("postgres_url"),
))
PATTERNS.append((
    "secret_env",
    re.compile(
        r"\b([A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY)[A-Z0-9_]*)="
        r"(?!\[REDACTED:)(\S+)"
    ),
    lambda m: f"{m.group(1)}=[REDACTED:secret_env]",
))


@dataclass
class ScrubStats:
    files_scanned: int = 0
    files_by_class: dict[str, set] = field(default_factory=dict)
    occurrences: dict[str, int] = field(default_factory=dict)

    def record(self, cls: str, path: str, count: int) -> None:
        self.files_by_class.setdefault(cls, set()).add(path)
        self.occurrences[cls] = self.occurrences.get(cls, 0) + count

    def report_lines(self) -> list[str]:
        by_label: dict[str, tuple[set, int]] = {}
        for cls, paths in self.files_by_class.items():
            label = CLASS_LABELS.get(cls, cls)
            l_paths, l_occ = by_label.get(label, (set(), 0))
            by_label[label] = (l_paths | paths, l_occ + self.occurrences[cls])
        lines = []
        for label in sorted(by_label):
            paths, occ = by_label[label]
            lines.append(
                f"{label}: {len(paths)} file(s), {occ} occurrence(s) -- needs human rotation"
            )
        return lines


def is_protected(path: Path) -> bool:
    return any(part in PROTECTED_DIR_NAMES for part in path.parts)


def looks_like_repo_root(root: Path) -> bool:
    root = root.resolve()
    if (root / ".git").exists():
        return True
    return root.name == "repo"


def iter_transcripts(root: Path, since: float = 0.0, include_compressed: bool = False):
    """since=0.0 (default) walks everything -- a real first run against a real corpus, or any
    test, wants every matching file. A positive `since` skips a file whose content could not
    have changed after that watermark, which is what keeps a routine incremental tick fast.

    include_compressed=True additionally yields already-gzipped .jsonl.gz transcripts -- only
    ever set by --full-scan (see module docstring); the ordinary incremental path leaves it
    False so a routine tick never re-reads the archived tail of the corpus."""
    if not root.is_dir():
        return
    suffixes = (".jsonl", ".jsonl.gz") if include_compressed else (".jsonl",)
    for p in sorted(root.rglob("*")):
        if not p.is_file() or is_protected(p):
            continue
        if not p.name.endswith(suffixes):
            continue
        if since:
            try:
                if p.stat().st_mtime < since:
                    continue
            except OSError:
                continue
        yield p


def load_watermark(state_file: str) -> float:
    try:
        return float(json.loads(Path(state_file).read_text()).get("last_scrub_run_at", 0.0))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0.0


def save_watermark(state_file: str, ts: float) -> None:
    p = Path(state_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"last_scrub_run_at": ts}))


def redact_text(text: str, stats: ScrubStats, path: str) -> str:
    for cls, pattern, sub_fn in PATTERNS:
        text, n = pattern.subn(sub_fn, text)
        if n:
            stats.record(cls, path, n)
    return text


def scrub_file(path: Path, stats: ScrubStats, execute: bool) -> bool:
    """Handles both raw .jsonl and already-archived .jsonl.gz transparently -- a .gz path is
    decompressed to text, scrubbed, and (if changed) recompressed back in place, so --full-scan
    can clean a credential that was archived before the pattern that catches it existed."""
    is_gz = path.name.endswith(".gz")
    try:
        if is_gz:
            with gzip.open(path, "rt", encoding="utf-8", errors="surrogateescape") as f:
                text = f.read()
        else:
            text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return False
    new_text = redact_text(text, stats, str(path))
    changed = new_text != text
    if changed and execute:
        if is_gz:
            with gzip.open(path, "wt", encoding="utf-8", errors="surrogateescape") as f:
                f.write(new_text)
        else:
            path.write_text(new_text, encoding="utf-8", errors="surrogateescape")
    return changed


def run_scrub(
    roots: list[Path],
    since: float,
    execute: bool,
    state_file: str,
    run_started: float,
    full_scan: bool,
    checkpoint_every_files: int = CHECKPOINT_EVERY_FILES,
) -> tuple[ScrubStats, int]:
    """Scans every root and, in --execute mode, checkpoints the watermark every
    checkpoint_every_files files rather than only once the whole scan finishes -- a run
    SIGKILLed mid-scan by its own timeout still banks the files it processed before the kill,
    instead of the next tick restarting from since=0.0 (gh#588).

    Every checkpoint (and the final save) writes run_started -- the timestamp captured before
    this run's very first file was touched, never a later "now" read at save time. Files are
    walked in path order, not mtime order, so a checkpoint that stamped a fresher timestamp
    could land past the mtime of a file this run hasn't reached yet, silently hiding that file
    from the next incremental run once this one gets killed before reaching it. run_started
    predates every file this run will touch, so nothing a kill leaves unprocessed can ever be
    mistaken for already-scrubbed.
    """
    stats = ScrubStats()
    changed_files = 0
    since_checkpoint = 0
    for root in roots:
        for f in iter_transcripts(root, since=since, include_compressed=full_scan):
            stats.files_scanned += 1
            if scrub_file(f, stats, execute):
                changed_files += 1
            since_checkpoint += 1
            if execute and since_checkpoint >= checkpoint_every_files:
                save_watermark(state_file, run_started)
                since_checkpoint = 0
    if execute:
        save_watermark(state_file, run_started)
    return stats, changed_files


def _compress(path: Path) -> Path:
    gz_path = path.with_name(path.name + ".gz")
    with open(path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    path.unlink()
    return gz_path


def retention_sweep(
    root: Path, execute: bool, compress_days: float, drop_days: float
) -> list[dict]:
    """Two-stage lifecycle: raw .jsonl older than compress_days -> gzip in place (space saved,
    still readable). Anything (raw or already-gz) older than drop_days -> deleted outright. On a
    normal recurring cadence everything reaching drop_days already passed through compress_days
    in an earlier run; a first-run outlier that's already past drop_days while still raw is
    deleted directly rather than compressed and then immediately unlinked."""
    if not root.is_dir():
        return []
    results = []
    now = time.time()
    for p in sorted(root.rglob("*")):
        if not p.is_file() or is_protected(p):
            continue
        if not p.name.endswith(TRANSCRIPT_SUFFIXES):
            continue
        try:
            age_days = (now - p.stat().st_mtime) / 86400
        except OSError:
            continue
        is_gz = p.name.endswith(".gz")
        if age_days >= drop_days:
            if execute:
                p.unlink()
            results.append({"path": str(p), "action": "drop", "age_days": round(age_days, 1)})
        elif age_days >= compress_days and not is_gz:
            if execute:
                _compress(p)
            results.append({"path": str(p), "action": "compress", "age_days": round(age_days, 1)})
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root", action="append", default=None,
        help=f"transcript root(s) to scan; default: glob {DEFAULT_ROOT_GLOB!r}",
    )
    ap.add_argument("--execute", action="store_true", help="apply changes; default is dry-run")
    ap.add_argument("--skip-scrub", action="store_true")
    ap.add_argument("--skip-retention", action="store_true")
    ap.add_argument("--compress-days", type=float, default=COMPRESS_AFTER_DAYS)
    ap.add_argument("--drop-days", type=float, default=DROP_AFTER_DAYS)
    ap.add_argument(
        "--full-scan", action="store_true",
        help="ignore the scrub watermark and re-read every transcript, not just ones modified "
             "since the last --execute run, AND reopen already-compressed .jsonl.gz archives "
             "too (use for the first run against a real corpus, or after the pattern list "
             "changes)",
    )
    ap.add_argument("--state-file", default=DEFAULT_STATE_FILE,
                     help="where the scrub watermark is kept")
    args = ap.parse_args()

    roots = [Path(r) for r in (args.root or sorted(glob.glob(DEFAULT_ROOT_GLOB)))]
    roots = [r for r in roots if r.is_dir()]
    if not roots:
        print("librarian: no transcript roots found", file=sys.stderr)
        return 1

    for r in roots:
        if looks_like_repo_root(r):
            print(
                f"librarian: refusing root {r} -- looks like a repo checkout, not a "
                "transcript store (has .git or is named 'repo')",
                file=sys.stderr,
            )
            return 1

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    stats = ScrubStats()
    changed_files = 0
    run_started = time.time()
    since = 0.0 if args.full_scan else load_watermark(args.state_file)
    if not args.skip_scrub:
        stats, changed_files = run_scrub(
            roots, since=since, execute=args.execute, state_file=args.state_file,
            run_started=run_started, full_scan=args.full_scan,
        )

    watermark_note = "" if args.full_scan or since == 0.0 else f", since={time.ctime(since)}"
    print(f"librarian scrub [{mode}{watermark_note}]: {stats.files_scanned} file(s) scanned, "
          f"{changed_files} redacted")
    report_lines = stats.report_lines()
    if report_lines:
        for line in report_lines:
            print(f"  {line}")
    else:
        print("  no secret-shaped strings found")

    retention_results: list[dict] = []
    if not args.skip_retention:
        for root in roots:
            retention_results.extend(
                retention_sweep(root, args.execute, args.compress_days, args.drop_days)
            )
    compressed = sum(1 for r in retention_results if r["action"] == "compress")
    dropped = sum(1 for r in retention_results if r["action"] == "drop")
    print(f"librarian retention [{mode}]: {compressed} compressed (>={args.compress_days}d), "
          f"{dropped} dropped (>={args.drop_days}d)")
    for r in retention_results:
        print(f"  {r['action']:>8}  {r['path']}  (age={r['age_days']}d)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
