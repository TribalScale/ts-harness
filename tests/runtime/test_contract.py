"""Runtime contract conformance tests."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock
import hmac
import hashlib
from http.server import ThreadingHTTPServer
from pathlib import Path
import urllib.error
import urllib.request

from ts_harness.agent.provider import FakeProvider
from ts_harness.gateway import server as gateway_server
from ts_harness.gateway.server import GatewayHandler


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_CONFIG = REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
SKILLS_DIR = REPO_ROOT / "skills"


def _request(path: str, body: dict | None = None, token: str = "test-token", method: str = "POST"):
    payload = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:18799{path}", data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


PRD_MARKDOWN = """# Test PRD

## Overview
Deterministic PRD fixture used by the runtime dispatch contract test.

## Problem statement
The shared fake provider answers with a generic completion, which does not
satisfy the draft_prd stage contract.

## Goals
- Exercise the draft_prd dispatch path end to end.
- Keep the assertion deterministic without contacting a real model.

## Acceptance criteria
- Dispatch returns a succeeded status.
- The response carries a runtime task id and an output payload.
"""


def _prd_provider() -> FakeProvider:
    """Fake provider that satisfies the draft_prd stage output contract."""
    return FakeProvider(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "summary": "Drafted the PRD.",
                                    "result": {
                                        "prd_title": "Test PRD",
                                        "prd_status": "Draft",
                                        "prd_markdown": PRD_MARKDOWN,
                                    },
                                    "confidence": "high",
                                    "warnings": [],
                                    "follow_up_needed": False,
                                }
                            ),
                        }
                    }
                ]
            }
        ]
    )


def _raw_request(path: str, body: dict, headers: dict[str, str], method: str = "POST"):
    payload = json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:18799{path}", data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._session_store_dir = tempfile.TemporaryDirectory()
        session_store_path = Path(cls._session_store_dir.name) / "sessions.jsonl"
        os.environ["TS_HARNESS_CONFIG"] = str(TEST_CONFIG)
        os.environ["SKILLS_DIR"] = str(SKILLS_DIR)
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["TS_HARNESS_SESSION_STORE"] = str(session_store_path)
        from ts_harness.agent.sessions import reset_session_store
        from ts_harness.agent.transcript import reset_transcript_store

        reset_session_store()
        reset_transcript_store()
        gateway_server._RUNTIME = None
        cls.runtime = gateway_server.get_runtime()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 18799), GatewayHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        os.environ.pop("TS_HARNESS_SESSION_STORE", None)
        from ts_harness.agent.sessions import reset_session_store
        from ts_harness.agent.transcript import reset_transcript_store

        reset_session_store()
        reset_transcript_store()
        cls._session_store_dir.cleanup()

    def test_healthz_reports_components(self):
        status, body = _request("/healthz", method="GET", token="")
        self.assertEqual(status, 200)
        self.assertIn("components", body)
        self.assertIn("provider", body["components"])

    def test_healthz_reports_slack_socket_when_connected(self):
        runtime = gateway_server.get_runtime()
        original_health = runtime.slack_socket.health

        def connected_health():
            from ts_harness.gateway.channels.slack_socket import SlackSocketHealth

            return SlackSocketHealth(status="connected")

        runtime.slack_socket.health = connected_health
        try:
            status, body = _request("/healthz", method="GET", token="")
            self.assertEqual(status, 200)
            self.assertEqual(body["components"]["slack_socket"]["status"], "ok")
        finally:
            runtime.slack_socket.health = original_health

    def test_dispatch_expand_returns_proposed_tasks(self):
        runtime = gateway_server.get_runtime()
        runtime.agent._provider_override = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "summary": "expand step",
                                        "proposed_tasks": [
                                            {"task_id": "child-1", "agent_id": "dev-agent", "step_name": "implement"}
                                        ],
                                    }
                                ),
                            }
                        }
                    ]
                }
            ]
        )
        status, body = _request(
            "/workflow/dispatch",
            {
                "workflow_id": "wf-expand",
                "task_id": "expand-1",
                "agent_id": "pm-agent",
                "step_name": "expand",
                "input": {},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["proposed_tasks"][0]["task_id"], "child-1")

    def test_entrypoints_propagate_correlation_id(self):
        correlation_id = "corr-contract-test-001"
        _request(
            "/workflow/dispatch",
            {
                "task_id": "corr-task",
                "agent_id": "pm-agent",
                "step_name": "draft",
                "input": {},
                "correlation_id": correlation_id,
            },
        )
        exports = self.runtime.audit.export(tenant_id="default")
        dispatch_events = [e for e in exports if e["action_type"] == "workflow_dispatch"]
        self.assertTrue(any(e.get("correlation_id") == correlation_id for e in dispatch_events))

    def test_dispatch_success_returns_runtime_task_id(self):
        # draft_prd enforces a stage output contract, so the shared generic fake
        # provider is swapped for one that returns a complete PRD payload.
        original = self.runtime.agent._provider_override
        self.runtime.agent._provider_override = _prd_provider()
        self.addCleanup(
            setattr, self.runtime.agent, "_provider_override", original
        )
        status, body = _request(
            "/workflow/dispatch",
            {
                "workflow_id": "wf-1",
                "task_id": "task-1",
                "agent_id": "pm-agent",
                "step_name": "draft_prd",
                "input": {"topic": "test"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "succeeded", body)
        self.assertIn("runtime_task_id", body)
        self.assertIn("output", body)
        self.assertIn(
            "prd_markdown",
            (body.get("output") or {}).get("result") or {},
            body,
        )

    def test_dispatch_blocked_never_raises(self):
        status, body = _request(
            "/workflow/dispatch",
            {"agent_id": "unknown-agent", "task_id": "t1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "blocked")

    def test_auth_rejects_invalid_token(self):
        status, body = _request("/workflow/dispatch", {"task_id": "t1"}, token="bad")
        self.assertEqual(status, 401)
        self.assertIn("error", body)

    def test_missing_tenant_normalized_in_single_tenant_mode(self):
        status, body = _request(
            "/workflow/dispatch",
            {
                "task_id": "task-2",
                "agent_id": "pm-agent",
                "step_name": "draft",
                "input": {},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "succeeded")

    def test_session_message_endpoint(self):
        status, body = _request(
            "/session/message",
            {"channel": "slack", "text": "hello", "thread_id": "123"},
        )
        self.assertEqual(status, 200)
        self.assertIn("session_id", body)
        self.assertEqual(body.get("status"), "succeeded")
        self.assertIn("summary", body.get("output") or {})

    def test_web_session_message_uses_text_contract(self):
        user_text = "Report system status"
        status, body = _request(
            "/session/message",
            {
                "channel": "web",
                "source": "web",
                "text": user_text,
                "thread_id": "web:test-contract",
                "binding_key": "web:test-contract",
            },
        )

        self.assertEqual(status, 200)
        session_id = body.get("session_id")
        self.assertTrue(session_id)
        output = body.get("output")
        self.assertTrue(
            (isinstance(output, dict) and bool(output.get("summary"))) or bool(body.get("error")),
            body,
        )

        status, messages_body = _request(
            f"/admin/sessions/{session_id}/messages",
            method="GET",
        )
        self.assertEqual(status, 200)
        messages = messages_body.get("messages") or []
        self.assertTrue(
            any((item.get("content") or item.get("text")) == user_text for item in messages),
            messages,
        )

    def test_mcp_tools_endpoint_requires_auth_and_returns_tools(self):
        status, body = _request("/mcp/tools", token="test-token", method="GET")

        self.assertEqual(status, 200)
        self.assertIn("tools", body)

    def test_sandbox_deny_check_reports_blocked_command(self):
        status, body = _request("/sandbox/deny-check", {"command": "rm -rf /"})

        self.assertEqual(status, 200)
        self.assertTrue(body["denied"])

    def test_slack_events_api_webhook_normalizes_nested_event(self):
        status, body = _request(
            "/channels/slack",
            {
                "type": "event_callback",
                "event_id": "events-api-1",
                "event": {"user": "U1", "text": "hello nested", "ts": "123.456"},
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["channel"], "slack")
        self.assertIn("session_id", body)

    def test_slack_url_verification_returns_challenge(self):
        status, body = _request(
            "/channels/slack",
            {"type": "url_verification", "challenge": "challenge-token"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["challenge"], "challenge-token")

    def test_slack_webhook_normalizes_to_session_message(self):
        status, body = _request(
            "/channels/slack",
            {"event_id": "release-gate-event", "user": "U1", "text": "hello", "thread_ts": "123"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["channel"], "slack")
        self.assertIn("session_id", body)

    def test_slack_webhook_accepts_valid_platform_signature_without_bearer(self):
        os.environ["SLACK_SIGNING_SECRET"] = "signing-secret"
        payload = {"event_id": "signed-event", "user": "U1", "text": "hello"}
        raw = json.dumps(payload).encode()
        timestamp = str(int(time.time()))
        base = b"v0:" + timestamp.encode() + b":" + raw
        signature = "v0=" + hmac.new(b"signing-secret", base, hashlib.sha256).hexdigest()
        try:
            status, body = _raw_request(
                "/channels/slack",
                payload,
                {
                    "Content-Type": "application/json",
                    "X-Slack-Request-Timestamp": timestamp,
                    "X-Slack-Signature": signature,
                },
            )
        finally:
            os.environ.pop("SLACK_SIGNING_SECRET", None)

        self.assertEqual(status, 200)
        self.assertEqual(body["channel"], "slack")

    def test_required_skill_resolution_failure_blocks_dispatch(self):
        runtime = gateway_server.get_runtime()
        original_catalog = runtime.skill_catalog

        class FailingCatalog:
            def resolve_for_workflow_dispatch(self, *args, **kwargs):
                raise ValueError("skip required-skill@1.0.0: missing toolset terminal")

        runtime.skill_catalog = FailingCatalog()
        try:
            status, body = _request(
                "/workflow/dispatch",
                {"tenant_id": "default", "agent_id": "pm-agent", "task_id": "skill-fail", "input": {}},
            )
        finally:
            runtime.skill_catalog = original_catalog

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "blocked")
        self.assertIn("required-skill", body["error"])

    def test_duplicate_dispatch_returns_cached_runtime_task_id(self):
        body = {
            "workflow_id": "wf-idem",
            "task_id": "idem-task-1",
            "agent_id": "pm-agent",
            "step_name": "draft",
            "input": {},
            "correlation_id": "corr-idem-001",
        }
        _, first = _request("/workflow/dispatch", body)
        _, second = _request("/workflow/dispatch", body)
        self.assertEqual(first["runtime_task_id"], second["runtime_task_id"])
        self.assertEqual(first["status"], second["status"])

    def test_skills_doctor_endpoint_returns_diagnostics(self):
        status, body = _request(
            "/skills/doctor?agent_id=pm-agent&tenant_id=default",
            method="GET",
            token="test-token",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["agent_id"], "pm-agent")
        self.assertIn("diagnostics", body)

    def test_static_token_dispatch_ignores_body_roles(self):
        status, body = _request(
            "/workflow/dispatch",
            {
                "tenant_id": "default",
                "agent_id": "pm-agent",
                "task_id": "rbac-body-roles",
                "actor": {"actor_id": "auditor-1", "roles": ["auditor"]},
                "input": {},
            },
        )

        self.assertNotEqual(status, 403)
        self.assertNotEqual(body.get("error"), "missing permission")

    def test_cron_dispatch_delivers_scheduler_announcement(self):
        from ts_harness.gateway.channels.router import ChannelRouter

        runtime = gateway_server.get_runtime()
        sent = []

        class FakeSlackAdapter:
            def send(self, args, *, tenant_id: str):
                sent.append((args, tenant_id))
                return {"status": "sent", "channel": "slack"}

        with unittest.mock.patch.object(
            gateway_server,
            "get_channel_router",
            lambda: ChannelRouter(adapters={"slack": FakeSlackAdapter()}),
        ):
            runtime._cron_dispatch(
                {
                    "agent_id": "orchestrator",
                    "prompt": "standup summary",
                    "announce": {"channel": "slack", "to": "channel:C123"},
                }
            )

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0]["channel"], "C123")

    def test_dispatch_rejects_unknown_tenant(self):
        status, body = _request(
            "/workflow/dispatch",
            {
                "tenant_id": "not-configured",
                "agent_id": "pm-agent",
                "task_id": "bad-tenant",
                "input": {},
            },
        )

        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "tenant not configured")


if __name__ == "__main__":
    unittest.main()
