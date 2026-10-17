#!/usr/bin/env python3
"""Isolated sandbox worker for TS Harness command execution."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import json
import os
import re
import resource
import shutil
import subprocess
from typing import Any


MAX_STDOUT = 4000
MAX_STDERR = 2000
NETWORK_PATTERNS = [
    re.compile(r"\bcurl\b", re.I),
    re.compile(r"\bwget\b", re.I),
    re.compile(r"\bnc\b|\bnetcat\b", re.I),
    re.compile(r"\bssh\b", re.I),
    re.compile(r"\bscp\b", re.I),
]
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
PROCESS_LIMIT = 64


def egress_allowed(command: str, *, policy: str) -> tuple[bool, str | None]:
    if policy != "deny":
        return True, None
    for pattern in NETWORK_PATTERNS:
        if pattern.search(command):
            return False, f"egress denied by sandbox policy: {pattern.pattern}"
    return True, None


def _build_command_argv(command: str) -> list[str]:
    unshare = shutil.which("unshare")
    if unshare:
        return [unshare, "-n", "bash", "-lc", command]
    return ["bash", "-lc", command]


def _apply_resource_limits(timeout: int, *, memory_mb: int = 512) -> None:
    memory_bytes = max(int(memory_mb), 1) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_NPROC, (PROCESS_LIMIT, PROCESS_LIMIT))


def run_isolated_command(
    command: str,
    *,
    workspace: str | None = None,
    timeout: int = 120,
    memory_mb: int = 512,
) -> dict[str, Any]:
    """Run a command in an isolated subprocess without host shell=True."""
    argv = _build_command_argv(command)
    cwd = workspace if workspace and os.path.isdir(workspace) else None
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=lambda: _apply_resource_limits(timeout, memory_mb=memory_mb),
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-MAX_STDOUT:],
            "stderr": completed.stderr[-MAX_STDERR:],
            "backend": "sandbox-worker",
            "isolated": True,
            "network_isolated": shutil.which("unshare") is not None,
        }
    except subprocess.TimeoutExpired:
        return {"error": "sandbox command timed out", "backend": "sandbox-worker", "isolated": True}


class WorkerHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = os.environ.get("SANDBOX_WORKER_TOKEN", "").strip()
        if not token:
            allow = os.environ.get("SANDBOX_WORKER_ALLOW_UNAUTHENTICATED", "").strip().lower()
            return allow in {"1", "true", "yes"}
        presented = self.headers.get("Authorization") or ""
        return hmac.compare_digest(presented, f"Bearer {token}")

    def do_POST(self):
        if self.path != "/run":
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        command = str(body.get("command", ""))
        policy = str(body.get("egress_policy") or os.environ.get("SANDBOX_EGRESS_POLICY", "deny"))
        allowed, reason = egress_allowed(command, policy=policy)
        if not allowed:
            self._send(200, {"error": reason, "backend": "sandbox-worker"})
            return
        workspace = str(body.get("workspace", "/workspace"))
        timeout = int(body.get("timeout", os.environ.get("SANDBOX_TIMEOUT_SECONDS", "120")))
        memory_mb = int(body.get("memory_mb", os.environ.get("SANDBOX_MEMORY_MB", "512")))
        self._send(
            200,
            run_isolated_command(command, workspace=workspace, timeout=timeout, memory_mb=memory_mb),
        )


def main() -> None:
    port = int(os.environ.get("SANDBOX_WORKER_PORT", "18880"))
    HTTPServer(("0.0.0.0", port), WorkerHandler).serve_forever()


if __name__ == "__main__":
    main()
