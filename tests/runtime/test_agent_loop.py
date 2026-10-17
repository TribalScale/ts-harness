import json
import unittest
from pathlib import Path
from unittest.mock import patch

from ts_harness.agent.loop import EnterpriseAgent
from ts_harness.agent.provider import (
    AnthropicProvider,
    FakeProvider,
    OpenAICompatibleProvider,
    ProviderEndpoint,
    build_provider,
)
from ts_harness.config.schema import load_runtime_config
from ts_harness.tools.registry import build_default_registry

REPO_ROOT = Path(__file__).resolve().parents[2]


class AgentLoopTests(unittest.TestCase):
    def test_agent_loop_completes_with_fake_provider(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"summary": "done", "artifacts": {}}),
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={"task_id": "1", "step_name": "draft", "input": {}},
        )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.output["summary"], "done")

    def test_workflow_dispatch_links_tools_to_the_workflow(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "session_status",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"summary": "done", "artifacts": {}}),
                            }
                        }
                    ]
                },
            ]
        )
        registry = build_default_registry()
        seen: list[str | None] = []
        original = registry.dispatch

        def spy(name, args, *, context):
            seen.append(context.linked_workflow_id)
            return original(name, args, context=context)

        registry.dispatch = spy
        agent = EnterpriseAgent(config, registry, provider=provider)
        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-harness",
                "task_id": "1",
                "step_name": "draft",
                "input": {},
            },
        )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(seen, ["wf-harness"])

    def test_provider_builder_uses_anthropic_provider_for_anthropic_models(self):
        provider = build_provider("anthropic/claude-sonnet-4-6")

        self.assertIsInstance(provider, AnthropicProvider)

    def test_tool_call_then_final_answer_succeeds(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "session_status",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"summary": "completed after tool", "artifacts": {}}),
                            }
                        }
                    ]
                },
            ]
        )
        audits = []

        def on_audit(event):
            audits.append(event)

        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
            on_audit=on_audit,
        )
        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={"task_id": "tool-loop", "step_name": "draft", "input": {}},
            correlation_id="tool-loop-corr",
        )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertTrue(any(e.get("event") == "agent_run_completed" for e in audits))

    def test_workflow_dispatch_does_not_expose_workflow_update_tool(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {"summary": "done", "artifacts": {}}
                                ),
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={"task_id": "draft-1", "step_name": "draft", "input": {}},
        )

        tool_names = {
            tool["function"]["name"]
            for tool in (provider.calls[0].get("tools") or [])
        }
        self.assertNotIn("workflow_update", tool_names)

    def test_workflow_dispatch_tool_allow_limits_stage_tools(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        recall_call = {
            "id": "call-1",
            "type": "function",
            "function": {"name": "recall_memory", "arguments": "{}"},
        }
        provider = FakeProvider(
            [
                {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [recall_call]}}]},
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"summary": "done", "artifacts": {}}),
                            }
                        }
                    ]
                },
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "task_id": "publish-1",
                "step_name": "publish",
                "input": {},
                "tool_allow": ["read_file", "google_workspace_docs_append"],
            },
        )

        tool_names = {tool["function"]["name"] for tool in provider.calls[0].get("tools") or []}
        self.assertEqual(tool_names, {"read_file", "read_skill_reference"})
        self.assertEqual(result.tool_calls[0]["tool"], "recall_memory")
        self.assertEqual(result.tool_calls[0]["decision"], "deny")

    def test_resumable_flag_only_for_top_level_workflow_tasks(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        final = {
            "choices": [
                {"message": {"role": "assistant", "content": json.dumps({"summary": "ok", "artifacts": {}})}}
            ]
        }
        seen = []
        registry = build_default_registry()
        original = registry.definitions_for_agent

        def spy(agent, ctx, cfg):
            seen.append(ctx.approval_resumable)
            return original(agent, ctx, cfg)

        registry.definitions_for_agent = spy
        agent = EnterpriseAgent(config, registry, provider=FakeProvider([final, final, final]))

        agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={"workflow_id": "wf-1", "task_id": "t-1", "step_name": "s", "input": {}},
        )
        agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={"task_id": "eval-1", "step_name": "s", "input": {}},
        )
        agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={"workflow_id": "wf-1", "task_id": "t-2", "step_name": "s", "input": {}},
            source="subagent",
            is_subagent=True,
            parent_tools={"read_file"},
        )

        self.assertEqual(seen, [True, False, False])

    def test_workflow_stage_pauses_on_approval_required_call(self):
        from ts_harness.tools import approvals as approvals_mod

        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        call = {
            "id": "c1",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "/tmp/out.txt", "content": "hello"}),
            },
        }
        provider = FakeProvider(
            [{"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}}]}]
        )
        queue = approvals_mod.ApprovalQueue()
        with patch.object(approvals_mod, "_approval_queue", queue):
            result = EnterpriseAgent(config, build_default_registry(), provider=provider).run_workflow_dispatch(
                tenant_id="default",
                agent_id="dev-agent",
                payload={
                    "workflow_id": "wf-1",
                    "task_id": "task-1",
                    "step_name": "publish",
                    "input": {},
                    "tool_allow": ["write_file"],
                },
            )

        self.assertEqual(result.status, "blocked")
        approval_id = result.output["approval_id"]
        self.assertEqual(result.error, f"awaiting_tool_approval:{approval_id}")
        request = queue.get(approval_id)
        self.assertEqual((request.workflow_id, request.task_id), ("wf-1", "task-1"))
        self.assertEqual(len(provider.calls), 1)

    def _approval_fixture(self, queue, approved):
        req = queue.request(
            tenant_id="default",
            agent_id="pm-agent",
            tool_name="read_file",
            args={"path": "README.md"},
            reason="r",
            workflow_id="wf-1",
            task_id="task-1",
        )
        queue.decide(req.approval_id, approved=approved, actor_id="admin-1")
        return req

    def _resume(self, queue, registry, summary):
        from ts_harness.tools import approvals as approvals_mod

        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        final = {
            "choices": [
                {"message": {"role": "assistant", "content": json.dumps({"summary": summary, "artifacts": {}})}}
            ]
        }
        provider = FakeProvider([final])
        with patch.object(approvals_mod, "_approval_queue", queue):
            result = EnterpriseAgent(config, registry, provider=provider).run_workflow_dispatch(
                tenant_id="default",
                agent_id="pm-agent",
                payload={"workflow_id": "wf-1", "task_id": "task-1", "step_name": "publish", "input": {}},
            )
        return result, provider

    def _replayed_result(self, provider, req):
        call_id = f"approval-{req.approval_id}"
        return next(
            m["content"]
            for m in provider.calls[0]["messages"]
            if m.get("role") == "tool" and m.get("tool_call_id") == call_id
        )

    def test_resumed_stage_runs_approved_call_once(self):
        from ts_harness.tools import approvals as approvals_mod

        registry = build_default_registry()
        dispatched = []
        registry.dispatch = lambda name, args, *, context: dispatched.append((name, dict(args))) or {"ok": True}
        queue = approvals_mod.ApprovalQueue()
        req = self._approval_fixture(queue, approved=True)

        result, provider = self._resume(queue, registry, "published")

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(dispatched, [("read_file", {"path": "README.md", "approval_id": req.approval_id})])
        self.assertIsNotNone(queue.get(req.approval_id).consumed_at)
        self.assertIn("approved_call_executed", self._replayed_result(provider, req))

    def test_resumed_stage_reports_rejected_call_without_running_it(self):
        from ts_harness.tools import approvals as approvals_mod

        registry = build_default_registry()
        dispatched = []
        registry.dispatch = lambda name, args, *, context: dispatched.append(name) or {}
        queue = approvals_mod.ApprovalQueue()
        req = self._approval_fixture(queue, approved=False)

        _result, provider = self._resume(queue, registry, "skipped")

        self.assertEqual(dispatched, [])
        self.assertIn("rejected", self._replayed_result(provider, req))
        self.assertIsNotNone(queue.get(req.approval_id).consumed_at)

    def test_gpt_6_astra_tool_calls_disable_reasoning_effort(self):
        provider = OpenAICompatibleProvider(
            ProviderEndpoint(
                provider="openai",
                model="openai/gpt-6-astra",
                base_url="https://api.example.test/v1",
                api_key="test-key",
            )
        )
        captured = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_1",
                                "name": "read_file",
                                "arguments": "{\"path\":\"app/pricing.py\"}",
                            }
                        ]
                    }
                ).encode()

        def _open(request, timeout=0):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode())
            return _Response()

        with patch("ts_harness.agent.provider.urllib.request.urlopen", _open):
            result = provider.chat(
                [{"role": "system", "content": "review"}, {"role": "user", "content": "diff"}],
                tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
            )
        self.assertTrue(captured["url"].endswith("/responses"))
        self.assertEqual(captured["body"]["model"], "gpt-6-astra")
        self.assertEqual(captured["body"]["instructions"], "review")
        self.assertEqual(captured["body"]["tools"][0]["name"], "read_file")
        self.assertNotIn("reasoning_effort", captured["body"])
        message = result["choices"][0]["message"]
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(message["tool_calls"][0]["id"], "call_1")

    def test_provider_builder_uses_openai_compatible_provider_by_default(self):
        provider = build_provider("openai/gpt-4o-mini")

        self.assertIsInstance(provider, OpenAICompatibleProvider)

    def test_work_orders_blocks_memory_recall_duplicate_artifacts(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        recall = (
            "I've recalled relevant memories for the PRD. "
            "Here's a summary of the key points:\n\n"
            "# FRD-001 Lobby\n\nWhen a player opens the app, show lobbies.\n\n"
            "# FRD-002 Match\n\nWhen matched, start a game.\n\n"
            "# Product Overview\n\n## Business Problem\nPlayers need checkers.\n"
        )
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "summary": "refinery done",
                                        "artifacts": {
                                            "requirements_markdown": recall,
                                            "frds_markdown": recall,
                                            "feature_tree_markdown": recall,
                                            "product_overview_markdown": recall,
                                        },
                                    }
                                ),
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "task_id": "refine-1",
                "step_name": "refine_requirements",
                "skill_hints": ["prd-to-work-orders-governed"],
                "input": {
                    "stage": "refinery",
                    "approved_prd": {
                        "result": {
                            "prd_title": "Checkers",
                            "prd_markdown": "# PRD\n\n## Problem Statement\nNeed checkers.\n",
                        }
                    },
                },
            },
        )
        self.assertEqual(result.status, "blocked")
        self.assertIn("completed refinery content", result.error or "")
        self.assertGreaterEqual(len(provider.calls), 2)
        repair_msgs = [
            msg.get("content", "")
            for call in provider.calls[1:]
            for msg in call["messages"]
            if msg.get("role") == "user"
            and "failed workflow validation" in str(msg.get("content") or "")
        ]
        self.assertTrue(repair_msgs)
        tool_names = {
            tool["function"]["name"]
            for tool in (provider.calls[0].get("tools") or [])
        }
        self.assertNotIn("recall_memory", tool_names)
        user_payload = json.loads(provider.calls[0]["messages"][1]["content"])
        self.assertIsInstance(user_payload["input"]["approved_prd"], str)
        self.assertIn("# PRD", user_payload["input"]["approved_prd"])

    def test_work_orders_repairs_invalid_output_then_succeeds(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        good_frds = (
            "# FRD-001 Lobby\n\n"
            "## Overview\nPlayers browse and join public or private lobbies before a match.\n\n"
            "## Personas\nCasual gamer, returning player\n\n"
            "## User Stories\n- As a casual gamer, I want to join a lobby so that I can find an opponent quickly.\n\n"
            "## Requirements\n- REQ-001: The system shall list joinable lobbies with player counts.\n"
            "- REQ-002: The system shall let a host create a private lobby with a share code.\n\n"
            "## Acceptance Criteria\n- When a user opens the lobby screen, the system shall show available lobbies.\n"
            "- When a host creates a private lobby, the system shall return a share code.\n\n"
            "## Dependencies\nNone\n\n"
            "# FRD-002 Match\n\n"
            "## Overview\nTwo players are paired and enter a live checkers session.\n\n"
            "## Personas\nCasual gamer, competitive player\n\n"
            "## User Stories\n- As a player, I want to start a match so that I can play checkers in real time.\n\n"
            "## Requirements\n- REQ-003: The system shall start a match once two players are ready.\n"
            "- REQ-004: The system shall keep both clients synchronized during play.\n\n"
            "## Acceptance Criteria\n- When two players are ready, the system shall start a match session.\n"
            "- When a player moves, the opponent shall see the updated board.\n\n"
            "## Dependencies\nFRD-001\n"
        )
        good = {
            "summary": "Refinery complete",
            "artifacts": {
                "requirements_markdown": (
                    "# Requirements\n\n## Technical Requirements\n"
                    "- Real-time multiplayer over the web with low-latency board sync.\n"
                    "- Browser clients only for the first release.\n"
                    "- Support private lobbies and open matchmaking queues.\n"
                ),
                "frds_markdown": good_frds,
                "feature_tree_markdown": (
                    "# Feature Tree\n\n"
                    "- Multiplayer Checkers\n"
                    "  - Lobby browser and private rooms\n"
                    "  - Matchmaking queue\n"
                    "  - Real-time gameplay board sync\n"
                    "  - In-match chat\n"
                    "  - Leaderboard rankings\n"
                    "- Dependencies: gameplay depends on lobby/matchmaking; chat and "
                    "leaderboard depend on completed matches.\n"
                ),
                "product_overview_markdown": (
                    "# Product Overview\n\n## Business Problem\n"
                    "Casual players want a quick online checkers game with friends or "
                    "matchmaking during short breaks.\n\n"
                    "## Personas\nCasual gamers and competitive ladder players.\n\n"
                    "## Success Metrics\n500 weekly active players within 90 days.\n"
                ),
            },
            "confidence": "high",
            "warnings": [],
            "follow_up_needed": False,
        }
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "summary": "I've recalled relevant memories about the PRD.",
                                        "artifacts": {
                                            "frds_markdown": (
                                                "I've recalled relevant memories about the PRD."
                                            ),
                                            "feature_tree_markdown": (
                                                "I've recalled relevant memories about the PRD."
                                            ),
                                            "product_overview_markdown": (
                                                "I've recalled relevant memories about the PRD."
                                            ),
                                        },
                                    }
                                ),
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(good),
                            }
                        }
                    ]
                },
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "task_id": "refine-repair",
                "step_name": "refine_requirements",
                "skill_hints": ["prd-to-work-orders-governed"],
                "input": {
                    "stage": "refinery",
                    "approved_prd": "# PRD\n\n## Problem Statement\nNeed checkers.\n",
                },
            },
        )
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertIn("frds_markdown", (result.output or {}).get("artifacts") or {})
        self.assertEqual(len(provider.calls), 2)

    def test_deliver_rejects_fabricated_pr_url_after_coding_failure(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        agent = EnterpriseAgent(config, build_default_registry(), provider=FakeProvider([]))
        payload = {
            "step_name": "deliver_work_order",
            "skill_hints": ["verified-merge-governed"],
            "input": {"work_order_id": "WO-001"},
        }
        output = {
            "work_order_id": "WO-001",
            "pr_url": "https://github.com/example-org/prd-dev-project/pull/123",
            "summary": "Pull request created successfully",
        }
        tool_calls = [
            {
                "tool": "run_coding_backend",
                "decision": "allow",
                "result_preview": json.dumps(
                    {
                        "ok": False,
                        "error": "git fetch failed: fatal: couldn't find remote ref main",
                        "pr_url": None,
                    }
                ),
            }
        ]
        err = agent._validate_workflow_output(output, payload, tool_calls=tool_calls)
        self.assertIsNotNone(err)
        self.assertIn("run_coding_backend failed", err or "")

    def test_deliver_accepts_pr_url_matching_coding_tool_result(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        agent = EnterpriseAgent(config, build_default_registry(), provider=FakeProvider([]))
        payload = {
            "step_name": "deliver_work_order",
            "skill_hints": ["verified-merge-governed"],
        }
        pr = "https://github.com/example-org/prd-dev-project/pull/7"
        output = {"work_order_id": "WO-001", "pr_url": pr}
        tool_calls = [
            {
                "tool": "run_coding_backend",
                "decision": "allow",
                "result_preview": json.dumps({"ok": True, "pr_url": pr}),
            }
        ]
        self.assertIsNone(
            agent._validate_workflow_output(output, payload, tool_calls=tool_calls)
        )

    def test_coding_audit_preview_keeps_pr_url_when_full_result_is_huge(self):
        """Regression: truncating full coding JSON broke deliver validation."""
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        agent = EnterpriseAgent(config, build_default_registry(), provider=FakeProvider([]))
        pr = "https://github.com/example-org/prd-dev-project/pull/3"
        huge = {
            "ok": True,
            "pr_url": pr,
            "branch": "wo/wo-003",
            "work_order_id": "WO-003",
            "changed_files": [f"src/file_{i}.py" for i in range(40)],
            "log_tail": "x" * 4000,
            "summary": "OpenCode implemented WO-003",
        }
        preview = agent._tool_audit_preview("run_coding_backend", huge, json.dumps(huge))
        self.assertLessEqual(len(preview), 500)
        parsed = json.loads(preview)
        self.assertTrue(parsed.get("ok"))
        self.assertEqual(parsed.get("pr_url"), pr)
        payload = {
            "step_name": "deliver_work_order",
            "skill_hints": ["verified-merge-governed"],
        }
        output = {"work_order_id": "WO-003", "pr_url": pr}
        tool_calls = [
            {
                "tool": "run_coding_backend",
                "decision": "allow",
                "result_preview": preview,
            }
        ]
        self.assertIsNone(
            agent._validate_workflow_output(output, payload, tool_calls=tool_calls)
        )

    def test_propose_skill_audit_preview_keeps_learn_keys(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=FakeProvider([]),
        )
        huge = {
            "status": "draft",
            "skill_id": "learned-api",
            "revision_of": "learned-api",
            "path": "/very/" + "/nested" * 90 + "/drafts/learned-api.md",
            "error": "x" * 4000,
            "error_code": "skill_learn_use_revision",
            "approval_status": "draft",
        }
        preview = agent._tool_audit_preview(
            "propose_skill",
            huge,
            json.dumps(huge),
        )
        self.assertLessEqual(len(preview), 500)
        parsed = json.loads(preview)
        self.assertEqual(parsed.get("status"), "draft")
        self.assertEqual(parsed.get("skill_id"), "learned-api")
        self.assertEqual(parsed.get("revision_of"), "learned-api")
        self.assertIn("path", parsed)
        self.assertIn("error", parsed)
        self.assertEqual(parsed.get("error_code"), "skill_learn_use_revision")

    def test_truncated_full_coding_json_preview_fails_validation(self):
        """Documents the pre-fix failure mode (truncated JSON is unreadable)."""
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        agent = EnterpriseAgent(config, build_default_registry(), provider=FakeProvider([]))
        pr = "https://github.com/example-org/prd-dev-project/pull/3"
        huge = json.dumps(
            {
                "ok": True,
                "pr_url": pr,
                "log_tail": "x" * 4000,
            }
        )
        truncated = huge[:500]
        self.assertRaises(json.JSONDecodeError, json.loads, truncated)
        err = agent._validate_workflow_output(
            {"work_order_id": "WO-003", "pr_url": pr},
            {"step_name": "deliver_work_order"},
            tool_calls=[
                {
                    "tool": "run_coding_backend",
                    "decision": "allow",
                    "result_preview": truncated,
                }
            ],
        )
        self.assertIsNotNone(err)
        self.assertIn("run_coding_backend", err or "")

    def test_contract_blocks_invalid_output_and_requests_repair(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        contract = {
            "output_schema": {
                "type": "object",
                "required": ["work_order_id", "pr_url"],
                "properties": {
                    "work_order_id": {"type": "string", "minLength": 1},
                    "pr_url": {"type": "string", "pattern": "^https://"},
                },
            },
            "required_artifacts": {"pr_url": "/pr_url"},
            "semantic_validators": [],
            "outcome": None,
        }
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"summary": "no pr url"}),
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"summary": "still bad"}),
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"summary": "still bad again"}),
                            }
                        }
                    ]
                },
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="dev-agent",
            payload={
                "task_id": "deliver-1",
                "step_name": "deliver_work_order",
                "contract": contract,
                "input": {},
            },
        )
        self.assertEqual(result.status, "blocked")
        repair_msgs = [
            msg.get("content", "")
            for call in provider.calls[1:]
            for msg in call["messages"]
            if msg.get("role") == "user"
            and "stage output contract" in str(msg.get("content") or "")
        ]
        self.assertTrue(repair_msgs)

    def test_contract_valid_output_succeeds(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        contract = {
            "output_schema": {
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            "required_artifacts": {},
            "semantic_validators": [],
            "outcome": {
                "pointer": "/status",
                "succeeded_values": ["success"],
                "blocked_values": ["failed"],
            },
        }
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"status": "success", "summary": "ok"}),
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="qa-agent",
            payload={"task_id": "qa-1", "step_name": "qa_gate", "contract": contract, "input": {}},
        )
        self.assertEqual(result.status, "succeeded", result.error)

    def test_contract_valid_blocked_outcome_returns_blocked_without_repair(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        contract = {
            "output_schema": {
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            "required_artifacts": {},
            "semantic_validators": [],
            "outcome": {
                "pointer": "/status",
                "succeeded_values": ["success"],
                "blocked_values": ["failed"],
            },
        }
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {"status": "failed", "summary": "QA found defects"}
                                ),
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="qa-agent",
            payload={"task_id": "qa-2", "step_name": "qa_gate", "contract": contract, "input": {}},
        )
        self.assertEqual(result.status, "blocked")
        self.assertIn("outcome", result.error or "")
        # Exactly one model call: a blocked outcome must not trigger repair turns.
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.output.get("status"), "failed")

    def test_contract_summary_used_in_system_prompt(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        contract = {
            "output_schema": {"type": "object", "required": ["pr_url"]},
            "required_artifacts": {"pr_url": "/pr_url"},
            "semantic_validators": [],
            "outcome": None,
        }
        agent = EnterpriseAgent(config, build_default_registry(), provider=FakeProvider([]))
        prompt = agent._build_workflow_system_prompt(
            config.get_agent("dev-agent"),
            {"step_name": "deliver_work_order", "contract": contract, "input": {}},
        )
        self.assertIn("Stage output contract", prompt)
        self.assertIn("pr_url", prompt)

    def test_qa_revision_appended_to_contract_prompt(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        contract = {
            "output_schema": {"type": "object", "required": ["pr_url"]},
            "required_artifacts": {"pr_url": "/pr_url"},
            "semantic_validators": [],
            "outcome": None,
        }
        agent = EnterpriseAgent(config, build_default_registry(), provider=FakeProvider([]))
        prompt = agent._build_workflow_system_prompt(
            config.get_agent("dev-agent"),
            {
                "step_name": "deliver_work_order",
                "contract": contract,
                "input": {
                    "qa_feedback": "No evidence of implementation or testing",
                    "pr_url": "https://github.com/org/app/pull/8",
                },
            },
        )
        self.assertIn("QA revision mode", prompt)
        self.assertIn("input.qa_feedback", prompt)
        self.assertIn("No evidence of implementation or testing", prompt)

    def test_enrich_coding_backend_args_copies_qa_feedback_from_task_input(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        agent = EnterpriseAgent(config, build_default_registry(), provider=FakeProvider([]))
        enriched = agent._enrich_coding_backend_args(
            {"work_order_id": "WO-003", "work_order_body": "# WO-003"},
            {
                "input": {
                    "qa_feedback": "missing tests",
                    "pr_url": "https://github.com/org/app/pull/8",
                }
            },
        )
        self.assertEqual(enriched["qa_feedback"], "missing tests")
        self.assertEqual(enriched["pr_url"], "https://github.com/org/app/pull/8")

    def test_run_coding_backend_tool_loop_enriches_qa_feedback(self):
        from unittest.mock import patch

        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        captured: dict = {}

        def fake_dispatch(name, args, context=None):
            captured["name"] = name
            captured["args"] = dict(args)
            return {
                "ok": True,
                "pr_url": "https://github.com/org/app/pull/8",
                "work_order_id": "WO-003",
            }

        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "run_coding_backend",
                                            "arguments": json.dumps(
                                                {
                                                    "work_order_id": "WO-003",
                                                    "work_order_body": "# WO-003",
                                                }
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "work_order_id": "WO-003",
                                        "pr_url": "https://github.com/org/app/pull/8",
                                        "summary": "updated PR",
                                    }
                                ),
                            }
                        }
                    ]
                },
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        with patch.object(agent.registry, "dispatch", side_effect=fake_dispatch):
            result = agent.run_workflow_dispatch(
                tenant_id="default",
                agent_id="dev-agent",
                payload={
                    "task_id": "deliver-rev",
                    "step_name": "deliver_work_order",
                    "input": {
                        "work_order_id": "WO-003",
                        "work_order_body": "# WO-003",
                        "qa_feedback": "missing tests",
                        "pr_url": "https://github.com/org/app/pull/8",
                    },
                },
            )
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(captured.get("name"), "run_coding_backend")
        self.assertEqual(captured["args"]["qa_feedback"], "missing tests")
        self.assertEqual(captured["args"]["pr_url"], "https://github.com/org/app/pull/8")

    def test_deliver_output_carries_trusted_coding_backend_fields(self):
        from unittest.mock import patch

        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        pr = "https://github.com/org/app/pull/9"
        security = {"secret_scan": {"findings": [{"file": "app/x.py", "line": 3}]}, "dependency_audit": {"findings": []}}

        def fake_dispatch(name, args, context=None):
            return {
                "ok": True,
                "pr_url": pr,
                "work_order_id": "WO-004",
                "branch": "wo/wo-004",
                "head_sha": "c" * 40,
                "refresh_method": "server_merge",
                "security": security,
                "log_tail": "x" * 5000,
            }

        call = {"id": "call-1", "type": "function", "function": {"name": "run_coding_backend", "arguments": json.dumps({"work_order_id": "WO-004", "work_order_body": "# WO-004"})}}
        claimed = {"work_order_id": "WO-004", "pr_url": pr, "summary": "done", "security": {}}
        provider = FakeProvider(
            [
                {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}}]},
                {"choices": [{"message": {"role": "assistant", "content": json.dumps(claimed)}}]},
            ]
        )
        from ts_harness.workflows.materialize import load_skeleton

        contract = load_skeleton("deliver_work_order").resolve_stage_contract("deliver_work_order")
        self.assertIn("validate_coding_backend_pr_url", contract["semantic_validators"])
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        with patch.object(agent.registry, "dispatch", side_effect=fake_dispatch):
            result = agent.run_workflow_dispatch(
                tenant_id="default",
                agent_id="dev-agent",
                payload={
                    "task_id": "deliver-1",
                    "step_name": "deliver_work_order",
                    "contract": contract,
                    "input": {"work_order_id": "WO-004", "work_order_body": "# WO-004"},
                },
            )
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(result.output["security"], security)
        self.assertEqual(result.output["refresh_method"], "server_merge")
        self.assertEqual(result.output["head_sha"], "c" * 40)
        self.assertEqual(result.output["branch"], "wo/wo-004")
        self.assertNotIn("log_tail", result.output)

    def test_foundry_normalize_partitions_architecture_summary_blob(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        agent = EnterpriseAgent(config, build_default_registry(), provider=FakeProvider([]))
        blob = (
            "## ADR-001 Real-time Communication\n\n"
            "## Context\nNeed low-latency turns for checkers.\n\n"
            "## Alternatives Considered\nWebSockets vs long polling.\n\n"
            "## Decision\nUse WebSockets.\n\n"
            "## Rejection Rationale\nLong polling is slower.\n\n"
            "## Trade-off Scorecard\nLatency vs complexity.\n\n"
            "## Consequences and Downstream Impact\nClients need WS support.\n\n"
            "## ADR-002 Session Persistence\n\n"
            "## Context\nReconnect after network drops.\n\n"
            "## Alternatives Considered\nSticky sessions vs token resume.\n\n"
            "## Decision\nToken resume.\n\n"
            "## Rejection Rationale\nSticky sessions hurt scale.\n\n"
            "## Trade-off Scorecard\nScale vs simplicity.\n\n"
            "## Consequences and Downstream Impact\nStore resume tokens.\n\n"
            "# Blueprints\n\n"
            "## COMP-001 Game Server\n\n"
            "Owns match state and turn validation for online checkers.\n\n"
            "## FB-001 Matchmaking\n\n"
            "Covers queueing, pairing, and private rooms for FRD-001.\n\n"
            "# Architecture Summary\n\n"
            "WebSocket game server plus token resume keep matches coherent across reconnects.\n"
        )
        payload = {
            "step_name": "design_blueprints",
            "skill_hints": ["prd-to-work-orders-governed"],
            "input": {"stage": "foundry"},
        }
        normalized = agent._normalize_work_orders_output(
            {"summary": "foundry draft"},
            payload,
            raw_text=blob,
        )
        artifacts = normalized.get("artifacts") or {}
        self.assertIn("architecture_summary_markdown", artifacts)
        self.assertIn("Architecture Summary", artifacts["architecture_summary_markdown"])
        self.assertIsNone(agent._validate_workflow_output(normalized, payload))

    def test_workflow_proposal_closes_every_tool_call(self):
        from ts_harness.agent.sessions import reset_session_store

        reset_session_store()
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-propose",
                                        "type": "function",
                                        "function": {
                                            "name": "propose_start_workflow",
                                            "arguments": json.dumps(
                                                {
                                                    "workflow_type": "prd_request",
                                                    "summary": "start",
                                                }
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call-other",
                                        "type": "function",
                                        "function": {
                                            "name": "session_status",
                                            "arguments": "{}",
                                        },
                                    },
                                ],
                            }
                        }
                    ]
                }
            ]
        )
        registry = build_default_registry()

        def dispatch(name, args, *, context):
            if name == "propose_start_workflow":
                return {"status": "awaiting_confirmation", "workflow_type": "prd_request"}
            raise AssertionError(f"unexpected tool {name}")

        registry.dispatch = dispatch
        agent = EnterpriseAgent(config, registry, provider=provider)
        result = agent.run_channel_chat(
            tenant_id="default",
            agent_id="orchestrator",
            text="start a prd",
            session_id="pair-tools",
            source="web",
        )
        self.assertEqual(result.status, "succeeded")
        assistant = next(
            message
            for message in result.messages
            if message.get("tool_calls")
        )
        expected_ids = [call["id"] for call in assistant["tool_calls"]]
        tool_ids = [
            message.get("tool_call_id")
            for message in result.messages
            if message.get("role") == "tool"
        ]
        self.assertEqual(tool_ids, expected_ids)


if __name__ == "__main__":
    unittest.main()
