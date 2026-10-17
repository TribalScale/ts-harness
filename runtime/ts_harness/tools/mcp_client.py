"""MCP client lifecycle adapted from Hermes mcp_tool.py (MIT)."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any
import json
import os
import re
import subprocess
import time
import urllib.request

from ts_harness.tools.mcp_registry import set_registered_mcp_tool_names
from ts_harness.tools.mcp_sdk import discover_tools_stdio, sdk_available
from ts_harness.tools.registry import ToolDefinition, ToolRegistry


SENSITIVE_ENV_TOKENS = ("TOKEN", "SECRET", "PASSWORD", "KEY", "AUTH")
SAFE_STDIO_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "SAFE_FLAG", "PYTHONPATH"}
SUPPORTED_TRANSPORTS = {"stdio", "streamable_http", "sse"}


@dataclass
class MCPServerState:
    name: str
    config: dict[str, Any]
    transport: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    process: subprocess.Popen | None = None
    request_id: int = 0
    failures: int = 0
    circuit_open_until: float = 0.0
    healthy: bool = True


class MCPClientManager:
    COOLDOWN_SECONDS = 60

    def __init__(self, *, cooldown_seconds: float | None = None):
        self._servers: dict[str, MCPServerState] = {}
        self._lock = Lock()
        self.cooldown_seconds = cooldown_seconds if cooldown_seconds is not None else self.COOLDOWN_SECONDS

    def register_server(self, name: str, config: dict[str, Any]) -> None:
        sanitized = dict(config)
        transport = str(sanitized.get("transport", "stdio"))
        if transport not in SUPPORTED_TRANSPORTS:
            raise ValueError(f"unsupported MCP transport: {transport}")
        if transport == "stdio":
            # Keep operator-provided env (resolved secret: refs). Host env is still
            # filtered when the stdio process is spawned.
            sanitized["env"] = {
                str(key): str(value) for key, value in dict(sanitized.get("env") or {}).items()
            }
        with self._lock:
            self._servers[name] = MCPServerState(name=name, config=sanitized, transport=transport)

    def server_state(self, name: str) -> MCPServerState:
        state = self._servers.get(name)
        if state is None:
            raise KeyError(f"unknown mcp server: {name}")
        return state

    def list_tools(self, server_name: str) -> list[dict[str, Any]]:
        state = self._servers.get(server_name)
        if state is None:
            return []
        if time.time() < state.circuit_open_until:
            return []
        return list(state.tools)

    def refresh_tools(self, server_name: str, tools: list[dict[str, Any]]) -> None:
        state = self._servers.get(server_name)
        if state is None:
            return
        include = set(state.config.get("include_tools") or [])
        exclude = set(state.config.get("exclude_tools") or [])
        prefixed = []
        for tool in tools:
            tool_name = tool.get("name", "unknown")
            if include and tool_name not in include:
                continue
            if tool_name in exclude:
                continue
            prefixed.append(
                {
                    "name": f"mcp-{server_name}-{tool_name}",
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema") or tool.get("parameters") or {},
                    "server": server_name,
                }
            )
        state.tools = prefixed
        state.failures = 0
        state.circuit_open_until = 0.0
        state.healthy = True

    def discover_tools(self, server_name: str) -> list[dict[str, Any]]:
        state = self.server_state(server_name)
        try:
            tools = self._list_tools(state)
            self.refresh_tools(server_name, tools)
            return self.list_tools(server_name)
        except Exception as exc:  # noqa: BLE001
            self.record_failure(server_name)
            state.healthy = False
            state.config["last_error"] = self.redact_error(str(exc))
            return []

    def handle_notification(self, server_name: str, method: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if method in {"notifications/tools/list_changed", "tools/list_changed"}:
            return self.discover_tools(server_name)
        return self.list_tools(server_name)

    def _list_tools(self, state: MCPServerState) -> list[dict[str, Any]]:
        use_sdk = (
            state.transport == "stdio"
            and os.environ.get("TS_HARNESS_MCP_SDK", "").lower() == "true"
            and sdk_available()
        )
        if use_sdk:
            command = str(state.config.get("command") or "")
            args = [str(arg) for arg in (state.config.get("args") or [])]
            env = {**self._filter_stdio_env(dict(os.environ)), **(state.config.get("env") or {})}
            return discover_tools_stdio(command, args, env)
        response = self._jsonrpc(state, "tools/list", {})
        return response.get("tools") or response.get("result", {}).get("tools") or []

    def record_failure(self, server_name: str) -> None:
        state = self._servers.get(server_name)
        if state is None:
            return
        state.failures += 1
        if state.failures >= 3:
            state.circuit_open_until = time.time() + self.cooldown_seconds
            state.healthy = False

    def call_tool(self, server_name: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if server_name not in self._servers:
            return {"error": "unknown mcp server"}
        if time.time() < self._servers[server_name].circuit_open_until:
            return {"error": "mcp circuit breaker open"}
        state = self._servers[server_name]
        names = {tool["name"].removeprefix(f"mcp-{server_name}-") for tool in state.tools}
        if state.tools and tool_name not in names:
            return {"error": "unknown mcp tool"}
        try:
            result = self._jsonrpc(state, "tools/call", {"name": tool_name, "arguments": args})
            return {"status": "ok", "server": server_name, "transport": state.transport, "tool": tool_name, "result": result}
        except Exception as exc:  # noqa: BLE001
            self.record_failure(server_name)
            return {"error": self.redact_error(str(exc))}

    def register_tools(self, registry: ToolRegistry) -> None:
        discovered: set[str] = set()
        reserved = set(registry.snapshot())
        for state in list(self._servers.values()):
            for tool in state.tools:
                stable_name = tool["name"]
                if stable_name in reserved:
                    state.config["last_error"] = f"builtin tool collision: {stable_name}"
                    continue
                raw_tool_name = stable_name.removeprefix(f"mcp-{state.name}-")
                discovered.add(stable_name)

                def handler(args: dict[str, Any], ctx, server=state.name, name=raw_tool_name):
                    result = self.call_tool(server, name, args)
                    if "error" in result:
                        result["error"] = self.redact_error(str(result["error"]))
                    return result

                registry.register(
                    ToolDefinition(
                        name=stable_name,
                        description=tool.get("description", ""),
                        parameters=tool.get("parameters") or {"type": "object"},
                        handler=handler,
                        toolsets=["mcp"],
                    )
                )
        set_registered_mcp_tool_names(discovered)

    def close_all(self) -> None:
        with self._lock:
            for state in self._servers.values():
                if state.process and state.process.poll() is None:
                    state.process.terminate()
                state.process = None

    def health(self) -> bool:
        if not self._servers:
            return True
        return all(server.healthy for server in self._servers.values())

    def _jsonrpc(self, state: MCPServerState, method: str, params: dict[str, Any]) -> dict[str, Any]:
        state.request_id += 1
        request = {"jsonrpc": "2.0", "id": state.request_id, "method": method, "params": params}
        if state.transport == "stdio":
            return self._stdio_jsonrpc(state, request)
        return self._http_jsonrpc(state, request)

    def _stdio_jsonrpc(self, state: MCPServerState, request: dict[str, Any]) -> dict[str, Any]:
        if state.process is None or state.process.poll() is not None:
            command = state.config.get("command")
            if not command:
                raise RuntimeError("stdio MCP server command is required")
            args = [command, *list(state.config.get("args") or [])]
            state.process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env={**self._filter_stdio_env(dict(os.environ)), **(state.config.get("env") or {})},
            )
        assert state.process.stdin is not None and state.process.stdout is not None
        state.process.stdin.write(json.dumps(request) + "\n")
        state.process.stdin.flush()
        line = state.process.stdout.readline()
        if not line:
            raise RuntimeError("empty MCP stdio response")
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response.get("result", response)

    def _http_jsonrpc(self, state: MCPServerState, request: dict[str, Any]) -> dict[str, Any]:
        url = state.config.get("url")
        if not url:
            raise RuntimeError(f"{state.transport} MCP server url is required")
        body = json.dumps(request).encode()
        headers = {"Content-Type": "application/json"}
        extra = state.config.get("headers") or {}
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key and value is not None:
                    headers[str(key)] = str(value)
        http_request = urllib.request.Request(
            str(url),
            data=body,
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(http_request, timeout=int(state.config.get("timeout", 30))) as response:
            payload = json.loads(response.read())
        if "error" in payload:
            raise RuntimeError(payload["error"])
        return payload.get("result", payload)

    def redact_error(self, message: str) -> str:
        redacted = message
        for key, value in os.environ.items():
            if self._is_sensitive_name(key) and value:
                redacted = redacted.replace(value, "[REDACTED]")
        redacted = re.sub(r"Authorization:\s*Bearer\s+\S+", "Authorization: Bearer [REDACTED]", redacted)
        redacted = re.sub(r"(?i)(token|password|secret|api[_-]?key)=\S+", r"\1=[REDACTED]", redacted)
        return redacted

    def _filter_stdio_env(self, env: dict[str, str]) -> dict[str, str]:
        return {
            key: value
            for key, value in env.items()
            if key in SAFE_STDIO_ENV and not self._is_sensitive_name(key)
        }

    def _is_sensitive_name(self, name: str) -> bool:
        upper = name.upper()
        return any(token in upper for token in SENSITIVE_ENV_TOKENS)


_manager = MCPClientManager()


def get_mcp_manager() -> MCPClientManager:
    return _manager
