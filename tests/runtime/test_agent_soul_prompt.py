from __future__ import annotations

import unittest
from pathlib import Path

from ts_harness.agent.loop import EnterpriseAgent
from ts_harness.agent.provider import FakeProvider
from ts_harness.config.schema import load_runtime_config
from ts_harness.tools.registry import build_default_registry


REPO_ROOT = Path(__file__).resolve().parents[2]


class AgentSoulPromptTests(unittest.TestCase):
    def setUp(self):
        self.config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        self.agent = EnterpriseAgent(
            self.config,
            build_default_registry(),
            provider=FakeProvider([]),
        )
        self.marketing = self.config.get_agent("pm-agent")

    def _prompts(self, soul: str | None = None) -> tuple[str, str, str]:
        if soul is not None:
            self.marketing.soul = soul
        channel = self.agent._build_channel_system_prompt(self.marketing, "skill text")
        workflow = self.agent._build_workflow_system_prompt(
            self.marketing,
            {"skill_hints": [], "required_artifacts": []},
        )
        contract = self.agent._build_workflow_system_prompt(
            self.marketing,
            {"skill_hints": [], "required_artifacts": [], "contract": {"output_schema": {}}},
        )
        return channel, workflow, contract

    def test_catalog_soul_is_in_both_prompts(self):
        self.assertIn("User-centric", self.marketing.soul or "")
        channel, workflow, contract = self._prompts()
        for prompt in (channel, workflow, contract):
            self.assertIn("User-centric", prompt)
            self.assertNotIn("BEGIN GENERATED", prompt)
            self.assertNotIn("Durable delivery graphs", prompt)

    def test_overlay_soul_replaces_catalog_text_and_is_capped(self):
        channel, workflow, contract = self._prompts("A" * 500 + "TAIL")
        for prompt in (channel, workflow, contract):
            self.assertIn("A" * 500, prompt)
            self.assertNotIn("TAIL", prompt)
            self.assertNotIn("User-centric", prompt)
            self.assertNotIn("BEGIN GENERATED", prompt)


if __name__ == "__main__":
    unittest.main()
