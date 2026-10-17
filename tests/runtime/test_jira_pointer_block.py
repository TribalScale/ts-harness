from __future__ import annotations

import unittest

from ts_harness.jira.pointer_block import (
    DEFAULT_BASE_BRANCH,
    format_pointer_block,
    parse_pointer_block,
    resolve_pr_base_branch,
)


class PointerBlockTests(unittest.TestCase):
    def test_round_trip_splits_repo_and_branch(self):
        text = format_pointer_block(
            work_order_id="WO-003",
            repo_id="org/checkout-service",
            base_branch="dev",
            program_id="prog-1",
            library_url="https://drive.google.com/folder",
            manifest_url="https://docs.google.com/manifest",
        )
        parsed = parse_pointer_block(text)
        self.assertEqual(parsed.work_order_id, "WO-003")
        self.assertEqual(parsed.repo_id, "org/checkout-service")
        self.assertEqual(parsed.base_branch, "dev")
        self.assertEqual(parsed.program_id, "prog-1")
        self.assertEqual(parsed.library_url, "https://drive.google.com/folder")
        self.assertEqual(parsed.manifest_url, "https://docs.google.com/manifest")

    def test_parse_accepts_missing_drive_fields(self):
        parsed = parse_pointer_block("WO: WO-001\nRepo: org/svc@dev\nProgram: p1\n")
        self.assertEqual(parsed.work_order_id, "WO-001")
        self.assertIsNone(parsed.library_url)
        self.assertIsNone(parsed.manifest_url)
        self.assertTrue(parsed.has_spec_pointers)

    def test_engineer_branch_wins(self):
        self.assertEqual(
            resolve_pr_base_branch(
                engineer_branch="release",
                pointer_branch="dev",
                allowlist_default_branch="main",
            ),
            "release",
        )

    def test_pointer_branch_wins_over_allowlist(self):
        self.assertEqual(
            resolve_pr_base_branch(pointer_branch="dev", allowlist_default_branch="main"),
            "dev",
        )

    def test_allowlist_then_runtime_match(self):
        self.assertEqual(
            resolve_pr_base_branch(allowlist_default_branch="develop"),
            "develop",
        )
        self.assertEqual(
            resolve_pr_base_branch(
                runtime_base_branch="hotfix",
                runtime_repo_matches=True,
            ),
            "hotfix",
        )
        self.assertEqual(
            resolve_pr_base_branch(
                runtime_base_branch="hotfix",
                runtime_repo_matches=False,
            ),
            DEFAULT_BASE_BRANCH,
        )

    def test_omitted_branch_defaults_to_dev(self):
        self.assertEqual(resolve_pr_base_branch(), "dev")

    def test_main_only_when_explicit(self):
        self.assertEqual(resolve_pr_base_branch(pointer_branch="main"), "main")
        self.assertNotEqual(resolve_pr_base_branch(), "main")


if __name__ == "__main__":
    unittest.main()
