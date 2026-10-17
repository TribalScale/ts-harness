"""Prometheus metrics registry for TS Harness."""

from __future__ import annotations

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Histogram,
        generate_latest as _generate_latest,
    )

    agent_run_latency_ms = Histogram(
        "ts_harness_agent_run_latency_ms",
        "Agent run end-to-end latency in milliseconds",
        buckets=[50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000],
    )

    tool_call_total = Counter(
        "ts_harness_tool_call_total",
        "Total tool calls by name and policy decision",
        ["tool_name", "decision"],
    )

    workflow_dispatch_total = Counter(
        "ts_harness_workflow_dispatch_total",
        "Total workflow dispatch requests by outcome status",
        ["status"],
    )

    session_message_total = Counter(
        "ts_harness_session_message_total",
        "Total session messages received",
    )

    heartbeat_run_total = Counter(
        "ts_harness_heartbeat_run_total",
        "Total scheduled heartbeat / cron runs",
    )

    auth_failure_total = Counter(
        "ts_harness_auth_failure_total",
        "Total authentication failures",
    )

    channel_webhook_total = Counter(
        "ts_harness_channel_webhook_total",
        "Total inbound channel webhook events",
        ["channel"],
    )

    secret_reload_total = Counter(
        "ts_harness_secret_reload_total",
        "Total secret reload operations",
    )

    PROMETHEUS_AVAILABLE = True

    def generate_latest() -> str:
        """Return Prometheus text exposition format (UTF-8 string)."""
        return _generate_latest().decode("utf-8")

    def content_type() -> str:
        return CONTENT_TYPE_LATEST

except ImportError:
    PROMETHEUS_AVAILABLE = False

    def generate_latest() -> str:  # type: ignore[misc]
        return ""

    def content_type() -> str:  # type: ignore[misc]
        return "text/plain"
