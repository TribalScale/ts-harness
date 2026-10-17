from __future__ import annotations

import unittest
from pathlib import Path

from ts_harness.config.agents_overlay import (
    apply_agents_to_config,
    merge_agents,
    overlay_object_to_agent,
    validate_agents_overlay,
)
from ts_harness.config.schema import load_runtime_config
from ts_harness.skills.catalog import SkillCatalog


REPO_ROOT = Path(__file__).resolve().parents[2]


def _gate(agent_id: str, model: str) -> dict:
    return {
        "definition_type": "agents",
        "definition_version": "1.0.0",
        "agents": {
            agent_id: {
                "workspace": f"/workspace/{agent_id}",
                "model": model,
                "skills": ["ops"],
                "toolsets": ["core-read"],
                "default": False,
            }
        },
    }


def _legal(**overrides):
    obj = {
        "workspace": "/workspace/legal-agent",
        "model": "openai/gpt-4o",
        "display_name": "Legal",
        "soul": "Cautious counsel.",
        "skills": ["ops"],
        "toolsets": ["core-read"],
        "tools": {"allow": ["recall_memory", "read_file"], "deny": ["exec"]},
        "default": False,
        "delegatable": True,
    }
    obj.update(overrides)
    return {
        "definition_type": "agents",
        "definition_version": "1.0.0",
        "agents": {"legal-agent": obj},
    }


class AgentsOverlayTests(unittest.TestCase):
    def setUp(self):
        self.config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        self.yaml_ids = {a.id for a in self.config.agents}
        self.catalog = SkillCatalog(REPO_ROOT / "skills")
        self.catalog.load()

    def test_validate_rejects_default_true(self):
        payload = _legal(default=True)
        canonical, issues = validate_agents_overlay(
            payload,
            yaml_agent_ids=self.yaml_ids,
            toolset_keys=set(self.config.toolsets),
            catalog=self.catalog,
        )
        self.assertIsNone(canonical)
        self.assertIn("default_not_allowed", {i["code"] for i in issues})

    def test_validate_rejects_unknown_toolset(self):
        payload = _legal(toolsets=["not-a-toolset"])
        _, issues = validate_agents_overlay(
            payload,
            yaml_agent_ids=self.yaml_ids,
            toolset_keys=set(self.config.toolsets),
            catalog=self.catalog,
        )
        self.assertIn("unknown_toolset", {i["code"] for i in issues})

    def test_validate_rejects_bad_id(self):
        payload = _legal()
        payload["agents"] = {"Legal_Agent": payload["agents"]["legal-agent"]}
        _, issues = validate_agents_overlay(
            payload,
            yaml_agent_ids=self.yaml_ids,
            toolset_keys=set(self.config.toolsets),
            catalog=self.catalog,
        )
        self.assertIn("invalid_agent_id", {i["code"] for i in issues})

    def test_validate_rejects_unresolvable_skill(self):
        payload = _legal(skills=["definitely-not-a-skill-id"])
        _, issues = validate_agents_overlay(
            payload,
            yaml_agent_ids=self.yaml_ids,
            toolset_keys=set(self.config.toolsets),
            catalog=self.catalog,
        )
        self.assertTrue(any(i["code"] == "unresolvable_skill" for i in issues))

    def test_validate_rejects_empty_skills_for_overlay_only(self):
        payload = _legal(skills=[])
        _, issues = validate_agents_overlay(
            payload,
            yaml_agent_ids=self.yaml_ids,
            toolset_keys=set(self.config.toolsets),
            catalog=self.catalog,
        )
        self.assertIn("skills_required", {i["code"] for i in issues})

    def test_overlay_cannot_retarget_review_agent(self):
        canonical, issues = validate_agents_overlay(
            _gate("review-agent", "openai/gpt-4o"),
            yaml_agent_ids=self.yaml_ids,
            toolset_keys=set(self.config.toolsets),
            catalog=self.catalog,
            model_allowlist={"review-agent": ["openai/gpt-6-astra"]},
        )
        self.assertIsNone(canonical)
        self.assertIn("gate_model_not_allowed", {issue["code"] for issue in issues})

    def test_overlay_can_keep_the_allowlisted_model(self):
        canonical, issues = validate_agents_overlay(
            _gate("qa-agent", "openai/gpt-6-astra"),
            yaml_agent_ids=self.yaml_ids,
            toolset_keys=set(self.config.toolsets),
            catalog=self.catalog,
            model_allowlist={"qa-agent": ["openai/gpt-6-astra"]},
        )
        self.assertIsNotNone(canonical)
        self.assertFalse(any(issue["code"] == "gate_model_not_allowed" for issue in issues))

    def test_overlay_specialist_model_is_not_gated(self):
        canonical, issues = validate_agents_overlay(
            _legal(model="openai/gpt-4o"),
            yaml_agent_ids=self.yaml_ids,
            toolset_keys=set(self.config.toolsets),
            catalog=self.catalog,
            model_allowlist={"review-agent": ["openai/gpt-6-astra"]},
        )
        self.assertIsNotNone(canonical)
        self.assertFalse(any(issue["code"] == "gate_model_not_allowed" for issue in issues))

    def test_merge_yaml_inherited_overlay_replaces_and_appends(self):
        baseline = list(self.config.agents)
        overlay = _legal()
        overlay["agents"]["orchestrator"] = {
            "workspace": "/workspace/orchestrator",
            "model": "openai/gpt-4o-mini",
            "skills": ["ops"],
            "toolsets": ["core-read"],
            "default": False,
        }
        merged = merge_agents(baseline, overlay)
        ids = [a.id for a in merged]
        self.assertIn("legal-agent", ids)
        self.assertIn("orchestrator", ids)
        self.assertEqual(merged[ids.index("orchestrator")].skills, ["ops"])
        restored = merge_agents(
            baseline,
            {
                "definition_type": "agents",
                "definition_version": "1.0.0",
                "agents": {},
            },
        )
        self.assertEqual([a.id for a in restored], [a.id for a in baseline])
        self.assertNotIn("legal-agent", [a.id for a in restored])

    def test_merge_preserves_yaml_default_and_subagent_allow(self):
        baseline = list(self.config.agents)
        orchestrator = next(agent for agent in baseline if agent.default)
        overlay = {
            "definition_type": "agents",
            "definition_version": "1.0.0",
            "agents": {
                "orchestrator": {
                    "workspace": "/workspace/orchestrator",
                    "model": "openai/gpt-4o-mini",
                    "skills": ["ops"],
                    "toolsets": ["core-read"],
                    "default": False,
                }
            },
        }
        merged = merge_agents(baseline, overlay)
        replaced = next(agent for agent in merged if agent.id == "orchestrator")
        self.assertTrue(replaced.default)
        self.assertEqual(replaced.subagent_allow, orchestrator.subagent_allow)
        self.assertEqual(replaced.model, "openai/gpt-4o-mini")
        apply_agents_to_config(self.config, overlay, baseline=baseline)
        self.assertEqual(self.config.default_agent().id, "orchestrator")

    def test_overlay_object_keeps_soul_and_ignores_delegatable(self):
        agent = overlay_object_to_agent(
            "legal-agent",
            _legal()["agents"]["legal-agent"],
        )
        self.assertEqual(agent.id, "legal-agent")
        self.assertFalse(agent.default)
        self.assertEqual(agent.tool_allow, ["recall_memory", "read_file"])
        self.assertEqual(agent.soul, "Cautious counsel.")
        self.assertFalse(hasattr(agent, "delegatable"))

    def test_overlay_can_replace_google_account_and_keeps_deny(self):
        from ts_harness.config.schema import AgentConfig

        marketing = AgentConfig(
            id="marketing-agent",
            workspace="/workspace/marketing-agent",
            model="openai/gpt-4o",
            skills=["memory-api"],
            toolsets=["core-read", "channel", "google-workspace-docs"],
            tool_allow=["read_file"],
            tool_deny=["google_workspace_gmail_send"],
            connector_bindings={"google-workspace": "marketing"},
            heartbeat=None,
        )
        overlay = {
            "agents": {
                "marketing-agent": {
                    "workspace": "/workspace/marketing-agent",
                    "model": "openai/gpt-4o",
                    "skills": ["memory-api"],
                    "toolsets": ["core-read", "channel", "google-workspace-docs"],
                    "tools": {
                        "allow": ["read_file"],
                        "deny": ["google_workspace_gmail_send"],
                    },
                    "connector_bindings": {"google-workspace": "sales"},
                    "default": False,
                }
            }
        }
        merged = merge_agents([marketing], overlay)
        agent = next(item for item in merged if item.id == "marketing-agent")
        self.assertEqual(agent.connector_bindings["google-workspace"], "sales")
        self.assertIn("google_workspace_gmail_send", agent.tool_deny)
        self.assertIsNone(agent.heartbeat)

    def test_fill_agent_souls_uses_catalog_unless_set_or_cleared(self):
        from ts_harness.config.agents_overlay import fill_agent_souls
        from ts_harness.config.schema import AgentConfig

        souls = {"marketing-agent": "Catalog voice."}
        inherited = AgentConfig(id="marketing-agent", workspace="/w", model="m")
        fill_agent_souls([inherited], souls)
        self.assertEqual(inherited.soul, "Catalog voice.")
        cleared = AgentConfig(id="marketing-agent", workspace="/w", model="m", soul="")
        fill_agent_souls([cleared], souls)
        self.assertEqual(cleared.soul, "")
        replaced = AgentConfig(
            id="marketing-agent", workspace="/w", model="m", soul="Overlay voice."
        )
        fill_agent_souls([replaced], souls)
        self.assertEqual(replaced.soul, "Overlay voice.")


if __name__ == "__main__":
    unittest.main()
