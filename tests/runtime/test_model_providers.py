"""Claude and GPT-6 providers, per-agent effort, and usage recording (llm_usage, agent_token_usage)."""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ts_harness.agent.loop import _run_effort, _run_model
from ts_harness.agent.provider import (
    ANTHROPIC_CONTENT_KEY,
    AnthropicProvider,
    MeteredProvider,
    OpenAICompatibleProvider,
    ProviderEndpoint,
    anthropic_messages_from_chat,
    build_provider,
)
from ts_harness.implementation.backends.opencode_usage import record_opencode_usage, session_usage
from ts_harness.llm_proxy.metering import Usage
from ts_harness.llm_usage import record_usage

TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object"}}}]


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def _capture(*bodies):
    sent = []
    queue = list(bodies)

    def _open(request, timeout=0):
        sent.append({"url": request.full_url, "body": json.loads(request.data.decode()), "headers": dict(request.headers)})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Response(item)

    return sent, _open


def _claude(effort="high"):
    return AnthropicProvider(
        ProviderEndpoint(provider="anthropic", model="claude-opus-5-5", base_url="https://api.test/v1",
                         api_key="k", api_mode="anthropic", effort=effort)
    )


CLAUDE_TOOL_TURN = {
    "model": "claude-opus-5-5",
    "stop_reason": "tool_use",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig-1"},
        {"type": "text", "text": "Reading it."},
        {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}},
    ],
    "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 900},
}


class ClaudeProviderTests(unittest.TestCase):
    def test_request_has_no_temperature_and_carries_effort_cache_and_tools(self):
        sent, opener = _capture(CLAUDE_TOOL_TURN)
        with patch("ts_harness.agent.provider.urllib.request.urlopen", opener):
            _claude().chat([{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}], tools=TOOLS)
        body = sent[0]["body"]
        self.assertNotIn("temperature", body)
        self.assertNotIn("thinking", body)
        self.assertEqual(body["output_config"], {"effort": "high"})
        self.assertEqual(body["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["system"], "be brief")
        self.assertEqual(body["tools"][0]["name"], "read_file")
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertGreaterEqual(body["max_tokens"], 16000)

    def test_tool_use_comes_back_as_tool_calls_with_usage(self):
        sent, opener = _capture(CLAUDE_TOOL_TURN)
        with patch("ts_harness.agent.provider.urllib.request.urlopen", opener):
            result = _claude().chat([{"role": "user", "content": "hi"}], tools=TOOLS)
        message = result["choices"][0]["message"]
        self.assertEqual(message["content"], "Reading it.")
        call = message["tool_calls"][0]
        self.assertEqual((call["id"], call["function"]["name"]), ("toolu_1", "read_file"))
        self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "a.py"})
        self.assertEqual(result["usage"]["input_tokens"], 1200)
        self.assertEqual(message[ANTHROPIC_CONTENT_KEY][0]["signature"], "sig-1")

    def test_the_tool_loop_replays_claude_blocks_and_groups_tool_results(self):
        assistant = {"role": "assistant", "content": "Reading it.", "tool_calls": [], ANTHROPIC_CONTENT_KEY: CLAUDE_TOOL_TURN["content"]}
        system, messages = anthropic_messages_from_chat(
            [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "hi"},
                assistant,
                {"role": "tool", "tool_call_id": "toolu_1", "content": "print(1)"},
                {"role": "tool", "tool_call_id": "toolu_2", "content": "ok"},
            ]
        )
        self.assertEqual(system, "s")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[1]["content"], CLAUDE_TOOL_TURN["content"])
        self.assertEqual([b["tool_use_id"] for b in messages[2]["content"]], ["toolu_1", "toolu_2"])

    def test_openai_shaped_history_becomes_tool_use_blocks(self):
        _system, messages = anthropic_messages_from_chat(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"b\"}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "x"},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
            ]
        )
        self.assertEqual(messages[1]["content"], [{"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "b"}}])
        self.assertEqual(messages[2]["content"][0]["type"], "tool_result")
        self.assertEqual(messages[2]["content"][1]["source"], {"type": "base64", "media_type": "image/png", "data": "QUJD"})

    def test_a_rejected_thinking_replay_is_retried_without_thinking(self):
        error = urllib.error.HTTPError(
            "u", 400, "bad", {}, io.BytesIO(b'{"error":{"message":"messages.1.content.0: Invalid `signature` in `thinking` block."}}')
        )
        sent, opener = _capture(error, {"content": [{"type": "text", "text": "done"}], "usage": {}})
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", ANTHROPIC_CONTENT_KEY: CLAUDE_TOOL_TURN["content"]},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "x"},
        ]
        with patch("ts_harness.agent.provider.urllib.request.urlopen", opener):
            result = _claude().chat(history)
        self.assertEqual(result["choices"][0]["message"]["content"], "done")
        self.assertIn("thinking", [b["type"] for b in sent[0]["body"]["messages"][1]["content"]])
        self.assertNotIn("thinking", [b["type"] for b in sent[1]["body"]["messages"][1]["content"]])

    def test_a_refusal_says_so(self):
        sent, opener = _capture({"stop_reason": "refusal", "stop_details": {"category": "cyber"}, "content": [], "usage": {}})
        with patch("ts_harness.agent.provider.urllib.request.urlopen", opener):
            result = _claude().chat([{"role": "user", "content": "hi"}])
        self.assertIn("declined", result["choices"][0]["message"]["content"])


class Gpt6ProviderTests(unittest.TestCase):
    def _openai(self, model, effort=None):
        return OpenAICompatibleProvider(
            ProviderEndpoint(provider="openai", model=model, base_url="https://api.test/v1", api_key="k", effort=effort)
        )

    def test_every_gpt_6_model_uses_the_responses_api_with_its_effort(self):
        for model, effort, expected in (("gpt-6.1-sol", None, "low"), ("gpt-6-luna", "medium", "medium"), ("gpt-6-astra", "max", "high")):
            sent, opener = _capture({"output": [], "usage": {"input_tokens": 10, "output_tokens": 2}})
            with patch("ts_harness.agent.provider.urllib.request.urlopen", opener):
                result = self._openai(model, effort).chat([{"role": "user", "content": "hi"}], tools=TOOLS)
            with self.subTest(model=model):
                self.assertTrue(sent[0]["url"].endswith("/responses"))
                self.assertEqual(sent[0]["body"]["reasoning"], {"effort": expected})
                self.assertNotIn("temperature", sent[0]["body"])
                self.assertEqual(result["usage"]["input_tokens"], 10)

    def test_claude_only_keys_never_reach_openai(self):
        sent, opener = _capture({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
        with patch("ts_harness.agent.provider.urllib.request.urlopen", opener):
            self._openai("gpt-4o").chat([{"role": "assistant", "content": "x", ANTHROPIC_CONTENT_KEY: []}])
        self.assertNotIn(ANTHROPIC_CONTENT_KEY, sent[0]["body"]["messages"][0])


class _FakeCursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))


class _FakeConn(_FakeCursor):
    def cursor(self):
        return _FakeCursor(self.log)


class UsageRecordingTests(unittest.TestCase):
    def test_one_call_writes_llm_usage_and_agent_token_usage(self):
        log = []
        scope = {"tenant": "default", "agent": "pm-agent", "workflow": "wf-1", "task": None}
        with patch("ts_harness.llm_usage.pricing", return_value=provider_mod_pricing()):
            ok = record_usage(scope, provider="anthropic", model="claude-opus-5-5",
                              usage=Usage(input_tokens=1000, output_tokens=100, cache_read_tokens=2000),
                              connect=lambda: _FakeConn(log))
        self.assertTrue(ok)
        llm, agent = log
        self.assertIn("INSERT INTO llm_usage", llm[0])
        self.assertEqual((llm[1]["actor"], llm[1]["input"], llm[1]["output"]), ("pm-agent", 3000, 100))
        self.assertAlmostEqual(llm[1]["cost"], (1000 * 4 + 100 * 20 + 2000 * 0.2) / 1_000_000)
        self.assertIn("INSERT INTO agent_token_usage", agent[0])
        self.assertEqual(agent[1][:5], ("pm-agent", "anthropic", "claude-opus-5-5", 3000, 100))

    def test_nothing_is_written_through_the_proxy_or_without_a_database(self):
        with patch.dict(os.environ, {"LLM_PROXY_URL": "http://proxy", "LLM_PROXY_SECRET": "s", "TS_HARNESS_SESSION_DB_URL": "postgresql://x"}):
            self.assertFalse(record_usage({}, provider="openai", model="gpt-6.1-sol", usage=Usage(input_tokens=1)))
        with patch.dict(os.environ, {"TS_HARNESS_SESSION_DB_URL": "", "LLM_USAGE_DB_URL": ""}):
            self.assertFalse(record_usage({}, provider="openai", model="gpt-6.1-sol", usage=Usage(input_tokens=1)))

    def test_direct_calls_are_metered_with_their_scope(self):
        env = {"TS_HARNESS_SESSION_DB_URL": "postgresql://x", "LLM_PROXY_URL": "", "LLM_PROXY_SECRET": ""}
        with patch.dict(os.environ, env):
            provider = build_provider("anthropic/claude-sonnet-5-5", scope={"agent": "orchestrator"}, effort="medium")
        self.assertIsInstance(provider, MeteredProvider)
        self.assertEqual(provider.endpoint.effort, "medium")
        provider.inner = SimpleNamespace(chat=lambda *a, **k: {"choices": [{"message": {}}], "usage": {"input_tokens": 7, "output_tokens": 3}})
        with patch("ts_harness.llm_usage.record_usage") as record:
            provider.chat([{"role": "user", "content": "hi"}])
        args, kwargs = record.call_args
        self.assertEqual(args[0], {"agent": "orchestrator"})
        self.assertEqual((kwargs["provider"], kwargs["model"]), ("anthropic", "claude-sonnet-5-5"))
        self.assertEqual((kwargs["usage"].input_tokens, kwargs["usage"].output_tokens), (7, 3))


def provider_mod_pricing():
    from ts_harness.llm_proxy.metering import ModelPrice

    return {"anthropic/claude-opus-5-5": ModelPrice(4, 20, 0.2)}


class HeartbeatModelTests(unittest.TestCase):
    def test_heartbeats_run_on_their_own_model_at_low_effort(self):
        agent = SimpleNamespace(model="anthropic/claude-opus-5-5", effort="high",
                                heartbeat=SimpleNamespace(model="anthropic/claude-haiku-5-5"))
        beat = {"step_name": "heartbeat"}
        self.assertEqual(_run_model(agent, beat, "cron"), "anthropic/claude-haiku-5-5")
        self.assertEqual(_run_effort(agent, beat, "cron"), "low")
        self.assertEqual(_run_model(agent, beat, "workflow"), "anthropic/claude-opus-5-5")  # only the scheduler
        self.assertEqual(_run_effort(agent, {"step_name": "draft_prd"}, "workflow"), "high")


OPENCODE_EXPORT = {
    "messages": [
        {"info": {"role": "user"}},
        {"info": {"role": "assistant", "providerID": "anthropic", "modelID": "claude-sonnet-5-5", "cost": 0.5,
                  "tokens": {"input": 1000, "output": 200, "reasoning": 50, "cache": {"read": 4000, "write": 300}}}},
        {"info": {"role": "assistant", "providerID": "anthropic", "modelID": "claude-sonnet-5-5", "cost": 0.25,
                  "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}}}},
    ]
}


class OpencodeUsageTests(unittest.TestCase):
    def test_session_totals_per_model(self):
        totals = session_usage(OPENCODE_EXPORT)
        self.assertEqual(totals[("anthropic", "claude-sonnet-5-5")],
                         {"input": 1310, "output": 255, "cache_read": 4000, "cost": 0.75})

    def test_only_sessions_from_this_run_in_this_repo_are_recorded(self):
        repo = Path("/tmp/repo-a")
        sessions = [
            {"id": "new", "created": 2000, "directory": "/tmp/repo-a"},
            {"id": "old", "created": 500, "directory": "/tmp/repo-a"},
            {"id": "other", "created": 3000, "directory": "/tmp/repo-b"},
        ]
        calls = []

        def run_json(cmd, *, env, cwd):
            calls.append(cmd[1:])
            return sessions if cmd[1] == "session" else OPENCODE_EXPORT

        recorded = []
        written = record_opencode_usage(
            binary="opencode", env={}, repo_dir=repo, since_ms=1000, scope={"agent": "coding-backend"},
            run_json=run_json, record=lambda scope, **kw: recorded.append((scope, kw)) or True,
        )
        self.assertEqual(written, 1)
        self.assertEqual(calls, [["session", "list", "--format", "json", "-n", "50"], ["export", "new"]])
        scope, kw = recorded[0]
        self.assertEqual(scope, {"agent": "coding-backend"})
        self.assertEqual((kw["provider"], kw["model"], kw["session_id"]), ("anthropic", "claude-sonnet-5-5", "new"))
        self.assertEqual(kw["usage"].cache_read_tokens, 4000)


if __name__ == "__main__":
    unittest.main()


class SessionsListTests(unittest.TestCase):
    def test_newest_sessions_first_with_a_limit(self):
        from ts_harness.agent.sessions import SessionRecord, SessionStore, get_session_store, reset_session_store
        from ts_harness.tools.workflow_tools import sessions_list_handler

        reset_session_store(SessionStore())
        self.addCleanup(reset_session_store, SessionStore())
        store = get_session_store()
        for i in range(30):
            store.save(SessionRecord(session_id=f"s{i:02d}", routing_key=f"rk{i}", tenant_id="default",
                                     agent_id="pm-agent" if i % 2 else "orchestrator", source="web",
                                     started_at=f"2026-11-{1 + i % 28:02d}T00:00:{i:02d}Z"))
        ctx = SimpleNamespace(tenant_id="default")
        out = sessions_list_handler({}, ctx)
        self.assertEqual((out["total"], out["returned"]), (30, 20))
        started = [row["started_at"] for row in out["sessions"]]
        self.assertEqual(started, sorted(started, reverse=True))
        self.assertNotIn("metadata", out["sessions"][0])
        pm = sessions_list_handler({"agent_id": "pm-agent", "limit": 500}, ctx)
        self.assertEqual((pm["total"], pm["returned"]), (15, 15))
