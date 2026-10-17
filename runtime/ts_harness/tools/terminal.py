"""Terminal exec tool with approval scanning."""

from __future__ import annotations

from typing import Any
import threading
import uuid

from ts_harness.sandbox.docker_env import DockerExecutionEnvironment
from ts_harness.tools.approvals import scan_command
from ts_harness.tools.policy import ToolPolicyContext


_env = DockerExecutionEnvironment()
_background_jobs: dict[str, dict[str, Any]] = {}


def handle_exec(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    command = str(args.get("command", "")).strip()
    if not command:
        return {"error": "command is required"}
    allowed, reason = scan_command(command)
    if not allowed:
        return {"error": reason}
    if bool(args.get("background")):
        job_id = str(uuid.uuid4())
        _background_jobs[job_id] = {"status": "running", "stdout": "", "stderr": "", "exit_code": None}

        def run_job() -> None:
            result = _env.run(command, workspace=f"/workspace/{ctx.agent_id}", tenant_id=ctx.tenant_id)
            _background_jobs[job_id].update(
                {
                    "status": "completed" if "error" not in result else "failed",
                    "stdout": str(result.get("stdout", ""))[-4000:],
                    "stderr": str(result.get("stderr", ""))[-2000:],
                    "exit_code": result.get("exit_code"),
                    "error": result.get("error"),
                }
            )

        thread = threading.Thread(target=run_job, daemon=True, name=f"ec-bg-{job_id[:8]}")
        thread.start()
        thread.join(timeout=0.2)
        return {"status": "started", "job_id": job_id}
    result = _env.run(command, workspace=f"/workspace/{ctx.agent_id}", tenant_id=ctx.tenant_id)
    return result


def handle_background_logs(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    job_id = str(args.get("job_id", ""))
    job = _background_jobs.get(job_id)
    if job is None:
        return {"error": "background job not found"}
    return {"job_id": job_id, **job}
