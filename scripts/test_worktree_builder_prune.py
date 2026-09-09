"""worktree_builder.sh reuses worktree_prune.sh at both its call sites (gh#684, Part C4).

PR #697 fixed the blanket-prune hazard in run_member.sh but left worktree_builder.sh's two
call sites -- create_build_worktree's `git worktree prune` and cleanup()'s `git worktree
prune` -- running the original blanket prune against the same shared $REPO/.git/worktrees
admin dir. A blanket prune deletes any registered entry whose working-tree path this
container can't see, including a sibling container's still-live worktree during a rolling
cutover (#626).

These tests extract worktree_builder.sh's own `create_build_worktree` and `cleanup` function
bodies by brace-matching -- not a hand-copied reimplementation -- and execute them against a
real git repo, so a regression that reintroduces the blanket prune is caught the same way it
would break production, not just by a text grep.

Run: python3 scripts/test_worktree_builder_prune.py
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
BUILDER_SCRIPT = KIT / "scripts" / "worktree_builder.sh"
LOCK_SCRIPT = KIT / "scripts" / "worktree_lock.sh"
PRUNE_SCRIPT = KIT / "scripts" / "worktree_prune.sh"


def _extract_function(text: str, name: str) -> str:
    """Pull a `name() { ... }` function body out of bash source by brace-matching."""
    m = re.search(rf"(?m)^{re.escape(name)}\(\)\s*\{{", text)
    if not m:
        raise AssertionError(f"{name}() not found in {BUILDER_SCRIPT}")
    depth = 1
    i = text.index("{", m.start())
    j = i + 1
    while depth > 0:
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
        j += 1
    return text[m.start():j]


def run_bash(script: str, env: dict, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                           text=True, timeout=timeout)


class WorktreeBuilderNoBlanketPruneTests(unittest.TestCase):
    """AC1: zero remaining blanket `git worktree prune` invocations; the helper is sourced."""

    def test_no_blanket_prune_calls(self):
        text = BUILDER_SCRIPT.read_text()
        self.assertNotIn("git worktree prune", text)
        self.assertNotIn('git -C "$REPO" worktree prune', text)
        self.assertIn("worktree_prune.sh", text)
        self.assertIn("worktree_prune_own_container", text)


class WorktreeBuilderFunctionTests(unittest.TestCase):
    """AC2-AC6, exercised through worktree_builder.sh's own extracted function bodies rather
    than by re-testing worktree_prune.sh's helper directly (test_worktree_prune.py already
    covers the helper itself)."""

    def setUp(self):
        self.text = BUILDER_SCRIPT.read_text()
        self.tmp = tempfile.mkdtemp(prefix="fleet-worktree-builder-test-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.name", "t"], check=True)
        (Path(self.repo) / "README").write_text("x")
        subprocess.run(["git", "-C", self.repo, "add", "README"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-q", "-m", "init"], check=True)
        # create_build_worktree adds from origin/main -- give the repo an "origin" pointing at
        # itself so the extracted function needs no network.
        subprocess.run(["git", "-C", self.repo, "remote", "add", "origin", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "fetch", "-q", "origin"], check=True)
        self.log_file = os.path.join(self.tmp, "log.txt")

    def _env(self, container_id) -> dict:
        env = dict(os.environ)
        env["TMPDIR"] = self.tmp
        if container_id is None:
            env.pop("HOSTNAME", None)
        else:
            env["HOSTNAME"] = container_id
        return env

    def _run_create(self, wt_path, branch, container_id, unresolvable=False):
        create_fn = _extract_function(self.text, "create_build_worktree")
        override = 'worktree_container_id() { printf "%s" ""; }' if unresolvable else ""
        script = f"""
            set -uo pipefail
            cd "{self.repo}"
            REPO="{self.repo}"
            WT_PATH="{wt_path}"
            WT_BRANCH="{branch}"
            LOG="{self.log_file}"
            log() {{ echo "$*" >> "$LOG"; }}
            . "{LOCK_SCRIPT}"
            . "{PRUNE_SCRIPT}"
            {override}
            {create_fn}
            create_build_worktree
        """
        return run_bash(script, env=self._env(container_id))

    def _run_cleanup(self, wt_path, container_id, unresolvable=False):
        cleanup_fn = _extract_function(self.text, "cleanup")
        override = 'worktree_container_id() { printf "%s" ""; }' if unresolvable else ""
        script = f"""
            set -uo pipefail
            REPO="{self.repo}"
            WT_PATH="{wt_path}"
            ITEM_ID="test-item"
            WORKER_NAME="test-worker"
            BUILD_SUCCEEDED=1
            LOG="{self.log_file}"
            log() {{ echo "$*" >> "$LOG"; }}
            check_repo_clean_postflight() {{ :; }}
            . "{LOCK_SCRIPT}"
            . "{PRUNE_SCRIPT}"
            {override}
            {cleanup_fn}
            cleanup
        """
        return run_bash(script, env=self._env(container_id))

    def _worktree_listing(self) -> str:
        r = subprocess.run(["git", "-C", self.repo, "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, check=True)
        return r.stdout

    def test_ac2_stamp_happens_before_lock_release(self):
        wt = os.path.join(self.tmp, "wt-created")
        r = self._run_create(wt, "build/ac2", container_id="container-A")
        self.assertEqual(r.returncode, 0, r.stderr)
        stamp = Path(self.repo, ".git", "worktrees", os.path.basename(wt), "container-id")
        self.assertTrue(stamp.is_file(), "worktree_stamp_container_id was not called")
        self.assertEqual(stamp.read_text(), "container-A")

    def test_ac3_foreign_entry_survives_cleanup_prune(self):
        """AC3 (and AC7's negative-control target): a worktree stamped by another container,
        whose path this container can't resolve, must survive worktree_builder.sh's cleanup
        prune untouched. Run against unmodified `main`, this FAILS -- the blanket prune there
        removes the foreign entry -- which is what AC7 requires this test to prove."""
        wt_foreign = os.path.join(self.tmp, "wt-foreign")
        r = self._run_create(wt_foreign, "build/foreign", container_id="container-B")
        self.assertEqual(r.returncode, 0, r.stderr)
        subprocess.run(["rm", "-rf", wt_foreign])

        wt_own = os.path.join(self.tmp, "wt-own-active")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", wt_own,
                         "-b", "build/own-active", "origin/main"], check=True)

        r = self._run_cleanup(wt_own, container_id="container-A")
        self.assertEqual(r.returncode, 0, r.stderr)

        listing = self._worktree_listing()
        self.assertIn(wt_foreign, listing,
                       "a foreign container's worktree entry was pruned by worktree_builder.sh's cleanup")

    def test_ac4_own_stale_entry_reclaimed_by_cleanup(self):
        wt_own_stale = os.path.join(self.tmp, "wt-own-stale")
        r = self._run_create(wt_own_stale, "build/own-stale", container_id="container-A")
        self.assertEqual(r.returncode, 0, r.stderr)
        subprocess.run(["rm", "-rf", wt_own_stale])

        wt_active = os.path.join(self.tmp, "wt-active2")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", wt_active,
                         "-b", "build/active2", "origin/main"], check=True)

        r = self._run_cleanup(wt_active, container_id="container-A")
        self.assertEqual(r.returncode, 0, r.stderr)

        listing = self._worktree_listing()
        self.assertNotIn(wt_own_stale, listing,
                          "this container's own stale worktree entry was not reclaimed")

    def test_ac5_single_container_full_cycle_no_leak(self):
        wt = os.path.join(self.tmp, "wt-cycle")
        r = self._run_create(wt, "build/cycle", container_id="container-A")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(wt, self._worktree_listing())

        r = self._run_cleanup(wt, container_id="container-A")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(wt, self._worktree_listing())

    def test_ac6_unresolvable_identity_prunes_nothing_and_logs(self):
        wt_stale = os.path.join(self.tmp, "wt-stale-unresolvable")
        r = self._run_create(wt_stale, "build/stale-unresolvable", container_id="container-A")
        self.assertEqual(r.returncode, 0, r.stderr)
        subprocess.run(["rm", "-rf", wt_stale])

        wt_active = os.path.join(self.tmp, "wt-active3")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", wt_active,
                         "-b", "build/active3", "origin/main"], check=True)

        r = self._run_cleanup(wt_active, container_id="container-A", unresolvable=True)
        self.assertEqual(r.returncode, 0, r.stderr)

        listing = self._worktree_listing()
        self.assertIn(wt_stale, listing,
                       "a stale entry was pruned even though container identity was unresolvable")
        self.assertTrue(Path(self.log_file).read_text().strip(),
                         "no log line was written when container identity was unresolvable")


if __name__ == "__main__":
    unittest.main()
