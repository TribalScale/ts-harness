"""LLM proxy: tokens, usage metering, pricing, the pass-through app, and callers routed through it."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from starlette.testclient import TestClient

from ts_harness.agent.provider import resolve_provider
from ts_harness.coding.launcher import EcsConfig, EcsLauncher, LocalDockerConfig, LocalDockerLauncher, job_proxy_env, worker_env_names
from ts_harness.implementation.auth import coding_child_env, resolve_coding_backend_credentials
from ts_harness.llm_proxy.app import Upstream, build_app, upstreams_from_env
from ts_harness.llm_proxy.metering import (
    ModelPrice,
    SseUsageScanner,
    Usage,
    load_proxy_settings,
    match_model,
    parse_pricing,
    price,
    usage_from_body,
)
from ts_harness.llm_proxy.store import ModelRateLimiter
from ts_harness.llm_proxy.tokens import InvalidProxyToken, mint_proxy_token, verify_proxy_token

SECRET = "proxy-test-secret"


class TokenTests(unittest.TestCase):
    def test_round_trip_keeps_scope_and_drops_blanks(self):
        token = mint_proxy_token({"tenant": "acme", "team": "t1", "task": None, "user": "", "extra": "x"}, secret=SECRET)
        self.assertEqual(verify_proxy_token(token, secret=SECRET), {"tenant": "acme", "team": "t1"})

    def test_rejects_wrong_secret_expired_and_missing(self):
        token = mint_proxy_token({"tenant": "acme"}, secret=SECRET)
        with self.assertRaises(InvalidProxyToken):
            verify_proxy_token(token, secret="other-proxy-test-secret")
        with self.assertRaises(InvalidProxyToken):
            verify_proxy_token(mint_proxy_token({}, secret=SECRET, ttl_seconds=10, now=1000), secret=SECRET)
        with self.assertRaises(InvalidProxyToken):
            verify_proxy_token("", secret=SECRET)

    def test_mint_needs_a_secret(self):
        with mock.patch.dict(os.environ, {"LLM_PROXY_SECRET": ""}), self.assertRaises(InvalidProxyToken):
            mint_proxy_token({"tenant": "acme"})


class MeteringTests(unittest.TestCase):
    def test_openai_chat_body_separates_cached_prompt_tokens(self):
        usage = usage_from_body(
            {"usage": {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 400}}}
        )
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cache_read_tokens), (600, 200, 400))

    def test_anthropic_body_counts_cache_writes_as_input(self):
        usage = usage_from_body(
            {"usage": {"input_tokens": 50, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 300, "output_tokens": 70}}
        )
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cache_read_tokens), (150, 70, 300))

    def test_responses_api_body(self):
        usage = usage_from_body({"response": {"usage": {"input_tokens": 12, "output_tokens": 3}}})
        self.assertEqual((usage.input_tokens, usage.output_tokens), (12, 3))

    def test_sse_scanner_reads_openai_final_usage_across_chunk_boundaries(self):
        stream = (
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":5}}\n\n'
            b"data: [DONE]\n\n"
        )
        scanner = SseUsageScanner()
        for i in range(0, len(stream), 7):
            scanner.feed(stream[i : i + 7])
        usage = scanner.close()
        self.assertEqual((usage.input_tokens, usage.output_tokens), (20, 5))

    def test_sse_scanner_reads_anthropic_message_start_and_delta(self):
        scanner = SseUsageScanner()
        scanner.feed(b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":40,"output_tokens":1}}}\n\n')
        scanner.feed(b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":90}}')
        usage = scanner.close()
        self.assertEqual((usage.input_tokens, usage.output_tokens), (40, 90))

    def test_pricing_matches_exact_then_model_then_longest_prefix_then_star(self):
        table = {"openai/gpt-4o": 1, "gpt-4o-mini": 2, "claude-*": 3, "claude-opus-*": 4, "*": 5}
        self.assertEqual(match_model(table, "openai", "gpt-4o")[1], 1)
        self.assertEqual(match_model(table, "openrouter", "gpt-4o-mini")[1], 2)
        self.assertEqual(match_model(table, "anthropic", "claude-opus-4")[1], 4)
        self.assertEqual(match_model(table, "anthropic", "claude-sonnet-4")[1], 3)
        self.assertEqual(match_model(table, "x", "y")[1], 5)
        self.assertIsNone(match_model({}, "x", "y"))

    def test_cost_per_million_tokens_with_default_cache_rate(self):
        pricing = parse_pricing({"claude-*": {"input_per_mtok": 3, "output_per_mtok": 15}})
        usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000)
        self.assertAlmostEqual(price(pricing, "anthropic", "claude-sonnet", usage), 3 + 1.5 + 0.3)
        self.assertEqual(price(pricing, "openai", "gpt-4o", usage), 0.0)
        self.assertAlmostEqual(ModelPrice(1, 1, 0.5).cost(Usage(cache_read_tokens=2_000_000)), 1.0)

    def test_settings_load_from_runtime_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.yaml"
            path.write_text("llm_pricing:\n  '*': {input_per_mtok: 1, output_per_mtok: 2}\nllm_rate_limits:\n  'anthropic/*': {requests_per_minute: 60}\n")
            settings = load_proxy_settings(path)
        self.assertEqual(settings["pricing"]["*"], ModelPrice(1.0, 2.0, None))
        self.assertEqual(settings["rate_limits"], {"anthropic/*": {"requests_per_minute": 60}})
        self.assertEqual(load_proxy_settings(None)["pricing"], {})

    def test_rate_limiter_lets_unlisted_models_and_database_failures_through(self):
        def broken():
            raise RuntimeError("db down")

        self.assertTrue(ModelRateLimiter(broken, {}).allow("openai", "gpt-4o"))
        self.assertTrue(ModelRateLimiter(broken, {"*": {"requests_per_minute": 1}}).allow("openai", "gpt-4o"))
        self.assertTrue(ModelRateLimiter(None, {"*": {"requests_per_minute": 1}}).allow("openai", "gpt-4o"))


class _Upstream:
    """Fake provider: records requests and answers with a canned body."""

    def __init__(self, body: bytes, content_type: str = "application/json"):
        self.body = body
        self.content_type = content_type
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=self.body, headers={"content-type": self.content_type})


class ProxyAppTests(unittest.TestCase):
    def setUp(self):
        self.recorded: list[dict] = []
        self.token = mint_proxy_token({"tenant": "acme", "team": "team-1", "user": "dev@acme.test"}, secret=SECRET)

    def client(self, upstream: _Upstream, **kwargs) -> TestClient:
        app = build_app(
            secret=SECRET,
            upstreams={
                "openai": Upstream("https://api.openai.test/v1", "sk-real"),
                "anthropic": Upstream("https://api.anthropic.test/v1", "ak-real", "x-api-key"),
            },
            pricing=parse_pricing({"*": {"input_per_mtok": 1, "output_per_mtok": 2}}),
            record=lambda claims, **row: self.recorded.append({"claims": claims, **row}),
            transport=httpx.MockTransport(upstream),
            **kwargs,
        )
        return TestClient(app)

    def test_rejects_missing_or_bad_tokens(self):
        upstream = _Upstream(b"{}")
        client = self.client(upstream)
        self.assertEqual(client.post("/llm/openai/v1/chat/completions", json={}).status_code, 401)
        bad = mint_proxy_token({"tenant": "acme"}, secret="wrong-proxy-test-secret")
        response = client.post("/llm/openai/v1/chat/completions", json={}, headers={"Authorization": f"Bearer {bad}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(upstream.requests, [])

    def test_plain_call_swaps_the_key_and_records_usage(self):
        upstream = _Upstream(json.dumps({"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}}).encode())
        response = self.client(upstream).post(
            "/llm/openai/v1/chat/completions",
            json={"model": "gpt-4o", "messages": []},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(response.status_code, 200)
        sent = upstream.requests[0]
        self.assertEqual(str(sent.url), "https://api.openai.test/v1/chat/completions")
        self.assertEqual(sent.headers["authorization"], "Bearer sk-real")
        self.assertNotIn(self.token, json.dumps(dict(sent.headers)))
        [row] = self.recorded
        self.assertEqual(row["claims"], {"tenant": "acme", "team": "team-1", "user": "dev@acme.test"})
        self.assertEqual((row["provider"], row["model"], row["status"]), ("openai", "gpt-4o", 200))
        self.assertEqual((row["usage"].input_tokens, row["usage"].output_tokens), (1_000_000, 500_000))
        self.assertAlmostEqual(row["cost_usd"], 2.0)

    def test_anthropic_uses_x_api_key_both_ways_and_forwards_the_version(self):
        upstream = _Upstream(json.dumps({"usage": {"input_tokens": 10, "output_tokens": 2}}).encode())
        response = self.client(upstream).post(
            "/llm/anthropic/v1/messages",
            json={"model": "claude-sonnet-4", "max_tokens": 10, "messages": []},
            headers={"x-api-key": self.token, "anthropic-version": "2023-06-01"},
        )
        self.assertEqual(response.status_code, 200)
        sent = upstream.requests[0]
        self.assertEqual(sent.headers["x-api-key"], "ak-real")
        self.assertEqual(sent.headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", sent.headers)
        self.assertEqual(self.recorded[0]["usage"].input_tokens, 10)

    def test_streamed_chat_asks_for_usage_relays_bytes_and_records(self):
        stream = (
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
            b"data: [DONE]\n\n"
        )
        upstream = _Upstream(stream, "text/event-stream")
        response = self.client(upstream).post(
            "/llm/openai/v1/chat/completions",
            json={"model": "gpt-4o", "stream": True, "messages": []},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(response.content, stream)
        self.assertEqual(json.loads(upstream.requests[0].content)["stream_options"], {"include_usage": True})
        [row] = self.recorded
        self.assertEqual((row["usage"].input_tokens, row["usage"].output_tokens), (7, 3))

    def test_rate_limit_and_budget_refusals_never_reach_the_provider(self):
        upstream = _Upstream(b"{}")
        headers = {"Authorization": f"Bearer {self.token}"}
        limited = self.client(upstream, allow=lambda provider, model: False).post(
            "/llm/openai/v1/chat/completions", json={"model": "gpt-4o"}, headers=headers
        )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "5")
        refused = self.client(upstream, admit=lambda claims: f"team {claims['team']} is over budget").post(
            "/llm/openai/v1/chat/completions", json={"model": "gpt-4o"}, headers=headers
        )
        self.assertEqual(refused.status_code, 402)
        self.assertEqual(refused.json()["error"]["message"], "team team-1 is over budget")
        self.assertEqual(upstream.requests, [])
        self.assertEqual(self.recorded, [])

    def test_unknown_provider_without_a_fallback_is_404(self):
        response = self.client(_Upstream(b"{}")).post(
            "/llm/mistral/v1/chat/completions", json={"model": "m"}, headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status_code, 404)

    def test_upstreams_come_from_provider_keys(self):
        upstreams = upstreams_from_env(
            {"OPENAI_API_KEY": "sk", "MODEL_BASE_URL": "https://models.internal/v1/", "LLM_UPSTREAM_OPENAI_URL": "https://gw/v1"}
        )
        self.assertEqual(sorted(upstreams), ["*", "openai"])
        self.assertEqual(upstreams["openai"].base_url, "https://gw/v1")
        self.assertEqual(upstreams["*"], Upstream("https://models.internal/v1", "sk"))


class CallerRoutingTests(unittest.TestCase):
    def test_provider_goes_through_the_proxy_with_a_scoped_token(self):
        env = {"LLM_PROXY_URL": "http://127.0.0.1:18795/llm/", "LLM_PROXY_SECRET": SECRET}
        with mock.patch.dict(os.environ, env):
            endpoint = resolve_provider("anthropic/claude-sonnet-4", scope={"tenant": "acme", "task": "t-1"})
        self.assertEqual(endpoint.base_url, "http://127.0.0.1:18795/llm/anthropic/v1")
        self.assertEqual(endpoint.api_mode, "anthropic")
        self.assertEqual(endpoint.model, "claude-sonnet-4")
        self.assertEqual(verify_proxy_token(endpoint.api_key, secret=SECRET), {"tenant": "acme", "task": "t-1"})

    def test_provider_is_direct_without_the_secret(self):
        with mock.patch.dict(os.environ, {"LLM_PROXY_URL": "http://proxy/llm", "LLM_PROXY_SECRET": "", "OPENAI_API_KEY": "sk"}):
            endpoint = resolve_provider("openai/gpt-4o")
        self.assertNotIn("proxy", endpoint.base_url)

    def test_opencode_child_gets_a_proxy_config_and_no_real_key(self):
        env = {"LLM_PROXY_URL": "http://proxy/llm", "LLM_PROXY_TOKEN": "job-token", "CODING_BACKEND_API_KEY": "real"}
        with mock.patch.dict(os.environ, env), tempfile.TemporaryDirectory() as tmp:
            provider, key = resolve_coding_backend_credentials(provider="anthropic", auth_profiles_path=Path(tmp) / "none")
            child = coding_child_env(provider, key, config_dir=Path(tmp))
            config = json.loads(Path(child["OPENCODE_CONFIG"]).read_text())
        self.assertEqual(key, "job-token")
        self.assertEqual(
            config["provider"]["anthropic"]["options"],
            {"baseURL": "http://proxy/llm/anthropic/v1", "apiKey": "{env:LLM_PROXY_TOKEN}"},
        )
        self.assertEqual((child["ANTHROPIC_API_KEY"], child["CODING_BACKEND_API_KEY"], child["LLM_PROXY_TOKEN"]), ("job-token",) * 3)
        self.assertNotIn("real", child.values())

    def test_opencode_child_is_unchanged_without_the_proxy(self):
        with mock.patch.dict(os.environ, {"LLM_PROXY_URL": "", "LLM_PROXY_TOKEN": "", "LLM_PROXY_SECRET": ""}):
            child = coding_child_env("openai", "sk", config_dir=Path("/nonexistent"))
        self.assertEqual(child, {"CODING_BACKEND_PROVIDER": "openai", "CODING_BACKEND_API_KEY": "sk", "OPENAI_API_KEY": "sk"})


class LauncherProxyTests(unittest.TestCase):
    JOB = {"id": "job-1", "tenant_id": "acme", "workflow_id": "wf-1", "task_id": "task-1", "program_id": "p-1", "requested_by": "dev@acme.test"}
    ENV = {
        "LLM_PROXY_SECRET": SECRET,
        "LLM_PROXY_URL": "http://127.0.0.1:18795/llm",
        "OPENAI_API_KEY": "sk",
        "CODING_BACKEND_API_KEY": "real",
        "CODING_BACKEND_PROVIDER": "openai",
        "GITHUB_TOKEN": "gh",
    }

    def test_workers_get_a_job_token_instead_of_provider_keys(self):
        self.assertEqual(worker_env_names(self.ENV), ["CODING_BACKEND_PROVIDER", "GITHUB_TOKEN"])
        env = job_proxy_env(self.JOB, self.ENV)
        self.assertEqual(env["LLM_PROXY_URL"], "http://127.0.0.1:18795/llm")
        claims = verify_proxy_token(env["LLM_PROXY_TOKEN"], secret=SECRET)
        self.assertEqual(
            claims,
            {"tenant": "acme", "program": "p-1", "workflow": "wf-1", "task": "task-1", "user": "dev@acme.test",
             "agent": "coding-backend", "job": "job-1"},
        )

    def test_without_the_secret_workers_keep_provider_keys(self):
        env = {k: v for k, v in self.ENV.items() if k != "LLM_PROXY_SECRET"}
        self.assertIn("OPENAI_API_KEY", worker_env_names(env))
        self.assertEqual(job_proxy_env(self.JOB, env), {})

    def test_local_docker_passes_the_token_by_name(self):
        launcher = LocalDockerLauncher(LocalDockerConfig(), environ=self.ENV)
        cmd = launcher.command(self.JOB)
        self.assertIn("LLM_PROXY_URL=http://127.0.0.1:18795/llm", cmd)
        self.assertIn("LLM_PROXY_TOKEN", cmd)
        self.assertNotIn("OPENAI_API_KEY", cmd)
        self.assertFalse(any(part.startswith("LLM_PROXY_TOKEN=") for part in cmd))

    def test_ecs_override_carries_the_worker_proxy_url_and_token(self):
        env = {**self.ENV, "CODING_WORKER_LLM_PROXY_URL": "http://internal-lb/llm"}
        launcher = EcsLauncher(EcsConfig("c", "td", ["s"], ["sg"]), client=object(), environ=env)
        overrides = launcher.run_task_request(self.JOB, "job-secret")["overrides"]["containerOverrides"][0]["environment"]
        names = {item["name"]: item["value"] for item in overrides}
        self.assertEqual(names["LLM_PROXY_URL"], "http://internal-lb/llm")
        self.assertEqual(verify_proxy_token(names["LLM_PROXY_TOKEN"], secret=SECRET)["job"], "job-1")


if __name__ == "__main__":
    unittest.main()
