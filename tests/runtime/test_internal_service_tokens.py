"""Memory API and sandbox worker refuse work when their tokens are unset."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

from ts_harness.sandbox.docker_env import DockerExecutionEnvironment


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    path = REPO_ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _invoke(handler, method: str) -> dict:
    captured: dict = {}

    def send(status, payload):
        captured["status"] = status
        captured["payload"] = payload

    handler._send_json = send
    handler._send = send
    getattr(handler, method)()
    return captured


def _handler(cls, path: str, headers: dict, body: bytes = b"{}"):
    handler = cls.__new__(cls)
    handler.path = path
    handler.headers = headers
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    return handler


def test_memory_health_stays_open_when_token_missing(monkeypatch):
    monkeypatch.delenv("MEMORY_API_TOKEN", raising=False)
    monkeypatch.setenv("MEMORY_API_ALLOW_UNAUTHENTICATED", "")
    gateway = _load("memory_gateway_tokens", "scripts/memory-gateway.py")
    handler = _handler(gateway.MemoryHandler, "/health", {})

    result = _invoke(handler, "do_GET")

    assert result["status"] == 200
    assert result["payload"]["ok"] is True


def test_memory_api_fails_closed_without_token_or_hatch(monkeypatch):
    monkeypatch.delenv("MEMORY_API_TOKEN", raising=False)
    monkeypatch.setenv("MEMORY_API_ALLOW_UNAUTHENTICATED", "")
    gateway = _load("memory_gateway_tokens_closed", "scripts/memory-gateway.py")
    handler = _handler(gateway.MemoryHandler, "/memory/recall", {})

    result = _invoke(handler, "do_POST")

    assert result["status"] == 401


def test_memory_api_accepts_matching_bearer(monkeypatch):
    monkeypatch.setenv("MEMORY_API_TOKEN", "memory-secret")
    monkeypatch.delenv("MEMORY_API_ALLOW_UNAUTHENTICATED", raising=False)
    gateway = _load("memory_gateway_tokens_bearer", "scripts/memory-gateway.py")
    handler = _handler(
        gateway.MemoryHandler,
        "/memory/recall",
        {"Authorization": "Bearer memory-secret"},
    )

    assert handler._authorized() is True


def test_sandbox_worker_fails_closed_without_token_or_hatch(monkeypatch):
    monkeypatch.delenv("SANDBOX_WORKER_TOKEN", raising=False)
    monkeypatch.setenv("SANDBOX_WORKER_ALLOW_UNAUTHENTICATED", "")
    worker = _load("sandbox_worker_tokens", "scripts/sandbox-worker.py")
    handler = _handler(worker.WorkerHandler, "/run", {"Content-Length": "2"}, b"{}")

    result = _invoke(handler, "do_POST")

    assert result["status"] == 401


def test_sandbox_worker_runs_with_matching_bearer(monkeypatch):
    monkeypatch.setenv("SANDBOX_WORKER_TOKEN", "worker-secret")
    monkeypatch.setenv("SANDBOX_EGRESS_POLICY", "allow")
    worker = _load("sandbox_worker_tokens_ok", "scripts/sandbox-worker.py")
    body = json.dumps({"command": "echo ok", "workspace": "/tmp", "timeout": 5}).encode()
    handler = _handler(
        worker.WorkerHandler,
        "/run",
        {"Authorization": "Bearer worker-secret", "Content-Length": str(len(body))},
        body,
    )

    def fake_run(command, *, workspace, timeout, memory_mb=None):
        return {"exit_code": 0, "stdout": command, "isolated": True}

    monkeypatch.setattr(worker, "run_isolated_command", fake_run)
    result = _invoke(handler, "do_POST")

    assert result["status"] == 200
    assert result["payload"]["exit_code"] == 0


def test_docker_env_sends_sandbox_bearer(monkeypatch):
    captured: dict = {}

    class _Response:
        def read(self):
            return b'{"exit_code": 0}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(request, timeout=0):
        captured["authorization"] = request.get_header("Authorization")
        return _Response()

    monkeypatch.setenv("SANDBOX_WORKER_URL", "http://sandbox-worker:18880")
    monkeypatch.setenv("SANDBOX_WORKER_TOKEN", "worker-secret")
    monkeypatch.setattr("ts_harness.sandbox.docker_env.urllib.request.urlopen", urlopen)
    DockerExecutionEnvironment(enabled=True)._run_worker(
        "echo ok",
        workspace="/workspace",
        tenant_id="default",
    )

    assert captured["authorization"] == "Bearer worker-secret"


def test_install_provisions_internal_tokens():
    phase = (REPO_ROOT / "scripts" / "phases" / "10-fetch-secrets.sh").read_text()
    common = (REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh").read_text()
    compose = (REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh").read_text()
    aws = (REPO_ROOT / "infra" / "aws" / "secrets.tf").read_text()
    gcp = (REPO_ROOT / "infra" / "gcp" / "secrets.tf").read_text()

    for text in (phase, common, compose, aws, gcp):
        assert "memory-api-token" in text or "MEMORY_API_TOKEN" in text
        assert "sandbox-worker-token" in text or "SANDBOX_WORKER_TOKEN" in text
    assert 'fetch_secret "memory-api-token" false' in phase
    assert 'fetch_secret "sandbox-worker-token" false' in phase
    assert "random_password" in aws
    assert "random_password" in gcp
