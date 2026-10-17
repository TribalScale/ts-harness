"""Tests for Slack user email resolution."""

from __future__ import annotations

import unittest

from ts_harness.config.schema import AgentConfig, IdentityConfig, RuntimeConfig, GatewayConfig, TenantConfig
from ts_harness.gateway.channels.slack_users import (
    channel_requester_actor_id,
    resolve_slack_user_email,
    slack_user_id_from_actor,
)


class SlackUserResolverTests(unittest.TestCase):
    def _config(self) -> RuntimeConfig:
        return RuntimeConfig(
            gateway=GatewayConfig(token="t"),
            tenants=[TenantConfig(id="default", display_name="Default")],
            agents=[
                AgentConfig(
                    id="orchestrator",
                    workspace="/w",
                    model="m",
                    default=True,
                )
            ],
            identity=IdentityConfig(slack_user_emails={"U123": "user@corp.com"}),
        )

    def test_config_fallback_map(self):
        config = self._config()
        email = resolve_slack_user_email("U123", config=config, bot_token="")
        self.assertEqual(email, "user@corp.com")

    def test_slack_prefix_stripped(self):
        config = self._config()
        email = resolve_slack_user_email("slack:U123", config=config, bot_token="")
        self.assertEqual(email, "user@corp.com")

    def test_channel_requester_actor_id_prefers_session_slack_user(self):
        actor = channel_requester_actor_id(
            {"slack_user_id": "U123ABC01"},
            "orchestrator",
        )
        self.assertEqual(actor, "slack:U123ABC01")

    def test_slack_user_id_from_actor(self):
        self.assertEqual(slack_user_id_from_actor("slack:U123ABC01"), "U123ABC01")
        self.assertIsNone(slack_user_id_from_actor("C123CHANNEL"))
        self.assertIsNone(slack_user_id_from_actor("orchestrator"))


if __name__ == "__main__":
    unittest.main()
