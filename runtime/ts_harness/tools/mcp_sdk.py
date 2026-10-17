"""Optional MCP Python SDK transport (falls back to stdio JSON-RPC when unavailable)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine, TypeVar


T = TypeVar("T")
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()


def sdk_available() -> bool:
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


def _ensure_background_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop
        loop = asyncio.new_event_loop()

        def runner() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=runner, daemon=True, name="ec-mcp-sdk-loop")
        thread.start()
        _loop = loop
        _loop_thread = thread
        return loop


def run_sdk_coroutine(coro: Coroutine[Any, Any, T]) -> T:
    loop = _ensure_background_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=120)


def discover_tools_stdio(command: str, args: list[str], env: dict[str, str]) -> list[dict[str, Any]]:
    """Discover tools via the official MCP SDK when installed."""
    if not sdk_available():
        raise RuntimeError("mcp package is not installed")

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=command, args=args, env=env)

    async def _run() -> list[dict[str, Any]]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return [
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": tool.inputSchema or {"type": "object"},
                    }
                    for tool in result.tools
                ]

    return run_sdk_coroutine(_run())
