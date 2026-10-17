from __future__ import annotations

import unittest

from ts_harness.jira.program_config import (
    resolve_program_runtime_config,
    resolved_program_config,
    validate_program_config,
)


class ProgramRuntimeConfigTests(unittest.TestCase):
    def test_empty_repos_valid_and_defaults_merge(self):
        self.assertEqual(validate_program_config({"repo_picker": {"repos": []}}), [])
        merged = resolve_program_runtime_config(
            {"config": {"ticket_picker": {"statuses": ["Ready", "In Progress"]}}},
            {"jira_filing": {"default_issue_type": "Task"}},
        )
        self.assertEqual(merged["ticket_picker"]["statuses"], ["Ready", "In Progress"])
        self.assertEqual(merged["jira_filing"]["default_issue_type"], "Task")
        self.assertEqual(merged["repo_picker"]["repos"], [])

    def test_unknown_status_names_allowed(self):
        errors = validate_program_config(
            {"ticket_picker": {"statuses": ["Ready for Grooming"]}}
        )
        self.assertEqual(errors, [])

    def test_jira_filing_requires_default_issue_type(self):
        errors = validate_program_config({"jira_filing": {"issue_type_by_project": {}}})
        self.assertTrue(any("default_issue_type" in item for item in errors))

    def test_program_overlay_wins_repo_branch(self):
        merged = resolve_program_runtime_config(
            {
                "config": {
                    "repo_picker": {
                        "repos": [{"id": "org/checkout-service", "default_branch": "dev"}]
                    }
                }
            },
            {
                "repo_picker": {
                    "repos": [{"id": "org/other", "default_branch": "main"}]
                }
            },
        )
        self.assertEqual(
            merged["repo_picker"]["repos"],
            [{"id": "org/checkout-service", "default_branch": "dev"}],
        )

    def test_empty_repos_without_mode_inherit_tenant(self):
        tenant = {
            "repo_picker": {
                "repos": [{"id": "org/checkout-service", "default_branch": "dev"}]
            }
        }
        omitted = resolve_program_runtime_config({"config": {}}, tenant)
        empty = resolve_program_runtime_config(
            {"config": {"repo_picker": {"repos": []}}}, tenant
        )
        self.assertEqual(
            omitted["repo_picker"]["repos"],
            [{"id": "org/checkout-service", "default_branch": "dev"}],
        )
        self.assertEqual(
            empty["repo_picker"]["repos"],
            [{"id": "org/checkout-service", "default_branch": "dev"}],
        )

    def test_mode_inherit_ignores_program_repos(self):
        merged = resolve_program_runtime_config(
            {
                "config": {
                    "repo_picker": {
                        "mode": "inherit",
                        "repos": [{"id": "org/other", "default_branch": "main"}],
                    }
                }
            },
            {
                "repo_picker": {
                    "repos": [{"id": "org/checkout-service", "default_branch": "dev"}]
                }
            },
        )
        self.assertEqual(
            merged["repo_picker"]["repos"],
            [{"id": "org/checkout-service", "default_branch": "dev"}],
        )

    def test_mode_custom_empty_repos_fail_closed(self):
        merged = resolve_program_runtime_config(
            {"config": {"repo_picker": {"mode": "custom", "repos": []}}},
            {
                "repo_picker": {
                    "repos": [{"id": "org/checkout-service", "default_branch": "dev"}]
                }
            },
        )
        self.assertEqual(merged["repo_picker"]["repos"], [])

    def test_inherit_empty_tenant_fail_closed(self):
        merged = resolve_program_runtime_config(
            {"config": {"repo_picker": {"mode": "inherit"}}},
            {"repo_picker": {"repos": []}},
        )
        self.assertEqual(merged["repo_picker"]["repos"], [])

    def test_validate_mode_inherit_does_not_require_repos(self):
        self.assertEqual(
            validate_program_config({"repo_picker": {"mode": "inherit"}}), []
        )

    def test_validate_mode_custom_requires_repos_array(self):
        errors = validate_program_config({"repo_picker": {"mode": "custom"}})
        self.assertTrue(any("repos" in item for item in errors))

    def test_validate_unknown_mode_errors(self):
        errors = validate_program_config({"repo_picker": {"mode": "maybe", "repos": []}})
        self.assertTrue(any("mode" in item for item in errors))

    def test_resolved_program_config_inherits_empty_builtin_repos(self):
        resolved = resolved_program_config(
            {"config": {"repo_picker": {"repos": []}}},
            {
                "repo_picker": {
                    "repos": [{"id": "org/checkout-service", "default_branch": "dev"}]
                }
            },
        )
        self.assertEqual(
            resolved["repo_picker"]["repos"],
            [{"id": "org/checkout-service", "default_branch": "dev"}],
        )

    def test_validate_mode_allowed_on_filing_and_picker(self):
        self.assertEqual(
            validate_program_config(
                {
                    "jira_filing": {"mode": "inherit"},
                    "ticket_picker": {"mode": "inherit"},
                }
            ),
            [],
        )
        self.assertEqual(
            validate_program_config(
                {
                    "jira_filing": {"mode": "custom", "default_issue_type": "Task"},
                    "ticket_picker": {"mode": "custom", "statuses": ["Ready"]},
                }
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
