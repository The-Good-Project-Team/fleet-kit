"""Container-aware worktree pruning tests (gh#684).

A blanket `git worktree prune` deletes ANY registered entry whose working-tree path this
process can't see. $REPO/.git/worktrees is a shared bind mount, but a run worktree's path
lives under ${TMPDIR:-/tmp}, which is per-container -- so during a rolling cutover (#626) the
new container's blanket prune sees the OLD container's still-live worktree as "gone" and
deletes its registration out from under the still-running pass (observed live 2026-09-08 03:06Z,
minion-item4863 lost three minutes to `git worktree repair` + a re-clone before it could start
building).

worktree_prune.sh fixes this by stamping every worktree with the creating container's own
identity and pruning only entries stamped with the CURRENT container's identity. These tests
exercise the real functions against a real git repo -- no mocking of git itself, only of
container identity (via $HOSTNAME, which worktree_container_id() reads).

Run: python3 scripts/test_worktree_prune.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
PRUNE_SCRIPT = KIT / "scripts" / "worktree_prune.sh"


def run_bash(script: str, env: dict, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                           text=True, timeout=timeout)


class WorktreePruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-worktree-prune-test-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.name", "t"], check=True)
        (Path(self.repo) / "README").write_text("x")
        subprocess.run(["git", "-C", self.repo, "add", "README"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-q", "-m", "init"], check=True)

    def _env(self, container_id: str | None) -> dict:
        env = dict(os.environ)
        if container_id is None:
            env.pop("HOSTNAME", None)
        else:
            env["HOSTNAME"] = container_id
        return env

    def _add_stamped_worktree(self, wt_path: str, branch: str, container_id: str):
        """Register a worktree the way create_run_worktree does: `add` then stamp, both while
        HOSTNAME reports the creating container's identity."""
        script = f"""
            set -eu
            . "{PRUNE_SCRIPT}"
            git -C "{self.repo}" worktree add "{wt_path}" -b "{branch}" HEAD >/dev/null
            worktree_stamp_container_id "{self.repo}" "{wt_path}"
        """
        r = run_bash(script, env=self._env(container_id))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(
            os.path.isfile(os.path.join(self.repo, ".git", "worktrees",
                                         os.path.basename(wt_path), "container-id")),
            "stamp file was not written",
        )

    def _prune(self, container_id: str | None):
        script = f'set -eu; . "{PRUNE_SCRIPT}"; worktree_prune_own_container "{self.repo}"'
        r = run_bash(script, env=self._env(container_id))
        self.assertEqual(r.returncode, 0, r.stderr)

    def _worktree_listing(self) -> str:
        r = subprocess.run(["git", "-C", self.repo, "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, check=True)
        return r.stdout

    def test_ac1_foreign_container_entry_survives_unresolvable_path(self):
        """AC1: a worktree stamped by container B, whose path this process (container A)
        cannot resolve, survives container A's prune untouched -- no repair needed."""
        wt = os.path.join(self.tmp, "wt-foreign")
        self._add_stamped_worktree(wt, "member/foreign", container_id="container-B")
        subprocess.run(["rm", "-rf", wt])  # simulate: path doesn't exist from this container

        self._prune(container_id="container-A")

        listing = self._worktree_listing()
        self.assertIn(wt, listing, "a foreign container's live-elsewhere worktree was pruned")

    def test_ac2_own_stale_entry_is_reclaimed(self):
        """AC2: a worktree this container itself stamped, whose path is now gone, IS reclaimed
        -- stale local entries still get cleaned up."""
        wt = os.path.join(self.tmp, "wt-own")
        self._add_stamped_worktree(wt, "member/own", container_id="container-A")
        subprocess.run(["rm", "-rf", wt])

        self._prune(container_id="container-A")

        listing = self._worktree_listing()
        self.assertNotIn(wt, listing, "this container's own stale entry was not reclaimed")

    def test_ac3_cleanup_prunes_only_its_own_entries(self):
        """AC3: with a mix of stale entries from two containers, one container's prune (as run
        from the exit trap) removes only its own and leaves the other's registered."""
        wt_a = os.path.join(self.tmp, "wt-a")
        wt_b = os.path.join(self.tmp, "wt-b")
        self._add_stamped_worktree(wt_a, "member/a", container_id="container-A")
        self._add_stamped_worktree(wt_b, "member/b", container_id="container-B")
        subprocess.run(["rm", "-rf", wt_a])
        subprocess.run(["rm", "-rf", wt_b])

        self._prune(container_id="container-A")

        listing = self._worktree_listing()
        self.assertNotIn(wt_a, listing, "own entry should have been pruned")
        self.assertIn(wt_b, listing, "sibling container's entry should have survived")

    def test_ac4_single_container_lifecycle_unaffected(self):
        """AC4: create, use, and cleanly remove a worktree in the single-container case --
        no foreign entries, no observable change from a plain create/remove/prune."""
        wt = os.path.join(self.tmp, "wt-solo")
        self._add_stamped_worktree(wt, "member/solo", container_id="container-A")
        self.assertTrue(os.path.isdir(wt))

        script = f"""
            set -eu
            . "{PRUNE_SCRIPT}"
            git -C "{self.repo}" worktree remove --force "{wt}"
            worktree_prune_own_container "{self.repo}"
        """
        r = run_bash(script, env=self._env("container-A"))
        self.assertEqual(r.returncode, 0, r.stderr)

        listing = self._worktree_listing()
        self.assertNotIn(wt, listing)

    def test_unresolvable_identity_prunes_nothing(self):
        """A container that cannot determine its own identity must not guess -- it should
        prune nothing rather than risk deleting a live sibling's entry."""
        wt = os.path.join(self.tmp, "wt-cidless")
        self._add_stamped_worktree(wt, "member/cidless", container_id="container-A")
        subprocess.run(["rm", "-rf", wt])

        script = f"""
            set -eu
            . "{PRUNE_SCRIPT}"
            worktree_container_id() {{ printf '%s' ""; }}
            worktree_prune_own_container "{self.repo}"
        """
        r = run_bash(script, env=self._env(None))
        self.assertEqual(r.returncode, 0, r.stderr)

        listing = self._worktree_listing()
        self.assertIn(wt, listing, "prune ran despite being unable to resolve its own identity")


if __name__ == "__main__":
    unittest.main()
