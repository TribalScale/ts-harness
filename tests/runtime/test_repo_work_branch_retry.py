"""Retry-safe clone/branch/push behavior for coding-backend work orders."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation.repo import (
    claim_workspace,
    create_work_branch,
    ensure_repo_clone,
    push_branch,
)


def _run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=True,
    )


def _git_init_with_commit(repo: Path, *, branch: str = "dev") -> None:
    _run(["git", "init", "-b", branch], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "test"], cwd=repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-m", "first commit"], cwd=repo)


def _rev_parse(repo: Path, ref: str = "HEAD") -> str:
    return _run(["git", "rev-parse", ref], cwd=repo).stdout.strip()


class CreateWorkBranchRetryTests(unittest.TestCase):
    def test_create_work_branch_preserves_existing_local_commits(self) -> None:
        """Retry must not reset wo/* with checkout -B from base."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            _git_init_with_commit(repo, branch="dev")
            base = _rev_parse(repo)

            _run(["git", "checkout", "-b", "wo/wo-001"], cwd=repo)
            (repo / "gameplay.py").write_text("ok\n", encoding="utf-8")
            _run(["git", "add", "gameplay.py"], cwd=repo)
            _run(["git", "commit", "-m", "Implement WO-001"], cwd=repo)
            implement = _rev_parse(repo)

            # Simulate ensure_repo_clone leaving HEAD on base.
            _run(["git", "checkout", "dev"], cwd=repo)
            self.assertEqual(_rev_parse(repo), base)

            create_work_branch(
                repo,
                branch="wo/wo-001",
                base_branch="dev",
            )
            self.assertEqual(_rev_parse(repo), implement)
            self.assertTrue((repo / "gameplay.py").exists())

    def test_create_work_branch_creates_from_base_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            _git_init_with_commit(repo, branch="dev")
            base = _rev_parse(repo)

            create_work_branch(repo, branch="wo/wo-002", base_branch="dev")
            self.assertEqual(_run(["git", "branch", "--show-current"], cwd=repo).stdout.strip(), "wo/wo-002")
            self.assertEqual(_rev_parse(repo), base)

    def test_create_work_branch_recreates_when_unrelated_to_origin_base(self) -> None:
        """Stale shallow tip after base force-push must not keep unrelated wo/*."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            _run(["git", "init", "--bare", "-b", "dev", str(remote)])

            seed = root / "seed"
            seed.mkdir()
            _git_init_with_commit(seed, branch="dev")
            old_tip = _rev_parse(seed)
            _run(["git", "remote", "add", "origin", str(remote)], cwd=seed)
            _run(["git", "push", "-u", "origin", "dev"], cwd=seed)

            workspace = root / "workspace"
            repo = ensure_repo_clone(
                repo_url=str(remote),
                workspace_dir=workspace,
                base_branch="dev",
            )
            create_work_branch(repo, branch="wo/wo-002", base_branch="dev")
            (repo / "stale.txt").write_text("stale\n", encoding="utf-8")
            _run(["git", "add", "stale.txt"], cwd=repo)
            _run(["git", "commit", "-m", "Implement WO-002 on stale base"], cwd=repo)
            self.assertEqual(_rev_parse(repo, "HEAD^"), old_tip)

            # Orphan-reset remote dev (unrelated history), as in demo recovery.
            orphan = root / "orphan"
            orphan.mkdir()
            _run(["git", "init", "-b", "dev"], cwd=orphan)
            _run(["git", "config", "user.email", "test@example.com"], cwd=orphan)
            _run(["git", "config", "user.name", "test"], cwd=orphan)
            (orphan / "README.md").write_text("reset\n", encoding="utf-8")
            _run(["git", "add", "README.md"], cwd=orphan)
            _run(["git", "commit", "-m", "Reset demo repo"], cwd=orphan)
            new_base = _rev_parse(orphan)
            _run(["git", "remote", "add", "origin", str(remote)], cwd=orphan)
            _run(["git", "push", "--force", "origin", "dev"], cwd=orphan)

            ensure_repo_clone(
                repo_url=str(remote),
                workspace_dir=workspace,
                base_branch="dev",
            )
            create_work_branch(repo, branch="wo/wo-002", base_branch="dev")
            self.assertEqual(_run(["git", "branch", "--show-current"], cwd=repo).stdout.strip(), "wo/wo-002")
            self.assertEqual(_rev_parse(repo), new_base)
            self.assertFalse((repo / "stale.txt").exists())


class EnsureRepoCloneRetryTests(unittest.TestCase):
    def test_ensure_repo_clone_does_not_move_existing_work_branch_tip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            _run(["git", "init", "--bare", "-b", "dev", str(remote)])

            seed = root / "seed"
            seed.mkdir()
            _git_init_with_commit(seed, branch="dev")
            _run(["git", "remote", "add", "origin", str(remote)], cwd=seed)
            _run(["git", "push", "-u", "origin", "dev"], cwd=seed)

            workspace = root / "workspace"
            repo = ensure_repo_clone(
                repo_url=str(remote),
                workspace_dir=workspace,
                base_branch="dev",
            )
            create_work_branch(repo, branch="wo/wo-001", base_branch="dev")
            (repo / "impl.txt").write_text("v1\n", encoding="utf-8")
            _run(["git", "add", "impl.txt"], cwd=repo)
            _run(["git", "commit", "-m", "Implement WO-001"], cwd=repo)
            implement = _rev_parse(repo)

            # Second prepare (retry) must keep the local implement commit reachable
            # after create_work_branch.
            ensure_repo_clone(
                repo_url=str(remote),
                workspace_dir=workspace,
                base_branch="dev",
            )
            create_work_branch(repo, branch="wo/wo-001", base_branch="dev")
            self.assertEqual(_rev_parse(repo), implement)

    def test_revision_in_a_fresh_clone_starts_from_the_pr_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            _run(["git", "init", "--bare", "-b", "dev", str(remote)])
            seed = root / "seed"
            seed.mkdir()
            _git_init_with_commit(seed, branch="dev")
            _run(["git", "remote", "add", "origin", str(remote)], cwd=seed)
            _run(["git", "push", "-u", "origin", "dev"], cwd=seed)
            _run(["git", "checkout", "-b", "wo/wo-001"], cwd=seed)
            (seed / "impl.txt").write_text("v1\n", encoding="utf-8")
            _run(["git", "add", "impl.txt"], cwd=seed)
            _run(["git", "commit", "-m", "Implement WO-001"], cwd=seed)
            _run(["git", "push", "-u", "origin", "wo/wo-001"], cwd=seed)
            pr_tip = _rev_parse(seed)

            repo = ensure_repo_clone(repo_url=str(remote), workspace_dir=root / "fresh", base_branch="dev")
            create_work_branch(repo, branch="wo/wo-001", base_branch="dev", from_remote=True)
            self.assertEqual(_run(["git", "branch", "--show-current"], cwd=repo).stdout.strip(), "wo/wo-001")
            self.assertEqual(_rev_parse(repo), pr_tip)


class PushBranchRetryTests(unittest.TestCase):
    def test_push_branch_force_with_lease_when_non_fast_forward(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            _run(["git", "init", "--bare", "-b", "dev", str(remote)])

            # Publisher A: create remote wo/wo-001
            a = root / "a"
            a.mkdir()
            _git_init_with_commit(a, branch="dev")
            _run(["git", "remote", "add", "origin", str(remote)], cwd=a)
            _run(["git", "push", "-u", "origin", "dev"], cwd=a)
            _run(["git", "checkout", "-b", "wo/wo-001"], cwd=a)
            (a / "old.txt").write_text("old\n", encoding="utf-8")
            _run(["git", "add", "old.txt"], cwd=a)
            _run(["git", "commit", "-m", "Implement WO-001"], cwd=a)
            _run(["git", "push", "-u", "origin", "wo/wo-001"], cwd=a)

            # Publisher B: divergent rewrite from same base
            b = root / "b"
            _run(["git", "clone", str(remote), str(b)])
            _run(["git", "config", "user.email", "test@example.com"], cwd=b)
            _run(["git", "config", "user.name", "test"], cwd=b)
            _run(["git", "checkout", "dev"], cwd=b)
            _run(["git", "checkout", "-b", "wo/wo-001"], cwd=b)
            (b / "new.txt").write_text("new\n", encoding="utf-8")
            _run(["git", "add", "new.txt"], cwd=b)
            _run(["git", "commit", "-m", "Implement WO-001"], cwd=b)
            new_tip = _rev_parse(b)

            push_branch(b, branch="wo/wo-001")
            remote_tip = _run(
                ["git", "rev-parse", "wo/wo-001"],
                cwd=remote,
            ).stdout.strip()
            self.assertEqual(remote_tip, new_tip)


class ClaimWorkspaceTests(unittest.TestCase):
    def _workspace_with_clone(self, root: Path, owner: str = "") -> Path:
        workspace = root / "WO-001"
        if owner:
            self.assertFalse(claim_workspace(workspace, owner))
        (workspace / "repo" / ".git").mkdir(parents=True)
        (workspace / "repo" / "old.txt").write_text("old\n", encoding="utf-8")
        return workspace

    def test_same_delivery_keeps_its_clone_for_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace_with_clone(Path(tmp), owner="wf-1")
            self.assertFalse(claim_workspace(workspace, "wf-1"))
            self.assertTrue((workspace / "repo" / "old.txt").exists())

    def test_other_delivery_drops_the_old_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace_with_clone(Path(tmp), owner="wf-1")
            self.assertTrue(claim_workspace(workspace, "wf-2"))
            self.assertFalse((workspace / "repo").exists())
            self.assertFalse(claim_workspace(workspace, "wf-2"))

    def test_unowned_clone_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace_with_clone(Path(tmp))
            self.assertTrue(claim_workspace(workspace, "wf-1"))
            self.assertFalse((workspace / "repo").exists())

    def test_no_owner_leaves_workspace_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace_with_clone(Path(tmp))
            self.assertFalse(claim_workspace(workspace, ""))
            self.assertTrue((workspace / "repo" / "old.txt").exists())


if __name__ == "__main__":
    unittest.main()
