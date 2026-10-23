"""Start coding workers for queued jobs and stop the ones that were cancelled or lost."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
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
QUOTA_ENV = {"per_user": "CODING_QUOTA_PER_USER", "per_program": "CODING_QUOTA_PER_PROGRAM", "per_team": "CODING_QUOTA_PER_TEAM"}


class LaunchError(RuntimeError):
    pass


class CapacityError(LaunchError):
    """No room to start a worker right now (quota, throttling, capacity); retry later."""


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
    runtime_url: str = "http://127.0.0.1:18789"
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
            runtime_url=str(env.get("CODING_WORKER_RUNTIME_URL") or cls.runtime_url),
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
            "--cpus", str(int(job["worker_cpu"]) / 1024) if job.get("worker_cpu") else cfg.cpus,
            "--memory", f"{int(job['worker_memory_mb'])}m" if job.get("worker_memory_mb") else cfg.memory,
            "--pids-limit", str(cfg.pids_limit),
            "--mount", "type=volume,dst=/workspace",
            "-e", f"JOB_ID={job_id}",
            "-e", f"MEMORY_API_URL={cfg.memory_api_url}",
            "-e", f"HARNESS_RUNTIME_URL={cfg.runtime_url}",
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


def coding_schedule(environ: Mapping[str, str] | None = None) -> tuple[dict[str, int], int | None]:
    """Quotas per requester, program and team, and the global limit, from ``scheduling.coding`` in the
    runtime config at ``TS_HARNESS_CONFIG``; ``CODING_QUOTA_PER_*`` environment variables override."""
    import yaml

    from ts_harness.config.schema import SchedulingConfig, parse_scheduling

    env = environ if environ is not None else os.environ
    scheduling = SchedulingConfig()
    path = str(env.get("TS_HARNESS_CONFIG") or "").strip()
    if path and Path(path).is_file():
        try:
            data = yaml.safe_load(Path(path).read_text()) or {}
            scheduling = parse_scheduling(data.get("scheduling") if isinstance(data, dict) else None)
        except Exception as exc:  # noqa: BLE001
            _log("coding_schedule_config_unreadable", error=str(exc)[:300])
    quotas = scheduling.quotas()
    for key, name in QUOTA_ENV.items():
        raw = str(env.get(name) or "").strip()
        if raw:
            quotas[key] = max(0, int(raw))
    return quotas, scheduling.coding_global


def launcher_tick(
    memory: Any, launcher: Launcher, *, max_running: int, quotas: dict[str, int] | None = None
) -> dict[str, list[str]]:
    """Start claimed jobs, then stop workers whose jobs were cancelled or lost."""
    started: list[str] = []
    failed: list[str] = []
    waiting: list[str] = []
    claimed = memory.claim_coding_jobs(launcher=launcher.name, max_running=max_running, quotas=quotas)
    for job in claimed.get("jobs") or []:
        token = str(job.pop("token", ""))
        if waiting:
            memory.record_coding_job_launch(job["id"], error="launcher at capacity", retryable=True)
            waiting.append(str(job["id"]))
            continue
        try:
            ref = launcher.start(job, token)
        except CapacityError as exc:
            memory.record_coding_job_launch(job["id"], error=str(exc)[:500], retryable=True)
            waiting.append(str(job["id"]))
            _log("coding_job_launch_deferred", job_id=job["id"], error=str(exc)[:500])
            continue
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
    return {"started": started, "failed": failed, "waiting": waiting, "stopped": stopped}


def run_launcher_loop(
    memory: Any,
    launcher: Launcher,
    *,
    max_running: int,
    holder: str,
    stop: threading.Event,
    interval_seconds: float = 5,
    lease_ttl_seconds: int = 30,
    quotas: dict[str, int] | None = None,
) -> None:
    while not stop.is_set():
        try:
            if memory.acquire_service_lease(LEASE_NAME, holder=holder, ttl_seconds=lease_ttl_seconds).get("leader"):
                launcher_tick(memory, launcher, max_running=max_running, quotas=quotas)
        except Exception as exc:  # noqa: BLE001
            _log("coding_launcher_tick_failed", error=str(exc))
        stop.wait(interval_seconds)


_THROTTLE_CODES = {"ThrottlingException", "Throttling", "TooManyRequestsException", "RequestLimitExceeded"}
_CAPACITY_HINTS = ("capacity", "resource:", "limit", "quota")
_ECS_STOPPED = "STOPPED"


def _split(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


@dataclass
class EcsConfig:
    cluster: str
    task_definition: str
    subnets: list[str]
    security_groups: list[str]
    container: str = "coding-worker"
    capacity_provider: str = "FARGATE"
    region: str = ""

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "EcsConfig":
        env = environ if environ is not None else os.environ
        required = {
            "CODING_ECS_CLUSTER": env.get("CODING_ECS_CLUSTER"),
            "CODING_ECS_TASK_DEFINITION": env.get("CODING_ECS_TASK_DEFINITION"),
            "CODING_ECS_SUBNETS": env.get("CODING_ECS_SUBNETS"),
            "CODING_ECS_SECURITY_GROUPS": env.get("CODING_ECS_SECURITY_GROUPS"),
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"CODING_LAUNCHER=ecs needs {', '.join(missing)}")
        return cls(
            cluster=str(required["CODING_ECS_CLUSTER"]).strip(),
            task_definition=str(required["CODING_ECS_TASK_DEFINITION"]).strip(),
            subnets=_split(required["CODING_ECS_SUBNETS"]),
            security_groups=_split(required["CODING_ECS_SECURITY_GROUPS"]),
            container=str(env.get("CODING_ECS_CONTAINER") or cls.container).strip(),
            capacity_provider=str(env.get("CODING_ECS_CAPACITY_PROVIDER") or cls.capacity_provider).strip(),
            region=str(env.get("CLOUD_REGION") or env.get("AWS_REGION") or "").strip(),
        )


class EcsLauncher:
    """One Fargate task per job via ``ecs:RunTask``.

    The job token travels as a container override, so it shows in
    ``DescribeTasks`` to principals allowed to call it; it only reaches that
    job's spec, heartbeat and complete endpoints and expires with the job.
    """

    name = "ecs"

    def __init__(
        self,
        config: EcsConfig,
        *,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
    ):
        self.config = config
        self._client = client
        self._sleep = sleep
        self._max_attempts = max(1, max_attempts)

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "ecs",
                region_name=self.config.region or None,
                config=Config(retries={"max_attempts": 2, "mode": "standard"}),
            )
        return self._client

    def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        for attempt in range(self._max_attempts):
            try:
                return getattr(self.client, method)(**kwargs)
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code") or "")
                if code not in _THROTTLE_CODES:
                    raise
                if attempt + 1 >= self._max_attempts:
                    raise CapacityError(f"{method} throttled: {exc}") from exc
                self._sleep(min(2.0**attempt, 20.0))
        raise CapacityError(f"{method} throttled")

    @staticmethod
    def _sizing(job: dict[str, Any]) -> dict[str, str]:
        sizing: dict[str, str] = {}
        if job.get("worker_cpu"):
            sizing["cpu"] = str(int(job["worker_cpu"]))
        if job.get("worker_memory_mb"):
            sizing["memory"] = str(int(job["worker_memory_mb"]))
        return sizing

    def run_task_request(self, job: dict[str, Any], token: str) -> dict[str, Any]:
        cfg = self.config
        job_id = str(job["id"])
        tags = [{"key": "ts-harness:coding-job", "value": job_id}]
        if job.get("workflow_id"):
            tags.append({"key": "ts-harness:workflow", "value": str(job["workflow_id"])})
        return {
            "cluster": cfg.cluster,
            "taskDefinition": cfg.task_definition,
            "count": 1,
            "capacityProviderStrategy": [{"capacityProvider": cfg.capacity_provider, "weight": 1}],
            "networkConfiguration": {
                "awsvpcConfiguration": {
                    "subnets": list(cfg.subnets),
                    "securityGroups": list(cfg.security_groups),
                    "assignPublicIp": "DISABLED",
                }
            },
            "overrides": {
                **self._sizing(job),
                "containerOverrides": [
                    {
                        "name": cfg.container,
                        "environment": [
                            {"name": "JOB_ID", "value": job_id},
                            {"name": "JOB_TOKEN", "value": token},
                        ],
                    }
                ]
            },
            "startedBy": f"coding-job-{job_id[:24]}",
            "clientToken": f"{job_id}-{int(job.get('attempt') or 0)}"[:64],
            "enableECSManagedTags": True,
            "tags": tags,
        }

    def start(self, job: dict[str, Any], token: str) -> str:
        from botocore.exceptions import ClientError

        try:
            response = self._call("run_task", **self.run_task_request(job, token))
        except ClientError as exc:
            raise LaunchError(f"run_task failed: {exc}") from exc
        tasks = response.get("tasks") or []
        if tasks:
            return str(tasks[0]["taskArn"])
        failures = response.get("failures") or []
        reason = "; ".join(
            f"{f.get('reason') or 'unknown'}{': ' + f['detail'] if f.get('detail') else ''}" for f in failures
        ) or "run_task returned no task"
        if any(hint in reason.lower() for hint in _CAPACITY_HINTS):
            raise CapacityError(reason)
        raise LaunchError(reason)

    def stop(self, runner_ref: str) -> None:
        self._call("stop_task", cluster=self.config.cluster, task=runner_ref, reason="coding job cancelled or lost")

    def describe(self, runner_ref: str) -> str:
        response = self._call("describe_tasks", cluster=self.config.cluster, tasks=[runner_ref])
        tasks = response.get("tasks") or []
        if not tasks:
            return "missing"
        return "exited" if tasks[0].get("lastStatus") == _ECS_STOPPED else "running"


def build_launcher(environ: Mapping[str, str] | None = None) -> Launcher:
    env = environ if environ is not None else os.environ
    kind = str(env.get("CODING_LAUNCHER") or "").strip().lower()
    if not kind:
        from ts_harness.config.deployment_profile import resolve_setting

        kind = str(resolve_setting("coding_launcher")).strip().lower()
    if kind == "local":
        return LocalDockerLauncher(LocalDockerConfig.from_env(env), environ=env)
    if kind == "ecs":
        return EcsLauncher(EcsConfig.from_env(env))
    raise ValueError(f"unknown CODING_LAUNCHER: {kind}")
