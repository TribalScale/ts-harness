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

    def test_shipped_template_pins_gate_agents_to_gpt_6_astra(self):
        os.environ.setdefault("GATEWAY_TOKEN", "test-token")
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.yaml.tmpl")
        for agent_id in ("review-agent", "qa-agent"):
            self.assertEqual(config.get_agent(agent_id).model, "openai/gpt-6-astra")
            self.assertEqual(config.policy.model_allowlist[agent_id], ["openai/gpt-6-astra"])


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
