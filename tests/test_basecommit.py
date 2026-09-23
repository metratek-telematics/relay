"""A new task branch starts from the remote branch when the checkout is behind it.

Real git repositories in a temporary folder: a bare "remote", a checkout that is behind it, and the cases
where starting from the remote would be wrong (diverged, dirty, no upstream, switched off).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("RELAY_DATA_DIR", tempfile.mkdtemp(prefix="relay-base-test-"))
sys.path.insert(0, str(ROOT))

from orchestrator import gitops  # noqa: E402


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


class FakeRunner:
    def __init__(self):
        self.events = []

    def timeline(self, role, title, detail=""):
        self.events.append((role, title, detail))


def make_repo(tmp: Path):
    """A bare remote with two commits and a checkout parked on the first one."""
    remote, work = tmp / "remote.git", tmp / "work"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp)
    git("clone", str(remote), str(work), cwd=tmp)
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "t", cwd=work)
    (work / "a.txt").write_text("one\n")
    git("add", "-A", cwd=work)
    git("commit", "-m", "one", cwd=work)
    git("push", "-u", "origin", "main", cwd=work)
    first = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    (work / "b.txt").write_text("two\n")
    git("add", "-A", cwd=work)
    git("commit", "-m", "two", cwd=work)
    git("push", "origin", "main", cwd=work)
    # Park the checkout on the first commit: behind the remote by one, exactly like a server nobody pulls.
    git("reset", "--hard", first, cwd=work)
    return work


class BaseCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="relay-base-"))
        self.repo = make_repo(self.tmp)
        self.runner = FakeRunner()

    def test_behind_checkout_branches_from_the_remote(self):
        base = gitops.branch_base(self.repo, self.runner, {})
        self.assertEqual(base, "origin/main")
        self.assertTrue(any("Branching from origin/main" in t for _, t, _ in self.runner.events))
        # And the branch really carries the newer file.
        git("worktree", "add", "-b", "task", str(self.tmp / "wt"), base, cwd=self.repo)
        self.assertTrue((self.tmp / "wt" / "b.txt").exists())

    def test_uncommitted_work_keeps_the_local_checkout(self):
        (self.repo / "a.txt").write_text("edited\n")
        self.assertEqual(gitops.branch_base(self.repo, self.runner, {}), "HEAD")
        self.assertTrue(any("Starting from the local checkout" in t for _, t, _ in self.runner.events))

    def test_diverged_checkout_keeps_the_local_checkout(self):
        (self.repo / "own.txt").write_text("mine\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-m", "mine", cwd=self.repo)
        self.assertEqual(gitops.branch_base(self.repo, self.runner, {}), "HEAD")

    def test_up_to_date_checkout_uses_head(self):
        git("merge", "--ff-only", "origin/main", cwd=self.repo)
        self.assertEqual(gitops.branch_base(self.repo, self.runner, {}), "HEAD")

    def test_no_upstream_uses_head(self):
        git("remote", "remove", "origin", cwd=self.repo)
        self.assertEqual(gitops.branch_base(self.repo, self.runner, {}), "HEAD")

    def test_setting_off_uses_head(self):
        self.assertEqual(gitops.branch_base(self.repo, self.runner, {"branch_from_upstream": False}), "HEAD")
        self.assertEqual(self.runner.events, [])


if __name__ == "__main__":
    unittest.main()
