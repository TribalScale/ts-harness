"""Docker execution environment adapted from Hermes environments/docker.py (MIT)."""

from __future__ import annotations

from typing import Any
import json
import os
import subprocess
import urllib.request


_sandbox_limits: dict[str, Any] = {
    "timeout_seconds": 300,
    "egress_policy": "deny",
    "cpu_limit": 1.0,
    "memory_mb": 512,
}


def set_sandbox_limits(limits: dict[str, Any]) -> None:
    _sandbox_limits.update({key: limits[key] for key in _sandbox_limits if key in limits})


def get_sandbox_limits() -> dict[str, Any]:
    return dict(_sandbox_limits)


class DockerExecutionEnvironment:
    def __init__(self, *, enabled: bool | None = None, runner=None, persistent_workspace: bool = False):
        self.enabled = enabled if enabled is not None else os.environ.get("SANDBOX_ENABLED", "true").lower() == "true"
        self.runner = runner or subprocess.run
        self.persistent_workspace = persistent_workspace

    def run(self, command: str, *, workspace: str, tenant_id: str) -> dict[str, Any]:
        if os.environ.get("SANDBOX_WORKER_URL"):
            return self._run_worker(command, workspace=workspace, tenant_id=tenant_id)
        if not self.enabled:
            if os.environ.get("SANDBOX_STRICT", "true").lower() == "true":
                return {"error": "sandbox unavailable in strict mode", "backend": "denied"}
            try:
                completed = self.runner(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=workspace if os.path.isdir(workspace) else None,
                )
                return {
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout[-4000:],
                    "stderr": completed.stderr[-2000:],
                    "backend": "local",
                }
            except subprocess.TimeoutExpired:
                return {"error": "command timed out", "backend": "local"}
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            f"--memory={int(get_sandbox_limits()['memory_mb'])}m",
            f"--cpus={get_sandbox_limits()['cpu_limit']}",
            "--network=none",
            "--label",
            "ts-harness.sandbox=true",
            "--label",
            f"ts-harness.tenant={tenant_id}",
            "-w",
            workspace,
            "ts-harness-sandbox:latest",
            "sh",
            "-lc",
            command,
        ]
        if self.persistent_workspace:
            docker_cmd[16:16] = ["-v", f"{workspace}:{workspace}:rw"]
        try:
            completed = self.runner(docker_cmd, capture_output=True, text=True, timeout=120)
            return {
                "exit_code": completed.returncode,
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-2000:],
                "backend": "docker",
                "tenant_id": tenant_id,
            }
        except FileNotFoundError:
            return {"error": "docker unavailable", "backend": "docker"}
        except subprocess.TimeoutExpired:
            return {"error": "sandbox command timed out", "backend": "docker"}

    def _run_worker(self, command: str, *, workspace: str, tenant_id: str) -> dict[str, Any]:
        limits = get_sandbox_limits()
        body = json.dumps(
            {
                "command": command,
                "workspace": workspace,
                "tenant_id": tenant_id,
                "timeout": int(limits["timeout_seconds"]),
                "egress_policy": limits["egress_policy"],
                "cpu_limit": limits["cpu_limit"],
                "memory_mb": int(limits["memory_mb"]),
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("SANDBOX_WORKER_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{os.environ['SANDBOX_WORKER_URL'].rstrip('/')}/run",
            data=body,
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=130) as response:
            return json.loads(response.read())

    def reap_orphans(self, *, older_than_seconds: int = 3600) -> dict[str, Any]:
        ps_cmd = [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=ts-harness.sandbox=true",
        ]
        try:
            completed = self.runner(ps_cmd, capture_output=True, text=True, timeout=30)
            container_ids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if not container_ids:
                return {"reaped": 0}
            rm_cmd = ["docker", "rm", "-f", *container_ids]
            self.runner(rm_cmd, capture_output=True, text=True, timeout=60)
            return {"reaped": len(container_ids), "older_than_seconds": older_than_seconds}
        except FileNotFoundError:
            return {"error": "docker unavailable", "reaped": 0}
