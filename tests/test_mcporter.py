"""The mcporter Atlassian MCP bridge: auth modes, Jira read and write tools, and the stdio protocol."""

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
from typing import Any
from unittest import mock

from ts_harness.integrations.atlassian_auth import TOKEN_URL

REPO_ROOT = Path(__file__).resolve().parents[1]
MCPORTER = REPO_ROOT / "scripts" / "mcporter"
BASIC_ENV = {"ATLASSIAN_SITE_URL": "acme.atlassian.net", "ATLASSIAN_EMAIL": "bot@acme.io", "ATLASSIAN_API_TOKEN": "t"}
OAUTH_ENV = {"ATLASSIAN_CLIENT_ID": "id", "ATLASSIAN_CLIENT_SECRET": "secret", "ATLASSIAN_CLOUD_ID": "cloud-1"}
ISSUES = {"issues": [{"key": "PROJ-1", "fields": {"summary": "Fix login", "status": {"name": "To Do"}}}]}


def load_mcporter():
    loader = importlib.machinery.SourceFileLoader("mcporter_under_test", str(MCPORTER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class McporterTestCase(unittest.TestCase):
    def setUp(self):
        self.module = load_mcporter()
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.responses: list[tuple[int, Any]] = []
        self.tokens = 0

        def transport(method, url, headers, body):
            self.calls.append((method, url, headers, json.loads(body) if body else None))
            if url == TOKEN_URL:
                self.tokens += 1
                return 200, json.dumps({"access_token": f"tok-{self.tokens}", "expires_in": 3600}).encode(), {}
            status, payload = self.responses.pop(0) if self.responses else (200, ISSUES)
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            return status, raw, {}

        self.module.transport = transport

    def call(self, name: str, arguments: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> dict:
        with mock.patch.dict("os.environ", BASIC_ENV if env is None else env, clear=True):
            return self.module._handle_call({"name": name, "arguments": arguments or {}})

    def text(self, result: dict) -> str:
        return result["content"][0]["text"]

    def api_calls(self):
        return [c for c in self.calls if c[1] != TOKEN_URL]


class McporterSearchTests(McporterTestCase):
    def test_not_configured_names_both_modes(self):
        result = self.call("atlassian_search", {"query": "login"}, env={})
        self.assertTrue(result["isError"])
        self.assertIn("ATLASSIAN_CLIENT_ID", self.text(result))
        self.assertIn("ATLASSIAN_API_TOKEN", self.text(result))
        self.assertEqual(self.calls, [])

    def test_basic_mode_hits_site_search_jql(self):
        self.assertEqual(self.text(self.call("atlassian_search", {"query": "login"})), "PROJ-1: Fix login [To Do]")
        (method, url, headers, _), = self.api_calls()
        parsed = urllib.parse.urlparse(url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "https://acme.atlassian.net/rest/api/3/search/jql")
        self.assertEqual(urllib.parse.parse_qs(parsed.query)["jql"], ['text ~ "login"'])
        self.assertTrue(headers["Authorization"].startswith("Basic "))

    def test_oauth_mode_hits_gateway_with_bearer(self):
        self.call("jira_search", {"jql": "project = PROJ"}, env=OAUTH_ENV)
        (method, url, headers, _), = self.api_calls()
        self.assertTrue(url.startswith("https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/search/jql?"))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["jql"], ["project = PROJ"])
        self.assertEqual(query["maxResults"], ["20"])
        self.assertEqual(headers["Authorization"], "Bearer tok-1")

    def test_oauth_retries_once_after_401(self):
        self.responses = [(401, b"expired"), (200, ISSUES)]
        result = self.call("atlassian_search", {"query": "login"}, env=OAUTH_ENV)
        self.assertEqual(self.text(result), "PROJ-1: Fix login [To Do]")
        self.assertEqual([c[2]["Authorization"] for c in self.api_calls()], ["Bearer tok-1", "Bearer tok-2"])

    def test_unbounded_jql_is_refused_before_calling_jira(self):
        for name, arguments in (
            ("jira_search", {"jql": "order by created DESC"}),
            ("jira_search", {"jql": "  ORDER BY\n updated"}),
            ("atlassian_search", {"query": "ORDER BY created DESC"}),
        ):
            with self.subTest(name=name, arguments=arguments):
                result = self.call(name, arguments)
                self.assertTrue(result["isError"])
                self.assertIn("search restriction", self.text(result))
        self.assertEqual(self.api_calls(), [])

    def test_bounded_jql_with_order_by_is_sent(self):
        self.call("jira_search", {"jql": "project = PROJ ORDER BY created DESC"})
        (_, url, _, _), = self.api_calls()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["jql"], ["project = PROJ ORDER BY created DESC"])

    def test_error_status_is_reported(self):
        self.responses = [(403, b"denied")]
        result = self.call("atlassian_search", {"query": "login"})
        self.assertTrue(result["isError"])
        self.assertEqual(self.text(result), "Atlassian search failed (403): denied")


class McporterReadToolTests(McporterTestCase):
    def test_get_issue_renders_fields_and_description(self):
        self.responses = [
            (
                200,
                {
                    "key": "PROJ-7",
                    "fields": {
                        "summary": "Checkout",
                        "status": {"name": "Open"},
                        "issuetype": {"name": "Story"},
                        "parent": {"key": "PROJ-1"},
                        "labels": ["prd"],
                        "issuelinks": [{"type": {"outward": "blocks"}, "outwardIssue": {"key": "PROJ-8"}}],
                        "description": {
                            "type": "doc",
                            "version": 1,
                            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "WO: WO-003"}]}],
                        },
                    },
                },
            )
        ]
        text = self.text(self.call("jira_get_issue", {"issue_key": "proj-7"}))
        self.assertIn("PROJ-7: Checkout [Open] (Story)", text)
        self.assertIn("Parent: PROJ-1", text)
        self.assertIn("Link: blocks PROJ-8", text)
        self.assertIn("Assignee: unassigned", text)
        self.assertIn("WO: WO-003", text)
        self.assertIn("/rest/api/3/issue/PROJ-7?fields=", self.api_calls()[0][1])

    def test_get_all_projects_with_create_action(self):
        self.responses = [(200, {"values": [{"key": "PROJ", "name": "Foundry"}]})]
        self.assertEqual(self.text(self.call("jira_get_all_projects", {"action": "create"})), "PROJ: Foundry")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.api_calls()[0][1]).query)
        self.assertEqual(query["action"], ["create"])


class McporterWriteToolTests(McporterTestCase):
    def test_create_issue_builds_fields_without_assignee(self):
        self.responses = [(201, {"id": "1", "key": "PROJ-9"})]
        result = self.call(
            "jira_create_issue",
            {
                "project_key": "proj",
                "issue_type": "Epic",
                "summary": "Checkout",
                "description": "Line one\nLine two\n\nSecond paragraph",
                "labels": ["prd"],
                "components": ["web"],
                "priority": "Medium",
                "parent_key": "proj-1",
                "assignee": "someone",
            },
        )
        self.assertEqual(self.text(result), "Created PROJ-9 https://acme.atlassian.net/browse/PROJ-9")
        method, url, _, body = self.api_calls()[0]
        self.assertEqual((method, url), ("POST", "https://acme.atlassian.net/rest/api/3/issue"))
        fields = body["fields"]
        self.assertNotIn("assignee", fields)
        self.assertEqual(fields["project"], {"key": "PROJ"})
        self.assertEqual(fields["issuetype"], {"name": "Epic"})
        self.assertEqual(fields["parent"], {"key": "PROJ-1"})
        self.assertEqual(fields["components"], [{"name": "web"}])
        self.assertEqual(fields["priority"], {"name": "Medium"})
        paragraphs = fields["description"]["content"]
        self.assertEqual(len(paragraphs), 2)
        self.assertEqual([node["type"] for node in paragraphs[0]["content"]], ["text", "hardBreak", "text"])

    def test_create_issue_requires_project_type_and_summary(self):
        result = self.call("jira_create_issue", {"project_key": "PROJ", "summary": "x"})
        self.assertTrue(result["isError"])
        self.assertIn("issue_type is required", self.text(result))
        self.assertEqual(self.calls, [])

    def test_batch_create_reports_created_and_failed(self):
        self.responses = [
            (
                201,
                {
                    "issues": [{"key": "PROJ-10"}],
                    "errors": [{"failedElementNumber": 1, "elementErrors": {"errors": {"issuetype": "invalid"}}}],
                },
            )
        ]
        issue = {"project_key": "PROJ", "issue_type": "Story", "summary": "a", "parent_key": "PROJ-1"}
        text = self.text(self.call("jira_batch_create_issues", {"issues": [issue, {**issue, "issue_type": "Nope"}]}))
        self.assertIn("Created PROJ-10", text)
        self.assertIn("Failed item 1", text)
        _, url, _, body = self.api_calls()[0]
        self.assertTrue(url.endswith("/rest/api/3/issue/bulk"))
        self.assertEqual(len(body["issueUpdates"]), 2)

    def test_batch_create_caps_issue_count(self):
        issue = {"project_key": "PROJ", "issue_type": "Story", "summary": "a"}
        result = self.call("jira_batch_create_issues", {"issues": [issue] * 51})
        self.assertTrue(result["isError"])
        self.assertEqual(self.calls, [])

    def test_create_issue_link_makes_outward_issue_block_inward_issue(self):
        self.responses = [(201, b"")]
        args = {"link_type": "Blocks", "inward_issue_key": "PROJ-2", "outward_issue_key": "PROJ-3"}
        self.assertEqual(self.text(self.call("jira_create_issue_link", args)), "Linked PROJ-3 -> PROJ-2 (Blocks)")
        _, url, _, body = self.api_calls()[0]
        self.assertTrue(url.endswith("/rest/api/3/issueLink"))
        # Jira gives the outward description ("blocks") to the POST body's inwardIssue.
        self.assertEqual(body, {"type": {"name": "Blocks"}, "inwardIssue": {"key": "PROJ-3"}, "outwardIssue": {"key": "PROJ-2"}})

    def test_add_comment_sends_adf(self):
        self.responses = [(201, {"id": "c1"})]
        self.assertEqual(self.text(self.call("jira_add_comment", {"issue_key": "PROJ-2", "comment": "done"})), "Commented on PROJ-2")
        _, url, _, body = self.api_calls()[0]
        self.assertTrue(url.endswith("/rest/api/3/issue/PROJ-2/comment"))
        self.assertEqual(body["body"]["type"], "doc")

    def test_update_issue_sets_fields_and_label_changes(self):
        self.responses = [(204, b"")]
        args = {
            "issue_key": "proj-2",
            "summary": "New title",
            "description": "## Story\n- [ ] task",
            "add_labels": ["enabler"],
            "remove_labels": ["draft"],
        }
        self.assertEqual(self.text(self.call("jira_update_issue", args)), "Updated PROJ-2: summary, description, labels")
        method, url, _, body = self.api_calls()[0]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/rest/api/3/issue/PROJ-2"))
        self.assertEqual(body["fields"]["summary"], "New title")
        self.assertEqual([block["type"] for block in body["fields"]["description"]["content"]], ["heading", "taskList"])
        self.assertEqual(body["update"], {"labels": [{"add": "enabler"}, {"remove": "draft"}]})
        self.assertNotIn("assignee", body["fields"])

    def test_update_issue_ignores_assignee(self):
        self.responses = [(204, b"")]
        self.call("jira_update_issue", {"issue_key": "PROJ-2", "summary": "x", "assignee": "someone"})
        _, _, _, body = self.api_calls()[0]
        self.assertEqual(body, {"fields": {"summary": "x"}})

    def test_update_issue_needs_a_change(self):
        result = self.call("jira_update_issue", {"issue_key": "PROJ-2"})
        self.assertTrue(result["isError"])
        self.assertIn("nothing to update", self.text(result))
        self.assertEqual(self.calls, [])

    def test_unknown_tool(self):
        result = self.call("jira_delete_issue", {"issue_key": "PROJ-1"})
        self.assertTrue(result["isError"])
        self.assertEqual(self.calls, [])


class McporterStdioTests(unittest.TestCase):
    def test_tools_list_and_unconfigured_call(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "jira_search", "arguments": {"jql": "x"}}},
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
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "atlassian_search",
                "jira_search",
                "jira_get_issue",
                "jira_get_all_projects",
                "jira_create_issue",
                "jira_batch_create_issues",
                "jira_create_issue_link",
                "jira_add_comment",
                "jira_update_issue",
            },
        )
        self.assertIn("not configured", called["result"]["content"][0]["text"])

    def test_tool_names_match_toolsets_in_runtime_config(self):
        import yaml

        names = {f"mcp-atlassian-{tool['name']}" for tool in load_mcporter().TOOLS}
        for config_name in ("ts-harness-runtime.yaml.tmpl", "ts-harness-runtime.test.yaml"):
            toolsets = yaml.safe_load((REPO_ROOT / "config" / config_name).read_text())["toolsets"]
            self.assertEqual(set(toolsets["jira-read"]) | set(toolsets["jira-write"]), names, config_name)


if __name__ == "__main__":
    unittest.main()
