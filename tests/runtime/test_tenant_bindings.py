from __future__ import annotations

import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from ts_harness.config.bindings import (
    check_jira_projects_allowed,
    check_repo_allowed,
    github_repo_slug,
    is_jira_write_tool,
    jira_project_keys_from_args,
    tenant_bindings,
)
from ts_harness.config.runtime_settings import (
    apply_settings_to_config,
    validate_runtime_settings_payload,
)
from ts_harness.config.schema import load_runtime_config
from ts_harness.implementation.backends.base import (
    CodingRequest,
    attribution_markdown,
    commit_message,
)
from ts_harness.jira.filing import build_story_payload
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULTS = {
    "github": {"org": "Acme"},
    "repo_picker": {"repos": [{"id": "acme/checkout", "default_branch": "dev"}]},
    "jira_filing": {"default_issue_type": "Story", "allowed_projects": ["ACME", "OPS"]},
}


class BindingsTests(unittest.TestCase):
    def test_repo_slug_forms(self):
        self.assertEqual(github_repo_slug("https://github.com/Acme/Checkout.git"), "acme/checkout")
        self.assertEqual(github_repo_slug("git@github.com:acme/checkout.git"), "acme/checkout")
        self.assertEqual(github_repo_slug("acme/checkout"), "acme/checkout")
        self.assertIsNone(github_repo_slug("https://gitlab.com/acme/checkout"))

    def test_repo_outside_org_or_allowlist_is_denied(self):
        bindings = tenant_bindings(DEFAULTS)
        self.assertIsNone(check_repo_allowed("https://github.com/acme/checkout", bindings))
        self.assertIn("outside the tenant GitHub org", check_repo_allowed("https://github.com/other/checkout", bindings))
        self.assertIn("allowlist", check_repo_allowed("https://github.com/acme/billing", bindings))

    def test_unbound_tenant_allows_any_repo(self):
        self.assertIsNone(check_repo_allowed("https://github.com/any/repo", tenant_bindings({})))

    def test_jira_project_extraction(self):
        args = {
            "project_key": "acme",
            "fields": {"project": {"key": "OPS"}},
            "issues": [{"issue_key": "TFS-9"}],
        }
        self.assertEqual(jira_project_keys_from_args(args), {"ACME", "OPS", "TFS"})

    def test_jira_projects_outside_binding(self):
        bindings = tenant_bindings(DEFAULTS)
        self.assertIsNone(check_jira_projects_allowed({"ACME"}, bindings))
        self.assertIn("TFS", check_jira_projects_allowed({"ACME", "TFS"}, bindings))

    def test_jira_write_tool_detection(self):
        self.assertTrue(is_jira_write_tool("mcp-atlassian-jira_create_issue"))
        self.assertTrue(is_jira_write_tool("mcp-atlassian-jira_transition_issue"))
        self.assertFalse(is_jira_write_tool("mcp-atlassian-jira_search"))

    def test_story_payload_rejects_unbound_project(self):
        with self.assertRaises(ValueError):
            build_story_payload(
                project_key="TFS",
                summary="s",
                acceptance="a",
                pointer={"work_order_id": "WO-1", "repo_id": "acme/checkout", "program_id": "p"},
                allowed_projects=["ACME"],
            )
        payload = build_story_payload(
            project_key="ACME",
            summary="s",
            acceptance="a",
            pointer={"work_order_id": "WO-1", "repo_id": "acme/checkout", "program_id": "p"},
            allowed_projects=["ACME"],
        )
        self.assertEqual(payload["fields"]["project"]["key"], "ACME")


class SettingsValidationTests(unittest.TestCase):
    def _payload(self, **sections):
        payload = {"definition_type": "runtime_settings", "definition_version": "1.0.0"}
        payload.update(sections)
        return payload

    def test_bindings_and_identity_maps_canonicalize(self):
        canonical, issues = validate_runtime_settings_payload(
            self._payload(
                program_defaults={
                    "github": {"org": "Acme"},
                    "jira_filing": {"default_issue_type": "Story", "allowed_projects": ["acme", "OPS"]},
                },
                identity={
                    "github_logins": {"Ada@Example.com": "ada-l"},
                    "roles": {"ada@example.com": ["release_manager", "prd_approver"]},
                },
            )
        )
        self.assertEqual(issues, [])
        self.assertEqual(canonical["program_defaults"]["github"], {"org": "Acme"})
        self.assertEqual(
            canonical["program_defaults"]["jira_filing"]["allowed_projects"], ["ACME", "OPS"]
        )
        self.assertEqual(canonical["identity"]["github_logins"], {"ada@example.com": "ada-l"})
        self.assertEqual(
            canonical["identity"]["roles"], {"ada@example.com": ["prd_approver", "release_manager"]}
        )

    def test_github_login_mapped_to_two_people_errors(self):
        canonical, issues = validate_runtime_settings_payload(
            self._payload(
                identity={"github_logins": {"a@example.com": "same", "b@example.com": "Same"}}
            )
        )
        self.assertIsNone(canonical)
        self.assertIn("duplicate_github_login", {issue["code"] for issue in issues})

    def test_invalid_jira_project_errors(self):
        canonical, issues = validate_runtime_settings_payload(
            self._payload(
                program_defaults={"jira_filing": {"default_issue_type": "Story", "allowed_projects": ["no-dash"]}}
            )
        )
        self.assertIsNone(canonical)
        self.assertIn("invalid_jira_project", {issue["code"] for issue in issues})


class PolicyEnforcementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")

    def _config(self):
        config = copy.deepcopy(self.base)
        apply_settings_to_config(config, {"program_defaults": copy.deepcopy(DEFAULTS)})
        return config

    def _ctx(self):
        return ToolPolicyContext(tenant_id="default", agent_id="pm-agent", source="workflow")

    def test_jira_write_outside_binding_denied(self):
        config = self._config()
        agent = config.get_agent("pm-agent")
        decision = evaluate_tool_policy(
            "mcp-atlassian-jira_create_issue",
            agent,
            config,
            self._ctx(),
            tool_args={"project_key": "TFS", "summary": "x"},
        )
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.policy_id, "tenant_jira_binding")

    def test_jira_write_without_project_denied_when_bound(self):
        config = self._config()
        decision = evaluate_tool_policy(
            "mcp-atlassian-jira_create_issue",
            config.get_agent("pm-agent"),
            config,
            self._ctx(),
            tool_args={"summary": "x"},
        )
        self.assertEqual(decision.policy_id, "tenant_jira_binding")

    def test_jira_write_inside_binding_not_denied_by_binding(self):
        config = self._config()
        decision = evaluate_tool_policy(
            "mcp-atlassian-jira_create_issue",
            config.get_agent("pm-agent"),
            config,
            self._ctx(),
            tool_args={"project_key": "ACME", "summary": "x"},
        )
        self.assertNotEqual(decision.policy_id, "tenant_jira_binding")

    def test_jira_write_advertised_without_args_when_bound(self):
        config = self._config()
        decision = evaluate_tool_policy(
            "mcp-atlassian-jira_create_issue", config.get_agent("pm-agent"), config, self._ctx()
        )
        self.assertNotEqual(decision.policy_id, "tenant_jira_binding")

    def test_jira_write_denied_in_stage_without_jira_write_effect(self):
        config = self._config()
        ctx = ToolPolicyContext(tenant_id="default", agent_id="pm-agent", source="workflow", stage_external_effects=set())
        decision = evaluate_tool_policy(
            "mcp-atlassian-jira_create_issue",
            config.get_agent("pm-agent"),
            config,
            ctx,
            tool_args={"project_key": "ACME", "summary": "x"},
        )
        self.assertEqual((decision.decision, decision.policy_id), ("deny", "stage_external_effects"))

    def test_jira_write_allowed_in_stage_with_jira_write_effect(self):
        config = self._config()
        ctx = ToolPolicyContext(
            tenant_id="default", agent_id="pm-agent", source="workflow", stage_external_effects={"jira_write"}
        )
        decision = evaluate_tool_policy(
            "mcp-atlassian-jira_create_issue",
            config.get_agent("pm-agent"),
            config,
            ctx,
            tool_args={"project_key": "ACME", "summary": "x"},
        )
        self.assertEqual(decision.decision, "allow")

    def test_jira_read_not_limited_by_stage_effects(self):
        config = self._config()
        ctx = ToolPolicyContext(tenant_id="default", agent_id="pm-agent", source="workflow", stage_external_effects=set())
        decision = evaluate_tool_policy(
            "mcp-atlassian-jira_get_issue", config.get_agent("pm-agent"), config, ctx, tool_args={"issue_key": "TFS-1"}
        )
        self.assertEqual(decision.decision, "allow")

    def test_jira_reads_are_not_publish_tools(self):
        from ts_harness.tools.policy import publish_tool_names

        config = self._config()
        published = publish_tool_names(
            {"mcp-atlassian-jira_search", "mcp-atlassian-jira_get_issue", "mcp-atlassian-jira_create_issue"}, config
        )
        self.assertEqual(published, {"mcp-atlassian-jira_create_issue"})

    def test_agents_get_jira_toolsets(self):
        from ts_harness.tools.toolsets import agent_effective_tools

        config = self._config()
        pm = agent_effective_tools(config.get_agent("pm-agent"), config)
        self.assertIn("mcp-atlassian-jira_create_issue", pm)
        self.assertIn("mcp-atlassian-jira_get_issue", pm)
        for agent_id in ("orchestrator", "dev-agent", "comms-agent"):
            tools = agent_effective_tools(config.get_agent(agent_id), config)
            self.assertIn("mcp-atlassian-jira_get_issue", tools, agent_id)
            self.assertNotIn("mcp-atlassian-jira_create_issue", tools, agent_id)


class CodingBackendBindingTests(unittest.TestCase):
    def test_run_coding_backend_denies_repo_outside_binding(self):
        from ts_harness.implementation import coding_job
        from ts_harness.tools import coding_backend_tool

        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        apply_settings_to_config(config, {"program_defaults": copy.deepcopy(DEFAULTS)})
        config.implementation.coding_backend = "opencode"
        ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="workflow")
        with patch.object(coding_job, "_live_runtime_config", return_value=config), patch.object(
            coding_job, "load_active_runtime_config", return_value=config
        ), patch.object(coding_job, "OpenCodeBackend") as backend:
            result = coding_backend_tool.handle_run_coding_backend(
                {
                    "work_order_id": "WO-1",
                    "work_order_body": "body",
                    "repo_url": "https://github.com/other/repo",
                },
                ctx,
            )
        self.assertFalse(result["ok"])
        self.assertIn("outside the tenant GitHub org", result["error"])
        backend.return_value.implement.assert_not_called()

    def test_attribution_in_pr_body_and_commit(self):
        request = CodingRequest(
            repo_url="https://github.com/acme/checkout",
            base_branch="dev",
            work_order_id="WO-7",
            work_order_body="b",
            workspace_dir=Path("/tmp/x"),
            provider="p",
            requested_by="ada@example.com",
            requested_by_github_login="ada-l",
            program_id="prog-1",
            decision_id="dec-1",
        )
        body = attribution_markdown(request)
        self.assertIn("Requested by: ada@example.com (@ada-l)", body)
        self.assertIn("Program: `prog-1`", body)
        self.assertIn("Decision: `dec-1`", body)
        message = commit_message(request)
        self.assertTrue(message.startswith("Implement WO-7"))
        self.assertIn("Co-authored-by: ada-l <ada@example.com>", message)
        self.assertIn("TS-Harness-Program: prog-1", message)

    def test_no_attribution_without_requester(self):
        request = CodingRequest(
            repo_url="r",
            base_branch="dev",
            work_order_id="WO-7",
            work_order_body="b",
            workspace_dir=Path("/tmp/x"),
            provider="p",
        )
        self.assertEqual(attribution_markdown(request), "")
        self.assertEqual(commit_message(request), "Implement WO-7")


class ProgramTenantTests(unittest.TestCase):
    def test_explicit_tenant_wins(self):
        from ts_harness.workflows.delivery_program import resolve_program_tenant

        self.assertEqual(resolve_program_tenant("acme"), "acme")

    def test_missing_tenant_with_many_configured_raises(self):
        from ts_harness.workflows import delivery_program

        with patch.object(delivery_program, "_configured_tenant_ids", return_value=["a", "b"]):
            with self.assertRaises(ValueError):
                delivery_program.resolve_program_tenant(None)

    def test_missing_tenant_with_single_configured_uses_it(self):
        from ts_harness.workflows import delivery_program

        with patch.object(delivery_program, "_configured_tenant_ids", return_value=["solo"]):
            self.assertEqual(delivery_program.resolve_program_tenant(""), "solo")


if __name__ == "__main__":
    unittest.main()
