"""Server-Sent Events (SSE) draft for /session/message stream variant.

This module provides helpers for writing an SSE response from inside the
stdlib ``BaseHTTPRequestHandler``.  A long-lived GET or POST to
``/session/message/stream`` keeps the connection open and pushes partial
results as SSE events.

Usage (inside ``GatewayHandler.do_GET`` or ``do_POST``)::

    from ts_harness.gateway.streaming import SSEWriter, stream_agent_run

    writer = SSEWriter(self)
    stream_agent_run(writer, runtime, body, auth)

Note: SSE is not compatible with the default ``ThreadingHTTPServer``
keep-alive timeout out of the box – configure ``server.socket.settimeout``
appropriately and extend ``GatewayHandler.do_POST`` to route this path.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Generator


class SSEWriter:
    """Wraps a ``BaseHTTPRequestHandler`` to emit Server-Sent Events.

    Sends the SSE preamble on first use and provides ``send_event`` /
    ``send_data`` helpers.
    """

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self._headers_sent = False

    def _send_headers(self) -> None:
        if self._headers_sent:
            return
        h = self._handler
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("X-Accel-Buffering", "no")
        h.send_header("Connection", "keep-alive")
        h.end_headers()
        self._headers_sent = True

    def send_event(
        self,
        data: str | dict[str, Any],
        *,
        event: str | None = None,
        id: str | None = None,  # noqa: A002
        retry_ms: int | None = None,
    ) -> None:
        """Write a single SSE message to the response stream."""
        self._send_headers()
        lines: list[str] = []
        if retry_ms is not None:
            lines.append(f"retry: {retry_ms}")
        if id is not None:
            lines.append(f"id: {id}")
        if event is not None:
            lines.append(f"event: {event}")
        payload = json.dumps(data) if isinstance(data, dict) else str(data)
        for line in payload.split("\n"):
            lines.append(f"data: {line}")
        message = "\n".join(lines) + "\n\n"
        try:
            self._handler.wfile.write(message.encode("utf-8"))
            self._handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_ping(self) -> None:
        """Send a comment ping to keep the connection alive."""
        self._send_headers()
        try:
            self._handler.wfile.write(b": ping\n\n")
            self._handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_done(self) -> None:
        """Send a terminal ``done`` event."""
        self.send_event({"status": "done"}, event="done")


def iter_agent_tokens(
    runtime: Any,
    body: dict[str, Any],
    auth: Any,
) -> Generator[dict[str, Any], None, None]:
    """Yield incremental token/chunk dicts from an agent run.

    This is a *draft* implementation that wraps the synchronous
    ``dispatch_workflow`` call and yields the final result as a single chunk.
    A production implementation would hook into the provider's streaming API.
    """
    yield {"event": "start", "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    try:
        result = runtime.dispatch_workflow(body, auth)
        output = result.output or {}
        # Emit chunks if the output contains a "text" stream key; otherwise
        # emit the full result as one chunk.
        text: str = str(
            output.get("summary") or output.get("message") or output.get("text") or ""
        )
        if text:
            # Simulate token-level streaming by splitting on whitespace.
            words = text.split()
            chunk_size = max(1, len(words) // 10)
            for i in range(0, len(words), chunk_size):
                chunk = " ".join(words[i : i + chunk_size])
                yield {"event": "chunk", "text": chunk + " "}
        yield {
            "event": "result",
            "status": result.status,
            "output": output,
            "runtime_task_id": result.runtime_task_id,
        }
    except Exception as exc:
        yield {"event": "error", "error": str(exc)}


def stream_agent_run(
    writer: SSEWriter,
    runtime: Any,
    body: dict[str, Any],
    auth: Any,
    *,
    ping_interval: float = 15.0,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Drive ``iter_agent_tokens`` and push each chunk via *writer*.

    *ping_interval* seconds between chunks triggers a keepalive comment.
    """
    writer.send_event({"status": "connected"}, event="connected", retry_ms=3000)
    last_ping = time.time()

    for chunk in iter_agent_tokens(runtime, body, auth):
        now = time.time()
        if now - last_ping > ping_interval:
            writer.send_ping()
            last_ping = now

        event_name = chunk.get("event", "message")
        if on_event is not None:
            try:
                on_event(chunk)
            except Exception:
                pass
        writer.send_event(chunk, event=event_name)

    writer.send_done()
