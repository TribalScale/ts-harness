"""The work order's Files list as an edit scope and as a checklist of files the change must touch."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from ts_harness.implementation.verification import untouched_required_files
from ts_harness.workflows.code_review import declared_paths, required_paths

MIXED = """## Files
- `src/api/openapi.json`: new, committed copy of the spec
- `src/api/health.d.ts`, `hub.d.ts`, `moves.d.ts`: delete
- `src/features/schedule/WeekView.tsx`: read carry-over content from `lane`
- Day view and Machines view components: change them to the real response shapes
- `.ts-harness.yml`: keep `contract.openapi`

## E2E Acceptance Tests
"""

PATHS_ONLY = """## Files
- `src/a.ts`: change
- `src/b/`
- docs/x.md

## Next
"""


class ScopeTests(unittest.TestCase):
    def test_a_list_with_a_prose_entry_declares_nothing(self):
        self.assertEqual(declared_paths(MIXED), [])

    def test_a_list_of_paths_is_still_a_scope(self):
        self.assertEqual(declared_paths(PATHS_ONLY), ["src/a.ts", "src/b/", "docs/x.md"])

    def test_bare_names_take_the_folder_of_the_path_before_them(self):
        self.assertIn("src/api/hub.d.ts", required_paths(MIXED))
        self.assertIn("src/api/moves.d.ts", required_paths(MIXED))

    def test_required_files_skip_ones_to_keep_and_folders(self):
        required = required_paths(MIXED)
        self.assertNotIn(".ts-harness.yml", required)
        self.assertIn("src/features/schedule/WeekView.tsx", required)
        self.assertEqual(required_paths(PATHS_ONLY), ["src/a.ts", "docs/x.md"])


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


class UntouchedTests(unittest.TestCase):
    def test_files_the_change_left_alone_are_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "t")
            (repo / "src/api").mkdir(parents=True)
            (repo / "src/features/schedule").mkdir(parents=True)
            for name in ("health.d.ts", "hub.d.ts", "moves.d.ts"):
                (repo / "src/api" / name).write_text("x\n")
            (repo / "src/features/schedule/WeekView.tsx").write_text("old\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "base")
            _git(repo, "branch", "base")
            # The change: deletes one file, adds the spec, leaves the rest.
            (repo / "src/api/health.d.ts").unlink()
            (repo / "src/api/openapi.json").write_text("{}\n")

            untouched = untouched_required_files(repo, MIXED, "base")

        self.assertEqual(untouched, ["src/api/hub.d.ts", "src/api/moves.d.ts", "src/features/schedule/WeekView.tsx"])


if __name__ == "__main__":
    unittest.main()


class BeyondTheListTests(unittest.TestCase):
    def test_the_pr_body_lists_files_beyond_the_work_order(self):
        from ts_harness.implementation.backends.base import CodingRequest
        from ts_harness.implementation.pr_description import pr_body

        request = CodingRequest(
            repo_url="https://github.com/org/web", base_branch="dev", work_order_id="WO-1",
            work_order_body="# WO-1\n\n## Summary\nDo X.\n", workspace_dir=Path("/tmp/ws"), provider="anthropic",
        )
        body = pr_body(request, changed_files=["src/a.ts", "src/b.ts"], out_of_scope_files=["src/b.ts"])
        self.assertIn("## Beyond the work order's files", body)
        self.assertIn("- `src/b.ts`", body)
