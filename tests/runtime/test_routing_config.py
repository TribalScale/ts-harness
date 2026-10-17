"""Tests for config-driven routing registry."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from ts_harness.config.schema import load_runtime_config, parse_runtime_config
from ts_harness.routing.intents import classify_channel_intent
from ts_harness.routing.matcher import match_routing_rule


BASE_CONFIG = {
    "gateway": {"token": "test-token"},
    "agents": {
        "list": [
            {
                "id": "orchestrator",
                "default": True,
                "workspace": "/workspace/orchestrator",
                "model": "openai/gpt-4o-mini",
            },
            {"id": "pm-agent", "workspace": "/workspace/pm", "model": "openai/gpt-4o-mini"},
            {"id": "dev-agent", "workspace": "/workspace/dev", "model": "openai/gpt-4o-mini"},
        ]
    },
}


class RoutingConfigTests(unittest.TestCase):
    def test_yaml_loads_routing_rules(self):
        data = {
            **BASE_CONFIG,
            "routing": {
                "default": "ad_hoc_delegate",
                "intents": [
                    {
                        "id": "governed_prd",
                        "workflow_type": "prd_request",
                        "signals": ["create prd"],
                        "exclude": ["quick"],
                    },
                    {
                        "id": "dev_work",
                        "delegate_agent": "dev-agent",
                        "signals": ["fix bug", "open pr"],
                    },
                ],
            },
        }
        config = parse_runtime_config(data)
        self.assertIsNotNone(config.routing)
        self.assertEqual(len(config.routing.intents), 2)

    def test_matcher_routes_marketing_signal_when_rule_added(self):
        data = {
            **BASE_CONFIG,
            "routing": {
                "default": "ad_hoc_delegate",
                "intents": [
                    {
                        "id": "marketing_work",
                        "delegate_agent": "pm-agent",
                        "signals": ["launch campaign"],
                    }
                ],
            },
        }
        config = parse_runtime_config(data)
        intent = match_routing_rule("Please launch campaign for Q3", config)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.kind, "marketing_work")

    def test_classify_delegates_to_matcher_when_config_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.yaml"
            payload = {
                **BASE_CONFIG,
                "routing": {
                    "default": "ad_hoc_delegate",
                    "intents": [
                        {
                            "id": "governed_prd",
                            "workflow_type": "prd_request",
                            "signals": ["product requirements document"],
                        }
                    ],
                },
            }
            path.write_text(yaml.safe_dump(payload))
            config = load_runtime_config(path)
            intent = classify_channel_intent("Need a product requirements document for billing", {}, config=config)
            self.assertEqual(intent.kind, "governed_prd")
            self.assertEqual(intent.workflow_type, "prd_request")

    def test_second_governed_intent_routes_without_code_changes(self):
        data = {
            **BASE_CONFIG,
            "routing": {
                "default": "ad_hoc_delegate",
                "intents": [
                    {
                        "id": "governed_rfp",
                        "workflow_type": "rfp_request",
                        "signals": ["create rfp", "request for proposal"],
                        "policy": {
                            "mode": "require_workflow",
                            "deny_tools": ["propose_delegate_task", "delegate_task"],
                        },
                    },
                ],
            },
        }
        config = parse_runtime_config(data)
        intent = match_routing_rule("Please create rfp for the enterprise deal", config)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.kind, "governed_rfp")
        self.assertEqual(intent.workflow_type, "rfp_request")
        rule = next(item for item in config.routing.intents if item.id == "governed_rfp")
        self.assertEqual(rule.policy.mode, "require_workflow")
        self.assertIn("delegate_task", rule.policy.deny_tools)


if __name__ == "__main__":
    unittest.main()
