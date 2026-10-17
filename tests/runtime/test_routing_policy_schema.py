"""Tests for routing policy schema parsing and validation."""

from __future__ import annotations

import unittest

from ts_harness.config.schema import parse_runtime_config


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
        ]
    },
}


class RoutingPolicySchemaTests(unittest.TestCase):
    def test_yaml_loads_policy_fields(self):
        data = {
            **BASE_CONFIG,
            "routing": {
                "default": "ad_hoc_delegate",
                "intents": [
                    {
                        "id": "governed_prd",
                        "workflow_type": "prd_request",
                        "signals": ["create prd"],
                        "orchestrator_hint": "Use propose_start_workflow(prd_request).",
                        "policy": {
                            "mode": "require_workflow",
                            "deny_tools": ["propose_delegate_task", "delegate_task"],
                            "deny_start_workflow_types": ["governed_delivery"],
                        },
                    },
                ],
            },
        }
        config = parse_runtime_config(data)
        rule = config.routing.intents[0]
        self.assertEqual(rule.orchestrator_hint, "Use propose_start_workflow(prd_request).")
        self.assertIsNotNone(rule.policy)
        assert rule.policy is not None
        self.assertEqual(rule.policy.mode, "require_workflow")
        self.assertIn("delegate_task", rule.policy.deny_tools)

    def test_invalid_deny_start_workflow_type_raises(self):
        data = {
            **BASE_CONFIG,
            "routing": {
                "intents": [
                    {
                        "id": "bad_rule",
                        "workflow_type": "prd_request",
                        "policy": {
                            "mode": "require_workflow",
                            "deny_start_workflow_types": ["nonexistent_workflow"],
                        },
                    },
                ],
            },
        }
        with self.assertRaises(ValueError):
            parse_runtime_config(data)

    def test_invalid_policy_mode_raises(self):
        data = {
            **BASE_CONFIG,
            "routing": {
                "intents": [
                    {
                        "id": "bad_rule",
                        "workflow_type": "prd_request",
                        "policy": {"mode": "invalid_mode"},
                    },
                ],
            },
        }
        with self.assertRaises(ValueError):
            parse_runtime_config(data)


if __name__ == "__main__":
    unittest.main()
