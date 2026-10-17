from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from ts_harness.config.schema import RoutingConfig, RoutingIntentRule
from ts_harness.gateway.channel_confirmations import (
    should_auto_propose_program,
    should_auto_propose_workflow,
)
from ts_harness.routing.intents import ChannelIntent
from ts_harness.routing.overlay import (
    merge_routing_intents,
    validate_overlay_payload,
)
from ts_harness.routing.policy import evaluate_rule_policy, effective_rule_policy
from ts_harness.workflows.routing_definition_store import RoutingDefinitionStore


class RoutingOverlayMergeTests(unittest.TestCase):
    def test_overlay_wins_on_id_and_appends_new(self):
        yaml_intents = [
            RoutingIntentRule(id="governed_prd", signals=["create a prd"], workflow_type="prd_request"),
            RoutingIntentRule(id="dev_work", signals=["fix bug"], delegate_agent="dev-agent"),
        ]
        overlay = [
            RoutingIntentRule(
                id="governed_prd",
                signals=["create a managed prd"],
                program_type="software_delivery_ui",
                entry_phase="prd",
            ),
            RoutingIntentRule(
                id="governed_rfp",
                signals=["draft rfp"],
                program_type="rfp_response",
                entry_phase="rfp",
            ),
        ]
        merged = merge_routing_intents(yaml_intents, overlay)
        self.assertEqual([rule.id for rule in merged], ["governed_prd", "dev_work", "governed_rfp"])
        self.assertEqual(merged[0].program_type, "software_delivery_ui")
        self.assertEqual(merged[0].signals, ["create a managed prd"])

    def test_validate_unknown_program_type(self):
        payload = {
            "definition_type": "channel_routing",
            "definition_version": "1.0.0",
            "intents": [
                {
                    "id": "governed_rfp",
                    "signals": ["draft rfp"],
                    "program_type": "missing_program",
                }
            ],
        }
        canonical, issues = validate_overlay_payload(
            payload,
            known_program_types={"rfp_response"},
            known_workflow_types=set(),
        )
        self.assertIsNone(canonical)
        self.assertIn("unknown_program_type", {issue["code"] for issue in issues})

    def test_program_signal_collision_is_error(self):
        payload = {
            "definition_type": "channel_routing",
            "definition_version": "1.0.0",
            "intents": [
                {"id": "a", "signals": ["create a prd"], "program_type": "software_delivery"},
                {"id": "b", "signals": ["create a prd"], "program_type": "software_delivery"},
            ],
        }
        canonical, issues = validate_overlay_payload(
            payload,
            known_program_types={"software_delivery"},
            known_workflow_types=set(),
        )
        self.assertIsNone(canonical)
        collision = next(issue for issue in issues if issue["code"] == "signal_collision")
        self.assertEqual(collision["severity"], "error")

    def test_delegate_only_signal_collision_is_warning(self):
        payload = {
            "definition_type": "channel_routing",
            "definition_version": "1.0.0",
            "intents": [
                {"id": "a", "signals": ["draft copy"], "delegate_agent": "marketing-agent"},
                {"id": "b", "signals": ["draft copy"], "delegate_agent": "sales-agent"},
            ],
        }
        canonical, issues = validate_overlay_payload(
            payload,
            known_program_types=set(),
            known_workflow_types=set(),
        )
        self.assertIsNotNone(canonical)
        collision = next(issue for issue in issues if issue["code"] == "signal_collision")
        self.assertEqual(collision["severity"], "warning")


class RoutingAutoProposeAndPolicyTests(unittest.TestCase):
    def test_auto_propose_program_requires_program_type(self):
        intent = ChannelIntent(
            kind="governed_rfp",
            program_type="rfp_response",
            entry_phase="rfp",
            summary="Draft RFP",
        )
        self.assertTrue(should_auto_propose_program(intent, {}))
        self.assertFalse(should_auto_propose_workflow(intent, {}))

    def test_program_intent_denies_workflow_start_tools(self):
        rule = RoutingIntentRule(
            id="governed_rfp",
            signals=["draft rfp"],
            program_type="rfp_response",
            entry_phase="rfp",
        )
        policy = effective_rule_policy(rule)
        ctx = MagicMock(pending_workflow={}, pending_program={})
        decision = evaluate_rule_policy(
            "propose_start_workflow",
            ctx,
            {"workflow_type": "prd_request"},
            rule=rule,
            policy=policy,
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision, "deny")


class RoutingStoreActivateTests(unittest.TestCase):
    def test_store_activate_and_reload_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RoutingDefinitionStore(Path(tmp))
            draft = store.create_draft(
                {
                    "definition_type": "channel_routing",
                    "definition_version": "1.0.0",
                    "intents": [
                        {
                            "id": "governed_rfp",
                            "signals": ["draft rfp"],
                            "program_type": "rfp_response",
                            "entry_phase": "rfp",
                        }
                    ],
                },
                actor_id="admin",
            )
            activated = store.activate(
                draft["id"],
                expected_revision=draft["revision"],
                actor_id="admin",
            )
            self.assertEqual(activated["definition_type"], "channel_routing")
            active = store.get_active("channel_routing")
            self.assertEqual(active["intents"][0]["program_type"], "rfp_response")

            baseline = RoutingConfig(
                intents=[
                    RoutingIntentRule(
                        id="governed_prd",
                        signals=["create a prd"],
                        workflow_type="prd_request",
                    )
                ]
            )
            runtime = MagicMock()
            runtime._yaml_routing_baseline = baseline
            runtime.config.routing = baseline
            from ts_harness.gateway.routing_definitions_bff import (
                reload_routing_into_runtime,
            )
            import ts_harness.workflows.routing_definition_store as store_mod

            previous = store_mod._STORE
            store_mod._STORE = store
            try:
                self.assertTrue(reload_routing_into_runtime(runtime))
            finally:
                store_mod._STORE = previous
            ids = [rule.id for rule in runtime.config.routing.intents]
            self.assertEqual(ids, ["governed_prd", "governed_rfp"])

    def test_get_returns_existing_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RoutingDefinitionStore(Path(tmp))
            created = store.create_draft(
                {
                    "definition_type": "channel_routing",
                    "definition_version": "1.0.0",
                    "intents": [
                        {
                            "id": "governed_rfp",
                            "signals": ["draft rfp"],
                            "program_type": "rfp_response",
                        }
                    ],
                },
                actor_id="admin",
            )
            runtime = MagicMock()
            runtime._yaml_routing_baseline = RoutingConfig(intents=[])
            runtime.config.routing = runtime._yaml_routing_baseline
            from ts_harness.gateway.routing_definitions_bff import (
                get_routing_definition_admin,
            )
            import ts_harness.workflows.routing_definition_store as store_mod

            previous = store_mod._STORE
            store_mod._STORE = store
            try:
                self.assertEqual(len(store.list_drafts()), 1)
                payload = get_routing_definition_admin(runtime)
                self.assertEqual(payload["draft"]["id"], created["id"])
                self.assertIn("active_revision", payload)
            finally:
                store_mod._STORE = previous


if __name__ == "__main__":
    unittest.main()
