"""A run that changed nothing must not ask GitHub for a pull request.

The real failure this comes from: a three-hour device-test run changed no code (correctly), and delivery
still pushed the empty branch and called `gh pr create`, which answered "No commits between main and
feat/…". The same run also reported "no base branch is configured, so there was nothing to check the merge
against", because nothing had been set — on most repositories, which silently disabled the merge check.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("RELAY_DATA_DIR", tempfile.mkdtemp(prefix="relay-nothing-test-"))
sys.path.insert(0, str(ROOT))

from orchestrator import predelivery  # noqa: E402


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def repo_with_branch(tmp: Path, commit_on_branch: bool):
    """A remote, a checkout, and a task branch that either carries a commit or does not."""
    remote, work = tmp / "remote.git", tmp / "work"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp)
    git("clone", str(remote), str(work), cwd=tmp)
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "t", cwd=work)
    (work / "a.txt").write_text("one\n")
    git("add", "-A", cwd=work)
    git("commit", "-m", "one", cwd=work)
    git("push", "-u", "origin", "main", cwd=work)
    base = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    git("switch", "-c", "feat/task", cwd=work)
    if commit_on_branch:
        (work / "b.txt").write_text("two\n")
        git("add", "-A", cwd=work)
        git("commit", "-m", "two", cwd=work)
    return work, base


class CarriesWorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="relay-nothing-"))

    def test_a_branch_with_no_commits_is_empty(self):
        wt, base = repo_with_branch(self.tmp, commit_on_branch=False)
        out = predelivery.carries_work(wt, "origin/main", base)
        self.assertTrue(out["empty"])
        self.assertEqual(out["ahead"], 0)
        self.assertTrue(out["checked"])

    def test_a_branch_with_a_commit_is_not_empty(self):
        wt, base = repo_with_branch(self.tmp, commit_on_branch=True)
        out = predelivery.carries_work(wt, "origin/main", base)
        self.assertFalse(out["empty"])
        self.assertEqual(out["ahead"], 1)

    def test_work_left_uncommitted_is_named(self):
        wt, base = repo_with_branch(self.tmp, commit_on_branch=False)
        (wt / "unsaved.txt").write_text("edited but never committed\n")
        out = predelivery.carries_work(wt, "origin/main", base)
        self.assertTrue(out["empty"])
        self.assertTrue(any("unsaved.txt" in x for x in out["leftover"]))

    def test_it_falls_back_to_the_recorded_base_commit_without_a_ref(self):
        wt, base = repo_with_branch(self.tmp, commit_on_branch=False)
        out = predelivery.carries_work(wt, "", base)
        self.assertTrue(out["empty"])
        self.assertTrue(out["checked"])
        self.assertEqual(out["error"], "")

    def test_nothing_known_stays_unknown_rather_than_claiming_empty(self):
        wt, _ = repo_with_branch(self.tmp, commit_on_branch=False)
        out = predelivery.carries_work(wt, "", "")
        self.assertIsNone(out["empty"])
        self.assertFalse(out["checked"])


class BaseRefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="relay-baseref-"))

    def test_configured_base_wins(self):
        self.assertEqual(predelivery.base_ref({"github_pr_base": "develop"}, {}), "origin/develop")
        self.assertEqual(predelivery.base_ref({}, {"github_base": "release/1"}), "origin/release/1")

    def test_nothing_configured_falls_back_to_the_repository_default_branch(self):
        wt, _ = repo_with_branch(self.tmp, commit_on_branch=True)
        self.assertEqual(predelivery.base_ref({}, {}, wt=wt), "origin/main")

    def test_without_a_worktree_it_stays_empty(self):
        self.assertEqual(predelivery.base_ref({}, {}), "")


if __name__ == "__main__":
    unittest.main()
