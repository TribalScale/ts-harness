from __future__ import annotations

import unittest

from ts_harness.config.runtime_settings import (
    annotate_sources,
    apply_settings_to_config,
    merge_runtime_settings,
    redact_runtime_yaml,
    validate_runtime_settings_payload,
    yaml_settings_baseline,
)
from ts_harness.config.schema import (
    AgentConfig,
    GatewayConfig,
    IdentityConfig,
    RuntimeConfig,
    TenantConfig,
)


def _valid_payload(**overrides):
    payload = {
        "definition_type": "runtime_settings",
        "definition_version": "1.0.0",
        "program_defaults": {
            "repo_picker": {
                "repos": [{"id": "org/checkout-service", "default_branch": "dev"}]
            }
        },
        "identity": {
            "jira_account_ids": {"ada@example.com": "acct-1"},
        },
    }
    payload.update(overrides)
    return payload


class RuntimeSettingsOverlayTests(unittest.TestCase):
    def test_unknown_top_level_key_errors(self):
        canonical, issues = validate_runtime_settings_payload(
            _valid_payload(gateway={"token": "nope"})
        )
        self.assertIsNone(canonical)
        self.assertIn("unknown_field", {issue["code"] for issue in issues})

    def test_unknown_nested_key_errors(self):
        canonical, issues = validate_runtime_settings_payload(
            _valid_payload(identity={"oidc_client": "x", "jira_account_ids": {}})
        )
        self.assertIsNone(canonical)
        self.assertTrue(any("unknown" in issue["code"] for issue in issues))

    def test_missing_definition_type_or_version_errors(self):
        _, issues = validate_runtime_settings_payload(
            {"definition_type": "channel_routing", "definition_version": "1.0.0"}
        )
        self.assertIn("invalid_definition_type", {issue["code"] for issue in issues})
        _, issues = validate_runtime_settings_payload(
            {"definition_type": "runtime_settings", "definition_version": ""}
        )
        self.assertIn("missing_version", {issue["code"] for issue in issues})

    def test_duplicate_repo_ids_error(self):
        payload = _valid_payload()
        payload["program_defaults"] = {
            "repo_picker": {
                "repos": [
                    {"id": "org/a", "default_branch": "dev"},
                    {"id": "org/a", "default_branch": "main"},
                ]
            }
        }
        canonical, issues = validate_runtime_settings_payload(payload)
        self.assertIsNone(canonical)
        self.assertIn("duplicate_repo_id", {issue["code"] for issue in issues})

    def test_duplicate_email_keys_after_lowercase_error(self):
        payload = _valid_payload(
            identity={
                "jira_account_ids": {
                    "Ada@Example.com": "acct-1",
                    "ada@example.com": "acct-2",
                }
            }
        )
        canonical, issues = validate_runtime_settings_payload(payload)
        self.assertIsNone(canonical)
        self.assertIn("duplicate_email", {issue["code"] for issue in issues})

    def test_department_map_rejects_a_bad_name_and_a_non_email(self):
        bad_name = _valid_payload()
        bad_name["identity"] = {"departments": {"ada@example.com": "Sales-Team"}}
        canonical, issues = validate_runtime_settings_payload(bad_name)
        self.assertIsNone(canonical)
        self.assertIn("invalid_department", {issue["code"] for issue in issues})

        bad_email = _valid_payload()
        bad_email["identity"] = {"departments": {"not-an-email": "engineering"}}
        canonical, issues = validate_runtime_settings_payload(bad_email)
        self.assertIsNone(canonical)
        self.assertIn("invalid_email", {issue["code"] for issue in issues})

        good = _valid_payload()
        good["identity"] = {"departments": {"Ada@example.com": "Engineering"}}
        canonical, issues = validate_runtime_settings_payload(good)
        self.assertEqual(issues, [])
        self.assertEqual(canonical["identity"]["departments"], {"ada@example.com": "engineering"})

    def test_bad_email_shape_error(self):
        payload = _valid_payload(
            identity={"jira_account_ids": {"not-an-email": "acct-1"}}
        )
        canonical, issues = validate_runtime_settings_payload(payload)
        self.assertIsNone(canonical)
        self.assertIn("invalid_email", {issue["code"] for issue in issues})

    def test_overlay_replaces_jira_map_and_inherits_omitted_slack(self):
        yaml_sections = {
            "program_defaults": {
                "repo_picker": {"repos": [{"id": "org/yaml", "default_branch": "main"}]},
                "jira_filing": {"default_issue_type": "Story"},
            },
            "identity": {
                "slack_user_emails": {"U1": "yaml@example.com"},
                "jira_account_ids": {"old@example.com": "acct-old"},
            },
        }
        overlay = {
            "identity": {"jira_account_ids": {"new@example.com": "acct-new"}},
            "program_defaults": {
                "repo_picker": {
                    "repos": [{"id": "org/overlay", "default_branch": ""}]
                }
            },
        }
        merged = merge_runtime_settings(yaml_sections, overlay)
        self.assertEqual(
            merged["identity"]["jira_account_ids"],
            {"new@example.com": "acct-new"},
        )
        self.assertEqual(
            merged["identity"]["slack_user_emails"],
            {"U1": "yaml@example.com"},
        )
        self.assertEqual(
            merged["program_defaults"]["repo_picker"]["repos"],
            [{"id": "org/overlay", "default_branch": "dev"}],
        )
        self.assertEqual(
            merged["program_defaults"]["jira_filing"]["default_issue_type"],
            "Story",
        )
        sources = annotate_sources(yaml_sections, overlay)
        self.assertEqual(sources["identity.jira_account_ids"], "overlay")
        self.assertEqual(sources["identity.slack_user_emails"], "inherited")
        self.assertEqual(sources["program_defaults.repo_picker"], "overlay")
        self.assertEqual(sources["program_defaults.jira_filing"], "inherited")

    def test_redact_runtime_yaml_hides_token_and_secrets(self):
        data = {
            "gateway": {"token": "super-secret-token"},
            "connectors": {
                "google-workspace": {
                    "auth": {"delegation": {"credentials_secret": "google-workspace-sa-json"}}
                }
            },
            "note": "leak super-secret-value here",
        }
        text = redact_runtime_yaml(
            data,
            token="super-secret-token",
            secret_values=["super-secret-value"],
        )
        self.assertNotIn("super-secret-token", text)
        self.assertNotIn("super-secret-value", text)
        self.assertIn("***", text)
        self.assertIn("google-workspace-sa-json", text)

    def test_apply_settings_to_config_replaces_maps(self):
        config = RuntimeConfig(
            gateway=GatewayConfig(token="keep-me"),
            tenants=[TenantConfig(id="default", display_name="Default")],
            agents=[
                AgentConfig(id="orchestrator", workspace="/w", model="m", default=True)
            ],
            identity=IdentityConfig(
                slack_user_emails={"U1": "yaml@example.com"},
                jira_account_ids={"old@example.com": "acct-old"},
            ),
            program_defaults={
                "repo_picker": {"repos": [{"id": "org/yaml", "default_branch": "main"}]}
            },
        )
        baseline = yaml_settings_baseline(config)
        self.assertEqual(baseline["identity"]["slack_user_emails"]["U1"], "yaml@example.com")
        apply_settings_to_config(
            config,
            {
                "identity": {"jira_account_ids": {"new@example.com": "acct-new"}},
                "program_defaults": {
                    "repo_picker": {"repos": [{"id": "org/overlay", "default_branch": "dev"}]}
                },
            },
        )
        self.assertEqual(config.gateway.token, "keep-me")
        self.assertEqual(config.identity.slack_user_emails, {"U1": "yaml@example.com"})
        self.assertEqual(config.identity.jira_account_ids, {"new@example.com": "acct-new"})
        self.assertEqual(
            config.program_defaults["repo_picker"]["repos"],
            [{"id": "org/overlay", "default_branch": "dev"}],
        )

    def test_repo_picker_mode_survives_validation_and_merge(self):
        canonical, issues = validate_runtime_settings_payload(
            _valid_payload(
                program_defaults={
                    "repo_picker": {
                        "mode": "custom",
                        "repos": [{"id": "org/checkout-service", "default_branch": "dev"}],
                    }
                }
            )
        )
        self.assertFalse(any(issue["severity"] == "error" for issue in issues))
        assert canonical is not None
        self.assertEqual(canonical["program_defaults"]["repo_picker"]["mode"], "custom")
        merged = merge_runtime_settings(
            {
                "program_defaults": {
                    "repo_picker": {
                        "mode": "custom",
                        "repos": [{"id": "org/yaml", "default_branch": "main"}],
                    }
                }
            },
            {
                "program_defaults": {
                    "repo_picker": {
                        "repos": [{"id": "org/overlay", "default_branch": "dev"}],
                    }
                }
            },
        )
        self.assertEqual(merged["program_defaults"]["repo_picker"]["mode"], "custom")
        self.assertEqual(
            merged["program_defaults"]["repo_picker"]["repos"][0]["id"],
            "org/overlay",
        )

    def test_new_sections_inherit_when_omitted_and_replace_when_present(self):
        yaml_sections = {
            "policy": {"budget_warn_usd": 10, "budget_hard_stop_usd": 20},
            "sandbox": {"timeout_seconds": 30, "egress_policy": "deny"},
            "bindings": {"slack": "orchestrator", "discord": "orchestrator"},
            "toolsets": {"core-read": ["read_file"], "terminal": ["exec"]},
        }
        inherited = merge_runtime_settings(yaml_sections, {"identity": {}})
        self.assertEqual(inherited["policy"]["budget_warn_usd"], 10)
        self.assertEqual(inherited["bindings"]["slack"], "orchestrator")
        replaced = merge_runtime_settings(
            yaml_sections,
            {
                "policy": {"budget_warn_usd": 12},
                "sandbox": {"egress_policy": "allow"},
                "bindings": {"slack": "comms-agent"},
                "toolsets": {"core-read": ["recall_memory"]},
            },
        )
        self.assertEqual(replaced["policy"]["budget_warn_usd"], 12)
        self.assertEqual(replaced["policy"]["budget_hard_stop_usd"], 20)
        self.assertEqual(replaced["sandbox"]["egress_policy"], "allow")
        self.assertEqual(replaced["sandbox"]["timeout_seconds"], 30)
        self.assertEqual(replaced["bindings"]["slack"], "comms-agent")
        self.assertEqual(replaced["bindings"]["discord"], "orchestrator")
        self.assertEqual(replaced["toolsets"]["core-read"], ["recall_memory"])
        self.assertEqual(replaced["toolsets"]["terminal"], ["exec"])

    def test_partial_connector_keeps_accounts_and_artifact_publish(self):
        merged = merge_runtime_settings(
            {
                "connectors": {
                    "google-workspace": {
                        "enabled": True,
                        "accounts": {"marketing": {"impersonate_email": "m@example.com"}},
                        "scopes": ["old"],
                        "artifact_publish": {"folder_id": "folder", "min_chars": 10},
                    }
                }
            },
            {"connectors": {"google-workspace": {"scopes": ["new"]}}},
        )
        google = merged["connectors"]["google-workspace"]
        self.assertEqual(google["scopes"], ["new"])
        self.assertEqual(google["accounts"]["marketing"]["impersonate_email"], "m@example.com")
        self.assertEqual(google["artifact_publish"]["folder_id"], "folder")
        self.assertTrue(google["enabled"])

    def test_connector_secret_rejects_pem_and_json(self):
        pem = "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----"
        _, pem_issues = validate_runtime_settings_payload(
            _valid_payload(
                connectors={
                    "google-workspace": {
                        "auth": {"delegation": {"credentials_secret": pem}},
                    }
                }
            )
        )
        self.assertIn("secret_material", {issue["code"] for issue in pem_issues})
        blob = '{"type":"service_account","private_key":"x"}'
        canonical, json_issues = validate_runtime_settings_payload(
            _valid_payload(
                connectors={
                    "google-workspace": {
                        "auth": {"oauth": {"client_secret_secret": blob}},
                    }
                }
            )
        )
        self.assertIsNone(canonical)
        self.assertIn("secret_material", {issue["code"] for issue in json_issues})

    def test_budget_order_and_unknown_toolset_tool_fail(self):
        _, budget_issues = validate_runtime_settings_payload(
            _valid_payload(policy={"budget_warn_usd": 80, "budget_hard_stop_usd": 10})
        )
        self.assertIn("invalid_budget", {issue["code"] for issue in budget_issues})
        canonical, tool_issues = validate_runtime_settings_payload(
            _valid_payload(toolsets={"core-read": ["not_a_real_tool"]}),
            known_tool_names={"read_file"},
        )
        self.assertIsNone(canonical)
        self.assertIn("unknown_name", {issue["code"] for issue in tool_issues})


if __name__ == "__main__":
    unittest.main()
