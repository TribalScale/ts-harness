from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
_otel_provider: Any = None


def current_trace_id() -> str:
    return trace_id_var.get()


def new_trace_id() -> str:
    return secrets.token_hex(16)


def traceparent_header(trace_id: str) -> str:
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


def parse_traceparent(value: str) -> str | None:
    parts = (value or "").strip().split("-")
    if len(parts) != 4 or parts[0] != "00":
        return None
    trace_id = parts[1].lower()
    if len(trace_id) != 32 or any(char not in "0123456789abcdef" for char in trace_id):
        return None
    if trace_id == "0" * 32:
        return None
    return trace_id


def log_event(event: str, **fields: Any) -> None:
    payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    payload.update(fields)
    trace_id = current_trace_id()
    if trace_id and "trace_id" not in payload:
        payload["trace_id"] = trace_id
    print(json.dumps(payload, default=str), flush=True)


@contextmanager
def server_span(name: str) -> Iterator[None]:
    """Emit one server span when an OTLP endpoint and the SDK are both available."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        yield
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        yield
        return
    global _otel_provider
    try:
        if _otel_provider is None:
            provider = TracerProvider(
                resource=Resource.create({"service.name": "ts-harness-gateway"})
            )
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(provider)
            _otel_provider = provider
        tracer = trace.get_tracer("ts-harness")
        with tracer.start_as_current_span(name):
            yield
    except Exception:
        yield
