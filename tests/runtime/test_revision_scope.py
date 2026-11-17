"""Revision diffs stay inside the files a blocking finding named."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation.revision_scope import (
    allowed_revision_paths,
    revision_files_from_findings,
    unexpected_revision_paths,
)


class RevisionScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pytest_bump_is_outside_a_pricing_finding(self):
        allowed = allowed_revision_paths(["app/pricing_client.py"], self.repo_dir)
        extra = unexpected_revision_paths(
            ["app/pricing_client.py", "requirements-dev.txt"], allowed
        )
        self.assertEqual(extra, ["requirements-dev.txt"])

    def test_existing_lockfile_of_a_cited_manifest_is_allowed(self):
        (self.repo_dir / "poetry.lock").write_text("")
        allowed = allowed_revision_paths(["pyproject.toml"], self.repo_dir)
        self.assertEqual(
            unexpected_revision_paths(["pyproject.toml", "poetry.lock"], allowed),
            [],
        )

    def test_a_new_lockfile_is_not_allowed(self):
        allowed = allowed_revision_paths(["requirements.txt"], self.repo_dir)
        self.assertEqual(
            unexpected_revision_paths(["requirements.txt", "requirements.lock"], allowed),
            ["requirements.lock"],
        )

    def test_pipeline_files_are_allowed(self):
        allowed = allowed_revision_paths(["app/pricing_client.py"], self.repo_dir)
        self.assertEqual(unexpected_revision_paths([".opencode/opencode.json"], allowed), [])

    def test_tests_and_step_definitions_are_allowed(self):
        allowed = allowed_revision_paths(["src/PortfolioDashboard.jsx"], self.repo_dir)
        changed = [
            "src/PortfolioDashboard.jsx",
            "src/PortfolioDashboard.test.jsx",
            "src/__tests__/storage.js",
            "tests/test_pricing.py",
            "app/pricing_test.go",
            "features/portfolio.feature",
            "features/step_definitions/portfolio.steps.js",
        ]
        self.assertEqual(unexpected_revision_paths(changed, allowed), [])

    def test_app_source_outside_the_finding_is_still_unexpected(self):
        allowed = allowed_revision_paths(["src/PortfolioDashboard.jsx"], self.repo_dir)
        self.assertEqual(
            unexpected_revision_paths(["src/App.jsx", "src/contest.js"], allowed),
            ["src/App.jsx", "src/contest.js"],
        )

    def test_empty_citation_list_does_not_constrain(self):
        self.assertEqual(unexpected_revision_paths(["requirements-dev.txt"], set()), [])

    def test_revision_files_come_from_blocking_findings(self):
        files = revision_files_from_findings(
            [
                {"file": "app/pricing_client.py", "severity": "major", "category": "correctness"},
                {"file": "README.md", "severity": "nit", "category": "maintainability"},
                {"severity": "blocker", "category": "security", "message": "no file"},
            ]
        )
        self.assertEqual(files, ["app/pricing_client.py"])
