"""Release workflow permissions match what its steps actually do."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml


RELEASE_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


class ReleaseWorkflowContractTests(unittest.TestCase):
    def setUp(self):
        workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text())
        self.job = workflow["jobs"]["build-and-release"]
        self.steps = {step.get("name"): step for step in self.job["steps"]}

    def test_permissions_are_only_what_the_steps_use(self):
        self.assertEqual(
            self.job["permissions"],
            {"contents": "read", "security-events": "write"},
        )

    def test_image_is_not_pushed_while_packages_permission_is_absent(self):
        build = self.steps["Build agent-runtime image"]
        self.assertFalse(build["with"]["push"])

    def test_sarif_upload_failures_are_not_hidden(self):
        upload = self.steps["Upload Trivy scan results"]
        self.assertNotIn("continue-on-error", upload)
        self.assertIn("hashFiles('trivy-results.sarif')", upload["if"])


if __name__ == "__main__":
    unittest.main()
