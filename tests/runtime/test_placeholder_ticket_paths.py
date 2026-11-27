"""A `<JIRA-KEY>` placeholder in a work order's file list matches the file the agent created."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation.verification import untouched_required_files
from ts_harness.workflows.code_review import declared_paths, required_paths

BODY = """# WO-2 Draft badge

## Files
- `features/<JIRA-KEY>-draft-badge.feature` (new)
- `src/components/Badge.tsx` (change)
- `src/**/*.test.tsx` (change as needed)
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class PlaceholderPathTests(unittest.TestCase):
    def test_placeholders_become_globs_and_stay_required(self):
        self.assertEqual(required_paths(BODY), ["features/*-draft-badge.feature", "src/components/Badge.tsx"])
        self.assertIn("features/*-draft-badge.feature", declared_paths(BODY))
        self.assertIn("src/**/*.test.tsx", declared_paths(BODY))
        self.assertEqual(required_paths("## Files\n- `features/{TICKET}-x.feature` (new)\n"), ["features/*-x.feature"])

    def _repo(self, tmp: Path) -> Path:
        repo = tmp / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "dev")
        (repo / "src/components").mkdir(parents=True)
        (repo / "src/components/Badge.tsx").write_text("export {}\n")
        _git(repo, "add", ".")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "base")
        return repo

    def test_the_file_the_agent_named_satisfies_the_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            (repo / "features").mkdir()
            (repo / "features/PROJ-49-draft-badge.feature").write_text("Feature: badge\n")
            (repo / "src/components/Badge.tsx").write_text("export const x = 1\n")
            self.assertEqual(untouched_required_files(repo, BODY, "dev"), [])

    def test_a_missing_file_is_still_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            (repo / "src/components/Badge.tsx").write_text("export const x = 1\n")
            self.assertEqual(untouched_required_files(repo, BODY, "dev"), ["features/*-draft-badge.feature"])


if __name__ == "__main__":
    unittest.main()
