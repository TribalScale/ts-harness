"""Tests for OPA client and its integration with the tool policy pipeline."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.config.schema import parse_runtime_config
from ts_harness.policy import opa as opa_module
from ts_harness.policy.opa import OpaClient, configure_opa, get_opa_client
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy


def _make_ctx(**kwargs) -> ToolPolicyContext:
    defaults = dict(
        tenant_id="default",
        agent_id="orchestrator",
        source="workflow",
        is_subagent=False,
        channel_ingress=False,
        sandbox_restricted=False,
    )
    defaults.update(kwargs)
    return ToolPolicyContext(**defaults)


def _minimal_config():
    return parse_runtime_config({
        "gateway": {"token": "x"},
        "agents": {
            "list": [
                {
                    "id": "orchestrator",
                    "default": True,
                    "workspace": "/w",
                    "model": "openai/gpt-4o-mini",
                    "tools": {"allow": ["read_file"]},
                }
            ]
        },
    })


class OpaClientFailClosedTests(unittest.TestCase):
    """Verify fail-closed semantics when OPA is unreachable."""

    def test_denies_when_opa_unreachable_fail_closed(self):
        client = OpaClient(opa_url="http://localhost:19999", fail_closed=True, timeout=0.1)
        result = client.evaluate_opa("read_file", _make_ctx())
        self.assertEqual(result, "deny")

    def test_allows_when_opa_unreachable_fail_open(self):
        client = OpaClient(opa_url="http://localhost:19999", fail_closed=False, timeout=0.1)
        result = client.evaluate_opa("read_file", _make_ctx())
        self.assertEqual(result, "allow")

    def test_fail_closed_is_default(self):
        client = OpaClient(opa_url="http://localhost:19999", timeout=0.1)
        self.assertTrue(client.fail_closed)
        result = client.evaluate_opa("read_file", _make_ctx())
        self.assertEqual(result, "deny")


class OpaClientResponseTests(unittest.TestCase):
    """Verify parsing of OPA JSON responses."""

    def _mock_opa_response(self, allow: bool) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"result": {"allow": allow}}
        resp.raise_for_status = MagicMock()
        return resp

    def test_allow_when_opa_returns_true(self):
        client = OpaClient(fail_closed=True)
        with patch("httpx.post", return_value=self._mock_opa_response(True)):
            result = client.evaluate_opa("read_file", _make_ctx())
        self.assertEqual(result, "allow")

    def test_deny_when_opa_returns_false(self):
        client = OpaClient(fail_closed=True)
        with patch("httpx.post", return_value=self._mock_opa_response(False)):
            result = client.evaluate_opa("read_file", _make_ctx())
        self.assertEqual(result, "deny")

    def test_deny_when_opa_returns_missing_allow(self):
        client = OpaClient(fail_closed=True)
        resp = MagicMock()
        resp.json.return_value = {"result": {}}
        resp.raise_for_status = MagicMock()
        with patch("httpx.post", return_value=resp):
            result = client.evaluate_opa("read_file", _make_ctx())
        self.assertEqual(result, "deny")

    def test_opa_receives_full_context(self):
        client = OpaClient()
        captured: dict = {}

        def fake_post(url, json, timeout):
            captured["body"] = json
            resp = MagicMock()
            resp.json.return_value = {"result": {"allow": True}}
            resp.raise_for_status = MagicMock()
            return resp

        ctx = _make_ctx(
            tenant_id="tenant-a",
            agent_id="dev-agent",
            source="slack",
            is_subagent=True,
            channel_ingress=True,
            sandbox_restricted=True,
        )
        with patch("httpx.post", side_effect=fake_post):
            client.evaluate_opa("exec", ctx)

        input_doc = captured["body"]["input"]
        self.assertEqual(input_doc["tool"], "exec")
        self.assertEqual(input_doc["tenant_id"], "tenant-a")
        self.assertTrue(input_doc["is_subagent"])
        self.assertTrue(input_doc["channel_ingress"])
        self.assertTrue(input_doc["sandbox_restricted"])

    def test_opa_url_targets_correct_policy_path(self):
        client = OpaClient(
            opa_url="http://opa.internal:8181",
            policy_path="acme/tools",
        )
        captured_urls: list[str] = []

        def fake_post(url, json, timeout):
            captured_urls.append(url)
            resp = MagicMock()
            resp.json.return_value = {"result": {"allow": True}}
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.post", side_effect=fake_post):
            client.evaluate_opa("read_file", _make_ctx())

        self.assertEqual(captured_urls[0], "http://opa.internal:8181/v1/data/acme/tools")

    def test_http_error_from_opa_triggers_fail_closed(self):
        import httpx as _httpx

        client = OpaClient(fail_closed=True)
        with patch("httpx.post", side_effect=_httpx.HTTPError("500")):
            result = client.evaluate_opa("read_file", _make_ctx())
        self.assertEqual(result, "deny")


class OpaIntegrationWithPolicyPipelineTests(unittest.TestCase):
    """OPA is wired as the final stage in evaluate_tool_policy."""

    def _mock_opa(self, allow: bool) -> OpaClient:
        client = MagicMock(spec=OpaClient)
        client.evaluate_opa.return_value = "allow" if allow else "deny"
        return client

    def test_opa_deny_blocks_otherwise_allowed_tool(self):
        config = _minimal_config()
        agent = config.get_agent("orchestrator")
        ctx = _make_ctx()
        opa = self._mock_opa(allow=False)
        decision = evaluate_tool_policy("read_file", agent, config, ctx, opa_client=opa)
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.policy_id, "opa")

    def test_opa_allow_passes_through_to_default_allow(self):
        config = _minimal_config()
        agent = config.get_agent("orchestrator")
        ctx = _make_ctx()
        opa = self._mock_opa(allow=True)
        decision = evaluate_tool_policy("read_file", agent, config, ctx, opa_client=opa)
        self.assertEqual(decision.decision, "allow")

    def test_opa_not_consulted_when_python_rules_deny_first(self):
        """Earlier Python rules (e.g. global_deny) must not reach OPA."""
        config = _minimal_config()
        agent = config.get_agent("orchestrator")
        ctx = _make_ctx()
        opa = MagicMock(spec=OpaClient)
        # 'browser' is in GLOBAL_DENY_TOOLS — should be denied before OPA is called.
        evaluate_tool_policy("browser", agent, config, ctx, opa_client=opa)
        opa.evaluate_opa.assert_not_called()

    def test_opa_not_consulted_when_none(self):
        """When opa_client is None the pipeline behaves exactly as before."""
        config = _minimal_config()
        agent = config.get_agent("orchestrator")
        ctx = _make_ctx()
        decision = evaluate_tool_policy("read_file", agent, config, ctx, opa_client=None)
        self.assertEqual(decision.decision, "allow")


class OpaModuleSingletonTests(unittest.TestCase):
    def setUp(self):
        opa_patch = patch.object(opa_module, "_opa_client", None)
        opa_patch.start()
        self.addCleanup(opa_patch.stop)

    def test_configure_opa_creates_client(self):
        client = configure_opa(opa_url="http://localhost:8181", fail_closed=True)
        self.assertIsNotNone(client)
        self.assertEqual(client.opa_url, "http://localhost:8181")
        self.assertTrue(client.fail_closed)

    def test_get_opa_client_returns_configured_client(self):
        configure_opa(opa_url="http://test-opa:8181")
        client = get_opa_client()
        self.assertIsNotNone(client)
        self.assertEqual(client.opa_url, "http://test-opa:8181")

    def test_opa_url_from_env(self):
        import os

        os.environ["TS_HARNESS_OPA_URL"] = "http://env-opa:8181"
        try:
            client = OpaClient()
            self.assertEqual(client.opa_url, "http://env-opa:8181")
        finally:
            del os.environ["TS_HARNESS_OPA_URL"]


if __name__ == "__main__":
    unittest.main()
