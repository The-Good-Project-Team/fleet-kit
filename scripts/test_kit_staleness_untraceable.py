"""fk#1006: an untraceable kit snapshot reports itself instead of going silent.

The regression this locks in: `/fleet-kit/.deploy_sha` read the literal string `unknown` on the
philanthropy instance on 2026-09-14 while the baked snapshot was two days behind main, and
kit_staleness_check.sh exited 0 without a word -- so every member pass that hour read silence
and had no way to tell "current" from "cannot be checked". Staleness is still never guessed.
"""
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "kit_staleness_check.sh"


def run(tmp_path, contents):
    sha_file = tmp_path / ".deploy_sha"
    if contents is not None:
        sha_file.write_text(contents)
    env = {**os.environ, "KIT_STALENESS_DEPLOY_SHA_FILE": str(sha_file),
           "KIT_REPO_SLUG": "The-Good-Project-Team/fleet-kit"}
    p = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, p.stdout


def test_unknown_sha_says_so(tmp_path):
    rc, out = run(tmp_path, "unknown\n")
    assert rc == 0, "advisory only -- never a nonzero exit"
    assert "untraceable" in out
    assert "unknown" in out


def test_missing_file_says_so(tmp_path):
    rc, out = run(tmp_path, None)
    assert rc == 0
    assert "untraceable" in out and "missing" in out


def test_malformed_sha_says_so(tmp_path):
    rc, out = run(tmp_path, "not-a-sha!!\n")
    assert rc == 0
    assert "untraceable" in out


def test_untraceable_never_claims_staleness(tmp_path):
    """It reports what it knows (provenance is gone), never what it cannot know (N commits behind)."""
    _, out = run(tmp_path, "unknown\n")
    assert "behind origin/main" not in out


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
