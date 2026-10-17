import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.tools.mcp_client import MCPClientManager
from ts_harness.tools.policy import ToolPolicyContext
from ts_harness.tools.registry import ToolDefinition, ToolRegistry


class MCPClientTests(unittest.TestCase):
    def _mcp_fixture(self, root: Path) -> Path:
        script = root / "mcp_fixture.py"
        script.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    req=json.loads(line)\n"
            "    method=req.get('method')\n"
            "    if method == 'tools/list':\n"
            "        result={'tools':[{'name':'search','description':'Search Jira','inputSchema':{'type':'object'}}]}\n"
            "    elif method == 'tools/call':\n"
            "        result={'content':[{'type':'text','text':'called '+req['params']['name']}], 'arguments': req['params'].get('arguments')}\n"
            "    else:\n"
            "        result={}\n"
            "    print(json.dumps({'jsonrpc':'2.0','id':req.get('id'), 'result': result}), flush=True)\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def test_register_server_filters_sensitive_environment(self):
        manager = MCPClientManager()
        manager.register_server(
            "atlassian",
            {
                "transport": "stdio",
                "command": "mcporter",
                "env": {
                    "PATH": "/bin",
                    "ATLASSIAN_TOKEN": "secret",
                    "SAFE_FLAG": "1",
                },
            },
        )

        state = manager.server_state("atlassian")

        self.assertEqual(state.transport, "stdio")
        self.assertEqual(
            state.config["env"],
            {"PATH": "/bin", "ATLASSIAN_TOKEN": "secret", "SAFE_FLAG": "1"},
        )

    def test_health_is_false_when_any_server_is_unhealthy(self):
        manager = MCPClientManager()
        manager.register_server("good", {"transport": "sse", "url": "https://good.example/mcp"})
        manager.register_server("bad", {"transport": "sse", "url": "https://bad.example/mcp"})
        manager.server_state("bad").healthy = False
        self.assertFalse(manager.health())
        manager.server_state("bad").healthy = True
        self.assertTrue(manager.health())

    def test_http_jsonrpc_sends_configured_headers(self):
        manager = MCPClientManager()
        manager.register_server(
            "figma",
            {
                "transport": "streamable_http",
                "url": "https://mcp.example/mcp",
                "headers": {"Authorization": "Bearer resolved-token"},
            },
        )
        captured: dict[str, str] = {}

        class Resp:
            def read(self):
                return b'{"result":{"tools":[{"name":"get_file"}]}}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=30):
            captured.update({key.lower(): value for key, value in req.header_items()})
            return Resp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            tools = manager.discover_tools("figma")
        self.assertTrue(any(tool["name"].endswith("get_file") for tool in tools))
        self.assertEqual(captured.get("authorization"), "Bearer resolved-token")

    def test_failed_server_opens_circuit_and_recovers_after_successful_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._mcp_fixture(Path(tmp))
            manager = MCPClientManager(cooldown_seconds=0.01)
            manager.register_server("atlassian", {"transport": "stdio", "command": sys.executable, "args": [str(fixture)]})

            manager.record_failure("atlassian")
            manager.record_failure("atlassian")
            manager.record_failure("atlassian")

            self.assertEqual(manager.call_tool("atlassian", "search", {})["error"], "mcp circuit breaker open")

            manager.discover_tools("atlassian")

            self.assertEqual(manager.call_tool("atlassian", "search", {})["status"], "ok")

    def test_mcp_tools_register_with_stable_names_and_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._mcp_fixture(Path(tmp))
            manager = MCPClientManager()
            registry = ToolRegistry()
            manager.register_server("atlassian", {"transport": "stdio", "command": sys.executable, "args": [str(fixture)]})
            manager.discover_tools("atlassian")

            manager.register_tools(registry)

            ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="workflow")
            result = registry.dispatch("mcp-atlassian-search", {"query": "ABC-1"}, context=ctx)
            self.assertEqual(result["server"], "atlassian")
            self.assertEqual(result["tool"], "search")
            self.assertEqual(result["result"]["content"][0]["text"], "called search")

    def test_include_exclude_filters_limit_registered_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._mcp_fixture(Path(tmp))
            manager = MCPClientManager()
            manager.register_server(
                "atlassian",
                {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(fixture)],
                    "include_tools": ["search"],
                },
            )
            manager.discover_tools("atlassian")
            names = {tool["name"] for tool in manager.list_tools("atlassian")}
            self.assertEqual(names, {"mcp-atlassian-search"})

            manager.register_server(
                "atlassian",
                {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(fixture)],
                    "exclude_tools": ["search"],
                },
            )
            manager.discover_tools("atlassian")
            self.assertEqual(manager.list_tools("atlassian"), [])

    def test_list_changed_notification_refreshes_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._mcp_fixture(Path(tmp))
            manager = MCPClientManager()
            manager.register_server("atlassian", {"transport": "stdio", "command": sys.executable, "args": [str(fixture)]})
            manager.discover_tools("atlassian")
            refreshed = manager.handle_notification("atlassian", "notifications/tools/list_changed")
            self.assertEqual(len(refreshed), 1)
            self.assertEqual(refreshed[0]["name"], "mcp-atlassian-search")

    def test_builtin_tool_collision_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._mcp_fixture(Path(tmp))
            manager = MCPClientManager()
            registry = ToolRegistry()
            registry.register(
                ToolDefinition(
                    name="mcp-atlassian-search",
                    description="builtin placeholder",
                    parameters={"type": "object"},
                    handler=lambda args, ctx: {"status": "builtin"},
                    toolsets=["core"],
                )
            )
            manager.register_server("atlassian", {"transport": "stdio", "command": sys.executable, "args": [str(fixture)]})
            manager.discover_tools("atlassian")
            manager.register_tools(registry)
            self.assertEqual(len(registry.snapshot()), 1)
            ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="workflow")
            result = registry.dispatch("mcp-atlassian-search", {}, context=ctx)
            self.assertEqual(result["status"], "builtin")

    def test_http_transport_discovers_tools(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                request = json.loads(raw)
                response = {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": {
                        "tools": [
                            {
                                "name": "search",
                                "description": "HTTP search",
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                }
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            manager = MCPClientManager()
            manager.register_server(
                "atlassian",
                {"transport": "streamable_http", "url": f"http://127.0.0.1:{port}/mcp"},
            )
            tools = manager.discover_tools("atlassian")
            self.assertEqual(tools[0]["name"], "mcp-atlassian-search")
        finally:
            server.shutdown()

    def test_model_visible_errors_redact_credentials(self):
        manager = MCPClientManager()
        os.environ["ATLASSIAN_TOKEN"] = "super-secret-token"
        try:
            error = manager.redact_error("failed with super-secret-token and Authorization: Bearer abc")
        finally:
            os.environ.pop("ATLASSIAN_TOKEN", None)

        self.assertNotIn("super-secret-token", error)
        self.assertNotIn("Bearer abc", error)
        self.assertIn("[REDACTED]", error)


if __name__ == "__main__":
    unittest.main()
