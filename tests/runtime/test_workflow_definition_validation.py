from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from ts_harness.workflows.definition_validation import (
    validate_workflow_definition,
)
from ts_harness.workflows.hooks import build_default_registry
from ts_harness.repo_paths import ensure_scripts_importable

ensure_scripts_importable()

import workflow_definitions  # noqa: E402


def _builtin_workflow(name: str) -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "workflows"
        / f"{name}.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


class WorkflowDefinitionValidationTests(unittest.TestCase):
    def catalogs(self):
        return {
            "hook_names": set(build_default_registry().names()),
            "agent_ids": {
                "agent-a",
                "agent-b",
                "human",
                "pm-agent",
                "dev-agent",
                "qa-agent",
                "orchestrator",
                "ops-agent",
                "sales-agent",
            },
            "skill_ids": {
                "skill-a",
                "skill-b",
                "clarify-requirements",
                "draft-prd",
                "verified-merge-governed",
                "governed-qa-gate",
                "prd-to-work-orders-governed",
                "delivery-planning",
            },
            "built_in_types": {"prd_request", "implement_work_orders"},
        }

    def valid_payload(self):
        return {
            "workflow_type": "custom_flow",
            "definition_version": "1.0.0",
            "contracts": {"result": {"output_schema": {"type": "object"}}},
            "stages": [
                {
                    "name": "start",
                    "kind": "task",
                    "agent_id": "agent-a",
                    "skill_hints": ["skill-a"],
                    "approval_required": True,
                    "on_complete": {
                        "hook": "hydrate_input",
                        "downstream": ["finish"],
                    },
                    "input_map": {
                        "summary": "$input.summary",
                        "literal": "high",
                    },
                    "contract_ref": "result",
                },
                {
                    "name": "finish",
                    "kind": "task",
                    "agent_id": "agent-b",
                    "depends_on": ["start"],
                },
            ],
            "on_workflow_cancel": {"hook": "cancel_workflow"},
            "on_workflow_complete": {"hook": "complete_program_phase"},
        }

    def validate(self, payload, **overrides):
        catalogs = self.catalogs()
        catalogs.update(overrides)
        return validate_workflow_definition(payload, **catalogs)

    def issue_codes(self, issues):
        return {issue.code for issue in issues}

    def test_coding_job_execution_needs_a_contract_and_a_plain_task(self):
        payload = self.valid_payload()
        payload["stages"][0]["execution"] = "coding_job"
        skeleton, issues = self.validate(payload)
        self.assertEqual(skeleton.stage("start").execution, "coding_job")
        self.assertNotIn("coding_job_without_contract", self.issue_codes(issues))

        payload["stages"][1]["execution"] = "coding_job"
        _skeleton, issues = self.validate(payload)
        self.assertIn("coding_job_without_contract", self.issue_codes(issues))

        payload = self.valid_payload()
        payload["stages"][0]["execution"] = "robot"
        skeleton, issues = self.validate(payload)
        self.assertIsNone(skeleton)
        self.assertIn("schema_load_error", self.issue_codes(issues))

    def test_valid_definition_returns_skeleton_without_errors(self):
        skeleton, issues = self.validate(self.valid_payload())

        self.assertIsNotNone(skeleton)
        errors = [issue for issue in issues if issue.severity == "error"]
        self.assertEqual(errors, [])

    def test_stage_without_tool_allow_round_trips_without_the_key(self):
        skeleton, _issues = self.validate(self.valid_payload())

        self.assertIsNone(skeleton.stages[0].tool_allow)
        payload = workflow_definitions.skeleton_to_payload(skeleton)
        self.assertNotIn("tool_allow", payload["stages"][0])

    def test_stage_tool_allow_round_trips_and_rejects_empty_or_null(self):
        payload = self.valid_payload()
        payload["stages"][0]["tool_allow"] = ["read_file", "read_file", "exec"]
        skeleton = workflow_definitions.load_skeleton_payload(payload)
        self.assertEqual(skeleton.stages[0].tool_allow, ["read_file", "exec"])
        self.assertEqual(
            workflow_definitions.skeleton_to_payload(skeleton)["stages"][0]["tool_allow"],
            ["read_file", "exec"],
        )

        for value in ([], [" "], None):
            with self.subTest(value=value):
                payload["stages"][0]["tool_allow"] = value
                with self.assertRaises(ValueError):
                    workflow_definitions.load_skeleton_payload(payload)

    def test_stage_tool_allow_must_be_within_agent_tools(self):
        payload = self.valid_payload()
        payload["stages"][0]["tool_allow"] = ["read_file", "channel_send"]
        agent_tools = {"agent-a": {"read_file"}, "agent-b": {"channel_send"}}

        _skeleton, issues = self.validate(payload, agent_tools=agent_tools)
        unknown = [issue for issue in issues if issue.code == "unknown_tool"]
        self.assertEqual([issue.path for issue in unknown], ["$.stages[0].tool_allow[1]"])

        payload["stages"][0]["tool_allow"] = ["read_file"]
        _skeleton, issues = self.validate(payload, agent_tools=agent_tools)
        self.assertNotIn("unknown_tool", self.issue_codes(issues))

        _skeleton, issues = self.validate(payload)
        self.assertNotIn("unknown_tool", self.issue_codes(issues))

    def test_reports_schema_and_catalog_errors(self):
        payload = self.valid_payload()
        payload["extra"] = True
        payload["workflow_type"] = "prd_request"
        payload["stages"][0]["agent_id"] = "missing-agent"
        payload["stages"][0]["allowed_agents"] = ["missing-allowed"]
        payload["stages"][0]["skill_hints"] = ["missing-skill"]
        payload["stages"][0]["on_complete"] = {"hook": "missing-hook"}
        payload["stages"][0]["contract_ref"] = "missing-contract"
        payload["on_workflow_complete"] = {"hook": "missing-workflow-hook"}

        _skeleton, issues = self.validate(payload)

        self.assertIn("schema_load_error", self.issue_codes(issues))
        self.assertIn("built_in_type_collision", self.issue_codes(issues))
        self.assertIn("unknown_agent", self.issue_codes(issues))
        self.assertIn("unknown_skill", self.issue_codes(issues))
        self.assertIn("unknown_hook", self.issue_codes(issues))
        self.assertIn("unresolved_contract_ref", self.issue_codes(issues))

    def test_reports_dependency_graph_errors_and_warnings(self):
        payload = self.valid_payload()
        payload["stages"] = [
            {
                "name": "a",
                "kind": "task",
                "agent_id": "agent-a",
                "depends_on": ["c"],
            },
            {
                "name": "b",
                "kind": "task",
                "agent_id": "agent-a",
                "depends_on": ["a"],
            },
            {
                "name": "c",
                "kind": "task",
                "agent_id": "agent-a",
                "depends_on": ["b"],
            },
            {"name": "lonely", "kind": "task", "agent_id": "agent-a"},
        ]

        _skeleton, issues = self.validate(payload)

        self.assertIn("cycle", self.issue_codes(issues))
        self.assertIn("unreachable_stage", self.issue_codes(issues))
        self.assertIn("isolated_stage", self.issue_codes(issues))
        self.assertIn("no_approval_stage", self.issue_codes(issues))

    def test_reports_duplicate_missing_and_self_dependencies(self):
        payload = self.valid_payload()
        payload["stages"] = [
            {"name": "start", "kind": "task", "agent_id": "agent-a"},
            {
                "name": "start",
                "kind": "task",
                "agent_id": "agent-a",
                "depends_on": ["start", "missing"],
            },
        ]

        _skeleton, issues = self.validate(payload)

        self.assertIn("duplicate_stage", self.issue_codes(issues))
        self.assertIn("self_dependency", self.issue_codes(issues))
        self.assertIn("missing_dependency", self.issue_codes(issues))

    def test_allows_full_schema_features_and_engine_input_map_prefixes(self):
        payload = self.valid_payload()
        payload["program"] = {"next_phase": "delivery"}
        payload["stages"].append(
            {
                "name": "record_failure",
                "kind": "compensate",
                "agent_id": "agent-a",
            }
        )
        payload["stages"][0]["on_failure"] = {
            "hook": "compensate_on_failure",
            "compensate_stage": "record_failure",
        }
        payload["stages"][0]["input_map"] = {
            "from_workflow": "$workflow.approved_prd",
            "from_prior": "$prior.start.output.x",
            "from_reject": "$reject.comments",
            "from_source": "$source.output",
            "from_runtime": "$runtime.revision_count:start",
            "from_item": "$item.key",
            "from_revise": "$revise.id",
            "from_re_gate": "$re_gate.id",
            "from_input": "$input.summary",
        }
        payload["stages"][0]["barrier"] = {"hook": "collect_delivery_results"}
        payload["stages"][1]["join"] = {"hook": "merge_outputs"}
        payload["stages"][1]["wait_external"] = {
            "event_type": "ticket.updated",
        }

        _skeleton, issues = self.validate(payload)
        errors = [issue for issue in issues if issue.severity == "error"]

        self.assertEqual(errors, [])
        self.assertNotIn("v1_disallowed_compensate", self.issue_codes(issues))
        self.assertNotIn("v1_disallowed_barrier", self.issue_codes(issues))
        self.assertNotIn("v1_disallowed_join", self.issue_codes(issues))
        self.assertNotIn("v1_disallowed_wait_external", self.issue_codes(issues))
        self.assertNotIn("v1_disallowed_program", self.issue_codes(issues))

    def test_rejects_bad_input_map_and_invalid_hook_payloads(self):
        payload = self.valid_payload()
        payload["stages"][0]["input_map"] = {"bad": "$unknown.value"}
        payload["stages"][0]["on_complete"] = {"hook": "hydrate_input"}
        payload["stages"][0]["on_failure"] = {
            "hook": "compensate_on_failure",
            "compensate_stage": "missing_stage",
        }
        payload["stages"][1]["wait_external"] = {"timeout_seconds": 60}
        payload["stages"][1]["on_reject"] = {"hook": "revise_on_reject"}

        _skeleton, issues = self.validate(payload)
        codes = self.issue_codes(issues)

        self.assertIn("unsupported_input_map", codes)
        self.assertIn("invalid_hydrate_input", codes)
        self.assertIn("invalid_compensate_on_failure", codes)
        self.assertIn("invalid_wait_external", codes)
        self.assertIn("invalid_revise_on_reject", codes)

    def test_on_block_cases(self):
        def revise_issues(cases):
            payload = self.valid_payload()
            payload["stages"][1]["on_block"] = {"hook": "revise_on_reject", "revise": {"step_name": "draft"}, "cases": cases}
            _skeleton, issues = self.validate(payload)
            return [issue for issue in issues if issue.code == "invalid_revise_on_reject"]

        good = [
            {"error_prefix": "external_blocked:github.pr_changes_requested", "max_revisions": 3, "revise": {"step_name": "draft", "budget": "human"}},
            {"error_prefix": "external_blocked:github.pr_closed", "action": "stop"},
        ]
        self.assertEqual(revise_issues(good), [])
        for bad in (
            {"cases": "nope"},
            [{"action": "stop"}],
            [{"error_prefix": "x", "action": "retry"}],
            [{"error_prefix": "x", "revise": {}}],
            [{"error_prefix": "x", "max_revisions": "3"}],
        ):
            with self.subTest(cases=bad):
                self.assertTrue(revise_issues(bad))

    def test_empty_wait_external_means_no_wait(self):
        payload = self.valid_payload()
        payload["stages"][1]["wait_external"] = {}

        _skeleton, issues = self.validate(payload)

        self.assertNotIn("invalid_wait_external", self.issue_codes(issues))

    def test_mark_artifact_approved_does_not_require_downstream(self):
        payload = self.valid_payload()
        payload["stages"][0]["on_complete"] = {"hook": "mark_artifact_approved"}

        _skeleton, issues = self.validate(payload)
        errors = [issue for issue in issues if issue.severity == "error"]

        self.assertEqual(errors, [])

    def publish_payload(self):
        return {
            "workflow_type": "publish_flow",
            "definition_version": "1.0.0",
            "stages": [
                {"name": "draft", "kind": "task", "agent_id": "agent-a"},
                {
                    "name": "approve",
                    "kind": "task",
                    "agent_id": "orchestrator",
                    "depends_on": ["draft"],
                    "approval_required": True,
                },
                {
                    "name": "publish",
                    "kind": "task",
                    "agent_id": "agent-b",
                    "depends_on": ["approve"],
                    "tool_allow": ["mcp-cms-publish"],
                },
            ],
        }

    def publish_codes(self, payload, **overrides):
        kwargs = {
            "agent_tools": {
                "agent-a": {"read_file"},
                "agent-b": {"read_file", "mcp-cms-publish"},
                "orchestrator": {"workflow_status"},
            },
            "publish_tools": {"mcp-cms-publish", "channel_send"},
        }
        kwargs.update(overrides)
        _skeleton, issues = self.validate(payload, **kwargs)
        return [(issue.code, issue.severity, issue.path) for issue in issues if issue.code in {
            "ungated_publish_stage",
            "unrestricted_mcp_before_gate",
        }]

    def test_publish_stage_behind_approval_gate_passes(self):
        self.assertEqual(self.publish_codes(self.publish_payload()), [])

    def test_publish_stage_without_upstream_gate_warns(self):
        payload = self.publish_payload()
        payload["stages"][1]["approval_required"] = False

        self.assertEqual(
            self.publish_codes(payload),
            [("ungated_publish_stage", "warning", "$.stages[2].tool_allow")],
        )

    def test_own_approval_required_does_not_gate_the_stage(self):
        payload = self.publish_payload()
        payload["stages"][1]["approval_required"] = False
        payload["stages"][2]["approval_required"] = True

        self.assertEqual(
            [code for code, _sev, _path in self.publish_codes(payload)],
            ["ungated_publish_stage"],
        )

    def test_gate_counts_through_transitive_dependencies(self):
        payload = self.publish_payload()
        payload["stages"].insert(
            2,
            {"name": "middle", "kind": "task", "agent_id": "agent-a", "depends_on": ["approve"]},
        )
        payload["stages"][3]["depends_on"] = ["middle"]

        self.assertEqual(self.publish_codes(payload), [])

    def test_expand_stage_with_plan_approval_counts_as_gate(self):
        payload = self.publish_payload()
        payload["stages"][1] = {
            "name": "approve",
            "kind": "expand",
            "agent_id": "orchestrator",
            "allowed_agents": ["orchestrator"],
            "depends_on": ["draft"],
            "plan_approval_required": True,
        }

        _skeleton, issues = self.validate(
            payload,
            agent_tools={"agent-a": set(), "agent-b": {"mcp-cms-publish"}, "orchestrator": set()},
            publish_tools={"mcp-cms-publish"},
        )
        self.assertNotIn("ungated_publish_stage", self.issue_codes(issues))

    def test_unrestricted_stage_uses_agent_grant(self):
        payload = self.publish_payload()
        payload["stages"][0]["agent_id"] = "agent-b"

        self.assertEqual(
            self.publish_codes(payload),
            [("ungated_publish_stage", "warning", "$.stages[0]")],
        )

    def test_unrestricted_mcp_before_gate_warns_when_mcp_is_approval_gated(self):
        payload = self.publish_payload()
        payload["stages"][0]["agent_id"] = "agent-b"

        self.assertEqual(
            self.publish_codes(payload, publish_tools={"channel_send"}),
            [("unrestricted_mcp_before_gate", "warning", "$.stages[0]")],
        )

    def jira_codes(self, stage_overrides, payload_overrides=None):
        write = "mcp-atlassian-jira_create_issue"
        stage = {"name": "file", "kind": "task", "agent_id": "pm-agent", **stage_overrides}
        payload = {
            "workflow_type": "filing",
            "definition_version": "1.0.0",
            "stages": [stage],
            **(payload_overrides or {}),
        }
        return [
            code
            for code, _sev, _path in self.publish_codes(
                payload,
                agent_tools={"pm-agent": {write, "mcp-atlassian-jira_get_issue"}},
                publish_tools={write},
            )
        ]

    def test_jira_write_counts_only_where_stage_declares_it(self):
        self.assertEqual(self.jira_codes({}), [])
        self.assertEqual(self.jira_codes({"allowed_external_effects": ["jira_write"]}), ["ungated_publish_stage"])

    def test_jira_write_gated_by_approved_source_artifact(self):
        codes = self.jira_codes(
            {"allowed_external_effects": ["jira_write"]},
            {"promotion": {"requires_source_artifact": True}},
        )
        self.assertEqual(codes, [])

    def test_jira_write_in_expand_stage_runs_after_plan_approval(self):
        codes = self.jira_codes(
            {"kind": "expand", "allowed_agents": ["pm-agent"], "allowed_external_effects": ["jira_write"]}
        )
        self.assertEqual(codes, [])

    def test_publish_tools_none_skips_publish_checks(self):
        payload = self.publish_payload()
        payload["stages"][1]["approval_required"] = False

        self.assertEqual(self.publish_codes(payload, publish_tools=None), [])

    def test_builtin_workflows_get_no_publish_errors(self):
        from ts_harness.config.schema import load_runtime_config
        from ts_harness.tools.policy import publish_tool_names
        from ts_harness.tools.toolsets import agent_effective_tools

        root = Path(__file__).resolve().parents[2]
        config = load_runtime_config(root / "config" / "ts-harness-runtime.test.yaml")
        agent_tools = {
            agent.id: agent_effective_tools(agent, config) - set(agent.tool_deny)
            for agent in config.agents
        }
        publish_tools = publish_tool_names(set().union(*agent_tools.values()), config)
        warned: dict[str, list[str]] = {}
        for path in sorted((root / "config" / "workflows").glob("*.json")):
            if path.name in {"schema.json", "manifest.json"}:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            _skeleton, issues = validate_workflow_definition(
                payload,
                hook_names=set(build_default_registry().names()),
                agent_ids={agent.id for agent in config.agents},
                skill_ids=set(),
                built_in_types=set(),
                agent_tools=agent_tools,
                publish_tools=publish_tools,
            )
            for issue in issues:
                if issue.code in {"ungated_publish_stage", "unrestricted_mcp_before_gate"}:
                    self.assertEqual(issue.severity, "warning", path.name)
                    warned.setdefault(path.stem, []).append(issue.path)
        self.assertEqual(warned, {})

    def test_builtin_prd_request_validates_as_managed_clone(self):
        payload = copy.deepcopy(_builtin_workflow("prd_request"))
        payload["workflow_type"] = "prd_request_ui"
        payload["definition_version"] = "1.0.0"

        skeleton, issues = self.validate(payload)
        errors = [issue for issue in issues if issue.severity == "error"]

        self.assertIsNotNone(skeleton)
        self.assertEqual(errors, [])

    def test_builtin_implement_work_orders_validates_as_managed_clone(self):
        payload = copy.deepcopy(_builtin_workflow("implement_work_orders"))
        payload["workflow_type"] = "implement_work_orders_ui"
        payload["definition_version"] = "1.0.0"

        skeleton, issues = self.validate(payload)
        errors = [issue for issue in issues if issue.severity == "error"]

        self.assertIsNotNone(skeleton)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
