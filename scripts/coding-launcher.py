#!/usr/bin/env python3
"""Coding launcher: claims queued coding jobs and starts a worker container for each."""

from __future__ import annotations

import os
import signal
import socket
import threading

from ts_harness.coding.launcher import (
    LocalDockerConfig,
    build_launcher,
    coding_schedule,
    local_worker_limit,
    run_launcher_loop,
)
from ts_harness.memory.client import get_memory_client


def main() -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    quotas, global_limit = coding_schedule()
    launcher = build_launcher()
    max_running = global_limit if global_limit is not None else int(os.environ.get("CODING_MAX_RUNNING") or 3)
    if launcher.name == "local":
        # Workers share this host's memory; fit the limit to it (ECS sizes each task itself).
        max_running = local_worker_limit(max_running, LocalDockerConfig.from_env().memory)
    print(f'{{"event": "coding_launcher_capacity", "launcher": "{launcher.name}", "max_running": {max_running}}}', flush=True)
    run_launcher_loop(
        get_memory_client(),
        launcher,
        max_running=max_running,
        quotas=quotas,
        holder=f"{socket.gethostname()}-{os.getpid()}",
        stop=stop,
        interval_seconds=float(os.environ.get("CODING_LAUNCHER_INTERVAL_SECONDS") or 5),
    )


if __name__ == "__main__":
    main()
