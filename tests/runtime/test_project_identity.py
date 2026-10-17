from __future__ import annotations

import unittest

from ts_harness.workflows.project_identity import infer_project_id, normalize_project_id


class ProjectIdentityTests(unittest.TestCase):
    def test_jira_wins_over_repo(self):
        project_id, source = infer_project_id(
            {
                "jira_project_key": "tfs",
                "repo_url": "https://github.com/acme/payments.git",
            }
        )
        self.assertEqual(project_id, "TFS")
        self.assertEqual(source, "inferred_jira")

    def test_repo_used_when_no_jira_key(self):
        project_id, source = infer_project_id(
            {"repo_url": "https://github.com/acme/payments.git"}
        )
        self.assertEqual(project_id, "acme/payments")
        self.assertEqual(source, "inferred_repo")

    def test_slack_looking_keys_are_not_inferred(self):
        project_id, source = infer_project_id(
            {"binding_key": "slack:C123:thread", "channel": "C123"}
        )
        self.assertIsNone(project_id)
        self.assertIsNone(source)

    def test_empty_artifacts_are_unset(self):
        self.assertEqual(infer_project_id({}), (None, None))
        self.assertEqual(infer_project_id(None), (None, None))

    def test_normalize_uppercases_jira_keys(self):
        self.assertEqual(normalize_project_id("tfs"), "TFS")
        self.assertEqual(normalize_project_id("acme/payments"), "acme/payments")
        with self.assertRaises(ValueError):
            normalize_project_id("  ")


if __name__ == "__main__":
    unittest.main()
