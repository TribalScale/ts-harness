import os
import unittest
from pathlib import Path

from ts_harness.config.schema import load_runtime_config, parse_runtime_config


REPO_ROOT = Path(__file__).resolve().parents[2]
NINE_AGENT_IDS = (
    "orchestrator",
    "dev-agent",
    "ops-agent",
    "qa-agent",
    "analyst-agent",
    "pm-agent",
    "comms-agent",
    "marketing-agent",
    "sales-agent",
)


class ConfigTests(unittest.TestCase):
    def test_full_template_validates_nine_agent_topology(self):
        os.environ.setdefault("GATEWAY_TOKEN", "test-token")
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.yaml.tmpl")
        agent_ids = {agent.id for agent in config.agents}
        for agent_id in NINE_AGENT_IDS:
            self.assertIn(agent_id, agent_ids)
        self.assertEqual(config.default_agent().id, "orchestrator")

    def test_config_validates_topology(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        self.assertEqual(config.default_agent().id, "orchestrator")
        self.assertEqual(len(config.agents), 8)

    def test_missing_default_orchestrator_fails(self):
        data = {
            "gateway": {"token": "x"},
            "agents": {"list": [{"id": "dev-agent", "workspace": "/w", "model": "openai/gpt-4o-mini"}]},
        }
        with self.assertRaises(ValueError):
            parse_runtime_config(data)

    def test_conflicting_allow_deny_fails(self):
        data = {
            "gateway": {"token": "x"},
            "agents": {
                "list": [
                    {
                        "id": "orchestrator",
                        "default": True,
                        "workspace": "/w",
                        "model": "openai/gpt-4o-mini",
                        "tools": {"allow": ["exec"], "deny": ["exec"]},
                    }
                ]
            },
        }
        with self.assertRaises(ValueError):
            parse_runtime_config(data)

    def test_gate_agent_without_allowlist_is_rejected(self):
        data = _config_with_agents({"id": "review-agent", "model": "openai/gpt-6-astra"})
        with self.assertRaisesRegex(ValueError, "model_allowlist.review-agent"):
            parse_runtime_config(data)

    def test_gate_agent_on_mini_is_rejected_even_if_allowlisted(self):
        data = _config_with_agents(
            {"id": "qa-agent", "model": "openai/gpt-4o-mini"},
            allowlist={"qa-agent": ["openai/gpt-4o-mini"]},
        )
        with self.assertRaisesRegex(ValueError, "gpt-4o-mini"):
            parse_runtime_config(data)

    def test_shipped_template_pins_each_gate_agent_to_its_allowlisted_model(self):
        os.environ.setdefault("GATEWAY_TOKEN", "test-token")
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.yaml.tmpl")
        for agent_id, model in (("review-agent", "openai/gpt-6-astra"), ("qa-agent", "openai/gpt-6.1-sol")):
            self.assertEqual(config.get_agent(agent_id).model, model)
            self.assertEqual(config.policy.model_allowlist[agent_id], [model])

    def test_shipped_template_models_and_effort(self):
        os.environ.setdefault("GATEWAY_TOKEN", "test-token")
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.yaml.tmpl")
        expected = {
            "pm-agent": ("anthropic/claude-opus-5-5", "high"),
            "builder-agent": ("anthropic/claude-opus-5-5", "high"),
            "dev-agent": ("anthropic/claude-opus-5-5", "high"),
            "orchestrator": ("anthropic/claude-sonnet-5-5", "medium"),
            "sales-agent": ("anthropic/claude-sonnet-5-5", "medium"),
            "ops-agent": ("openai/gpt-6.1-sol", None),
            "analyst-agent": ("openai/gpt-6.1-sol", None),
            "marketing-agent": ("openai/gpt-6.1-sol", None),
            "comms-agent": ("openai/gpt-6-luna", None),
        }
        for agent_id, (model, effort) in expected.items():
            agent = config.get_agent(agent_id)
            self.assertEqual((agent.model, agent.effort), (model, effort), agent_id)
        heartbeat_models = {agent.heartbeat.model for agent in config.agents if agent.heartbeat}
        self.assertEqual(heartbeat_models, {"anthropic/claude-haiku-5-5"})

    def test_unknown_effort_is_rejected(self):
        data = _config_with_agents({"id": "pm-agent", "model": "anthropic/claude-opus-5-5", "effort": "extreme"})
        with self.assertRaisesRegex(ValueError, "effort must be one of"):
            parse_runtime_config(data)


def _config_with_agents(*agents: dict, allowlist: dict | None = None) -> dict:
    listing = [
        {
            "id": "orchestrator",
            "default": True,
            "workspace": "/w",
            "model": "openai/gpt-4o",
        },
        *agents,
    ]
    data: dict = {"gateway": {"token": "x"}, "agents": {"list": listing}}
    if allowlist is not None:
        data["policy"] = {"model_allowlist": allowlist}
    return data


if __name__ == "__main__":
    unittest.main()
