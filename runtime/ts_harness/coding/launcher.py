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

from ts_harness.coding import egress

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
PROVIDER_KEY_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "MODEL_API_KEY", "CODING_BACKEND_API_KEY")
JOB_PROXY_TOKEN_TTL_SECONDS = 8 * 3600
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


def worker_proxy_url(environ: Mapping[str, str]) -> str:
    """Where workers reach the LLM proxy; empty when the proxy is off."""
    if not str(environ.get("LLM_PROXY_SECRET") or "").strip():
        return ""
    return str(environ.get("CODING_WORKER_LLM_PROXY_URL") or environ.get("LLM_PROXY_URL") or "").strip().rstrip("/")


def worker_env_names(environ: Mapping[str, str]) -> list[str]:
    """Secrets a worker needs, by name only, so values never reach the docker command line.
    With the LLM proxy on, workers get a proxy token instead of provider keys."""
    extra = [n.strip() for n in str(environ.get("CODING_WORKER_ENV") or "").split(",") if n.strip()]
    names = {n for n in (*WORKER_ENV_NAMES, *extra) if environ.get(n)}
    names.update(n for n in environ if n.startswith(WORKER_ENV_PREFIXES) and environ.get(n))
    if worker_proxy_url(environ):
        names.difference_update(PROVIDER_KEY_NAMES)
    return sorted(names)


def job_proxy_env(job: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, str]:
    """``LLM_PROXY_URL`` and a token scoped to this job. The proxy takes the team from the job's task."""
    url = worker_proxy_url(environ)
    if not url:
        return {}
    from ts_harness.llm_proxy.tokens import mint_proxy_token

    token = mint_proxy_token(
        {
            "tenant": job.get("tenant_id"),
            "project": job.get("project_id"),
            "program": job.get("program_id"),
            "workflow": job.get("workflow_id"),
            "task": job.get("task_id"),
            "user": job.get("requester") or job.get("requested_by"),
            "agent": "coding-backend",
            "job": job.get("id"),
        },
        secret=str(environ["LLM_PROXY_SECRET"]).strip(),
        ttl_seconds=int(environ.get("LLM_PROXY_JOB_TTL_SECONDS") or JOB_PROXY_TOKEN_TTL_SECONDS),
    )
    return {"LLM_PROXY_URL": url, "LLM_PROXY_TOKEN": token}


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, default=str), flush=True)


_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3}


def parse_memory(value: str | int | float) -> int:
    """``"2g"``, ``"1536m"`` or a byte count as bytes."""
    text = str(value).strip().lower().removesuffix("b")
    if text and text[-1] in _UNITS:
        return int(float(text[:-1]) * _UNITS[text[-1]])
    return int(float(text))


def host_memory_bytes(meminfo: str = "/proc/meminfo") -> int | None:
    try:
        for line in Path(meminfo).read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def local_worker_limit(
    configured: int,
    per_worker: str,
    *,
    environ: Mapping[str, str] | None = None,
    total_bytes: int | None = None,
) -> int:
    """How many coding runs fit on this host at once: never more than ``configured``, at least 1.

    The platform (agent-runtime, memory-api, runner, database client) keeps
    ``CODING_HOST_RESERVED_MEMORY`` (default 2.5 GB); the rest is shared by workers of
    ``per_worker`` each. Three runs on a 4 GB host were killed by the kernel; this keeps
    such a host at one while an 8 GB host runs two and a 16 GB host the configured three.
    """
    env = environ if environ is not None else os.environ
    total = total_bytes if total_bytes is not None else host_memory_bytes()
    if not total:
        return max(1, configured)
    reserved = parse_memory(str(env.get("CODING_HOST_RESERVED_MEMORY") or "2.5g"))
    fits = int(max(0, total - reserved) // max(1, parse_memory(per_worker)))
    return max(1, min(configured, fits))


@dataclass
class LocalDockerConfig:
    image: str = "ts-harness-coding-worker:latest"
    memory_api_url: str = "http://127.0.0.1:18792"
    runtime_url: str = "http://127.0.0.1:18789"
    network: str = ""
    cpus: str = "2"
    # An OpenCode run with a JavaScript test suite peaks around 1 GB; 2 GB leaves headroom
    # and lets a 4 GB host run one worker beside the platform, an 8 GB host two.
    memory: str = "2g"
    pids_limit: int = 2048
    config_host_path: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)
    # CODING_EGRESS_ALLOWLIST=on: workers join an internal network and reach the internet
    # only through the egress proxy (docker/docker-compose.coding-egress.yml).
    egress_allowlist: bool = False
    egress_network: str = egress.DEFAULT_NETWORK
    egress_allowlist_path: str = egress.DEFAULT_ALLOWLIST_PATH

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
            egress_allowlist=egress.enabled(env),
            egress_network=str(env.get("CODING_EGRESS_NETWORK") or egress.DEFAULT_NETWORK).strip(),
            egress_allowlist_path=str(env.get("CODING_EGRESS_ALLOWLIST_PATH") or egress.DEFAULT_ALLOWLIST_PATH),
        )


WORKSPACE_LABEL = "ts-harness.coding-workspace"
WORKSPACE_PRUNE_SECONDS = 3600


def workspace_volume(job: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    """The named volume a ticket's coding jobs share, or "" to give each job a fresh one.

    A revision of the same delivery then finds its clone, installed dependencies and Node
    toolchain from the run before: the backend fetches the PR head and reinstalls only when a
    lockfile changed. ``CODING_WORKSPACE_REUSE=off`` turns it off.
    """
    if str(environ.get("CODING_WORKSPACE_REUSE") or "").strip().lower() in {"off", "0", "false", "no"}:
        return ""
    workflow_id = str(job.get("workflow_id") or "").strip()
    if not workflow_id:
        return ""
    import hashlib

    key = "|".join(str(job.get(name) or "") for name in ("tenant_id", "workflow_id", "work_order_id", "repo_url"))
    return f"ts-coding-ws-{hashlib.sha256(key.encode()).hexdigest()[:20]}"


class LocalDockerLauncher:
    """``docker run --rm`` one container per job on this host."""

    name = "local"
    _pruned_at = 0.0

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
        self._egress_synced_at = 0.0
        self._registry_hosts: list[str] = []

    def _endpoints(self) -> tuple[str, str, str, str]:
        """Network, memory-api, runtime and LLM proxy URLs as the worker sees them."""
        cfg = self.config
        urls = (cfg.memory_api_url, cfg.runtime_url, worker_proxy_url(self._environ))
        if not cfg.egress_allowlist:
            return (cfg.network, *urls)
        host = egress.internal_host(self._environ)
        memory_url, runtime_url, llm_url = (egress.internal_url(url, host) for url in urls)
        return cfg.egress_network, memory_url, runtime_url, llm_url

    def command(self, job: dict[str, Any]) -> list[str]:
        job_id = str(job["id"])
        cfg = self.config
        volume = workspace_volume(job, self._environ)
        network, memory_url, runtime_url, llm_url = self._endpoints()
        cmd = [
            # --init reaps the processes a coding run leaves behind (PID 1 is the worker script).
            "docker", "run", "-d", "--rm", "--init",
            "--name", f"ts-coding-{job_id[:8]}",
            "--label", f"ts-harness.coding-job={job_id}",
            "--cpus", str(int(job["worker_cpu"]) / 1024) if job.get("worker_cpu") else cfg.cpus,
            "--memory", f"{int(job['worker_memory_mb'])}m" if job.get("worker_memory_mb") else cfg.memory,
            "--pids-limit", str(cfg.pids_limit),
            "--mount", (f"type=volume,src={volume},dst=/workspace" if volume else "type=volume,dst=/workspace"),
            "-e", f"JOB_ID={job_id}",
            "-e", f"MEMORY_API_URL={memory_url}",
            "-e", f"HARNESS_RUNTIME_URL={runtime_url}",
            "-e", "JOB_TOKEN",
        ]
        if llm_url:
            cmd += ["-e", f"LLM_PROXY_URL={llm_url}", "-e", "LLM_PROXY_TOKEN"]
        for name in worker_env_names(self._environ):
            cmd += ["-e", name]
        if cfg.egress_allowlist:
            for name, value in egress.proxy_env(self._environ).items():
                cmd += ["-e", f"{name}={value}"]
        for name, value in sorted(cfg.extra_env.items()):
            cmd += ["-e", f"{name}={value}"]
        if network:
            cmd += ["--network", network]
        if cfg.config_host_path:
            cmd += [
                "-v", f"{cfg.config_host_path}:/config/ts-harness-runtime.yaml:ro",
                "-e", "TS_HARNESS_CONFIG=/config/ts-harness-runtime.yaml",
            ]
        cmd.append(cfg.image)
        return cmd

    def _memory_in_use(self) -> int:
        """Bytes reserved by the coding workers running on this host (their --memory limits)."""
        listed = self._run(
            ["docker", "ps", "-q", "--filter", "label=ts-harness.coding-job"], capture_output=True, text=True, errors="replace", timeout=30
        )
        ids = (listed.stdout or "").split() if listed.returncode == 0 else []
        if not ids:
            return 0
        inspected = self._run(
            ["docker", "inspect", "-f", "{{.HostConfig.Memory}}", *ids], capture_output=True, text=True, errors="replace", timeout=30
        )
        return sum(int(line) for line in (inspected.stdout or "").split() if line.isdigit())

    def _check_memory(self, job: dict[str, Any]) -> None:
        """A larger job (an out-of-memory retry) waits until it fits beside the running workers."""
        total = host_memory_bytes()
        if not total:
            return
        wanted = int(job["worker_memory_mb"]) * 2**20 if job.get("worker_memory_mb") else parse_memory(self.config.memory)
        budget = total - parse_memory(str(self._environ.get("CODING_HOST_RESERVED_MEMORY") or "2.5g"))
        in_use = self._memory_in_use()
        if in_use and in_use + wanted > budget:
            raise CapacityError(
                f"waiting for memory: {wanted // 2**20} MB wanted, {in_use // 2**20} MB in use of {max(budget, 0) // 2**20} MB"
            )

    def _ensure_volume(self, job: dict[str, Any]) -> None:
        volume = workspace_volume(job, self._environ)
        if not volume:
            return
        created = self._run(
            ["docker", "volume", "create", "--label", f"{WORKSPACE_LABEL}={job.get('workflow_id') or ''}", volume],
            capture_output=True, text=True, errors="replace", timeout=60,
        )
        if created.returncode != 0:
            raise LaunchError((created.stderr or created.stdout or "docker volume create failed").strip()[-500:])

    def prune_workspaces(self, *, now: float | None = None) -> list[str]:
        """Remove ticket workspaces older than ``CODING_WORKSPACE_TTL_HOURS`` (default 48) that no worker uses.

        Runs at most hourly. A volume still mounted by a worker cannot be removed and stays.
        A ticket revised after its workspace was removed just clones again.
        """
        now = time.time() if now is None else now
        if now - self._pruned_at < WORKSPACE_PRUNE_SECONDS:
            return []
        self._pruned_at = now
        try:
            ttl = float(self._environ.get("CODING_WORKSPACE_TTL_HOURS") or 48) * 3600
        except ValueError:
            ttl = 48 * 3600
        listed = self._run(
            ["docker", "volume", "ls", "-q", "--filter", f"label={WORKSPACE_LABEL}"], capture_output=True, text=True, errors="replace", timeout=60
        )
        names = (listed.stdout or "").split() if listed.returncode == 0 else []
        if not names:
            return []
        inspected = self._run(
            ["docker", "volume", "inspect", "-f", "{{.Name}} {{.CreatedAt}}", *names], capture_output=True, text=True, errors="replace", timeout=60
        )
        removed: list[str] = []
        from datetime import datetime

        for line in (inspected.stdout or "").splitlines():
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            name, created = parts
            try:
                stamp = datetime.fromisoformat(created.strip().replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if now - stamp < ttl:
                continue
            gone = self._run(["docker", "volume", "rm", name], capture_output=True, text=True, errors="replace", timeout=60)
            if gone.returncode == 0:
                removed.append(name)
        if removed:
            _log("coding_workspaces_pruned", volumes=removed)
        return removed

    def _egress_proxy_ids(self) -> list[str]:
        listed = self._run(
            ["docker", "ps", "-q", "--filter", f"label={egress.PROXY_LABEL}"], capture_output=True, text=True, errors="replace", timeout=30
        )
        return (listed.stdout or "").split() if listed.returncode == 0 else []

    def sync_egress(self, memory: Any, *, now: float | None = None) -> dict[str, Any] | None:
        """Write the proxy's allowlist and reload squid when it changed (at most every
        ``CODING_EGRESS_SYNC_SECONDS``, default 60). A no-op with the allowlist off."""
        if not self.config.egress_allowlist:
            return None
        now = time.time() if now is None else now
        try:
            interval = float(self._environ.get("CODING_EGRESS_SYNC_SECONDS") or egress.SYNC_SECONDS)
        except ValueError:
            interval = egress.SYNC_SECONDS
        if self._egress_synced_at and now - self._egress_synced_at < interval:
            return None
        self._egress_synced_at = now
        try:
            self._registry_hosts = list(memory.registry_hosts())
        except Exception as exc:  # noqa: BLE001 - keep the last known registry hosts
            _log("coding_egress_registry_hosts_failed", error=str(exc)[:300])
        domains = egress.allowlist(self._environ, self._registry_hosts)
        changed = egress.write_if_changed(Path(self.config.egress_allowlist_path), egress.render_domains_file(domains))
        proxies = self._egress_proxy_ids()
        reloaded = False
        if changed and proxies:
            # squid re-reads its configuration and ACL files on SIGHUP; open tunnels stay up.
            sent = self._run(["docker", "kill", "-s", "HUP", *proxies], capture_output=True, text=True, errors="replace", timeout=30)
            reloaded = sent.returncode == 0
        summary = {"domains": len(domains), "changed": changed, "reloaded": reloaded, "proxy_running": bool(proxies)}
        if changed or not proxies:
            _log("coding_egress_allowlist_synced", **summary)
        return summary

    def start(self, job: dict[str, Any], token: str) -> str:
        if self.config.egress_allowlist and not self._egress_proxy_ids():
            raise CapacityError("waiting for the egress proxy: no running container labelled " + egress.PROXY_LABEL)
        self._check_memory(job)
        self._ensure_volume(job)
        proc = self._run(
            self.command(job),
            capture_output=True,
            text=True, errors="replace",
            timeout=120,
            env={**self._environ, "JOB_TOKEN": token, **job_proxy_env(job, self._environ)},
        )
        if proc.returncode != 0:
            raise LaunchError((proc.stderr or proc.stdout or "docker run failed").strip()[-500:])
        return (proc.stdout or "").strip()[:64]

    def stop(self, runner_ref: str) -> None:
        self._run(["docker", "stop", "-t", "10", runner_ref], capture_output=True, text=True, errors="replace", timeout=60)

    def describe(self, runner_ref: str) -> str:
        proc = self._run(
            ["docker", "inspect", "-f", "{{.State.Status}}", runner_ref], capture_output=True, text=True, errors="replace", timeout=30
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


def globally_paused(memory: Any) -> dict[str, Any] | None:
    """The global pause entry (``runtime_kv`` coding-pause/state), or None. Unreadable means not
    paused: the claim itself also refuses while the flag is set (memory_routes/pause.py)."""
    if not hasattr(memory, "kv_get"):
        return None
    from ts_harness.workflows.pause import global_entry, read_state

    return global_entry(read_state(memory))


def launcher_tick(
    memory: Any, launcher: Launcher, *, max_running: int, quotas: dict[str, int] | None = None
) -> dict[str, list[str]]:
    """Start claimed jobs, then stop workers whose jobs were cancelled or lost."""
    started: list[str] = []
    failed: list[str] = []
    waiting: list[str] = []
    sync_egress = getattr(launcher, "sync_egress", None)
    if callable(sync_egress):
        try:
            sync_egress(memory)
        except Exception as exc:  # noqa: BLE001 - a failed sync keeps the last allowlist
            _log("coding_egress_sync_failed", error=str(exc)[:300])
    paused = globally_paused(memory)
    # Everything paused (an admin, System page): claim and start nothing. Queued jobs stay queued,
    # running workers finish, and the stop sweep below still runs; no container is stopped for it.
    claimed = {} if paused else memory.claim_coding_jobs(launcher=launcher.name, max_running=max_running, quotas=quotas)
    if paused:
        _log("coding_launcher_paused", paused_by=paused.get("by"), paused_at=paused.get("at"))
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
    prune = getattr(launcher, "prune_workspaces", None)
    if callable(prune):
        try:
            prune()
        except Exception as exc:  # noqa: BLE001 - pruning never stops the launcher
            _log("coding_workspace_prune_failed", error=str(exc)[:300])
    return {"started": started, "failed": failed, "waiting": waiting, "stopped": stopped, "paused": bool(paused)}


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
    # Default-VPC workers need a public IP for outbound traffic; private subnets use the egress proxy.
    assign_public_ip: bool = False

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
            assign_public_ip=str(env.get("CODING_ECS_ASSIGN_PUBLIC_IP") or "").strip().lower() in {"1", "true", "yes", "on"},
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
        environ: Mapping[str, str] | None = None,
    ):
        self.config = config
        self._environ = dict(environ if environ is not None else os.environ)
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
                    "assignPublicIp": "ENABLED" if cfg.assign_public_ip else "DISABLED",
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
                            *(
                                {"name": name, "value": value}
                                for name, value in sorted(job_proxy_env(job, self._environ).items())
                            ),
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
        return EcsLauncher(EcsConfig.from_env(env), environ=env)
    raise ValueError(f"unknown CODING_LAUNCHER: {kind}")
