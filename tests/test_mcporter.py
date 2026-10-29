"""The mcporter Atlassian MCP bridge: auth modes, the search/jql endpoint and stdio protocol."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from ts_harness.integrations.atlassian_auth import TOKEN_URL

REPO_ROOT = Path(__file__).resolve().parents[1]
MCPORTER = REPO_ROOT / "scripts" / "mcporter"


def load_mcporter():
    loader = importlib.machinery.SourceFileLoader("mcporter_under_test", str(MCPORTER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ISSUES = {"issues": [{"key": "TFS-1", "fields": {"summary": "Fix login", "status": {"name": "To Do"}}}]}


class McporterSearchTests(unittest.TestCase):
    def setUp(self):
        self.module = load_mcporter()
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.search_statuses: list[int] = []
        self.tokens = 0

        def transport(method, url, headers, body):
            self.calls.append((method, url, headers))
            if url == TOKEN_URL:
                self.tokens += 1
                return 200, json.dumps({"access_token": f"tok-{self.tokens}", "expires_in": 3600}).encode(), {}
            status = self.search_statuses.pop(0) if self.search_statuses else 200
            return status, (json.dumps(ISSUES).encode() if status == 200 else b"denied"), {}

        self.module.transport = transport

    def _search(self, env: dict[str, str], query: str = "login") -> str:
        with mock.patch.dict("os.environ", env, clear=True):
            return self.module._atlassian_search(query)

    def _search_calls(self):
        return [c for c in self.calls if "/search/jql" in c[1]]

    def test_not_configured_names_both_modes(self):
        text = self._search({})
        self.assertIn("ATLASSIAN_CLIENT_ID", text)
        self.assertIn("ATLASSIAN_API_TOKEN", text)
        self.assertEqual(self.calls, [])

    def test_basic_mode_hits_site_search_jql(self):
        text = self._search({"ATLASSIAN_SITE_URL": "acme.atlassian.net", "ATLASSIAN_EMAIL": "bot@acme.io", "ATLASSIAN_API_TOKEN": "t"})
        self.assertEqual(text, "TFS-1: Fix login [To Do]")
        (method, url, headers), = self._search_calls()
        parsed = urllib.parse.urlparse(url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "https://acme.atlassian.net/rest/api/3/search/jql")
        self.assertEqual(urllib.parse.parse_qs(parsed.query)["jql"], ['text ~ "login"'])
        self.assertTrue(headers["Authorization"].startswith("Basic "))

    def test_oauth_mode_hits_gateway_with_bearer(self):
        env = {"ATLASSIAN_CLIENT_ID": "id", "ATLASSIAN_CLIENT_SECRET": "secret", "ATLASSIAN_CLOUD_ID": "cloud-1"}
        self._search(env, "project = TFS")
        (method, url, headers), = self._search_calls()
        self.assertTrue(url.startswith("https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/search/jql?"))
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["jql"], ["project = TFS"])
        self.assertEqual(headers["Authorization"], "Bearer tok-1")

    def test_oauth_retries_once_after_401(self):
        self.search_statuses = [401, 200]
        env = {"ATLASSIAN_CLIENT_ID": "id", "ATLASSIAN_CLIENT_SECRET": "secret", "ATLASSIAN_CLOUD_ID": "cloud-1"}
        self.assertEqual(self._search(env), "TFS-1: Fix login [To Do]")
        self.assertEqual([c[2]["Authorization"] for c in self._search_calls()], ["Bearer tok-1", "Bearer tok-2"])

    def test_error_status_is_reported(self):
        self.search_statuses = [403]
        text = self._search({"ATLASSIAN_SITE_URL": "acme.atlassian.net", "ATLASSIAN_EMAIL": "bot@acme.io", "ATLASSIAN_API_TOKEN": "t"})
        self.assertEqual(text, "Atlassian search failed (403): denied")


class McporterStdioTests(unittest.TestCase):
    def test_tools_list_and_unconfigured_call(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "atlassian_search", "arguments": {"query": "x"}}},
        ]
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run(
            [sys.executable, str(MCPORTER)],
            input="".join(json.dumps(r) + "\n" for r in requests),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        listed, called = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(listed["result"]["tools"][0]["name"], "atlassian_search")
        self.assertIn("not configured", called["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
