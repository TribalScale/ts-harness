"""PM skills load, their references exist, and jira_from_prd files Jira only after the plan gate."""

from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path

from ts_harness.config.schema import load_runtime_config
from ts_harness.skills.catalog import SkillCatalog, skill_files
from ts_harness.tools.policy import publish_tool_names
from ts_harness.tools.toolsets import agent_effective_tools
from ts_harness.workflows.definition_validation import validate_workflow_definition
from ts_harness.workflows.hooks import build_default_registry

ROOT = Path(__file__).resolve().parents[2]
PM_SKILLS = {"jira-ticket-format", "prd-drafter", "prd-review", "story-writer", "prd-to-jira", "bug-intake", "file-work-orders-as-jira"}
REFERENCE = re.compile(r"read_skill_reference skill_id=([\w-]+) path=([\w./<>-]+)")


class PmSkillCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = SkillCatalog(ROOT / "skills")
        self.catalog.load()
        self.by_id = {meta.id: meta for meta in self.catalog.index.values()}

    def test_pm_skills_are_indexed_and_approved(self):
        for skill_id in PM_SKILLS:
            with self.subTest(skill=skill_id):
                meta = self.by_id[skill_id]
                self.assertEqual(meta.category, "pm")
                self.assertEqual(meta.approval_status, "approved")
                self.assertTrue(meta.description)

    def test_ticket_templates_ship_with_the_format_skill(self):
        files = skill_files(self.by_id["jira-ticket-format"])
        self.assertEqual(
            sorted(files),
            ["references/bug.md", "references/epic.md", "references/story.md", "references/task.md"],
        )

    def test_every_cited_reference_exists(self):
        for skill_id in PM_SKILLS:
            text = Path(self.by_id[skill_id].source_path).read_text(encoding="utf-8")
            for cited_id, cited_path in REFERENCE.findall(text):
                if "<" in cited_path:
                    continue
                with self.subTest(skill=skill_id, reference=f"{cited_id}:{cited_path}"):
                    self.assertIn(cited_path, skill_files(self.by_id[cited_id]))

    def test_pm_agent_resolves_the_new_skills_for_workflow_dispatch(self):
        config = load_runtime_config(ROOT / "config" / "ts-harness-runtime.test.yaml")
        resolved, _ = self.catalog.resolve_for_workflow_dispatch(
            config.get_agent("pm-agent"), config, tenant_id="default", skill_hints=["story-writer"]
        )
        ids = {skill.metadata.id for skill in resolved}
        self.assertTrue({"jira-ticket-format", "prd-drafter", "prd-review", "story-writer", "prd-to-jira"} <= ids)


class JiraFromPrdGateTests(unittest.TestCase):
    def test_skill_hints_resolve_and_file_jira_sits_behind_the_plan_gate(self):
        config = load_runtime_config(ROOT / "config" / "ts-harness-runtime.test.yaml")
        agent_tools = {agent.id: agent_effective_tools(agent, config) - set(agent.tool_deny) for agent in config.agents}
        payload = json.loads((ROOT / "config" / "workflows" / "jira_from_prd.json").read_text(encoding="utf-8"))
        payload = copy.deepcopy(payload)
        payload["promotion"].pop("requires_source_artifact")
        catalog = SkillCatalog(ROOT / "skills")
        catalog.load()
        _skeleton, issues = validate_workflow_definition(
            payload,
            hook_names=set(build_default_registry().names()),
            agent_ids={agent.id for agent in config.agents},
            skill_ids={meta.id for meta in catalog.index.values()},
            built_in_types=set(),
            agent_tools=agent_tools,
            publish_tools=publish_tool_names(set().union(*agent_tools.values()), config),
        )
        self.assertEqual([issue for issue in issues if issue.severity == "error"], [])
        self.assertEqual([issue.code for issue in issues if issue.code in {"ungated_publish_stage", "unrestricted_mcp_before_gate"}], [])

        stages = {stage["name"]: stage for stage in payload["stages"]}
        writers = [name for name, stage in stages.items() if "jira_write" in stage.get("allowed_external_effects", [])]
        self.assertEqual(writers, ["file_jira"])
        self.assertEqual(stages["file_jira"]["retry"]["max_retries"], 0)
        self.assertEqual(stages["approve_plan"]["on_reject"]["resume_after"], [{"step_name": "file_jira", "agent_id": "pm-agent"}])


if __name__ == "__main__":
    unittest.main()
