"""Trace ids cross the gateway, the memory client, and the runner."""

from __future__ import annotations

import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ts_harness.gateway.asgi_app import _TraceMiddleware
from ts_harness.memory.client import MemoryClient
from ts_harness.observability.logging import log_event, trace_id_var
from starlette.middleware import Middleware


REPO_ROOT = Path(__file__).resolve().parents[2]
TRACE = "ab" * 16


def _probe(request):
    log_event("first")
    log_event("second")
    return JSONResponse({"ok": True})


def _app() -> Starlette:
    return Starlette(
        routes=[Route("/probe", endpoint=_probe)],
        middleware=[Middleware(_TraceMiddleware)],
    )


def _events(buf: str) -> list[dict]:
    return [json.loads(line) for line in buf.splitlines() if line.strip()]


def test_gateway_mints_one_trace_id_for_the_request():
    client = TestClient(_app())
    with redirect_stdout(io.StringIO()) as captured:
        response = client.get("/probe")

    trace_id = response.headers["x-trace-id"]
    assert len(trace_id) == 32
    assert all(char in "0123456789abcdef" for char in trace_id)
    events = _events(captured.getvalue())
    assert [event["trace_id"] for event in events] == [trace_id, trace_id]


def test_gateway_echoes_an_incoming_traceparent():
    client = TestClient(_app())
    with redirect_stdout(io.StringIO()):
        response = client.get("/probe", headers={"traceparent": f"00-{TRACE}-{'cd' * 8}-01"})

    assert response.headers["x-trace-id"] == TRACE


def test_memory_client_sends_the_current_traceparent():
    captured: dict = {}

    class _Response:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(request, timeout=0):
        captured["traceparent"] = request.get_header("Traceparent")
        return _Response()

    token = trace_id_var.set(TRACE)
    try:
        with mock.patch("ts_harness.memory.client.urllib.request.urlopen", urlopen):
            MemoryClient("http://memory")._request("GET", "/health")
    finally:
        trace_id_var.reset(token)

    assert captured["traceparent"].startswith(f"00-{TRACE}-")
    assert captured["traceparent"].endswith("-01")


def test_runner_claim_and_dispatch_share_a_trace_id():
    spec = importlib.util.spec_from_file_location(
        "workflow_runner_trace", REPO_ROOT / "scripts" / "workflow-runner.py"
    )
    runner_module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(runner_module)
    test_spec = importlib.util.spec_from_file_location(
        "workflow_runner_fakes", REPO_ROOT / "tests" / "test_workflow_runner.py"
    )
    fakes = importlib.util.module_from_spec(test_spec)
    assert test_spec.loader is not None
    test_spec.loader.exec_module(fakes)

    task = {
        "id": "task-1",
        "workflow_id": "workflow-1",
        "workflow_type": "prd_request",
        "step_name": "draft_prd",
        "agent_id": "pm-agent",
        "input": {},
    }
    memory = fakes.FakeMemoryClient(ready=[task])
    adapter = fakes.FakeAdapter({"status": "succeeded", "output": {}, "runtime_task_id": "rt-1"})
    workflow_runner = runner_module.WorkflowRunner(memory, adapter, runner_id="runner-1")
    with redirect_stdout(io.StringIO()) as captured:
        workflow_runner.tick()

    events = {
        event["event"]: event
        for event in _events(captured.getvalue())
        if event["event"] in {"claimed", "dispatched"}
    }
    assert events["claimed"]["trace_id"] == events["dispatched"]["trace_id"]
    assert len(events["claimed"]["trace_id"]) == 32


def test_runner_dispatch_sends_traceparent():
    spec = importlib.util.spec_from_file_location(
        "workflow_runner_dispatch_trace", REPO_ROOT / "scripts" / "workflow-runner.py"
    )
    runner_module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(runner_module)
    captured: dict = {}

    class _Response:
        def read(self):
            return b'{"status":"succeeded"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(request, timeout=0):
        captured["traceparent"] = request.get_header("Traceparent")
        return _Response()

    adapter = runner_module.GatewayHttpAdapter("http://gateway", "token")
    token = trace_id_var.set(TRACE)
    try:
        with mock.patch.object(runner_module.urllib.request, "urlopen", urlopen):
            adapter.dispatch({"workflow_id": "wf", "task_id": "task-1"})
    finally:
        trace_id_var.reset(token)

    assert captured["traceparent"].startswith(f"00-{TRACE}-")
