import os
import unittest
import importlib.util
import urllib.error
from pathlib import Path
from unittest.mock import patch

from ts_harness.gateway.release_gates import (
    GateSpec,
    ReleaseGateRunner,
    gates_for_profile,
    load_gate_manifest,
    load_required_gates,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("release_gates_script", REPO_ROOT / "scripts" / "release-gates.py")
release_gates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_gates)


class ReleaseGateTests(unittest.TestCase):
    def test_release_gate_runner_reports_all_required_gates(self):
        runner = ReleaseGateRunner(
            checks={
                "runtime_health": lambda: True,
                "memory_smoke": lambda: True,
                "workflow_dispatch": lambda: True,
                "mcp_discovery": lambda: True,
                "sandbox_denial": lambda: True,
                "subagent_delegation": lambda: True,
                "cron_heartbeat": lambda: True,
                "tenant_isolation": lambda: True,
                "rbac_identity": lambda: True,
                "audit_export": lambda: True,
                "secrets_redaction": lambda: True,
                "agent_harness_security": lambda: True,
                "agent_harness_quality": lambda: True,
                "budget_hard_stop": lambda: True,
                "channel_chat": lambda: True,
                "workflow_record_create": lambda: True,
                "workflow_promotion_smoke": lambda: True,
                "workflow_hook_smoke": lambda: True,
                "prd_request_smoke": lambda: True,
                "continue_bridge_smoke": lambda: True,
                "continue_delivery_smoke": lambda: True,
                "intent_routing_smoke": lambda: True,
                "workflow_runner_import_smoke": lambda: True,
                "skeleton_hooks_registered": lambda: True,
                "delivery_program": lambda: True,
                "work_orders_delivery_smoke": lambda: True,
            },
            profile="ci",
        )

        result = runner.run()

        self.assertTrue(result.passed)
        self.assertEqual(set(result.results), set(load_required_gates()))

    def test_agent_harness_security_gate_is_invoked_and_missing_without_a_check(self):
        calls: list[str] = []
        runner = ReleaseGateRunner(
            checks={"agent_harness_security": lambda: calls.append("ran") or True},
            manifest=[GateSpec("agent_harness_security", tier="required")],
            profile="ci",
        )
        self.assertTrue(runner.run().passed)
        self.assertEqual(calls, ["ran"])

        missing = ReleaseGateRunner(
            checks={},
            manifest=[GateSpec("agent_harness_security", tier="required")],
            profile="ci",
        )
        self.assertEqual(missing.run().results["agent_harness_security"].status, "missing")
        self.assertTrue(release_gates.agent_harness_security())

    def test_agent_harness_quality_gate_is_invoked_and_missing_without_a_check(self):
        calls: list[str] = []
        runner = ReleaseGateRunner(
            checks={"agent_harness_quality": lambda: calls.append("ran") or True},
            manifest=[GateSpec("agent_harness_quality", tier="required")],
            profile="ci",
        )
        self.assertTrue(runner.run().passed)
        self.assertEqual(calls, ["ran"])

        missing = ReleaseGateRunner(
            checks={},
            manifest=[GateSpec("agent_harness_quality", tier="required")],
            profile="ci",
        )
        self.assertEqual(missing.run().results["agent_harness_quality"].status, "missing")
        self.assertTrue(release_gates.agent_harness_quality())

    def test_release_gate_runner_fails_closed_when_gate_missing_or_failed(self):
        runner = ReleaseGateRunner(checks={"runtime_health": lambda: True, "memory_smoke": lambda: False})

        result = runner.run()

        self.assertFalse(result.passed)
        self.assertEqual(result.results["memory_smoke"].status, "failed")
        self.assertEqual(result.results["workflow_dispatch"].status, "missing")

    def test_live_release_gates_require_ok_health_and_successful_workflow(self):
        with patch.object(release_gates, "request", return_value={"status": "degraded", "components": {}}):
            self.assertFalse(release_gates.runtime_health())

        with patch.object(release_gates, "request", return_value={"status": "ok", "components": {
            "provider": {"status": "ok"},
            "memory_api": {"status": "ok"},
            "scheduler": {"status": "ok"},
        }}):
            self.assertTrue(release_gates.runtime_health())

        with patch.object(release_gates, "TOKEN", "test-token"), patch.object(
            release_gates, "request", return_value={"status": "blocked", "error": "no provider"}
        ):
            self.assertFalse(release_gates.workflow_dispatch())

    def test_live_release_gates_require_registered_mcp_tools(self):
        with patch.object(release_gates, "TOKEN", "test-token"), patch.object(
            release_gates,
            "request",
            return_value={"tools": [{"name": "search"}]},
        ):
            self.assertFalse(release_gates.mcp_discovery())

        with patch.object(release_gates, "TOKEN", "test-token"), patch.object(
            release_gates,
            "request",
            return_value={"tools": [{"name": "mcp-atlassian-search", "server": "atlassian"}]},
        ):
            self.assertTrue(release_gates.mcp_discovery())

    def test_live_release_gates_require_scheduler_trigger(self):
        with patch.object(release_gates, "TOKEN", "test-token"), patch.object(
            release_gates,
            "request",
            side_effect=[
                {"status": "ok", "components": {"scheduler": {"status": "ok"}}},
                {"status": "ok"},
            ],
        ):
            self.assertFalse(release_gates.cron_heartbeat())

        with patch.object(release_gates, "TOKEN", "test-token"), patch.object(
            release_gates,
            "request",
            side_effect=[
                {"status": "ok", "components": {"scheduler": {"status": "ok"}}},
                {"status": "ok", "scheduler": "triggered"},
            ],
        ):
            self.assertTrue(release_gates.cron_heartbeat())

    def test_live_release_gates_call_runtime_endpoints_for_integration_checks(self):
        calls = []

        def fake_request(method, path, body=None, auth=False, **kwargs):
            calls.append((method, path, body, auth))
            if path == "/mcp/tools":
                return {"tools": [{"name": "mcp-atlassian-search", "server": "atlassian"}]}
            if path == "/cron/trigger":
                return {"status": "ok", "scheduler": "triggered"}
            if path == "/healthz":
                return {"status": "ok", "components": {"scheduler": {"status": "ok"}}}
            if path == "/sandbox/deny-check":
                return {"denied": True}
            if path == "/channels/slack/send-test":
                return {"status": "sent"}
            if path == "/workflow/dispatch":
                return {"status": "succeeded", "runtime_task_id": "rt"}
            return {"status": "ok"}

        with patch.object(release_gates, "TOKEN", "test-token"), patch.dict(
            os.environ, {"SLACK_RELEASE_GATE_CHANNEL": "C123"}
        ), patch.object(release_gates, "request", side_effect=fake_request):
            self.assertTrue(release_gates.mcp_discovery())
            self.assertTrue(release_gates.cron_heartbeat())
            self.assertTrue(release_gates.sandbox_denial())
            self.assertTrue(release_gates.subagent_delegation())
            self.assertTrue(release_gates.session_message())

        self.assertIn(("GET", "/mcp/tools", None, True), calls)
        self.assertIn(("POST", "/cron/trigger", {"tenant_id": "default", "source": "release-gate"}, True), calls)
        self.assertIn(("POST", "/sandbox/deny-check", {"command": "rm -rf /"}, True), calls)
        self.assertTrue(any(call[1] == "/channels/slack/send-test" for call in calls))
        self.assertTrue(any(call[1] == "/workflow/dispatch" and call[3] for call in calls))

    def test_session_message_requires_slack_socket_health_when_app_token_set(self):
        def fake_request(method, path, body=None, auth=False, **kwargs):
            if path == "/channels/slack/send-test":
                return {"status": "sent"}
            if path == "/healthz":
                return {"status": "ok", "components": {"slack_socket": {"status": "degraded"}}}
            return {"status": "ok"}

        with patch.object(release_gates, "TOKEN", "test-token"), patch.dict(
            os.environ,
            {"SLACK_RELEASE_GATE_CHANNEL": "C123", "SLACK_APP_TOKEN": "xapp-1"},
            clear=False,
        ), patch.object(release_gates, "request", side_effect=fake_request):
            self.assertFalse(release_gates.session_message())

        def healthy_request(method, path, body=None, auth=False):
            if path == "/channels/slack/send-test":
                return {"status": "sent"}
            if path == "/healthz":
                return {"status": "ok", "components": {"slack_socket": {"status": "ok"}}}
            return {"status": "ok"}

        with patch.object(release_gates, "TOKEN", "test-token"), patch.dict(
            os.environ,
            {"SLACK_RELEASE_GATE_CHANNEL": "C123", "SLACK_APP_TOKEN": "xapp-1"},
            clear=False,
        ), patch.object(release_gates, "request", side_effect=healthy_request):
            self.assertTrue(release_gates.session_message())

    def test_subagent_delegation_accepts_blocked_dispatch_with_runtime_task_id(self):
        with patch.object(release_gates, "TOKEN", "test-token"), patch.object(
            release_gates,
            "request",
            return_value={"status": "blocked", "runtime_task_id": "rt-1", "error": "max iterations exceeded"},
        ):
            self.assertTrue(release_gates.subagent_delegation())

    def test_wait_for_runtime_ready_retries_until_health_ok(self):
        health = {
            "status": "ok",
            "components": {
                "provider": {"status": "ok"},
                "memory_api": {"status": "ok"},
                "scheduler": {"status": "ok"},
            },
        }
        with patch.object(release_gates, "request", side_effect=[urllib.error.URLError("refused"), health]):
            self.assertTrue(release_gates.wait_for_runtime_ready(timeout_seconds=1, interval_seconds=0.01))

    def test_workflow_runner_import_smoke_uses_installed_layout(self):
        self.assertTrue(release_gates.workflow_runner_import_smoke())

    def test_delivery_program_gate_loads_default_definition_and_pinned_workflows(self):
        source = Path(release_gates.__file__).read_text(encoding="utf-8")
        self.assertNotIn("PHASE_ORDER", source)
        self.assertNotIn("PHASE_SKELETON", source)
        self.assertTrue(release_gates.delivery_program())

    def test_ci_profile_excludes_optional_gates(self):
        manifest = load_gate_manifest()
        ci_gates = {spec.id for spec in gates_for_profile("ci", manifest)}
        self.assertIn("runtime_health", ci_gates)
        self.assertNotIn("slack_round_trip", ci_gates)
        self.assertNotIn("agent_output_judge", ci_gates)
        self.assertNotIn("google_gmail_send_smoke", ci_gates)

    def test_optional_gate_skipped_when_env_missing(self):
        manifest = [
            GateSpec("runtime_health", tier="required"),
            GateSpec("slack_round_trip", tier="optional", requires_env=("SLACK_RELEASE_GATE_CHANNEL",)),
        ]
        runner = ReleaseGateRunner(
            {"runtime_health": lambda: True, "slack_round_trip": lambda: False},
            profile="full",
            manifest=manifest,
        )
        with patch.dict(os.environ, {}, clear=True):
            result = runner.run()
        self.assertTrue(result.passed)
        self.assertEqual(result.results["slack_round_trip"].status, "skipped")
        self.assertEqual(result.results["runtime_health"].status, "passed")

    def test_live_profile_fails_when_the_model_key_is_missing(self):
        env = os.environ.copy()
        env.pop("OPENAI_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            report = ReleaseGateRunner(checks={}, profile="live").run()
        self.assertFalse(report.passed)
        self.assertEqual(
            {name: result.status for name, result in report.results.items()},
            {"agent_redteam": "failed", "agent_output_judge": "failed"},
        )

    def test_ci_profile_excludes_live_model_gates(self):
        ids = {spec.id for spec in gates_for_profile("ci")}
        self.assertNotIn("agent_redteam", ids)
        self.assertNotIn("agent_output_judge", ids)

    def test_full_profile_still_skips_live_gates_without_a_key(self):
        env = os.environ.copy()
        env.pop("OPENAI_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            report = ReleaseGateRunner(checks={}, profile="full").run()
        self.assertEqual(report.results["agent_redteam"].status, "skipped")
        self.assertEqual(report.results["agent_output_judge"].status, "skipped")

    def test_live_main_does_not_wait_for_a_runtime(self):
        env = os.environ.copy()
        env["RELEASE_GATES_PROFILE"] = "live"
        env.pop("OPENAI_API_KEY", None)
        with patch.object(release_gates, "wait_for_runtime_ready", side_effect=AssertionError("waited")):
            with patch.dict(os.environ, env, clear=True):
                code = release_gates.main()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
