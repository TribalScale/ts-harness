"""Start coding workers for queued jobs and stop the ones that were cancelled or lost."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

LEASE_NAME = "coding-launcher"
WORKER_ENV_NAMES = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "MODEL_API_KEY",
    "MODEL_BASE_URL",
    "OPENAI_BASE_URL",
    "ANTHROPIC_BASE_URL",
)
WORKER_ENV_PREFIXES = ("CODING_BACKEND_", "OPENCODE_")
STOP_LOOKBACK_SECONDS = 3600


class LaunchError(RuntimeError):
    pass


class Launcher(Protocol):
    name: str

    def start(self, job: dict[str, Any], token: str) -> str: ...

    def stop(self, runner_ref: str) -> None: ...

    def describe(self, runner_ref: str) -> str: ...


def worker_env_names(environ: Mapping[str, str]) -> list[str]:
    """Secrets a worker needs, by name only, so values never reach the docker command line."""
    extra = [n.strip() for n in str(environ.get("CODING_WORKER_ENV") or "").split(",") if n.strip()]
    names = {n for n in (*WORKER_ENV_NAMES, *extra) if environ.get(n)}
    names.update(n for n in environ if n.startswith(WORKER_ENV_PREFIXES) and environ.get(n))
    return sorted(names)


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, default=str), flush=True)


@dataclass
class LocalDockerConfig:
    image: str = "ts-harness-coding-worker:latest"
    memory_api_url: str = "http://127.0.0.1:18792"
    network: str = ""
    cpus: str = "2"
    memory: str = "4g"
    pids_limit: int = 2048
    config_host_path: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "LocalDockerConfig":
        env = environ if environ is not None else os.environ
        network = str(env.get("CODING_WORKER_NETWORK") or "").strip()
        if network == "auto":
            # The launcher shares agent-runtime's network namespace, whose hostname is that container's id.
            network = f"container:{socket.gethostname()}"
        return cls(
            image=str(env.get("CODING_WORKER_IMAGE") or cls.image),
            memory_api_url=str(env.get("CODING_WORKER_MEMORY_API_URL") or env.get("MEMORY_API_URL") or cls.memory_api_url),
            network=network,
            cpus=str(env.get("CODING_WORKER_CPUS") or cls.cpus),
            memory=str(env.get("CODING_WORKER_MEMORY") or cls.memory),
            pids_limit=int(env.get("CODING_WORKER_PIDS_LIMIT") or cls.pids_limit),
            config_host_path=str(env.get("CODING_WORKER_CONFIG_HOST_PATH") or ""),
        )


class LocalDockerLauncher:
    """``docker run --rm`` one container per job on this host."""

    name = "local"

    def __init__(
        self,
        config: LocalDockerConfig,
        *,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        environ: Mapping[str, str] | None = None,
    ):
        self.config = config
        self._run = run
        self._environ = dict(environ if environ is not None else os.environ)

    def command(self, job: dict[str, Any]) -> list[str]:
        job_id = str(job["id"])
        cfg = self.config
        cmd = [
            "docker", "run", "-d", "--rm",
            "--name", f"ts-coding-{job_id[:8]}",
            "--label", f"ts-harness.coding-job={job_id}",
            "--cpus", cfg.cpus,
            "--memory", cfg.memory,
            "--pids-limit", str(cfg.pids_limit),
            "--mount", "type=volume,dst=/workspace",
            "-e", f"JOB_ID={job_id}",
            "-e", f"MEMORY_API_URL={cfg.memory_api_url}",
            "-e", "JOB_TOKEN",
        ]
        for name in worker_env_names(self._environ):
            cmd += ["-e", name]
        for name, value in sorted(cfg.extra_env.items()):
            cmd += ["-e", f"{name}={value}"]
        if cfg.network:
            cmd += ["--network", cfg.network]
        if cfg.config_host_path:
            cmd += [
                "-v", f"{cfg.config_host_path}:/config/ts-harness-runtime.yaml:ro",
                "-e", "TS_HARNESS_CONFIG=/config/ts-harness-runtime.yaml",
            ]
        cmd.append(cfg.image)
        return cmd

    def start(self, job: dict[str, Any], token: str) -> str:
        proc = self._run(
            self.command(job),
            capture_output=True,
            text=True,
            timeout=120,
            env={**self._environ, "JOB_TOKEN": token},
        )
        if proc.returncode != 0:
            raise LaunchError((proc.stderr or proc.stdout or "docker run failed").strip()[-500:])
        return (proc.stdout or "").strip()[:64]

    def stop(self, runner_ref: str) -> None:
        self._run(["docker", "stop", "-t", "10", runner_ref], capture_output=True, text=True, timeout=60)

    def describe(self, runner_ref: str) -> str:
        proc = self._run(
            ["docker", "inspect", "-f", "{{.State.Status}}", runner_ref], capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            return "missing"
        return (proc.stdout or "").strip() or "missing"


def launcher_tick(memory: Any, launcher: Launcher, *, max_running: int) -> dict[str, list[str]]:
    """Start claimed jobs, then stop workers whose jobs were cancelled or lost."""
    started: list[str] = []
    failed: list[str] = []
    for job in memory.claim_coding_jobs(launcher=launcher.name, max_running=max_running).get("jobs") or []:
        token = str(job.pop("token", ""))
        try:
            ref = launcher.start(job, token)
        except Exception as exc:  # noqa: BLE001
            memory.record_coding_job_launch(job["id"], error=str(exc)[:500])
            failed.append(str(job["id"]))
            _log("coding_job_launch_failed", job_id=job["id"], error=str(exc)[:500])
            continue
        memory.record_coding_job_launch(job["id"], runner_ref=ref)
        started.append(str(job["id"]))
        _log("coding_job_launched", job_id=job["id"], runner_ref=ref)
    stopped: list[str] = []
    for job in memory.list_coding_jobs(status="cancelled,lost", finished_within=STOP_LOOKBACK_SECONDS, limit=100):
        ref = str(job.get("runner_ref") or "")
        if not ref or job.get("launcher") != launcher.name:
            continue
        if launcher.describe(ref) == "running":
            launcher.stop(ref)
            stopped.append(str(job["id"]))
            _log("coding_job_worker_stopped", job_id=job["id"], status=job.get("status"))
    return {"started": started, "failed": failed, "stopped": stopped}


def run_launcher_loop(
    memory: Any,
    launcher: Launcher,
    *,
    max_running: int,
    holder: str,
    stop: threading.Event,
    interval_seconds: float = 5,
    lease_ttl_seconds: int = 30,
) -> None:
    while not stop.is_set():
        try:
            if memory.acquire_service_lease(LEASE_NAME, holder=holder, ttl_seconds=lease_ttl_seconds).get("leader"):
                launcher_tick(memory, launcher, max_running=max_running)
        except Exception as exc:  # noqa: BLE001
            _log("coding_launcher_tick_failed", error=str(exc))
        stop.wait(interval_seconds)


def build_launcher(environ: Mapping[str, str] | None = None) -> Launcher:
    env = environ if environ is not None else os.environ
    kind = str(env.get("CODING_LAUNCHER") or "local").strip().lower()
    if kind == "local":
        return LocalDockerLauncher(LocalDockerConfig.from_env(env), environ=env)
    raise ValueError(f"unknown CODING_LAUNCHER: {kind}")
