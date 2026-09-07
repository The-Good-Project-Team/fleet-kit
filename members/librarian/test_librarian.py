#!/usr/bin/env python3
"""test_librarian.py -- proves the two load-bearing properties from philanthropy#4439's
acceptance criteria: (1) a secret-shaped string gets redacted inside the transcript store and
left completely untouched anywhere that looks like a repo checkout, verified together in one
run; (2) after a scrub, the tracked patterns are gone from the store while the run's own report
still names every class it found, for a human to rotate."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import librarian  # noqa: E402

TRACKED_NEEDLES = ("gho_", "ghp_", "ghs_", "ghu_", "ghr_", "sk-ant-", "PGPASSWORD=", "postgresql://")


class RedactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "transcripts"
        self.root.mkdir()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()

    def test_seeded_token_redacted_in_transcript_untouched_in_repo_code_fence(self):
        """AC1: RED before, GREEN after, both verified in one run."""
        token = "gho_" + "A" * 36
        transcript = self.root / "sess" / "abc.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"role": "user", "content": f"here is a token={token}"}) + "\n")

        readme = self.repo / "README.md"
        readme.write_text(f"example format, never a real secret:\n```\ntoken={token}\n```\n")

        # RED: before any scrub runs, both copies still hold the raw token.
        self.assertIn(token, transcript.read_text())
        self.assertIn(token, readme.read_text())

        stats = librarian.ScrubStats()
        changed = librarian.scrub_file(transcript, stats, execute=True)

        # GREEN: the transcript is redacted to the stable marker...
        self.assertTrue(changed)
        scrubbed = transcript.read_text()
        self.assertNotIn(token, scrubbed)
        self.assertIn("[REDACTED:gho]", scrubbed)
        self.assertEqual(stats.occurrences.get("gho"), 1)
        # ...and the /repo copy was never even opened: scrub_file only touches the path it's
        # given, and main()'s own root-scoping (proven in test_main_refuses_repo_shaped_root
        # below) is what keeps a real run from ever handing it a /repo path to begin with.
        self.assertIn(token, readme.read_text())

    def test_marker_is_stable_and_idempotent(self):
        token = "sk-ant-" + "B" * 40
        f = self.root / "s.jsonl"
        f.write_text(json.dumps({"text": token}) + "\n")

        stats1 = librarian.ScrubStats()
        librarian.scrub_file(f, stats1, execute=True)
        first_pass = f.read_text()
        self.assertIn("[REDACTED:sk-ant]", first_pass)

        stats2 = librarian.ScrubStats()
        changed_again = librarian.scrub_file(f, stats2, execute=True)
        self.assertFalse(changed_again, "re-scrubbing an already-redacted file changed it")
        self.assertEqual(f.read_text(), first_pass)

    def test_specific_class_not_double_redacted_by_generic_secret_env_pattern(self):
        """A key name like GH_TOKEN also looks like a *_TOKEN= pair to the generic secret_env
        catch-all -- the specific gho/ghp/... classification must win, not get overwritten."""
        token = "ghp_" + "C" * 36
        f = self.root / "s.jsonl"
        f.write_text(f"GH_TOKEN={token}\n")
        stats = librarian.ScrubStats()
        librarian.scrub_file(f, stats, execute=True)
        text = f.read_text()
        self.assertIn("[REDACTED:ghp]", text)
        self.assertNotIn("secret_env", text)

    def test_iter_transcripts_skips_memory_dir_and_non_jsonl(self):
        mem_dir = self.root / "-repo" / "memory"
        mem_dir.mkdir(parents=True)
        note = mem_dir / "MEMORY.md"
        note.write_text("gho_" + "D" * 36)

        other = self.root / "sess2" / "notes.txt"
        other.parent.mkdir(parents=True)
        other.write_text("not a transcript")

        found = list(librarian.iter_transcripts(self.root))
        self.assertNotIn(note, found)
        self.assertNotIn(other, found)

    def test_iter_transcripts_skips_jsonl_inside_memory_dir(self):
        """The memory-dir guard itself, not the suffix filter: a .jsonl file (the one shape the
        protection actually exists for) inside a memory/ dir must still be excluded -- this
        test fails if the is_protected(p) call site in iter_transcripts() is ever deleted,
        unlike the .md-seeded test above which the suffix filter alone already satisfies."""
        mem_dir = self.root / "-repo" / "memory"
        mem_dir.mkdir(parents=True)
        note = mem_dir / "notes.jsonl"
        note.write_text("gho_" + "M" * 36)

        found = list(librarian.iter_transcripts(self.root))
        self.assertNotIn(note, found)
        found_full = list(librarian.iter_transcripts(self.root, include_compressed=True))
        self.assertNotIn(note, found_full)

    def test_report_names_every_class_after_grep_returns_zero_hits(self):
        """AC2: 0 grep hits post-scrub, report still names the classes for human rotation."""
        (self.root / "a.jsonl").write_text("token gho_" + "E" * 36)
        (self.root / "b.jsonl").write_text("PGPASSWORD=hunter2hunter2")
        (self.root / "c.jsonl").write_text("db at postgresql://u:secretpw@host:5432/db")

        stats = librarian.ScrubStats()
        for f in librarian.iter_transcripts(self.root):
            librarian.scrub_file(f, stats, execute=True)

        combined = "\n".join(p.read_text() for p in librarian.iter_transcripts(self.root))
        for needle in TRACKED_NEEDLES:
            self.assertNotIn(needle, combined, f"{needle!r} survived a scrub run")

        lines = stats.report_lines()
        self.assertTrue(any("GitHub OAuth" in l and "1 file" in l for l in lines), lines)
        self.assertTrue(any("Postgres" in l and "2 file" in l for l in lines), lines)

    def test_full_scan_rescans_already_compressed_transcript(self):
        """AC1: a credential archived to .jsonl.gz before a pattern existed for it is still
        reachable by a later --full-scan, decompress-scrub-recompress."""
        import gzip as gzip_mod

        token = "gho_" + "J" * 36
        gz = self.root / "sess" / "old.jsonl.gz"
        gz.parent.mkdir(parents=True)
        with gzip_mod.open(gz, "wt", encoding="utf-8") as f:
            f.write(json.dumps({"content": f"leaked {token}"}) + "\n")

        found = list(librarian.iter_transcripts(self.root, include_compressed=True))
        self.assertIn(gz, found)

        stats = librarian.ScrubStats()
        changed = librarian.scrub_file(gz, stats, execute=True)
        self.assertTrue(changed)
        with gzip_mod.open(gz, "rt", encoding="utf-8") as f:
            scrubbed = f.read()
        self.assertNotIn(token, scrubbed)
        self.assertIn("[REDACTED:gho]", scrubbed)

    def test_incremental_scan_never_lists_compressed_transcript(self):
        """AC2: the ordinary (non-full-scan) path must not even enumerate a .jsonl.gz, let
        alone decompress it -- only --full-scan pays that cost."""
        import gzip as gzip_mod

        gz = self.root / "sess" / "old.jsonl.gz"
        gz.parent.mkdir(parents=True)
        with gzip_mod.open(gz, "wt", encoding="utf-8") as f:
            f.write("gho_" + "K" * 36)

        found = list(librarian.iter_transcripts(self.root))
        self.assertNotIn(gz, found)

    def test_main_refuses_repo_shaped_root(self):
        script = Path(__file__).resolve().parent / "librarian.py"
        proc = subprocess.run(
            [sys.executable, str(script), "--root", str(self.repo)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refus", (proc.stdout + proc.stderr).lower())

    def test_main_end_to_end_scrubs_and_reports(self):
        token = "gho_" + "F" * 36
        (self.root / "run.jsonl").write_text(f"leaked {token}\n")
        script = Path(__file__).resolve().parent / "librarian.py"
        # --state-file MUST be isolated here: the default is the real production watermark
        # (~/.cache/fleet-kit/librarian_state.json). Omitting it would stamp that file with
        # this test's "now" on every CI/dev run, and since scrub skips anything older than the
        # watermark, the real first deploy run against the actual 726MB corpus (all older than
        # a test just run) would then skip almost everything -- silently defeating AC1/AC2.
        state_file = str(Path(self.tmp.name) / "state.json")
        proc = subprocess.run(
            [sys.executable, str(script), "--root", str(self.root), "--execute",
             "--state-file", state_file],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("GitHub OAuth: 1 file(s), 1 occurrence(s)", proc.stdout)
        self.assertNotIn(token, (self.root / "run.jsonl").read_text())


class WatermarkTest(unittest.TestCase):
    """A full-text scan of the real corpus is minutes, not seconds -- this is what keeps a
    routine hourly tick from re-reading a closed transcript it already scrubbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "transcripts"
        self.root.mkdir()
        self.state_file = str(Path(self.tmp.name) / "state.json")

    def _age(self, path: Path, days: float) -> None:
        old = time.time() - days * 86400
        os.utime(path, (old, old))

    def test_missing_state_file_means_scan_everything(self):
        self.assertEqual(librarian.load_watermark(self.state_file), 0.0)

    def test_save_then_load_roundtrips(self):
        librarian.save_watermark(self.state_file, 12345.0)
        self.assertEqual(librarian.load_watermark(self.state_file), 12345.0)

    def test_unchanged_file_skipped_on_incremental_rerun(self):
        old = self.root / "old.jsonl"
        old.write_text("gho_" + "G" * 36)
        self._age(old, 5)
        watermark = time.time() - 1 * 86400  # 1 day ago; old.jsonl is 5 days old
        found = list(librarian.iter_transcripts(self.root, since=watermark))
        self.assertNotIn(old, found)

    def test_touched_file_still_picked_up_on_incremental_rerun(self):
        fresh = self.root / "fresh.jsonl"
        fresh.write_text("gho_" + "H" * 36)
        watermark = time.time() - 1 * 86400
        found = list(librarian.iter_transcripts(self.root, since=watermark))
        self.assertIn(fresh, found)

    def test_main_execute_advances_watermark_dry_run_does_not(self):
        (self.root / "a.jsonl").write_text("gho_" + "I" * 36)
        script = Path(__file__).resolve().parent / "librarian.py"
        base_cmd = [sys.executable, str(script), "--root", str(self.root),
                    "--state-file", self.state_file, "--skip-retention"]

        subprocess.run(base_cmd, capture_output=True, text=True, timeout=30)
        self.assertEqual(librarian.load_watermark(self.state_file), 0.0, "dry-run advanced the watermark")

        subprocess.run(base_cmd + ["--execute"], capture_output=True, text=True, timeout=30)
        self.assertGreater(librarian.load_watermark(self.state_file), 0.0)

    def test_checkpoint_saves_progress_before_a_simulated_mid_scan_kill(self):
        """gh#588 AC1/AC3: a run interrupted mid-loop (simulating its own SIGKILLed timeout)
        still leaves a watermark strictly newer than what existed before the run started,
        because run_scrub() checkpoints every checkpoint_every_files rather than only after
        the whole loop across all roots completes."""
        for i in range(10):
            (self.root / f"f{i}.jsonl").write_text(f"file {i}\n")

        before = 111.0
        librarian.save_watermark(self.state_file, before)

        real_scrub_file = librarian.scrub_file
        call_count = {"n": 0}

        def flaky_scrub_file(path, stats, execute):
            call_count["n"] += 1
            if call_count["n"] > 4:
                raise RuntimeError("simulated kill mid-scan")
            return real_scrub_file(path, stats, execute)

        run_started = time.time()
        with mock.patch.object(librarian, "scrub_file", side_effect=flaky_scrub_file):
            with self.assertRaises(RuntimeError):
                librarian.run_scrub(
                    [self.root], since=0.0, execute=True, state_file=self.state_file,
                    run_started=run_started, full_scan=False, checkpoint_every_files=2,
                )

        # The kill hit after file 5 (4 succeeded, then the 5th raised); a checkpoint every 2
        # files means files 1-2 and 3-4 each triggered a checkpoint before the kill, so the
        # on-disk watermark must already be newer than what existed before the run.
        after_kill = librarian.load_watermark(self.state_file)
        self.assertGreater(after_kill, before)

    def test_uninterrupted_run_final_watermark_is_run_start_time_not_finish_time(self):
        """gh#588 AC4: checkpointing must not change the final saved value for an
        uninterrupted run -- it's still run_started (captured before the first file was
        touched), the same value the pre-checkpointing code saved at the end."""
        for i in range(5):
            (self.root / f"f{i}.jsonl").write_text(f"file {i}\n")
        run_started = time.time() - 1000.0
        librarian.run_scrub(
            [self.root], since=0.0, execute=True, state_file=self.state_file,
            run_started=run_started, full_scan=False, checkpoint_every_files=2,
        )
        self.assertEqual(librarian.load_watermark(self.state_file), run_started)

    def test_interrupted_run_watermark_does_not_orphan_unprocessed_older_files(self):
        """The bug this file's checkpoint fix (see librarian.py's CHECKPOINTING docstring)
        replaced: the original gh#588 shape checkpointed a constant run_started value no
        matter how far the path-ordered walk actually got. Since every real candidate file's
        mtime already predates run_started by definition (it's why the file was a candidate at
        all), a kill partway through permanently orphaned every unreached file the moment that
        checkpoint fired -- confirmed live 2026-09-07, secrets in 305 transcripts survived
        weeks of incremental runs that each reported success. This test seeds several
        days-old files, kills the scan after only some of them are processed, and proves a
        second incremental run (using the watermark the killed run left behind) still reaches
        every file the first run never got to -- the old constant-run_started checkpoint would
        leave zero candidates for this second call."""
        for i in range(6):
            f = self.root / f"f{i}.jsonl"
            f.write_text(f"file {i} token gho_" + "Q" * 36)
            self._age(f, 6.0 - i * 0.5)  # f0 oldest (6.0d) ... f5 newest (3.5d), strictly ascending

        real_scrub_file = librarian.scrub_file
        call_count = {"n": 0}

        def flaky_scrub_file(path, stats, execute):
            call_count["n"] += 1
            if call_count["n"] > 3:
                raise RuntimeError("simulated kill mid-scan")
            return real_scrub_file(path, stats, execute)

        run_started = time.time()
        with mock.patch.object(librarian, "scrub_file", side_effect=flaky_scrub_file):
            with self.assertRaises(RuntimeError):
                librarian.run_scrub(
                    [self.root], since=0.0, execute=True, state_file=self.state_file,
                    run_started=run_started, full_scan=False, checkpoint_every_files=2,
                )

        watermark_after_kill = librarian.load_watermark(self.state_file)
        # The old (buggy) code saved run_started here -- days newer than every seeded file --
        # which would make the assertions below fail identically to the real incident.
        self.assertLess(watermark_after_kill, run_started)

        second_stats, _ = librarian.run_scrub(
            [self.root], since=watermark_after_kill, execute=True, state_file=self.state_file,
            run_started=time.time(), full_scan=False,
        )
        # f3, f4, f5 were never reached by the killed run (only 3 succeeded); the fix's
        # contract is that a second incremental run still finds them via the watermark left
        # behind, instead of silently treating them as already scrubbed.
        self.assertGreaterEqual(second_stats.files_scanned, 3, "unprocessed older files were orphaned by the interrupted run's watermark")

    def test_full_scan_cli_ignores_watermark_even_with_checkpointing(self):
        """gh#588 AC5: --full-scan still ignores the on-disk watermark entirely, unaffected
        by the new mid-loop checkpointing."""
        old_file = self.root / "old.jsonl"
        old_file.write_text("gho_" + "Z" * 36)
        self._age(old_file, 5)
        # Seed a watermark newer than old.jsonl's mtime -- an ordinary incremental run would
        # skip it, so redaction only happens here if --full-scan truly bypasses the watermark.
        librarian.save_watermark(self.state_file, time.time())

        script = Path(__file__).resolve().parent / "librarian.py"
        proc = subprocess.run(
            [sys.executable, str(script), "--root", str(self.root), "--execute",
             "--full-scan", "--state-file", self.state_file, "--skip-retention"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[REDACTED:gho]", old_file.read_text())


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _age(self, path: Path, days: float) -> None:
        old = time.time() - days * 86400
        os.utime(path, (old, old))

    def test_mid_age_compressed_ancient_dropped(self):
        """AC3: past the retention window, compressed then dropped, on a real directory walk."""
        mid = self.root / "mid.jsonl"
        mid.write_text("hello\n")
        self._age(mid, 45)

        ancient = self.root / "ancient.jsonl"
        ancient.write_text("bye\n")
        self._age(ancient, 120)

        fresh = self.root / "fresh.jsonl"
        fresh.write_text("still relevant\n")

        results = librarian.retention_sweep(self.root, execute=True, compress_days=30, drop_days=90)
        by_name = {Path(r["path"]).name: r["action"] for r in results}

        self.assertEqual(by_name.get("mid.jsonl"), "compress")
        self.assertTrue((self.root / "mid.jsonl.gz").exists())
        self.assertFalse(mid.exists())

        self.assertEqual(by_name.get("ancient.jsonl"), "drop")
        self.assertFalse(ancient.exists())
        self.assertFalse((self.root / "ancient.jsonl.gz").exists())

        self.assertNotIn("fresh.jsonl", by_name)
        self.assertTrue(fresh.exists())

    def test_already_compressed_file_dropped_once_past_drop_days(self):
        gz = self.root / "old.jsonl.gz"
        gz.write_bytes(b"\x1f\x8b\x00")  # not valid gzip, but retention never reads gz contents
        self._age(gz, 95)
        librarian.retention_sweep(self.root, execute=True, compress_days=30, drop_days=90)
        self.assertFalse(gz.exists())

    def test_memory_dir_never_swept_regardless_of_age(self):
        mem = self.root / "-repo" / "memory"
        mem.mkdir(parents=True)
        note = mem / "MEMORY.md"
        note.write_text("keep me forever")
        self._age(note, 99999)
        librarian.retention_sweep(self.root, execute=True, compress_days=30, drop_days=90)
        self.assertTrue(note.exists())
        self.assertEqual(note.read_text(), "keep me forever")

    def test_jsonl_inside_memory_dir_never_swept(self):
        """The memory-dir guard itself: a .jsonl file (TRANSCRIPT_SUFFIXES would otherwise
        match it) inside memory/ must survive retention -- fails if is_protected(p) is ever
        deleted from retention_sweep(), unlike the .md-seeded test above."""
        mem = self.root / "-repo" / "memory"
        mem.mkdir(parents=True)
        note = mem / "notes.jsonl"
        note.write_text("keep me forever")
        self._age(note, 99999)
        librarian.retention_sweep(self.root, execute=True, compress_days=30, drop_days=90)
        self.assertTrue(note.exists())
        self.assertEqual(note.read_text(), "keep me forever")

    def test_dry_run_changes_nothing(self):
        old = self.root / "old.jsonl"
        old.write_text("data\n")
        self._age(old, 120)
        librarian.retention_sweep(self.root, execute=False, compress_days=30, drop_days=90)
        self.assertTrue(old.exists())
        self.assertEqual(old.read_text(), "data\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
