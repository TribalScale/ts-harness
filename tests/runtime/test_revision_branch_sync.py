"""A revision of an open PR builds on the PR as it is on GitHub, in a shallow single-branch clone."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation.repo import create_work_branch, push_branch

BRANCH = "wo/wo-004-abc"


def git(cwd, *args):
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout.strip()


def commit(cwd, name, text):
    (Path(cwd) / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", f"add {name}")


class RevisionBranchSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(self.origin)], check=True)
        seed = self.tmp / "seed"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(seed)], check=True, capture_output=True)
        git(seed, "checkout", "-qb", "dev")
        commit(seed, "base.txt", "base")
        git(seed, "push", "-q", "origin", "dev")
        # The harness clone: shallow and single-branch, as ensure_repo_clone makes it.
        self.clone = self.tmp / "repo"
        subprocess.run(["git", "clone", "-q", "--depth=1", "--branch", "dev", f"file://{self.origin}", str(self.clone)],
                       check=True, capture_output=True)
        git(self.clone, "checkout", "-qb", BRANCH)
        commit(self.clone, "feature.txt", "v1")
        git(self.clone, "push", "-q", "origin", BRANCH)
        # GitHub moves the PR: a refresh merge commit lands on the branch.
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", "--branch", BRANCH, str(self.origin), str(other)], check=True, capture_output=True)
        commit(other, "refresh.txt", "merged base")
        git(other, "push", "-q", "origin", BRANCH)
        git(self.clone, "checkout", "-q", "dev")

    def test_the_revision_starts_from_the_pr_on_github_and_pushes_without_force(self):
        create_work_branch(self.clone, branch=BRANCH, base_branch="dev", from_remote=True)
        self.assertTrue((self.clone / "refresh.txt").exists())
        commit(self.clone, "feature.txt", "v2")
        push_branch(self.clone, branch=BRANCH)
        remote_files = git(self.origin, "ls-tree", "--name-only", BRANCH).split()
        self.assertEqual(sorted(remote_files), ["base.txt", "feature.txt", "refresh.txt"])

    def test_unpushed_local_commits_are_kept_and_merged_with_the_remote(self):
        git(self.clone, "checkout", "-q", BRANCH)
        commit(self.clone, "local.txt", "not pushed yet")
        git(self.clone, "checkout", "-q", "dev")
        create_work_branch(self.clone, branch=BRANCH, base_branch="dev", from_remote=True)
        self.assertTrue((self.clone / "refresh.txt").exists())
        self.assertTrue((self.clone / "local.txt").exists())
        push_branch(self.clone, branch=BRANCH)

    def test_a_rewritten_branch_is_pushed_with_a_fresh_lease(self):
        git(self.clone, "checkout", "-q", BRANCH)
        git(self.clone, "reset", "-q", "--hard", "origin/dev")
        commit(self.clone, "feature.txt", "rewritten")
        push_branch(self.clone, branch=BRANCH)  # used to fail with "stale info"
        self.assertEqual(git(self.origin, "rev-parse", BRANCH), git(self.clone, "rev-parse", "HEAD"))


if __name__ == "__main__":
    unittest.main()
